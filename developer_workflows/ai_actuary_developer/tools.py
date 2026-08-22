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
    AdkBenchmarkRequest,
    AdkDebugContext,
    AdkStartRequest,
    EXPECTED_ARTIFACT_TYPES,
    adk_debug_request_fingerprint,
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
DEBUG_TOOL_NAMES = (
    "rerun_run",
    "replay_run",
    "compare_repeatability",
    "run_bounded_benchmark",
    "export_run_report",
    "get_debug_operation_status",
    "wait_debug_operation",
)
_RUN_STATUSES = {"accepted", "queued", "running", "completed", "needs_review", "failed"}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ClientFactory = Callable[[], ReadOnlyControlPlaneClient]
_ExecutionClientFactory = Callable[[], AdkControlPlaneClient]


def _default_client_factory() -> ReadOnlyControlPlaneClient:
    credential = os.environ.get("AI_ACTUARY_ADK_CREDENTIAL", "")
    if len(credential) < 8:
        raise ValueError("ADK capability credential is not configured")
    return ReadOnlyControlPlaneClient(
        CONTROL_PLANE_BASE_URL,
        headers={"Authorization": f"Bearer {credential}"},
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
    result = _invoke_execution(
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
    _record_start_workflow_trace_span(
        session_id=session_id,
        invocation_id=invocation_id,
        workflow_id=workflow_id,
        case_id=case_id,
        result=result,
    )
    return result


def rerun_run(run_id: str, tool_context: Any) -> dict[str, Any]:
    """Confirm and create one child run from a trusted ADK run ID."""

    if not _valid_identifier(run_id) or tool_context is None:
        return _invalid_arguments()
    context = _adk_debug_context(tool_context)
    if context is None:
        return _invalid_arguments()
    pending = _pending_debug_confirmation(
        action="rerun_run",
        object_id=run_id,
        request=context,
        tool_context=tool_context,
        confirmation_payload={
            "action": "rerun_run",
            "run_id": run_id,
            "workspace_id": "adk-development",
            "creates_child_run": True,
        },
    )
    if pending.get("ok") is not True:
        return pending
    return _invoke_execution(
        lambda client: client.rerun_run(
            run_id=run_id,
            adk_app=context.adk_app,
            adk_session_id=context.adk_session_id,
            adk_invocation_id=context.adk_invocation_id,
            idempotency_key=str(pending["idempotency_key"]),
        )
    )


def replay_run(run_id: str) -> dict[str, Any]:
    """Read a bounded replay projection for one trusted run ID."""

    if not _valid_identifier(run_id):
        return _invalid_arguments()
    return _invoke_execution(lambda client: client.replay_run(run_id))


def compare_repeatability(run_ids: list[str]) -> dict[str, Any]:
    """Compare a small set of trusted run IDs without path inputs."""

    if (
        not isinstance(run_ids, list)
        or not 2 <= len(run_ids) <= 5
        or len(set(run_ids)) != len(run_ids)
        or any(not _valid_identifier(run_id) for run_id in run_ids)
    ):
        return _invalid_arguments()
    return _invoke_execution(lambda client: client.compare_repeatability(run_ids))


def run_bounded_benchmark(
    case_pack_id: str = "deterministic-v1",
    lane: str = "offline",
    tool_context: Any = None,
    *,
    case_limit: int | None = None,
) -> dict[str, Any]:
    """Confirm and run an isolated evaluation lane by trusted case-pack ID."""

    if not _valid_identifier(case_pack_id) or lane not in {"offline", "real_model"} or tool_context is None:
        return _invalid_arguments()
    try:
        request = AdkBenchmarkRequest(
            case_pack_id=case_pack_id,
            lane=lane,
            case_limit=case_limit,
        )
    except ValueError:
        return _invalid_arguments()
    confirmation_payload = {
        "action": "run_bounded_benchmark",
        "case_pack_id": case_pack_id,
        "lane": lane,
        "workspace_id": "adk-development",
        "isolated_evaluation_state": True,
    }
    if request.case_limit is not None:
        confirmation_payload["case_limit"] = request.case_limit
    pending = _pending_debug_confirmation(
        action="run_bounded_benchmark",
        object_id=case_pack_id,
        request=request,
        tool_context=tool_context,
        confirmation_payload=confirmation_payload,
    )
    if pending.get("ok") is not True:
        return pending
    return _invoke_execution(
        lambda client: client.run_bounded_benchmark(
            case_pack_id=case_pack_id,
            lane=lane,
            idempotency_key=str(pending["idempotency_key"]),
            case_limit=request.case_limit,
        )
    )


def export_run_report(run_id: str, tool_context: Any = None) -> dict[str, Any]:
    """Confirm and create a bounded path-free report artifact for one trusted run ID."""

    if not _valid_identifier(run_id) or tool_context is None:
        return _invalid_arguments()
    context = _adk_debug_context(tool_context)
    if context is None:
        return _invalid_arguments()
    pending = _pending_debug_confirmation(
        action="export_run_report",
        object_id=run_id,
        request=context,
        tool_context=tool_context,
        confirmation_payload={
            "action": "export_run_report",
            "run_id": run_id,
            "workspace_id": "adk-development",
            "creates_report_artifact": True,
        },
    )
    if pending.get("ok") is not True:
        return pending
    return _invoke_execution(
        lambda client: client.export_run_report(
            run_id=run_id,
            adk_app=context.adk_app,
            adk_session_id=context.adk_session_id,
            adk_invocation_id=context.adk_invocation_id,
            idempotency_key=str(pending["idempotency_key"]),
        )
    )


def get_debug_operation_status(operation_id: str) -> dict[str, Any]:
    """Read one bounded ADK debug operation by logical operation ID."""

    if not _valid_identifier(operation_id):
        return _invalid_arguments()
    return _invoke_execution(
        lambda client: client.get_debug_operation_status(operation_id)
    )


def wait_debug_operation(
    operation_id: str,
    timeout_seconds: float = 1.0,
) -> dict[str, Any]:
    """Wait briefly for one bounded ADK debug operation by logical operation ID."""

    if (
        not _valid_identifier(operation_id)
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 <= timeout_seconds <= 30
    ):
        return _invalid_arguments()
    return _invoke_execution(
        lambda client: client.wait_debug_operation(
            operation_id=operation_id,
            timeout_seconds=float(timeout_seconds),
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


def _adk_debug_context(tool_context: Any) -> AdkDebugContext | None:
    invocation_id = getattr(tool_context, "invocation_id", None)
    session = getattr(tool_context, "session", None)
    session_id = getattr(session, "id", None)
    try:
        return AdkDebugContext(
            adk_app="ai_actuary_developer",
            adk_session_id=session_id,
            adk_invocation_id=invocation_id,
        )
    except ValueError:
        return None


def _pending_debug_confirmation(
    *,
    action: str,
    object_id: str,
    request: AdkDebugContext | AdkBenchmarkRequest,
    tool_context: Any,
    confirmation_payload: dict[str, Any],
) -> dict[str, Any]:
    state = getattr(tool_context, "state", None)
    if state is None:
        return _invalid_arguments()
    fingerprint = adk_debug_request_fingerprint(
        action=action,
        object_id=object_id,
        request=request,
    )
    state_key = f"ai_actuary.pending_debug.{action}.{object_id}.{getattr(tool_context, 'invocation_id', '')}"
    pending = state.get(state_key)
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
            hint=f"Run ADK debug action {action} for {object_id}?",
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
                "message": "Confirmed debug context does not match this request.",
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
                "message": "Confirmed debug context is unavailable.",
            },
        }
    return {"ok": True, "idempotency_key": idempotency_key}


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


def _optional_otel_trace() -> Any | None:
    try:
        from opentelemetry import trace
    except Exception:
        return None
    return trace


def _record_start_workflow_trace_span(
    *,
    session_id: str,
    invocation_id: str,
    workflow_id: str,
    case_id: str,
    result: dict[str, Any],
) -> None:
    """Emit a project-owned ADK trace span without making OTel a core dependency."""

    data = result.get("data") if result.get("ok") is True else None
    if not isinstance(data, dict):
        return
    run_id = data.get("run_id")
    operation_id = data.get("operation_id")
    correlation_id = data.get("correlation_id")
    if not all(
        isinstance(value, str) and _valid_identifier(value)
        for value in (session_id, invocation_id, workflow_id, case_id, run_id, operation_id, correlation_id)
    ):
        return
    trace = _optional_otel_trace()
    if trace is None:
        return
    attributes = {
        "gen_ai.conversation.id": session_id,
        "gcp.vertex.agent.session_id": session_id,
        "ai_actuary.adk.invocation_id": invocation_id,
        "ai_actuary.run_id": run_id,
        "ai_actuary.operation_id": operation_id,
        "ai_actuary.correlation_id": correlation_id,
        "ai_actuary.workflow_id": workflow_id,
        "ai_actuary.case_id": case_id,
    }
    try:
        tracer = trace.get_tracer("ai_actuary.adk")
        with tracer.start_as_current_span("ai_actuary.start_workflow_run") as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
    except Exception:
        return


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
    *DEBUG_TOOL_NAMES,
    "DEBUG_TOOL_NAMES",
    "EXECUTION_TOOL_NAMES",
    "READ_TOOL_NAMES",
    "use_execution_client_factory",
    "use_read_client_factory",
]
