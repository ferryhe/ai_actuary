"""Review packet generation for Hermes worker flows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SUPPORTED_TASK = "build_review_packet"


def build_review_packet(worker_result: Any, *, output_dir: str | Path | None = None) -> dict[str, Any]:
    worker_payload = worker_result.model_dump(mode="json") if hasattr(worker_result, "model_dump") else dict(worker_result)
    artifact_paths = dict(worker_payload.get("artifact_paths", {}))
    base_dir = Path(output_dir) if output_dir is not None else _infer_output_dir(artifact_paths)
    base_dir.mkdir(parents=True, exist_ok=True)

    packet = {
        "case_id": worker_payload.get("case_id"),
        "run_id": worker_payload.get("run_id"),
        "status": _map_review_status(worker_payload),
        "case_summary": worker_payload.get("summary"),
        "deterministic_outputs": worker_payload.get("deterministic_result", {}),
        "failed_checks": _collect_failed_checks(worker_payload),
        "draft_narrative": worker_payload.get("narrative_draft", {}),
        "artifact_links": artifact_paths,
    }

    json_path = base_dir / "review_packet.json"
    markdown_path = base_dir / "review_packet.md"
    json_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown_packet(packet), encoding="utf-8")
    packet["packet_paths"] = {
        "json": str(json_path),
        "markdown": str(markdown_path),
    }
    json_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return packet


def _collect_failed_checks(worker_payload: dict[str, Any]) -> list[str]:
    constitution = worker_payload.get("constitution_check", {}) or {}
    review_reasons = list(worker_payload.get("review_reasons", []) or [])
    hard_constraints = list(constitution.get("hard_constraints", []) or worker_payload.get("errors", []) or [])
    review_triggers = list(constitution.get("review_triggers", []) or [])
    checks: list[str] = []
    for item in [*hard_constraints, *review_triggers, *review_reasons]:
        if item not in checks:
            checks.append(item)
    return checks


def _map_review_status(worker_payload: dict[str, Any]) -> str:
    worker_status = worker_payload.get("status")
    if worker_status == "needs_review":
        return "review_required"
    if worker_status == "failed":
        return "failed"
    return "not_required"


def _infer_output_dir(artifact_paths: dict[str, str]) -> Path:
    if artifact_paths:
        first_path = next(iter(artifact_paths.values()))
        return Path(first_path).resolve().parent
    return Path.cwd() / "artifacts" / "review-packet"


def _render_markdown_packet(packet: dict[str, Any]) -> str:
    failed_checks = packet.get("failed_checks", [])
    deterministic = packet.get("deterministic_outputs", {}).get("reserve_summary", {})
    draft_summary = packet.get("draft_narrative", {}).get("summary", "")
    artifact_links = packet.get("artifact_links", {})
    lines = [
        f"# Review Packet — {packet.get('case_id')}",
        "",
        f"- Status: `{packet.get('status')}`",
        f"- Run ID: `{packet.get('run_id')}`",
        f"- Case summary: {packet.get('case_summary')}",
        "",
        "## Deterministic outputs",
    ]
    for key, value in deterministic.items():
        lines.append(f"- {key}: {value}")
    lines.extend([
        "",
        "## Failed checks / triggered rules",
    ])
    if failed_checks:
        lines.extend([f"- {item}" for item in failed_checks])
    else:
        lines.append("- None")
    lines.extend([
        "",
        "## Draft narrative",
        draft_summary or "- None",
        "",
        "## Artifact links",
    ])
    for key, value in artifact_links.items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"
