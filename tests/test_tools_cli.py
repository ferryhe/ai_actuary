from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
GOLDEN_RUN_DIR = REPO_ROOT / "tests" / "fixtures" / "tool_contracts" / "golden_run"


def _run_cli(module_name: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT)
    return subprocess.run(
        [sys.executable, "-m", module_name, *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def golden_run_copy(tmp_path: Path) -> Path:
    target = tmp_path / "golden_run"
    shutil.copytree(GOLDEN_RUN_DIR, target)
    return target


def test_chainladder_narrative_constitution_review_replay_clis_write_expected_artifacts(golden_run_copy: Path) -> None:
    chainladder = _run_cli(
        "reserving_workflow.tools_cli.chainladder_calc",
        "--case-input",
        str(golden_run_copy / "case_input.json"),
        "--output",
        str(golden_run_copy / "deterministic_result.generated.json"),
    )
    assert chainladder.returncode == 0, chainladder.stdout + chainladder.stderr
    chainladder_payload = json.loads(chainladder.stdout)
    assert chainladder_payload == {
        "ok": True,
        "status": "ok",
        "tool_id": "chainladder-calc",
        "outputs": {"deterministic_result": str((golden_run_copy / "deterministic_result.generated.json").resolve())},
    }
    deterministic_result = json.loads((golden_run_copy / "deterministic_result.generated.json").read_text(encoding="utf-8"))
    assert deterministic_result["case_id"] == "golden-raa"
    assert deterministic_result["method"] == "chainladder"

    narrative = _run_cli(
        "reserving_workflow.tools_cli.narrative_draft",
        "--case-input",
        str(golden_run_copy / "case_input.json"),
        "--deterministic-result",
        str(golden_run_copy / "deterministic_result.generated.json"),
    )
    assert narrative.returncode == 0, narrative.stdout + narrative.stderr
    narrative_payload = json.loads(narrative.stdout)
    assert narrative_payload["tool_id"] == "narrative-draft"
    narrative_artifact = Path(narrative_payload["outputs"]["narrative_draft"])
    narrative_result = json.loads(narrative_artifact.read_text(encoding="utf-8"))
    assert narrative_result["case_id"] == "golden-raa"
    assert "ultimate=" in narrative_result["summary"]

    constitution = _run_cli(
        "reserving_workflow.tools_cli.constitution_check",
        "--case-input",
        str(golden_run_copy / "case_input.json"),
        "--deterministic-result",
        str(golden_run_copy / "deterministic_result.generated.json"),
        "--narrative-draft",
        str(narrative_artifact),
        "--run-manifest",
        str(golden_run_copy / "run_manifest.json"),
    )
    assert constitution.returncode == 0, constitution.stdout + constitution.stderr
    constitution_payload = json.loads(constitution.stdout)
    constitution_artifact = Path(constitution_payload["outputs"]["constitution_check"])
    constitution_result = json.loads(constitution_artifact.read_text(encoding="utf-8"))
    assert constitution_result["case_id"] == "golden-raa"
    assert constitution_result["status"] in {"pass", "review_required", "fail"}

    review = _run_cli(
        "reserving_workflow.tools_cli.review_generator",
        "--constitution-check",
        str(constitution_artifact),
        "--deterministic-result",
        str(golden_run_copy / "deterministic_result.generated.json"),
        "--narrative-draft",
        str(narrative_artifact),
        "--run-manifest",
        str(golden_run_copy / "run_manifest.json"),
        "--output-dir",
        str(golden_run_copy),
    )
    assert review.returncode == 0, review.stdout + review.stderr
    review_payload = json.loads(review.stdout)
    review_packet_json = Path(review_payload["outputs"]["review_packet"])
    review_packet = json.loads(review_packet_json.read_text(encoding="utf-8"))
    assert review_packet["case_id"] == "golden-raa"
    assert Path(review_payload["outputs"]["review_packet_markdown"]).exists()

    replay = _run_cli(
        "reserving_workflow.tools_cli.replay_run",
        "--run-manifest",
        str(golden_run_copy / "run_manifest.json"),
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    replay_payload = json.loads(replay.stdout)
    replay_artifact = Path(replay_payload["outputs"]["replayed_result"])
    replay_result = json.loads(replay_artifact.read_text(encoding="utf-8"))
    assert replay_result["case_id"] == "golden-raa"
    assert replay_result["method"] == "chainladder"


def test_repeatability_and_report_export_clis_emit_structured_output(tmp_path: Path, golden_run_copy: Path) -> None:
    second_run = tmp_path / "golden_run_two"
    shutil.copytree(GOLDEN_RUN_DIR, second_run)

    repeatability = _run_cli(
        "reserving_workflow.tools_cli.repeatability_check",
        "--run-manifest",
        str(golden_run_copy / "run_manifest.json"),
        str(second_run / "run_manifest.json"),
    )
    assert repeatability.returncode == 0, repeatability.stdout + repeatability.stderr
    repeatability_payload = json.loads(repeatability.stdout)
    stability_report = json.loads(Path(repeatability_payload["outputs"]["stability_report"]).read_text(encoding="utf-8"))
    assert stability_report["case_id"] == "golden-raa"
    assert stability_report["run_count"] == 2

    review_store_root = tmp_path / "reviews"
    review_store_root.mkdir(parents=True, exist_ok=True)
    registry_path = tmp_path / "run-registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "task_id": "golden-task",
                        "case_id": "golden-raa",
                        "run_id": "golden-raa-20260520T120000Z",
                        "status": "needs_review",
                        "artifact_root": str(golden_run_copy.resolve()),
                        "summary": "Golden run requires review.",
                        "created_at": "2026-05-20T12:00:00Z",
                        "updated_at": "2026-05-20T12:00:00Z",
                        "created_by": "fixture",
                        "operator_id": "operator-001",
                        "workspace_id": "workspace-001",
                        "review_required": True,
                        "operator_params": {"case_id": "golden-raa", "artifact_dir": str(golden_run_copy.resolve())},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report_export = _run_cli(
        "reserving_workflow.tools_cli.report_export",
        "--registry-path",
        str(registry_path),
        "--run-id",
        "golden-raa-20260520T120000Z",
        "--review-store-dir",
        str(review_store_root),
    )
    assert report_export.returncode == 0, report_export.stdout + report_export.stderr
    report_payload = json.loads(report_export.stdout)
    assert report_payload["tool_id"] == "report-export"
    assert Path(report_payload["outputs"]["operator_handoff"]).exists()
    assert Path(report_payload["outputs"]["reserve_summary_json"]).exists()
    assert Path(report_payload["outputs"]["reserve_summary_markdown"]).exists()


def test_cli_validation_errors_are_distinguishable(tmp_path: Path) -> None:
    invalid_case_input = tmp_path / "case_input.json"
    invalid_case_input.write_text(json.dumps({"triangles": {}}), encoding="utf-8")

    completed = _run_cli(
        "reserving_workflow.tools_cli.chainladder_calc",
        "--case-input",
        str(invalid_case_input),
    )

    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["tool_id"] == "chainladder-calc"
    assert payload["error_category"] == "validation_error"
    assert payload["status"] == "error"


def test_cli_parse_errors_are_structured_json() -> None:
    completed = _run_cli("reserving_workflow.tools_cli.chainladder_calc")

    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["tool_id"] == "chainladder-calc"
    assert payload["error_category"] == "validation_error"
    assert "usage" in payload["details"]


def test_replay_run_defaults_output_to_manifest_artifact_root_and_needs_only_case_input(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    artifact_root = run_root / "artifacts"
    artifact_root.mkdir(parents=True)
    shutil.copy2(GOLDEN_RUN_DIR / "case_input.json", artifact_root / "case_input.json")
    manifest = json.loads((GOLDEN_RUN_DIR / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["artifact_root"] = "artifacts"
    manifest["artifact_paths"] = {"case_input": "case_input.json"}
    manifest_path = run_root / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = _run_cli(
        "reserving_workflow.tools_cli.replay_run",
        "--run-manifest",
        str(manifest_path),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert Path(payload["outputs"]["replayed_result"]) == artifact_root.resolve() / "replayed_result.json"
    assert (artifact_root / "replayed_result.json").exists()


def test_repeatability_defaults_output_to_first_manifest_artifact_root(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    artifact_root = run_root / "artifacts"
    artifact_root.mkdir(parents=True)
    for name in ["case_input.json", "deterministic_result.json", "constitution_check.json"]:
        shutil.copy2(GOLDEN_RUN_DIR / name, artifact_root / name)
    manifest = json.loads((GOLDEN_RUN_DIR / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["artifact_root"] = "artifacts"
    manifest_path = run_root / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = _run_cli(
        "reserving_workflow.tools_cli.repeatability_check",
        "--run-manifest",
        str(manifest_path),
        str(manifest_path),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert Path(payload["outputs"]["stability_report"]) == artifact_root.resolve() / "stability_report.json"
    assert (artifact_root / "stability_report.json").exists()
