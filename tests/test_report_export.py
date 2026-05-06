from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORT_MODULE_PATH = REPO_ROOT / "src" / "reserving_workflow" / "reports" / "export.py"
SCRIPT_PATH = REPO_ROOT / "scripts" / "export_run_report.py"
LOCAL_STORAGE_PATH = REPO_ROOT / "src" / "reserving_workflow" / "storage" / "local.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _build_run_fixture(tmp_path: Path, *, review_decision: dict | None = None, reserve_summary: dict | None = None):
    storage_module = _load_module("report_export_storage", LOCAL_STORAGE_PATH)
    run_store = storage_module.LocalRunStore(tmp_path / "run-registry.json")
    artifact_root = tmp_path / "artifacts" / "report-case"
    artifact_root.mkdir(parents=True, exist_ok=True)
    review_store_root = tmp_path / "reviews"
    run_id = "operator-report-case-20260506T120000Z"
    manifest_path = artifact_root / "run_manifest.json"
    deterministic_result_path = artifact_root / "deterministic_result.json"
    review_packet_path = artifact_root / "review_packet.json"
    review_packet_md_path = artifact_root / "review_packet.md"
    validated_input_path = artifact_root / "validated_input.json"

    deterministic_result_path.write_text(
        json.dumps(
            {
                "case_id": "report-case",
                "method": "chainladder",
                "reserve_summary": reserve_summary if reserve_summary is not None else {"ibnr": 123.45, "ultimate": 987.65},
                "diagnostics": {"origin_count": 10},
            }
        ),
        encoding="utf-8",
    )
    review_packet_path.write_text(
        json.dumps({"status": "review_required", "failed_checks": ["threshold"], "assigned_to": "reviewer-001"}),
        encoding="utf-8",
    )
    review_packet_md_path.write_text("# Review Packet\n", encoding="utf-8")
    validated_input_path.write_text(
        json.dumps({"case_id": "report-case", "tool_id": "chainladder", "inputs": {"sample_name": "RAA"}}),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "case_id": "report-case",
                "run_id": run_id,
                "artifact_root": str(artifact_root),
                "artifact_paths": {
                    "run_manifest": str(manifest_path),
                    "deterministic_result": str(deterministic_result_path),
                    "review_packet": str(review_packet_path),
                    "review_packet_markdown": str(review_packet_md_path),
                    "validated_input": str(validated_input_path),
                },
            }
        ),
        encoding="utf-8",
    )

    run_store.create_run(
        task_id="operator-report-case",
        case_id="report-case",
        run_id=run_id,
        status="needs_review",
        artifact_root=str(artifact_root),
        summary="Review required for report export",
        created_by="planner-001",
        operator_id="operator-001",
        workspace_id="workspace-001",
        review_required=True,
        operator_params={"case_id": "report-case", "artifact_dir": str(artifact_root)},
    )

    if review_decision is not None:
        review_store = storage_module.LocalReviewStore(review_store_root)
        review_store.create_review(
            review_id=f"review-{run_id}",
            run_id=run_id,
            case_id="report-case",
            status="review_required",
            reason_codes=["threshold"],
            assigned_to="reviewer-001",
            workspace_id="workspace-001",
            packet={"status": "review_required", "failed_checks": ["threshold"]},
        )
        review_store.submit_decision(
            review_id=f"review-{run_id}",
            decision=review_decision["decision"],
            comment=review_decision.get("comment"),
            decided_by=review_decision.get("decided_by"),
            follow_up_run_id=review_decision.get("follow_up_run_id"),
        )

    return {
        "registry_path": tmp_path / "run-registry.json",
        "review_store_root": review_store_root,
        "artifact_root": artifact_root,
        "run_id": run_id,
    }


def test_report_export_builds_handoff_and_reserve_summary_without_fabricating_values(tmp_path):
    export_module = _load_module("report_export_module", EXPORT_MODULE_PATH)
    fixture = _build_run_fixture(
        tmp_path,
        review_decision={"decision": "changes_requested", "comment": "Re-run with updated assumptions.", "decided_by": "actuary-001"},
        reserve_summary={"ibnr": 123.45},
    )

    payload = export_module.export_run_report(
        registry_path=fixture["registry_path"],
        run_id=fixture["run_id"],
        review_store_root=fixture["review_store_root"],
    )

    assert payload["run"]["status"] == "needs_review"
    assert payload["review"]["status"] == "review_decided"
    assert payload["review"]["decision"]["decision"] == "changes_requested"
    assert payload["reserve_summary"]["values"] == {"ibnr": 123.45}
    assert "ultimate" not in payload["reserve_summary"]["values"]
    assert payload["reserve_summary"]["missing_metrics"] == ["ultimate", "latest_diagonal"]
    assert payload["source_artifacts"]["deterministic_result"]["present"] is True
    assert Path(payload["exports"]["operator_handoff_markdown"]).exists()
    assert Path(payload["exports"]["reserve_summary_json"]).exists()
    assert Path(payload["exports"]["reserve_summary_markdown"]).exists()

    handoff_markdown = Path(payload["exports"]["operator_handoff_markdown"]).read_text(encoding="utf-8")
    assert "changes_requested" in handoff_markdown
    assert "ultimate: missing" in handoff_markdown
    assert "latest_diagonal: missing" in handoff_markdown

    reserve_summary_json = json.loads(Path(payload["exports"]["reserve_summary_json"]).read_text(encoding="utf-8"))
    assert reserve_summary_json["values"] == {"ibnr": 123.45}
    assert reserve_summary_json["missing_metrics"] == ["ultimate", "latest_diagonal"]

    manifest = json.loads((fixture["artifact_root"] / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_paths"]["operator_handoff"] == payload["exports"]["operator_handoff_markdown"]
    assert manifest["artifact_paths"]["reserve_summary_json"] == payload["exports"]["reserve_summary_json"]
    assert manifest["artifact_paths"]["reserve_summary_markdown"] == payload["exports"]["reserve_summary_markdown"]


def test_report_export_cli_writes_json_summary_to_stdout(tmp_path):
    fixture = _build_run_fixture(
        tmp_path,
        review_decision={"decision": "approved", "comment": "Looks good.", "decided_by": "actuary-002"},
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--registry-path",
            str(fixture["registry_path"]),
            "--run-id",
            fixture["run_id"],
            "--review-store-dir",
            str(fixture["review_store_root"]),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    payload = json.loads(completed.stdout)
    assert payload["run"]["run_id"] == fixture["run_id"]
    assert payload["review"]["decision"]["decision"] == "approved"
    assert Path(payload["exports"]["operator_handoff_markdown"]).exists()
