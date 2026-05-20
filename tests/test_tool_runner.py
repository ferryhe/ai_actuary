from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reserving_workflow.tool_runner.contracts import PipelineStepSpec
from reserving_workflow.tool_runner.runner import ToolPipelineRunner, ToolRunnerError


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_tool_pipeline.py"
PIPELINE_PATH = REPO_ROOT / "tests" / "fixtures" / "tool_pipelines" / "actuarial_reserving_review.yaml"
GOLDEN_RUN_DIR = REPO_ROOT / "tests" / "fixtures" / "tool_contracts" / "golden_run"


def _run_pipeline(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def golden_input_copy(tmp_path: Path) -> Path:
    target = tmp_path / "case_input.json"
    shutil.copy2(GOLDEN_RUN_DIR / "case_input.json", target)
    return target


def test_tool_pipeline_runner_executes_review_path_and_collects_logs(tmp_path: Path, golden_input_copy: Path) -> None:
    payload = json.loads(golden_input_copy.read_text(encoding="utf-8"))
    payload["run_config"] = {"review_thresholds": {"origin_count": 1}}
    golden_input_copy.write_text(json.dumps(payload), encoding="utf-8")

    artifact_root = tmp_path / "pipeline-smoke"
    completed = _run_pipeline(
        "--pipeline",
        str(PIPELINE_PATH),
        "--input",
        str(golden_input_copy),
        "--artifact-root",
        str(artifact_root),
        "--run-id",
        "smoke-run",
        "--json",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["run_status"] == "needs_review"
    assert [step["step_id"] for step in payload["steps"]] == ["calc", "narrative", "governance", "review", "export"]
    assert all(step["status"] == "completed" for step in payload["steps"])

    manifest = json.loads((artifact_root / "run_manifest.json").read_text(encoding="utf-8"))
    for artifact_id in [
        "case_input",
        "deterministic_result",
        "narrative_draft",
        "constitution_check",
        "review_packet",
        "review_packet_markdown",
        "operator_handoff",
        "reserve_summary_json",
        "reserve_summary_markdown",
    ]:
        assert artifact_id in manifest["artifact_paths"]
        path = artifact_root / manifest["artifact_paths"][artifact_id]
        assert path.exists(), artifact_id

    for step in payload["steps"]:
        assert Path(step["stdout_log_path"]).exists()
        assert Path(step["stderr_log_path"]).exists()
        assert step["exit_code"] == 0

    registry_payload = json.loads((artifact_root / "run-registry.json").read_text(encoding="utf-8"))
    assert registry_payload["runs"][0]["status"] == "needs_review"


def test_tool_pipeline_runner_resolves_prior_step_artifact_templates(tmp_path: Path, golden_input_copy: Path) -> None:
    pipeline_path = tmp_path / "templated-pipeline.yaml"
    pipeline_path.write_text(
        """
pipelineId: templated-actuarial-reserving
version: actuarial-reserving.v1
artifactRoot: tmp/pipeline-runs/{{run_id}}
steps:
  - id: calc
    toolId: chainladder-calc
    inputs:
      case_input: case_input.json
    outputs:
      deterministic_result: deterministic_result.json
  - id: narrative
    toolId: narrative-draft
    inputs:
      case_input: case_input.json
      deterministic_result: "{{steps.calc.outputs.deterministic_result}}"
    outputs:
      narrative_draft: narrative_draft.json
  - id: governance
    toolId: constitution-check
    inputs:
      case_input: case_input.json
      deterministic_result: "{{steps.calc.outputs.deterministic_result}}"
      narrative_draft: "{{steps.narrative.outputs.narrative_draft}}"
    outputs:
      constitution_check: constitution_check.json
""".strip()
        + "\n",
        encoding="utf-8",
    )
    artifact_root = tmp_path / "templated-run"

    completed = _run_pipeline(
        "--pipeline",
        str(pipeline_path),
        "--input",
        str(golden_input_copy),
        "--artifact-root",
        str(artifact_root),
        "--run-id",
        "templated-run",
        "--json",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    narrative_step = next(step for step in payload["steps"] if step["step_id"] == "narrative")
    assert narrative_step["inputs"]["deterministic_result"] == str(artifact_root / "deterministic_result.json")


def test_tool_pipeline_runner_skips_review_when_condition_is_not_met(tmp_path: Path, golden_input_copy: Path) -> None:
    payload = json.loads(golden_input_copy.read_text(encoding="utf-8"))
    payload["run_config"] = {}
    golden_input_copy.write_text(json.dumps(payload), encoding="utf-8")

    artifact_root = tmp_path / "pipeline-pass"
    completed = _run_pipeline(
        "--pipeline",
        str(PIPELINE_PATH),
        "--input",
        str(golden_input_copy),
        "--artifact-root",
        str(artifact_root),
        "--run-id",
        "pass-run",
        "--json",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["run_status"] == "completed"

    review_step = next(step for step in payload["steps"] if step["step_id"] == "review")
    assert review_step["status"] == "skipped"
    assert "condition not met" in review_step["skip_reason"]
    assert not (artifact_root / "review_packet.json").exists()

    export_step = next(step for step in payload["steps"] if step["step_id"] == "export")
    assert export_step["status"] == "completed"
    assert (artifact_root / "operator_handoff.md").exists()


def test_tool_pipeline_runner_returns_stable_error_json_and_stops_after_failure(tmp_path: Path, golden_input_copy: Path) -> None:
    payload = json.loads(golden_input_copy.read_text(encoding="utf-8"))
    payload["run_config"]["method"] = "unsupported_method"
    golden_input_copy.write_text(json.dumps(payload), encoding="utf-8")

    artifact_root = tmp_path / "pipeline-failure"
    completed = _run_pipeline(
        "--pipeline",
        str(PIPELINE_PATH),
        "--input",
        str(golden_input_copy),
        "--artifact-root",
        str(artifact_root),
        "--run-id",
        "failure-run",
        "--json",
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["failed_step_id"] == "calc"
    assert payload["error"]["category"] == "validation_error"

    statuses = {step["step_id"]: step["status"] for step in payload["steps"]}
    assert statuses["calc"] == "failed"
    assert statuses["narrative"] == "skipped"
    assert statuses["governance"] == "skipped"
    assert statuses["review"] == "skipped"
    assert statuses["export"] == "skipped"

    calc_step = next(step for step in payload["steps"] if step["step_id"] == "calc")
    assert Path(calc_step["stdout_log_path"]).exists()
    assert Path(calc_step["stderr_log_path"]).exists()
    assert calc_step["exit_code"] == 1

    registry_payload = json.loads((artifact_root / "run-registry.json").read_text(encoding="utf-8"))
    assert registry_payload["runs"][0]["status"] == "failed"


def test_tool_pipeline_runner_rejects_missing_required_output_refs(tmp_path: Path) -> None:
    runner = ToolPipelineRunner(repo_root=REPO_ROOT)
    output_path = tmp_path / "deterministic_result.json"
    output_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ToolRunnerError) as exc_info:
        runner._validate_step_outputs(
            step=PipelineStepSpec(
                id="calc",
                toolId="chainladder-calc",
                outputs={"deterministic_result": "deterministic_result.json"},
            ),
            declared_outputs={"deterministic_result": output_path},
            payload_outputs={},
            artifact_root=tmp_path,
        )

    assert exc_info.value.category == "execution_error"
    assert exc_info.value.details["missing_outputs"] == ["deterministic_result"]


def test_tool_pipeline_cli_returns_stable_json_for_setup_errors(tmp_path: Path) -> None:
    completed = _run_pipeline(
        "--pipeline",
        str(PIPELINE_PATH),
        "--input",
        str(tmp_path / "missing-case-input.json"),
        "--artifact-root",
        str(tmp_path / "setup-error"),
        "--json",
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["error"]["category"] in {"io_error", "runner_error"}
    assert "Traceback" not in completed.stderr
