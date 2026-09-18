"""Optional model-backed narrative drafting for the executor side.

The executor (Hermes worker) numbers are deterministic. This slot is enabled
by default: a model rewrites the **wording** of the draft summary and key
points. Set `AI_ACTUARY_NARRATIVE_ENABLED=0` for template-only wording.
Numbers stay deterministic:
`cited_values` are copied from the deterministic result, and every number that
appears in the generated text must exist in the run evidence. Any violation,
model error, or missing credential falls back to the template silently, so a
run can never fail because of drafting.

Environment, read on every call so edits do not require a restart:

    AI_ACTUARY_NARRATIVE_ENABLED            1/0, default 1 (enabled; 0 = template only)
    AI_ACTUARY_NARRATIVE_MODEL              model id, any OpenAI-compatible provider
    AI_ACTUARY_NARRATIVE_BASE_URL           provider endpoint, falls back to OPENAI_BASE_URL
    AI_ACTUARY_NARRATIVE_API_KEY            falls back to DEEPSEEK_API_KEY when the model or
                                            base URL points at DeepSeek, else OPENAI_API_KEY
    AI_ACTUARY_NARRATIVE_MAX_TOKENS         default 1200
    AI_ACTUARY_NARRATIVE_TIMEOUT_SECONDS    default 60
"""

from __future__ import annotations

import json
import re
from typing import Any

from reserving_workflow.llm_client import chat_json, resolve_llm_settings, truncate

DEFAULT_NARRATIVE_MODEL = "gpt-5.6-luna"
DEFAULT_NARRATIVE_MAX_TOKENS = 1200
DEFAULT_NARRATIVE_TIMEOUT = 60.0
MAX_KEY_POINTS = 8
MAX_TEXT_CHARS = 600
NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")
# Case ids such as `case-42` must not look like a negative number.
CASE_ID_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
# Percentages and ratios a drafter may legitimately write without citing them.
IMPLICIT_NUMBERS = (0.0, 1.0, 100.0)

_SYSTEM_PROMPT = (
    "You draft the narrative wording for one actuarial run. You receive the deterministic "
    "evidence of that run and rewrite the summary and key points in clear language.\n"
    "Hard rules:\n"
    "1. Never compute, correct, round, or invent any number. Use only numbers that appear in the evidence.\n"
    "2. Do not add percentages, ratios, or comparisons that are not in the evidence.\n"
    "3. Keep it factual and short: one summary sentence or two, and at most eight key points.\n"
    "4. Do not promise a decision, a sign-off, or a recommendation; describe what the run produced.\n"
    "5. Do not copy the template summary verbatim: write fresh wording for the same facts.\n"
    "6. Write in English unless the evidence content is Chinese.\n"
    "Reply with JSON only, shaped as:\n"
    '{"summary": str, "key_points": [str]}'
)


class NarrativeDraftWriter:
    """Optional model slot for narrative wording; never raises."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self._settings = settings

    @property
    def model(self) -> str | None:
        return self._settings.get("model")

    @property
    def enabled(self) -> bool:
        return bool(self._settings.get("enabled"))

    def draft_text(
        self,
        *,
        case_id: str,
        method: str,
        evidence: dict[str, Any],
        fallback_summary: str,
        fallback_key_points: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return ``{"summary", "key_points", "meta"}`` for one narrative draft.

        ``meta["source"]`` is ``"model"`` only when the model text passed the
        numeric guard; otherwise it is ``"template"`` with a reason.
        """

        key_points = [str(item) for item in (fallback_key_points or [])]
        meta: dict[str, Any] = {
            "source": "template",
            "model": self.model,
            "base_url": self._settings.get("base_url"),
        }
        if not self.enabled:
            meta["reason"] = "disabled"
            return {"summary": fallback_summary, "key_points": key_points, "meta": meta}
        if not self._settings.get("api_key"):
            meta["reason"] = "missing_api_key"
            return {"summary": fallback_summary, "key_points": key_points, "meta": meta}

        payload, error = chat_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_render_user_prompt(
                case_id=case_id, method=method, evidence=evidence, fallback_summary=fallback_summary
            ),
            settings=self._settings,
            is_useful=lambda parsed: bool(str(parsed.get("summary") or "").strip()),
        )
        if error is not None:
            meta["reason"] = "model_error"
            meta["error"] = error
            return {"summary": fallback_summary, "key_points": key_points, "meta": meta}

        assert payload is not None  # guaranteed by chat_json contract
        summary = _clip(str(payload.get("summary") or "").strip())
        drafted_points = [_clip(str(item)) for item in payload.get("key_points") or []]
        drafted_points = [item for item in drafted_points if item][:MAX_KEY_POINTS]
        if not summary:
            meta["reason"] = "empty_model_response"
            return {"summary": fallback_summary, "key_points": key_points, "meta": meta}
        allowed = evidence_numbers(evidence, case_id=case_id)
        if not numbers_supported([summary, *drafted_points], allowed):
            meta["reason"] = "numeric_guard_rejected"
            meta["error"] = "the draft contained numbers that are not in the run evidence"
            return {"summary": fallback_summary, "key_points": key_points, "meta": meta}
        meta["source"] = "model"
        return {"summary": summary, "key_points": drafted_points or key_points, "meta": meta}


