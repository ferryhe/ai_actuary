from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module(name: str, relative_path: str):
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_case_worker_completes_single_case_loop(tmp_path):
    task_contracts = _load_module(
        "task_contracts",
        "workflows/agent-runtimes/hermes-worker/task_contracts.py",
    )
    case_worker = _load_module(
        "case_worker",
        "workflows/agent-runtimes/hermes-worker/case_worker.py",
    )

    artifact_dir = tmp_path / "artifacts"
    required_artifacts = [
        "case_input",
        "deterministic_result",
        "narrative_draft",
        "constitution_check",
        "run_manifest",
    ]
    task = task_contracts.WorkerTask(
        task_id="task-pass-001",
        task_kind="run_case",
        case_ref="raa-sample",
        objective="Run governed case worker loop",
        inputs={
            "artifact_dir": str(artifact_dir),
            "case_payload": {
                "case_id": "raa-sample",
                "metadata": {"chainladder_sample": "RAA"},
                "run_config": {
                    "method": "chainladder",
                    "required_artifacts": required_artifacts,
                },
            },
        },
        required_artifacts=required_artifacts,
    )

    result = case_worker.run_case_worker(task)

    assert result.status == "completed"
    assert result.case_id == "raa-sample"
    assert result.constitution_check["status"] == "pass"
    assert set(required_artifacts).issubset(result.artifact_paths)
    assert Path(result.artifact_paths["run_manifest"]).exists()

    manifest = json.loads(Path(result.artifact_paths["run_manifest"]).read_text())
    assert manifest["case_id"] == "raa-sample"
    assert set(required_artifacts).issubset(manifest["artifact_paths"])



def test_run_case_worker_marks_review_when_constitution_requests_it(tmp_path):
    task_contracts = _load_module(
        "task_contracts",
        "workflows/agent-runtimes/hermes-worker/task_contracts.py",
    )
    case_worker = _load_module(
        "case_worker",
        "workflows/agent-runtimes/hermes-worker/case_worker.py",
    )

    task = task_contracts.WorkerTask(
        task_id="task-review-001",
        task_kind="run_case",
        case_ref="raa-review",
        objective="Run case worker loop with review trigger",
        inputs={
            "artifact_dir": str(tmp_path / "review-artifacts"),
            "case_payload": {
                "case_id": "raa-review",
                "metadata": {"chainladder_sample": "RAA"},
                "run_config": {
                    "method": "chainladder",
                    "required_artifacts": [
                        "case_input",
                        "deterministic_result",
                        "narrative_draft",
                        "constitution_check",
                        "run_manifest",
                    ],
                    "review_thresholds": {"origin_count": 5},
                },
            },
        },
    )

    result = case_worker.run_case_worker(task)

    assert result.status == "needs_review"
    assert result.constitution_check["status"] == "review_required"
    assert any(
        item.startswith("diagnostic_threshold:origin_count")
        for item in result.review_reasons
    )



def test_run_case_worker_returns_structured_failure_metadata_for_invalid_input(tmp_path):
    task_contracts = _load_module(
        "task_contracts_invalid_input",
        "workflows/agent-runtimes/hermes-worker/task_contracts.py",
    )
    case_worker = _load_module(
        "case_worker_invalid_input",
        "workflows/agent-runtimes/hermes-worker/case_worker.py",
    )

    task = task_contracts.WorkerTask(
        task_id="task-fail-001",
        task_kind="run_case",
        case_ref="broken-case",
        objective="Run worker with invalid payload",
        inputs={"artifact_dir": str(tmp_path / "broken-artifacts")},
    )

    result = case_worker.run_case_worker(task)

    assert result.status == "failed"
    assert result.case_id == "broken-case"
    assert result.errors
    assert result.worker_metadata["adapter"] == "local-callable"
    assert result.worker_metadata["failure_category"] == "input_validation"
    assert result.worker_metadata["failure_stage"] == "worker_input"



def test_run_case_worker_labels_chainladder_adapter_failures_as_deterministic_engine(tmp_path):
    task_contracts = _load_module(
        "task_contracts_adapter_failure",
        "workflows/agent-runtimes/hermes-worker/task_contracts.py",
    )
    case_worker = _load_module(
        "case_worker_adapter_failure",
        "workflows/agent-runtimes/hermes-worker/case_worker.py",
    )

    task = task_contracts.WorkerTask(
        task_id="task-fail-002",
        task_kind="run_case",
        case_ref="adapter-failure-case",
        objective="Run worker with malformed triangle rows",
        inputs={
            "artifact_dir": str(tmp_path / "adapter-failure-artifacts"),
            "case_payload": {
                "case_id": "adapter-failure-case",
                "metadata": {
                    "triangle_rows": [{"origin": 2018, "development": 12}],
                },
                "run_config": {
                    "method": "chainladder",
                    "required_artifacts": [
                        "case_input",
                        "deterministic_result",
                        "narrative_draft",
                        "constitution_check",
                        "run_manifest",
                    ],
                },
            },
        },
    )

    result = case_worker.run_case_worker(task)

    assert result.status == "failed"
    assert result.worker_metadata["failure_category"] == "deterministic_engine"
    assert result.worker_metadata["failure_stage"] == "deterministic_engine"
    assert result.worker_metadata["error_type"] == "ChainladderAdapterError"
