"""LLM-backed review guidance attached to deterministic review packets.

The reviewer never recomputes numbers: it only reads the deterministic
review packet and points a human reviewer at what deserves attention.
Provider and model are configurable independently from the planner so a
run can use, for example, `gpt-5.6-luna` for planning and a different
OpenAI-compatible provider for review.

Environment (.env), read on every call so edits do not require a restart:

    AI_ACTUARY_AI_REVIEW_ENABLED   1/0, default 1
    AI_ACTUARY_REVIEW_MODEL        model id, any OpenAI-compatible provider
    AI_ACTUARY_REVIEW_BASE_URL     provider endpoint, falls back to OPENAI_BASE_URL
    AI_ACTUARY_REVIEW_API_KEY      falls back to DEEPSEEK_API_KEY when the model or
                                   base URL points at DeepSeek, else OPENAI_API_KEY
    AI_ACTUARY_REVIEW_MAX_TOKENS   default 4000
    AI_ACTUARY_REVIEW_TIMEOUT_SECONDS  default 60
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from reserving_workflow.artifacts.storage import write_json_artifact, write_text_artifact
from reserving_workflow.llm_client import (
    chat_json,
    resolve_llm_settings,
    truncate,
)

DEFAULT_REVIEW_MODEL = "gpt-5.6-luna"
# Reasoning models spend completion tokens on thinking, so keep the budget generous.
DEFAULT_REVIEW_MAX_TOKENS = 4000
DEFAULT_REVIEW_TIMEOUT = 60.0
DISABLED_VALUES = {"0", "false", "no", "off"}
AI_REVIEW_ARTIFACT_ID = "ai_review"
AI_REVIEW_MARKDOWN_ARTIFACT_ID = "ai_review_markdown"

_SYSTEM_PROMPT = (
    "You are an actuarial review assistant. You receive a machine-generated review packet "
    "for one reserving or experience-study run and you tell a human actuary what to look at.\n"
    "Hard rules:\n"
    "1. Never recompute, correct, or invent numbers. Quote only values that already appear in the packet.\n"
    "2. If the evidence is thin, say so instead of guessing.\n"
    "3. Focus on risk, data quality, methodology fit, and what a human must verify before sign-off.\n"
    "4. Write in English unless the packet content (summary, failed checks, diagnostics) is Chinese.\n"
    "Reply with JSON only, shaped as:\n"
    '{"summary": str, "focus_points": [{"title": str, "severity": "info|low|medium|high", '
    '"rationale": str, "evidence": [str]}], "suggested_actions": [str]}'
)


class AiReviewFocusPoint(BaseModel):
    title: str
    severity: Literal["info", "low", "medium", "high"] = "medium"
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)


class AiReviewResult(BaseModel):
    status: Literal["ok", "skipped", "failed"]
    model: str | None = None
    base_url: str | None = None
    summary: str = ""
    focus_points: list[AiReviewFocusPoint] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "AI-generated guidance only. It cannot change numbers and does not replace a human decision."
    )
    error: str | None = None


def review_llm_settings() -> dict[str, Any]:
    """Resolve reviewer provider settings from the environment."""

    return resolve_llm_settings(
        enabled_var="AI_ACTUARY_AI_REVIEW_ENABLED",
        model_var="AI_ACTUARY_REVIEW_MODEL",
        base_url_var="AI_ACTUARY_REVIEW_BASE_URL",
        api_key_var="AI_ACTUARY_REVIEW_API_KEY",
        max_tokens_var="AI_ACTUARY_REVIEW_MAX_TOKENS",
        timeout_var="AI_ACTUARY_REVIEW_TIMEOUT_SECONDS",
        default_model=DEFAULT_REVIEW_MODEL,
        default_max_tokens=DEFAULT_REVIEW_MAX_TOKENS,
        default_timeout=DEFAULT_REVIEW_TIMEOUT,
    )


def generate_ai_review(
    review_packet: dict[str, Any],
    *,
    output_dir: str | Path | None = None,
    case_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Generate reviewer guidance for one review packet; never raises."""

    settings = review_llm_settings()
    result = AiReviewResult(
        status="skipped",
        model=settings["model"],
        base_url=settings["base_url"],
        summary="AI review is disabled (AI_ACTUARY_AI_REVIEW_ENABLED=0).",
    )
    if settings["enabled"]:
        if not settings["api_key"]:
            result = AiReviewResult(
                status="failed",
                model=settings["model"],
                base_url=settings["base_url"],
                summary="AI review unavailable.",
                error="missing_api_key: set AI_ACTUARY_REVIEW_API_KEY or OPENAI_API_KEY",
            )
        else:
            result = _call_review_model(review_packet, settings)
    payload = result.model_dump()
    payload["case_id"] = case_id or review_packet.get("case_id")
    payload["run_id"] = run_id or review_packet.get("run_id")
    if output_dir is not None:
        _write_ai_review_artifacts(payload, output_dir=output_dir)
    return payload


