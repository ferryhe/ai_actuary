"""Build auditable operator handoff exports from recorded run evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reserving_workflow.artifacts.storage import read_json_artifact, resolve_artifact_path, write_json_artifact, write_text_artifact
from reserving_workflow.review import build_review_contract
from reserving_workflow.review.store import build_review_id, ensure_review_record
from reserving_workflow.storage.local import LocalReviewStore
from reserving_workflow.runtime import run_registry

EXPECTED_RESERVE_METRICS = ("ibnr", "ultimate", "latest_diagonal")


def export_run_report(
    *,
    registry_path: str | Path,
    run_id: str,
    review_store_root: str | Path,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    entry = run_registry.get_run(registry_path, run_id)
    artifact_root = _resolve_artifact_root_from_entry(entry)
    manifest = _load_manifest(artifact_root)
    review_store = LocalReviewStore(review_store_root)
    review_packet_result = _load_review_packet(artifact_root)
    review_record = ensure_review_record(
        review_store=review_store,
        run_entry=entry,
        review_packet=review_packet_result.get("packet") if review_packet_result.get("present") else None,
    )
    review = build_review_contract(
        review_record,
        review_packet_result=review_packet_result,
        review_store_root=review_store_root,
    )
    deterministic_result = _load_optional_json_artifact(manifest, artifact_root, "deterministic_result")
    reserve_summary = _build_reserve_summary_payload(deterministic_result)
    source_artifacts = _build_source_artifacts(manifest, artifact_root, review_store_root, run_id)
    export_root = Path(output_dir).expanduser().resolve() if output_dir is not None else artifact_root
    export_root.mkdir(parents=True, exist_ok=True)

    report = {
        "report_type": "operator_handoff",
        "generated_at": _utc_now(),
        "run": {
            "run_id": str(entry.get("run_id")),
            "case_id": entry.get("case_id"),
            "status": entry.get("status"),
            "summary": entry.get("summary"),
            "artifact_root": str(artifact_root),
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
            "created_by": entry.get("created_by"),
            "operator_id": entry.get("operator_id"),
            "workspace_id": entry.get("workspace_id"),
            "workflow_id": entry.get("workflow_id") or (entry.get("operator_params", {}) or {}).get("workflow_id"),
        },
        "review": {
            "review_id": review.get("review_id"),
            "status": review.get("status"),
            "review_required": review.get("review_required", False),
            "decision": (review.get("decision") or None),
            "reason_codes": list(review.get("reason_codes", []) or []),
            "assigned_to": review.get("assigned_to"),
            "json_path": review.get("json_path"),
            "markdown_path": review.get("markdown_path"),
        },
        "reserve_summary": reserve_summary,
        "source_artifacts": source_artifacts,
    }

    reserve_summary_json_path = write_json_artifact(
        resolve_artifact_path(export_root, "reserve_summary.json"),
        reserve_summary,
    )
    reserve_summary_markdown_path = write_text_artifact(
        resolve_artifact_path(export_root, "reserve_summary.md"),
        _render_reserve_summary_markdown(report),
    )
    operator_handoff_path = write_text_artifact(
        resolve_artifact_path(export_root, "operator_handoff.md"),
        _render_operator_handoff_markdown(report),
    )

    report["exports"] = {
        "output_dir": str(export_root),
        "operator_handoff_markdown": str(operator_handoff_path),
        "reserve_summary_json": str(reserve_summary_json_path),
        "reserve_summary_markdown": str(reserve_summary_markdown_path),
    }
    _update_manifest_with_exports(manifest, artifact_root, report["exports"])
    return report


def _resolve_artifact_root_from_entry(entry: dict[str, Any]) -> Path:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        raise ValueError(f"Run {entry.get('run_id')} is missing artifact_root.")
    path = Path(str(artifact_root)).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Artifact root does not exist: {path}")
    return path


def _load_manifest(artifact_root: Path) -> dict[str, Any]:
    manifest_path = artifact_root / "run_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"run_manifest.json not found under artifact root: {artifact_root}")
    return read_json_artifact(manifest_path)


def _build_reserve_summary_payload(deterministic_result: dict[str, Any] | None) -> dict[str, Any]:
    raw_summary = {}
    if isinstance(deterministic_result, dict):
        raw_summary = dict(deterministic_result.get("reserve_summary", {}) or {})
    present_values = {
        key: value
        for key, value in raw_summary.items()
        if isinstance(value, (int, float))
    }
    missing_metrics = [metric for metric in EXPECTED_RESERVE_METRICS if metric not in present_values]
    return {
        "values": present_values,
        "missing_metrics": missing_metrics,
        "deterministic_method": deterministic_result.get("method") if isinstance(deterministic_result, dict) else None,
        "source": "deterministic_result.reserve_summary",
    }


def _build_source_artifacts(
    manifest: dict[str, Any],
    artifact_root: Path,
    review_store_root: str | Path,
    run_id: str,
) -> dict[str, dict[str, Any]]:
    artifact_paths = dict(manifest.get("artifact_paths", {}) or {})
    refs = {
        artifact_id: _artifact_ref(path, artifact_root)
        for artifact_id, path in artifact_paths.items()
    }
    review_store_dir = Path(review_store_root).expanduser().resolve()
    review_id = build_review_id(run_id)
    review_decision_path = review_store_dir / review_id / "review_decision.json"
    refs["review_decision"] = {
        "path": str(review_decision_path),
        "present": review_decision_path.exists(),
    }
    return refs


def _artifact_ref(path: Any, artifact_root: Path) -> dict[str, Any]:
    resolved = Path(str(path)).expanduser()
    if not resolved.is_absolute():
        resolved = artifact_root / resolved
    resolved = resolved.resolve()
    return {"path": str(resolved), "present": resolved.exists()}


def _load_optional_json_artifact(manifest: dict[str, Any], artifact_root: Path, artifact_id: str) -> dict[str, Any] | None:
    artifact_paths = dict(manifest.get("artifact_paths", {}) or {})
    path = artifact_paths.get(artifact_id)
    if path is None:
        return None
    resolved = Path(str(path)).expanduser()
    if not resolved.is_absolute():
        resolved = artifact_root / resolved
    resolved = resolved.resolve()
    if not resolved.exists():
        return None
    return read_json_artifact(resolved)


def _load_review_packet(artifact_root: Path) -> dict[str, Any]:
    packet_json = artifact_root / "review_packet.json"
    packet_markdown = artifact_root / "review_packet.md"
    if not packet_json.exists():
        return {"present": False, "packet": None, "json_path": None, "markdown_path": str(packet_markdown)}
    return {
        "present": True,
        "packet": read_json_artifact(packet_json),
        "json_path": str(packet_json),
        "markdown_path": str(packet_markdown),
    }


def _update_manifest_with_exports(manifest: dict[str, Any], artifact_root: Path, exports: dict[str, str]) -> None:
    artifact_paths = dict(manifest.get("artifact_paths", {}) or {})
    artifact_paths["operator_handoff"] = exports["operator_handoff_markdown"]
    artifact_paths["reserve_summary_json"] = exports["reserve_summary_json"]
    artifact_paths["reserve_summary_markdown"] = exports["reserve_summary_markdown"]
    manifest["artifact_paths"] = artifact_paths
    write_json_artifact(artifact_root / "run_manifest.json", manifest)


def _render_reserve_summary_markdown(report: dict[str, Any]) -> str:
    summary = report["reserve_summary"]
    lines = [
        "# Reserve Summary",
        "",
        f"- run_id: {report['run']['run_id']}",
        f"- case_id: {report['run']['case_id']}",
        f"- execution_status: {report['run']['status']}",
        f"- review_status: {report['review']['status']}",
        f"- deterministic_method: {summary.get('deterministic_method') or 'missing'}",
        "",
        "## Values",
        "",
    ]
    if summary["values"]:
        for metric in EXPECTED_RESERVE_METRICS:
            if metric in summary["values"]:
                lines.append(f"- {metric}: {summary['values'][metric]}")
            else:
                lines.append(f"- {metric}: missing")
    else:
        lines.append("- no deterministic reserve summary values available")
    return "\n".join(lines) + "\n"


def _render_operator_handoff_markdown(report: dict[str, Any]) -> str:
    review_decision = report["review"].get("decision") or {}
    lines = [
        "# Operator Handoff",
        "",
        "## Run",
        "",
        f"- run_id: {report['run']['run_id']}",
        f"- case_id: {report['run']['case_id']}",
        f"- execution_status: {report['run']['status']}",
        f"- summary: {report['run'].get('summary') or 'missing'}",
        f"- artifact_root: {report['run']['artifact_root']}",
        "",
        "## Review",
        "",
        f"- review_status: {report['review']['status']}",
        f"- review_required: {report['review']['review_required']}",
        f"- assigned_to: {report['review'].get('assigned_to') or 'missing'}",
        f"- decision: {review_decision.get('decision') or 'not_decided'}",
        f"- decided_by: {review_decision.get('decided_by') or 'missing'}",
        f"- comment: {review_decision.get('comment') or 'missing'}",
        "",
        "## Reserve Summary",
        "",
    ]
    for metric in EXPECTED_RESERVE_METRICS:
        value = report["reserve_summary"]["values"].get(metric)
        if value is None:
            lines.append(f"- {metric}: missing")
        else:
            lines.append(f"- {metric}: {value}")
    lines.extend(
        [
            "",
            "## Source Artifacts",
            "",
        ]
    )
    for artifact_id, ref in sorted(report["source_artifacts"].items()):
        lines.append(f"- {artifact_id}: {'present' if ref['present'] else 'missing'} ({ref['path']})")
    return "\n".join(lines) + "\n"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
