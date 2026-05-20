"""Review packet generation helpers for artifact-backed reserving workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reserving_workflow.artifacts.storage import write_json_artifact, write_text_artifact


def build_review_packet(worker_result: Any, *, output_dir: str | Path | None = None) -> dict[str, Any]:
    worker_payload = worker_result.model_dump(mode="json") if hasattr(worker_result, "model_dump") else dict(worker_result)
    artifact_paths = dict(worker_payload.get("artifact_paths", {}))
    artifact_manifest = dict(worker_payload.get("artifact_manifest", {}) or {})
    base_dir = (
        _resolve_output_dir(output_dir)
        if output_dir is not None
        else _infer_output_dir(artifact_paths, artifact_root=artifact_manifest.get("artifact_root"))
    )
    return _build_review_packet_from_payload(worker_payload, artifact_paths=artifact_paths, output_dir=base_dir)


def build_review_packet_from_artifacts(
    *,
    constitution_check: dict[str, Any],
    deterministic_result: dict[str, Any],
    narrative_draft: dict[str, Any],
    run_manifest: dict[str, Any],
    run_manifest_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    case_summary: str | None = None,
) -> dict[str, Any]:
    artifact_paths = dict(run_manifest.get("artifact_paths", {}) or {})
    base_dir = (
        _resolve_output_dir(output_dir)
        if output_dir is not None
        else _infer_output_dir(
            artifact_paths,
            artifact_root=run_manifest.get("artifact_root"),
            run_manifest_path=run_manifest_path,
        )
    )
    payload = {
        "case_id": constitution_check.get("case_id") or run_manifest.get("case_id") or deterministic_result.get("case_id"),
        "run_id": run_manifest.get("run_id"),
        "status": str(constitution_check.get("status") or "not_required"),
        "summary": case_summary,
        "deterministic_result": deterministic_result,
        "constitution_check": constitution_check,
        "narrative_draft": narrative_draft,
        "artifact_paths": artifact_paths,
        "review_reasons": list(constitution_check.get("review_triggers", []) or []),
        "errors": list(constitution_check.get("hard_constraints", []) or []),
    }
    return _build_review_packet_from_payload(payload, artifact_paths=artifact_paths, output_dir=base_dir)


def _build_review_packet_from_payload(
    worker_payload: dict[str, Any],
    *,
    artifact_paths: dict[str, str],
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
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

    json_path = output_dir / "review_packet.json"
    markdown_path = output_dir / "review_packet.md"
    packet["packet_paths"] = {
        "json": str(json_path.resolve()),
        "markdown": str(markdown_path.resolve()),
    }
    write_text_artifact(markdown_path, _render_markdown_packet(packet))
    write_json_artifact(json_path, packet)
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
    constitution_status = worker_payload.get("constitution_check", {}).get("status")
    if constitution_status in {"review_required", "fail", "pass"}:
        return str(constitution_status)
    return "not_required"


def _resolve_output_dir(output_dir: str | Path) -> Path:
    return Path(output_dir).expanduser().resolve()


def _infer_output_dir(
    artifact_paths: dict[str, str],
    *,
    artifact_root: str | Path | None = None,
    run_manifest_path: str | Path | None = None,
) -> Path:
    if artifact_root:
        root_path = Path(artifact_root).expanduser()
        if root_path.is_absolute():
            return root_path.resolve()
        base_dir = Path(run_manifest_path).expanduser().resolve().parent if run_manifest_path is not None else Path.cwd()
        return (base_dir / root_path).resolve()
    if artifact_paths:
        first_path = next(iter(artifact_paths.values()))
        path = Path(first_path).expanduser()
        if not path.is_absolute():
            base_dir = Path(run_manifest_path).expanduser().resolve().parent if run_manifest_path is not None else Path.cwd()
            return (base_dir / path).resolve().parent
        return path.resolve().parent
    return (Path.cwd() / "artifacts" / "review-packet").resolve()


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