def narrative_llm_settings() -> dict[str, Any]:
    """Resolve the narrative drafting slot from the environment."""

    return resolve_llm_settings(
        enabled_var="AI_ACTUARY_NARRATIVE_ENABLED",
        model_var="AI_ACTUARY_NARRATIVE_MODEL",
        base_url_var="AI_ACTUARY_NARRATIVE_BASE_URL",
        api_key_var="AI_ACTUARY_NARRATIVE_API_KEY",
        max_tokens_var="AI_ACTUARY_NARRATIVE_MAX_TOKENS",
        timeout_var="AI_ACTUARY_NARRATIVE_TIMEOUT_SECONDS",
        default_model=DEFAULT_NARRATIVE_MODEL,
        default_max_tokens=DEFAULT_NARRATIVE_MAX_TOKENS,
        default_timeout=DEFAULT_NARRATIVE_TIMEOUT,
        default_enabled="1",
    )


def narrative_writer_from_env() -> NarrativeDraftWriter:
    """Build a writer from the environment; enabled by default."""

    return NarrativeDraftWriter(narrative_llm_settings())


def evidence_numbers(evidence: Any, *, case_id: str = "") -> set[float]:
    """Numbers a generated draft is allowed to quote."""

    numbers: set[float] = set(IMPLICIT_NUMBERS)
    for value in _iter_numbers(evidence):
        numbers.add(float(value))
    for token in CASE_ID_NUMBER_PATTERN.findall(case_id or ""):
        numbers.add(float(token))
    return numbers


def numbers_supported(texts: list[str], allowed: set[float]) -> bool:
    """True when every number in ``texts`` is present in ``allowed``.

    Quoting an evidence value with fewer decimals is allowed; any number that
    is not traceable to the run evidence is rejected.
    """

    for text in texts:
        for token in _number_tokens(text):
            value = float(token)
            if _is_year(value):
                continue
            # `case-42` is `42`, not a new negative claim.
            if any(_matches_evidence(token, candidate, allowed) for candidate in (value, abs(value))):
                continue
            return False
    return True


def _number_tokens(text: str) -> list[str]:
    # Drop thousands separators so `52,135.23` reads as one number.
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text or "")
    return NUMBER_PATTERN.findall(cleaned)


def _matches_evidence(token: str, value: float, allowed: set[float]) -> bool:
    decimals = len(token.split(".")[1]) if "." in token else 0
    for item in allowed:
        if abs(value - item) <= 1e-6 * max(1.0, abs(item)):
            return True
        if abs(round(item, decimals) - value) <= 1e-9:
            return True
    return False


def _iter_numbers(value: Any, depth: int = 0) -> list[Any]:
    if depth > 4:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, str):
        # Evidence such as a valuation date carries numbers a draft may quote.
        return [float(token) for token in NUMBER_PATTERN.findall(value)[:20]]
    if isinstance(value, dict):
        collected: list[Any] = []
        for item in list(value.values())[:50]:
            collected.extend(_iter_numbers(item, depth + 1))
        return collected
    if isinstance(value, (list, tuple)):
        collected = []
        for item in list(value)[:50]:
            collected.extend(_iter_numbers(item, depth + 1))
        return collected
    return []


def _is_year(value: float) -> bool:
    return 1900.0 <= value <= 2100.0 and float(value).is_integer()


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text[:MAX_TEXT_CHARS]


def draft_narrative_dict(
    narrative: dict[str, Any],
    *,
    case_id: str,
    method: str,
    evidence: dict[str, Any],
    writer: NarrativeDraftWriter | None = None,
) -> dict[str, Any]:
    """Optionally rewrite the wording of a dict-shaped narrative draft.

    Used by tool runners (for example the experience study) whose narrative is
    not a `NarrativeDraft` model. Numbers remain guarded and the template is
    kept whenever the model is disabled, fails, or quotes unsupported numbers.
    """

    active_writer = writer or narrative_writer_from_env()
    try:
        drafted = active_writer.draft_text(
            case_id=case_id,
            method=method,
            evidence=evidence,
            fallback_summary=str(narrative.get("summary") or ""),
            fallback_key_points=[str(item) for item in narrative.get("key_points") or []],
        )
    except Exception:  # noqa: BLE001 - drafting must never break a run
        return narrative
    meta = drafted.get("meta") or {}
    if meta.get("source") != "model":
        return narrative
    return {
        **narrative,
        "summary": drafted["summary"],
        "key_points": drafted["key_points"],
        "narrative_source": "model",
        "narrative_model": meta.get("model"),
    }


def _render_user_prompt(
    *,
    case_id: str,
    method: str,
    evidence: dict[str, Any],
    fallback_summary: str,
) -> str:
    payload = {
        "case_id": case_id,
        "method": method,
        "evidence": truncate(evidence, limit=2500),
        "template_summary": fallback_summary,
    }
    return (
        "Deterministic run evidence (authoritative):\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}\n\n"
        "Rewrite the summary and key points. Quote only numbers from the evidence. JSON only."
    )
