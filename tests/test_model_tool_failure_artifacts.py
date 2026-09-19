"""The failure path must register the directory the model-tool runner writes."""

from __future__ import annotations

import json
from pathlib import Path

from reserving_workflow.api.app import _record_run_failure
from reserving_workflow.model_tools import (
    MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
    resolve_run_artifact_root,
)


def test_resolve_run_artifact_root_is_idempotent(tmp_path):
    case_dir = tmp_path / "c1"
    run_dir = case_dir / "run-1"

    assert resolve_run_artifact_root(case_dir, "run-1") == run_dir.resolve()
    assert resolve_run_artifact_root(run_dir, "run-1") == run_dir.resolve()


def test_failure_registers_the_directory_the_runner_writes(tmp_path):
    """A failed run is exactly when artifacts matter most.

    `artifact_dir` already ends with the run id, so re-appending it registered a
    directory that does not exist and degraded every operator entry point to
    `manifest_missing`.
    """

    run_dir = tmp_path / "c1" / "run-1"
    run_dir.mkdir(parents=True)
    registry_path = tmp_path / "registry.json"

    _record_run_failure(
        {
            "case_id": "c1",
            "run_id": "run-1",
            "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
            "artifact_dir": str(run_dir),
            "registry_path": str(registry_path),
            "created_by": "tester",
            "operator_id": "tester",
            "workspace_id": "test",
        },
        RuntimeError("model tool exploded"),
        execution_mode="synchronous",
    )

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    runs = registry["runs"]
    entry = runs["run-1"] if isinstance(runs, dict) else next(
        run for run in runs if run["run_id"] == "run-1"
    )
    assert Path(entry["artifact_root"]).resolve() == run_dir.resolve()
    assert Path(entry["artifact_root"]).exists()
