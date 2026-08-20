"""Bounded read-only ADK tools backed only by public control-plane HTTP APIs."""

from __future__ import annotations

import os
import re
import secrets
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from reserving_workflow.adapters.control_plane import (
    AdkControlPlaneClient,
    ControlPlaneError,
    ReadOnlyControlPlaneClient,
)
from reserving_workflow.runtime.adk_execution import (
    ALLOWED_ADK_WORKFLOWS,
    AdkStartRequest,
    EXPECTED_ARTIFACT_TYPES,
    request_fingerprint,
    summarize_adk_inputs,
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


CONTROL_PLANE_BASE_URL = os.environ.get(
    "AI_ACTUARY_CONTROL_PLANE_URL", "http://127.0.0.1:8000"
).rstrip("/")
_control_plane_url = urlsplit(CONTROL_PLANE_BASE_URL)
if (
    _control_plane_url.scheme != "http"
    or _control_plane_url.hostname != "127.0.0.1"
    or _control_plane_url.port is None
    or _control_plane_url.path not in {"", "/"}
    or _control_plane_url.query
    or _control_plane_url.fragment
    or _control_plane_url.username is not None
):
    raise RuntimeError("ADK control plane must use a fixed loopback HTTP origin")
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
EXECUTION_TOOL_NAMES = (
    "start_workflow_run",
    "wait_run",
    "get_run_status",
    "summarize_run",
)
_RUN_STATUSES = {"accepted", "queued", "running", "completed", "needs_review", "failed"}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ClientFactory = Callable[[], ReadOnlyControlPlaneClient]
_ExecutionClientFactory = Callable[[], AdkControlPlaneClient]


def _default_client_factory() -> ReadOnlyControlPlaneClient:
    return AdkControlPlaneClient(
        CONTROL_PLANE_BASE_URL,
        credential=os.environ.get("AI_ACTUARY_ADK_CREDENTIAL", ""),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _default_execution_client_factory() -> AdkControlPlaneClient:
    return AdkControlPlaneClient(
        CONTROL_PLANE_BASE_URL,
        credential=os.environ.get("AI_ACTUARY_ADK_CREDENTIAL", ""),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


_read_client_factory: _ClientFactory = _default_client_factory
_execution_client_factory: _ExecutionClientFactory = _default_execution_client_factory


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


@contextmanager
def use_execution_client_factory(factory: _ExecutionClientFactory) -> Iterator[None]:
    """Temporarily inject model-free execution transport for tests."""

    global _execution_client_factory
    previous = _execution_client_factory
    _execution_client_factory = factory
    try:
        yield
    finally:
        _execution_client_factory = previous


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


def start_workflow_run(
    workflow_id: str,
    case_id: str,
    inputs: dict[str, Any],
    tool_context: Any,
) -> dict[str, Any]:
    """Confirm and start one published workflow in the isolated ADK namespace."""

    if (
        workflow_id not in ALLOWED_ADK_WORKFLOWS
        or not _valid_identifier(case_id)
        or not isinstance(inputs, dict)
        or tool_context is None
    ):
        return _invalid_arguments()
    invocation_id = getattr(tool_context, "invocation_id", None)
    session = getattr(tool_context, "session", None)
    session_id = getattr(session, "id", None)
    state = getattr(tool_context, "state", None)
    if not _valid_identifier(invocation_id) or not _valid_identifier(session_id) or state is None:
        return _invalid_arguments()
    try:
        start_request = AdkStartRequest(
            workflow_id=workflow_id,
            case_id=case_id,
            inputs=inputs,
            adk_app="ai_actuary_developer",
            adk_session_id=session_id,
            adk_invocation_id=invocation_id,
        )
        fingerprint = request_fingerprint(start_request)
        input_summary = summarize_adk_inputs(inputs)
    except (TypeError, ValueError):
        return _invalid_arguments()
    state_key = f"ai_actuary.pending_start.{invocation_id}"
    pending = state.get(state_key)
    confirmation_payload = {
        "request_fingerprint": fingerprint,
        "workflow_id": workflow_id,
        "case_id": case_id,
        "bounded_input_summary": input_summary,
        "workspace_id": "adk-development",
        "expected_artifact_types": list(EXPECTED_ARTIFACT_TYPES[workflow_id]),
    }

    confirmation = getattr(tool_context, "tool_confirmation", None)
    if confirmation is None:
        if not isinstance(pending, dict) or pending.get("request_fingerprint") != fingerprint:
            pending = {
                "request_fingerprint": fingerprint,
                "idempotency_key": secrets.token_urlsafe(32),
                "confirmation_payload": confirmation_payload,
            }
            state[state_key] = pending
        tool_context.request_confirmation(
            hint=(
                f"Start published workflow {workflow_id} for case {case_id} in "
                "the isolated adk-development workspace?"
            ),
            payload=pending["confirmation_payload"],
        )
        return {"ok": False, "status": "confirmation_required"}
    if (
        not isinstance(pending, dict)
        or pending.get("request_fingerprint") != fingerprint
        or pending.get("confirmation_payload") != confirmation_payload
        or getattr(confirmation, "payload", None) != confirmation_payload
    ):
        return {
            "ok": False,
            "error": {
                "code": "confirmation_context_mismatch",
                "message": "Confirmed start context does not match this request.",
            },
        }
    if not bool(getattr(confirmation, "confirmed", False)):
        state.pop(state_key, None)
        return {"ok": False, "status": "rejected"}
    idempotency_key = pending.get("idempotency_key")
    if not isinstance(idempotency_key, str):
        return {
            "ok": False,
            "error": {
                "code": "confirmation_context_missing",
                "message": "Confirmed start context is unavailable.",
            },
        }
    return _invoke_execution(
        lambda client: client.start_workflow_run(
            workflow_id=workflow_id,
            case_id=case_id,
            inputs=inputs,
            adk_app="ai_actuary_developer",
            adk_session_id=session_id,
            adk_invocation_id=invocation_id,
            idempotency_key=idempotency_key,
        )
    )


def wait_run(
    run_id: str,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.25,
) -> dict[str, Any]:
    """Wait for a bounded interval without cancelling a nonterminal run."""

    if (
        not _valid_identifier(run_id)
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 <= timeout_seconds <= 30
        or isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, (int, float))
        or not 0.05 <= poll_interval_seconds <= 2
    ):
        return _invalid_arguments()
    return _invoke_execution(
        lambda client: client.wait_run(
            run_id=run_id,
            timeout_seconds=float(timeout_seconds),
            poll_interval_seconds=float(poll_interval_seconds),
        )
    )


def get_run_status(run_id: str) -> dict[str, Any]:
    """Read the current authoritative status of one ADK run."""

    if not _valid_identifier(run_id):
        return _invalid_arguments()
    return _invoke_execution(lambda client: _status_projection(client.get_run_status(run_id)))


def summarize_run(run_id: str) -> dict[str, Any]:
    """Summarize persisted run, event, artifact, and review state without recomputation."""

    if not _valid_identifier(run_id):
        return _invalid_arguments()
    return _invoke_execution(lambda client: _summary_projection(client, run_id))


def _status_projection(status: Any) -> dict[str, Any]:
    if isinstance(status, dict):
        return dict(status)
    return project_run(status)


def _summary_projection(client: AdkControlPlaneClient, run_id: str) -> dict[str, Any]:
    summary = client.summarize_run(run_id).model_dump(mode="json")
    status = _status_projection(client.get_run_status(run_id))
    for key in ("source", "provenance", "recovery_state"):
        if key in status:
            summary[key] = status[key]
    return summary


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


def _invoke_execution(operation: Callable[[AdkControlPlaneClient], Any]) -> dict[str, Any]:
    try:
        with _execution_client_factory() as client:
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
                "message": "ADK control-plane tool failed safely.",
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


__all__ = [
    *READ_TOOL_NAMES,
    *EXECUTION_TOOL_NAMES,
    "EXECUTION_TOOL_NAMES",
    "READ_TOOL_NAMES",
    "use_execution_client_factory",
    "use_read_client_factory",
]
