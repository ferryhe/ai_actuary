"""Bounded read-only ADK tools backed only by public control-plane HTTP APIs."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from reserving_workflow.adapters.control_plane import (
    ControlPlaneError,
    ReadOnlyControlPlaneClient,
)
from reserving_workflow.adapters.control_plane.projections import (
    project_artifact_metadata,
    project_artifact_projection,
    project_event,
    project_health,
    project_preflight,
    project_review,
    project_run,
    project_tool,
    project_workflow,
)


CONTROL_PLANE_BASE_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_SECONDS = 2.0
READ_TOOL_NAMES = (
    "get_health",
    "get_preflight",
    "list_tools",
    "get_tool",
    "list_workflows",
    "get_workflow",
    "list_runs",
    "get_run",
    "get_run_events",
    "get_run_artifacts",
    "get_run_review_snapshot",
    "get_artifact_projection",
)
_RUN_STATUSES = {"accepted", "queued", "running", "completed", "needs_review", "failed"}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ClientFactory = Callable[[], ReadOnlyControlPlaneClient]


def _default_client_factory() -> ReadOnlyControlPlaneClient:
    return ReadOnlyControlPlaneClient(
        CONTROL_PLANE_BASE_URL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


_read_client_factory: _ClientFactory = _default_client_factory


@contextmanager
def use_read_client_factory(factory: _ClientFactory) -> Iterator[None]:
    """Temporarily inject a model-free transport for isolated tests."""

    global _read_client_factory
    previous = _read_client_factory
    _read_client_factory = factory
    try:
        yield
    finally:
        _read_client_factory = previous


def get_health() -> dict[str, Any]:
    """Read control-plane health without changing business state."""

    return _invoke(lambda client: project_health(client.get_health()))


def get_preflight() -> dict[str, Any]:
    """Read path-free runtime readiness and catalog metadata."""

    return _invoke(lambda client: project_preflight(client.get_preflight()))


def list_tools() -> dict[str, Any]:
    """List Tool Registry metadata through the public HTTP API."""

    return _invoke(lambda client: [project_tool(item) for item in client.list_tools()])


def get_tool(tool_id: str) -> dict[str, Any]:
    """Get one Tool Registry entry by bounded logical ID."""

    if not _valid_identifier(tool_id):
        return _invalid_arguments()
    return _invoke(lambda client: project_tool(client.get_tool(tool_id)))


def list_workflows() -> dict[str, Any]:
    """List Workflow Catalog metadata through the public HTTP API."""

    return _invoke(lambda client: [project_workflow(item) for item in client.list_workflows()])


def get_workflow(workflow_id: str) -> dict[str, Any]:
    """Get one workflow definition by bounded logical ID."""

    if not _valid_identifier(workflow_id):
        return _invalid_arguments()
    return _invoke(lambda client: project_workflow(client.get_workflow(workflow_id)))


def list_runs(
    limit: int = 20,
    status: str | None = None,
    operator_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """List bounded, path-free run summaries from the public API."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
        or (
            status is not None
            and (not isinstance(status, str) or status not in _RUN_STATUSES)
        )
        or (operator_id is not None and not _valid_identifier(operator_id))
        or (workspace_id is not None and not _valid_identifier(workspace_id))
    ):
        return _invalid_arguments()
    return _invoke(
        lambda client: [
            project_run(item)
            for item in client.list_runs(
                limit=limit,
                status=status,
                operator_id=operator_id,
                workspace_id=workspace_id,
            )
        ]
    )


def get_run(run_id: str) -> dict[str, Any]:
    """Get a path-free run status projection."""

    if not _valid_identifier(run_id):
        return _invalid_arguments()
    return _invoke(lambda client: project_run(client.get_run(run_id)))


def get_run_events(run_id: str) -> dict[str, Any]:
    """Read the authoritative ordered run-event sequence."""

    if not _valid_identifier(run_id):
        return _invalid_arguments()
    return _invoke(lambda client: [project_event(item) for item in client.get_run_events(run_id)])


def get_run_artifacts(run_id: str) -> dict[str, Any]:
    """List logical artifact metadata without filesystem references."""

    if not _valid_identifier(run_id):
        return _invalid_arguments()
    return _invoke(
        lambda client: [
            project_artifact_metadata(item)
            for item in client.get_run_artifacts(run_id)
        ]
    )


def get_run_review_snapshot(run_id: str) -> dict[str, Any]:
    """Read an in-memory or persisted review snapshot without materializing it."""

    if not _valid_identifier(run_id):
        return _invalid_arguments()
    return _invoke(lambda client: project_review(client.get_run_review_snapshot(run_id)))


def get_artifact_projection(run_id: str, artifact_id: str) -> dict[str, Any]:
    """Read one allowlisted JSON artifact projection by logical IDs only."""

    if not _valid_identifier(run_id) or not _valid_identifier(artifact_id):
        return _invalid_arguments()
    return _invoke(
        lambda client: project_artifact_projection(
            client.get_artifact_projection(run_id, artifact_id)
        )
    )


def _invoke(operation: Callable[[ReadOnlyControlPlaneClient], Any]) -> dict[str, Any]:
    try:
        with _read_client_factory() as client:
            return {"ok": True, "data": operation(client)}
    except ControlPlaneError as exc:
        return exc.to_envelope()
    except (TypeError, ValueError):
        return _invalid_arguments()
    except Exception:
        return {
            "ok": False,
            "error": {
                "code": "tool_failed",
                "message": "Read-only control-plane tool failed safely.",
            },
        }


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) is not None


def _invalid_arguments() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "invalid_argument",
            "message": "Tool arguments failed validation.",
        },
    }


__all__ = [*READ_TOOL_NAMES, "READ_TOOL_NAMES", "use_read_client_factory"]
