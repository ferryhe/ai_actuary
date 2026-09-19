"""The failure path must register the directory the model-tool runner writes.

A failed run is exactly when artifacts matter most, but the handler that records
the failure must stay free of filesystem side effects: its only job is to leave
a `failed` event behind.
"""

from __future__ import annotations

import json
from pathlib import Path

from reserving_workflow.api import app as api_app
from reserving_workflow.api.app import _record_run_failure
from reserving_workflow.model_tools import (
    MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
    resolve_run_artifact_root,
)


def _operator_params(run_dir: Path, registry_path: Path) -> dict:
    return {
        "case_id": "c1",
        "run_id": "run-1",
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "artifact_dir": str(run_dir),
        "registry_path": str(registry_path),
        "created_by": "tester",
        "operator_id": "tester",
        "workspace_id": "test",
    }


def _recorded(registry_path: Path, run_id: str) -> dict:
    runs = json.loads(registry_path.read_text(encoding="utf-8"))["runs"]
    if isinstance(runs, dict):
        return runs[run_id]
    return next(run for run in runs if run["run_id"] == run_id)


def test_resolve_run_artifact_root_is_idempotent_and_pure(tmp_path):
    """Pure path arithmetic: resolving must not create anything."""

    case_dir = tmp_path / "c1"
    run_dir = case_dir / "run-1"

    assert resolve_run_artifact_root(case_dir, "run-1") == run_dir.resolve()
    assert resolve_run_artifact_root(run_dir, "run-1") == run_dir.resolve()
    assert not case_dir.exists()


def test_failure_registers_the_directory_the_runner_writes(tmp_path):
    """`artifact_dir` already ends with the run id.

    Appending it a second time registered a directory that does not exist and
    degraded every operator entry point to `manifest_missing`.
    """

    run_dir = tmp_path / "c1" / "run-1"
    registry_path = tmp_path / "registry.json"

    _record_run_failure(
        _operator_params(run_dir, registry_path),
        RuntimeError("model tool exploded"),
        execution_mode="synchronous",
    )

    entry = _recorded(registry_path, "run-1")
    assert Path(entry["artifact_root"]).resolve() == run_dir.resolve()
    # Recording a failure is not allowed to create the directory it refers to:
    # the run produced no artifacts, and a failure handler must not do I/O.
    assert not run_dir.exists()


def test_failure_is_recorded_even_when_the_artifact_path_is_unusable(tmp_path):
    """A parent component that is really a file must not swallow the event.

    With the mkdir-carrying helper this path raised `NotADirectoryError` and the
    failure event was lost entirely, so this locks the "no I/O" property.
    """

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    registry_path = tmp_path / "registry.json"

    _record_run_failure(
        _operator_params(blocker / "run-1", registry_path),
        RuntimeError("model tool exploded"),
        execution_mode="background",
    )

    entry = _recorded(registry_path, "run-1")
    assert entry["status"] == "failed"
    assert entry["error_category"] == "background_runtime"


def test_failure_is_recorded_even_when_the_run_root_cannot_be_resolved(
    tmp_path, monkeypatch
):
    """The guard around the shared helper must keep the event, not the error.

    Losing a `failed` event because a path could not be derived is worse than
    registering an imperfect artifact root.
    """

    def _raise(*_args, **_kwargs):
        raise NotADirectoryError(20, "Not a directory")

    monkeypatch.setattr(api_app, "resolve_run_artifact_root", _raise)
    registry_path = tmp_path / "registry.json"

    _record_run_failure(
        _operator_params(tmp_path / "c1" / "run-1", registry_path),
        RuntimeError("model tool exploded"),
        execution_mode="synchronous",
    )

    entry = _recorded(registry_path, "run-1")
    assert entry["status"] == "failed"
    assert entry["error_category"] == "synchronous_runtime"
