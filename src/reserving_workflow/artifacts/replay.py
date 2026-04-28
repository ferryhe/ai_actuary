"""Replay and repeatability helpers for artifact-backed case runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reserving_workflow.calculators import calculate_deterministic_reserve
from reserving_workflow.schemas import ReservingCaseInput, RunArtifactManifest



def load_manifest(manifest_path: str | Path) -> RunArtifactManifest:
    path = Path(manifest_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RunArtifactManifest.model_validate(payload)



def replay_case_from_manifest(manifest_path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    case_input_payload = _read_json(manifest.artifact_paths["case_input"])
    saved_result_payload = _read_json(manifest.artifact_paths["deterministic_result"])
    saved_constitution_payload = _read_json(manifest.artifact_paths["constitution_check"])

    case_input = ReservingCaseInput.model_validate(case_input_payload)
    replayed_result = calculate_deterministic_reserve(case_input_payload).model_dump(mode="json")

    return {
        "case_id": manifest.case_id,
        "run_id": manifest.run_id,
        "artifact_root": manifest.artifact_root,
        "saved_summary": dict(saved_result_payload.get("reserve_summary", {})),
        "replayed_summary": dict(replayed_result.get("reserve_summary", {})),
        "saved_constitution_status": saved_constitution_payload.get("status"),
        "matches_saved_result": _normalize_summary(saved_result_payload.get("reserve_summary", {}))
        == _normalize_summary(replayed_result.get("reserve_summary", {})),
        "method": case_input.run_config.get("method", replayed_result.get("method")),
    }



def compare_repeatability(manifest_paths: list[str | Path]) -> dict[str, Any]:
    if not manifest_paths:
        raise ValueError("compare_repeatability requires at least one manifest path")

    runs: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for manifest_path in manifest_paths:
        manifest = load_manifest(manifest_path)
        deterministic_payload = _read_json(manifest.artifact_paths["deterministic_result"])
        constitution_payload = _read_json(manifest.artifact_paths["constitution_check"])
        case_ids.add(manifest.case_id)
        runs.append(
            {
                "run_id": manifest.run_id,
                "artifact_root": manifest.artifact_root,
                "status": _map_constitution_to_worker_status(constitution_payload.get("status")),
                "reserve_summary": dict(deterministic_payload.get("reserve_summary", {})),
            }
        )

    if len(case_ids) != 1:
        raise ValueError("compare_repeatability requires manifests for exactly one case_id")

    ibnrs = [float(run["reserve_summary"].get("ibnr")) for run in runs if run["reserve_summary"].get("ibnr") is not None]
    statuses = [str(run["status"]) for run in runs]
    return {
        "case_id": next(iter(case_ids)),
        "run_count": len(runs),
        "all_statuses": statuses,
        "stable_ibnr": len(set(ibnrs)) <= 1,
        "ibnr_values": ibnrs,
        "runs": runs,
    }



def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))



def _normalize_summary(summary: dict[str, Any]) -> dict[str, float]:
    return {key: float(value) for key, value in summary.items()}



def _map_constitution_to_worker_status(status: Any) -> str:
    return {
        "pass": "completed",
        "review_required": "needs_review",
        "fail": "failed",
    }.get(str(status), str(status))