def build_ai_suggestion_payload(ai_review: dict[str, Any]) -> dict[str, Any]:
    """Project an AI review into the console-facing `ai_suggestion` shape."""

    focus_points = ai_review.get("focus_points") or []
    return {
        "status": ai_review.get("status"),
        "model": ai_review.get("model"),
        "summary": ai_review.get("summary") or "",
        "recommendation": "; ".join(ai_review.get("suggested_actions") or []),
        "key_points": [
            f"[{item.get('severity', 'medium')}] {item.get('title', '')}"
            + (f" — {item['rationale']}" if item.get("rationale") else "")
            for item in focus_points
        ],
        "evidence": [item.get("evidence") or [] for item in focus_points],
        "disclaimer": ai_review.get("disclaimer"),
        "error": ai_review.get("error"),
    }


def _call_review_model(review_packet: dict[str, Any], settings: dict[str, Any]) -> AiReviewResult:
    payload, error = chat_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_render_user_prompt(review_packet),
        settings=settings,
        is_useful=lambda parsed: bool(parsed.get("summary") or parsed.get("focus_points")),
    )
    if error is not None or payload is None:
        return AiReviewResult(
            status="failed",
            model=settings["model"],
            base_url=settings["base_url"],
            summary="AI review unavailable.",
            error=error or "empty_model_response",
        )
    return AiReviewResult(
        status="ok",
        model=settings["model"],
        base_url=settings["base_url"],
        summary=str(payload.get("summary") or ""),
        focus_points=[
            AiReviewFocusPoint.model_validate(item) for item in payload.get("focus_points") or []
        ],
        suggested_actions=[str(item) for item in payload.get("suggested_actions") or []],
    )


def _render_user_prompt(review_packet: dict[str, Any]) -> str:
    deterministic = review_packet.get("deterministic_outputs") or {}
    payload = {
        "case_id": review_packet.get("case_id"),
        "run_id": review_packet.get("run_id"),
        "status": review_packet.get("status"),
        "case_summary": review_packet.get("case_summary"),
        "failed_checks": review_packet.get("failed_checks"),
        "review_reasons": review_packet.get("review_reasons"),
        "diagnostics": truncate(deterministic.get("diagnostics"), limit=2000),
        "deterministic_summary": truncate(
            {key: value for key, value in deterministic.items() if key != "results"}, limit=2000
        ),
        "results_excerpt": truncate((deterministic.get("results") or [])[:4], limit=1500),
        "draft_narrative": truncate(review_packet.get("draft_narrative"), limit=1000),
    }
    return (
        "Review packet (deterministic, authoritative):\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}\n\n"
        "Point the human reviewer at what matters. JSON only."
    )


def _write_ai_review_artifacts(payload: dict[str, Any], *, output_dir: str | Path) -> None:
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    write_json_artifact(directory / "ai_review.json", payload)
    write_text_artifact(directory / "ai_review.md", _render_ai_review_markdown(payload))


def _render_ai_review_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# AI Review Guidance",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Model: `{payload.get('model')}`",
        f"- Case: {payload.get('case_id')}",
        f"- Run: {payload.get('run_id')}",
        "",
        "## Summary",
        payload.get("summary") or "- None",
        "",
        "## Focus points",
    ]
    focus_points = payload.get("focus_points") or []
    if focus_points:
        for item in focus_points:
            lines.append(f"- **[{item.get('severity', 'medium')}] {item.get('title', '')}**")
            if item.get("rationale"):
                lines.append(f"  - {item['rationale']}")
            for evidence in item.get("evidence") or []:
                lines.append(f"  - evidence: {evidence}")
    else:
        lines.append("- None")
    lines.extend(["", "## Suggested actions"])
    actions = payload.get("suggested_actions") or []
    lines.extend([f"- {item}" for item in actions] or ["- None"])
    if payload.get("error"):
        lines.extend(["", "## Error", "", str(payload["error"])])
    lines.extend(["", "---", payload.get("disclaimer") or ""])
    return "\n".join(lines) + "\n"
