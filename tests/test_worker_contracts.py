from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_task_contracts_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "workflows" / "agent-runtimes" / "hermes-worker" / "task_contracts.py"
    spec = importlib.util.spec_from_file_location("task_contracts", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_worker_task_and_result_serialization():
    module = _load_task_contracts_module()
    task = module.WorkerTask(
        task_id="task-001",
        task_kind="run_case",
        case_ref="case-001",
        objective="Run governed case",
        inputs={"mode": "governed"},
        allowed_actions=["calculator_call"],
        required_artifacts=["run_manifest"],
    )
    result = module.WorkerResult(
        task_id="task-001",
        status="completed",
        summary="Task finished",
        artifact_paths={"run_manifest": "artifacts/run_manifest.json"},
        metrics={"duration_sec": 1.5},
    )

    assert task.model_dump()["task_kind"] == "run_case"
    assert result.model_dump()["status"] == "completed"
    assert result.model_dump()["artifact_paths"]["run_manifest"].endswith("run_manifest.json")
