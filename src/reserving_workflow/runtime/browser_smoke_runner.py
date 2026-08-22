"""Model-free governed runner used only by the local browser smoke harness."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def run_openai_governed_workflow(task: Any, *, user_prompt: str | None = None) -> dict[str, Any]:
    """Return a deterministic review-required governed result without model credentials."""

    artifact_root = Path(task.inputs["artifact_dir"]).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    run_id = str(getattr(task, "run_id"))
    case_id = str(getattr(task, "case_ref"))
    case_payload = dict(task.inputs.get("case_payload") or {})
    artifact_paths = {
        "case_input": str(_write_json(artifact_root / "case_input.json", case_payload)),
        "deterministic_result": str(
            _write_json(
                artifact_root / "deterministic_result.json",
                {
                    "case_id": case_id,
                    "run_id": run_id,
                    "method": "chainladder",
                    "ibnr": 1.0,
                    "status": "completed",
                    "source": "browser_smoke_deterministic_runner",
                },
            )
        ),
        "narrative_draft": str(
            _write_json(
                artifact_root / "narrative_draft.json",
                {
                    "summary": "Browser smoke deterministic narrative draft.",
                    "recommendation": "Requires human Operator review.",
                    "user_prompt": user_prompt,
                },
            )
        ),
        "constitution_check": str(
            _write_json(
                artifact_root / "constitution_check.json",
                {
                    "status": "review_required",
                    "failed_checks": ["browser_smoke_review_required"],
                    "checked_at": _utc_now(),
                },
            )
        ),
    }
    review_packet = {
        "status": "review_required",
        "run_id": run_id,
        "case_id": case_id,
        "workspace_id": "adk-development",
        "case_summary": "Browser smoke deterministic ADK run requires review.",
        "assigned_to": "browser-smoke-operator",
        "review_reasons": ["browser_smoke_review_required"],
        "failed_checks": ["browser_smoke_review_required"],
        "automated_result": {
            "status": "review_required",
            "ibnr": 1.0,
            "source": "browser_smoke_deterministic_runner",
        },
        "review_checklist": [
            {
                "id": "browser-smoke-review-boundary",
                "title": "Review boundary",
                "question": "Can only the Operator capability approve this review?",
            }
        ],
        "decision_note": "Approve only after the ADK capability receives 403.",
        "artifact_links": dict(artifact_paths),
    }
    review_packet_path = _write_json(artifact_root / "review_packet.json", review_packet)
    review_packet_markdown = artifact_root / "review_packet.md"
    review_packet_markdown.write_text(
        "\n".join(
            [
                f"# Browser smoke review — {case_id}",
                "",
                f"- Run ID: `{run_id}`",
                "- Status: `review_required`",
                "- Reason: browser_smoke_review_required",
            ]
        ),
        encoding="utf-8",
    )
    review_packet["packet_paths"] = {
        "json": str(review_packet_path),
        "markdown": str(review_packet_markdown),
    }
    artifact_paths["review_packet"] = str(review_packet_path)
    artifact_paths["review_packet_markdown"] = str(review_packet_markdown)
    manifest_path = artifact_root / "run_manifest.json"
    artifact_paths["run_manifest"] = str(manifest_path)
    _write_json(
        manifest_path,
        {
            "case_id": case_id,
            "run_id": run_id,
            "artifact_root": str(artifact_root),
            "status": "review_required",
            "artifact_paths": artifact_paths,
        },
    )
    return {
        "stage": "collect",
        "route": {"mode": "browser-smoke-deterministic"},
        "trace": {"correlation_surface": "browser_smoke"},
        "worker_result": {
            "status": "needs_review",
            "case_id": case_id,
            "run_id": run_id,
            "summary": "Browser smoke deterministic run requires review.",
            "artifact_paths": artifact_paths,
            "metrics": {"ibnr": 1.0},
            "review_reasons": ["browser_smoke_review_required"],
            "errors": [],
            "worker_metadata": {"adapter": "browser-smoke-deterministic"},
        },
        "final_output": {
            "case_id": case_id,
            "worker_status": "needs_review",
            "deterministic_method": "chainladder",
            "cited_values": {"ibnr": 1.0},
            "review_reasons": ["browser_smoke_review_required"],
            "artifact_manifest_path": str(manifest_path),
            "narrative_summary": "Browser smoke deterministic run requires review.",
        },
        "review_packet": review_packet,
        "prompt": user_prompt or "browser-smoke-deterministic",
    }


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

