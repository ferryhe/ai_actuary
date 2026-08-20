"""Installable bounded client for public read-only control-plane endpoints."""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from reserving_workflow.contracts import (
    AgentRunSummary,
    Review,
    Run,
    RunEvent,
    is_terminal_run_status,
)

from .contracts import (
    ArtifactListEnvelope,
    ArtifactMetadata,
    ArtifactProjection,
    HealthStatus,
    PreflightStatus,
    ReviewEnvelope,
    RunEnvelope,
    RunEventListEnvelope,
    RunListEnvelope,
    ToolDetail,
    ToolListEnvelope,
    ToolSummary,
    Workflow,
    WorkflowListEnvelope,
    WorkflowSummary,
)
from .errors import (
    ControlPlaneContractError,
    ControlPlaneError,
    ControlPlaneResponseError,
    ControlPlaneTransportError,
    error_for_status,
)
from .projections import (
    ARTIFACT_PROJECTION_SPECS,
    ArtifactProjectionReadError,
    validate_projected_artifact_payload_schema,
)


DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_MAX_GET_ATTEMPTS = 2
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class ReadOnlyControlPlaneClient:
    """Read-only facade with bounded transport, retries, and typed responses."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_get_attempts: int = DEFAULT_MAX_GET_ATTEMPTS,
        retry_backoff_seconds: float = 0.05,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least 1")
        if max_get_attempts < 1:
            raise ValueError("max_get_attempts must be at least 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self._owns_client = client is None
        client_headers = httpx.Headers(headers)
        if "accept-encoding" not in client_headers:
            client_headers["accept-encoding"] = "identity"
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=client_headers,
            timeout=timeout,
            transport=transport,
        )
        self._max_response_bytes = max_response_bytes
        self._max_get_attempts = max_get_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._owns_client and not self._closed:
            self._client.close()
        self._closed = True

    def __enter__(self) -> "ReadOnlyControlPlaneClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def get_health(self) -> HealthStatus:
        return self._request_model("GET", "/health", HealthStatus)

    def get_preflight(self) -> PreflightStatus:
        return self._request_model("GET", "/health/preflight", PreflightStatus)

    def list_tools(self) -> list[ToolSummary]:
        envelope = self._request_model("GET", "/tools", ToolListEnvelope)
        _require_contract_identity(envelope.tool_count == len(envelope.tools))
        return envelope.tools

    def get_tool(self, tool_id: str) -> ToolDetail:
        safe_tool_id = _identifier(tool_id, field_name="tool_id")
        tool = self._request_model("GET", f"/tools/{safe_tool_id}", ToolDetail)
        _require_contract_identity(tool.tool_id == safe_tool_id)
        return tool

    def list_workflows(self) -> list[WorkflowSummary]:
        envelope = self._request_model("GET", "/workflows", WorkflowListEnvelope)
        _require_contract_identity(
            envelope.workflow_count == len(envelope.workflows)
        )
        return envelope.workflows

    def get_workflow(self, workflow_id: str) -> Workflow:
        safe_workflow_id = _identifier(workflow_id, field_name="workflow_id")
        workflow = self._request_model(
            "GET",
            f"/workflows/{safe_workflow_id}",
            Workflow,
        )
        _require_contract_identity(workflow.workflow_id == safe_workflow_id)
        return workflow

    def list_runs(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        operator_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Run]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        safe_operator_id = (
            _identifier(operator_id, field_name="operator_id")
            if operator_id is not None
            else None
        )
        safe_workspace_id = (
            _identifier(workspace_id, field_name="workspace_id")
            if workspace_id is not None
            else None
        )
        params: dict[str, str] = {}
        if safe_operator_id is not None:
            params["operator_id"] = safe_operator_id
        if safe_workspace_id is not None:
            params["workspace_id"] = safe_workspace_id
        envelope = self._request_model("GET", "/runs", RunListEnvelope, params=params)
        _require_contract_identity(envelope.run_count == len(envelope.runs))
        runs = envelope.runs
        _require_contract_identity(
            all(
                (safe_operator_id is None or run.operator_id == safe_operator_id)
                and (safe_workspace_id is None or run.workspace_id == safe_workspace_id)
                for run in runs
            )
        )
        if status is not None:
            if status not in {"accepted", "queued", "running", "completed", "needs_review", "failed"}:
                raise ValueError("unsupported run status")
            runs = [run for run in runs if run.status == status]
        return runs[:limit]

    def get_run(self, run_id: str) -> Run:
        safe_run_id = _identifier(run_id, field_name="run_id")
        run = self._request_model(
            "GET",
            f"/runs/{safe_run_id}",
            RunEnvelope,
        ).run
        _require_contract_identity(run.run_id == safe_run_id)
        return run

    def get_run_events(self, run_id: str) -> list[RunEvent]:
        safe_run_id = _identifier(run_id, field_name="run_id")
        envelope = self._request_model(
            "GET", f"/runs/{safe_run_id}/events", RunEventListEnvelope
        )
        _require_contract_identity(
            envelope.run_id == safe_run_id
            and envelope.event_count == len(envelope.events)
            and all(event.run_id == safe_run_id for event in envelope.events)
            and all(_event_type_matches_status(event) for event in envelope.events)
        )
        return envelope.events

    def get_run_artifacts(self, run_id: str) -> list[ArtifactMetadata]:
        safe_run_id = _identifier(run_id, field_name="run_id")
        envelope = self._request_model(
            "GET", f"/runs/{safe_run_id}/artifacts", ArtifactListEnvelope
        )
        _require_contract_identity(envelope.run_id == safe_run_id)
        artifacts: list[ArtifactMetadata] = []
        for artifact in envelope.artifacts:
            spec = ARTIFACT_PROJECTION_SPECS.get(artifact.artifact_id)
            expected_provenance = spec.provenance if spec is not None else None
            _require_contract_identity(
                artifact.provenance is None
                or (
                    expected_provenance is not None
                    and artifact.provenance == expected_provenance
                )
            )
            artifacts.append(
                artifact.model_copy(update={"provenance": expected_provenance})
            )
        return artifacts

    def get_run_review_snapshot(self, run_id: str) -> Review:
        safe_run_id = _identifier(run_id, field_name="run_id")
        envelope = self._request_model(
            "GET", f"/runs/{safe_run_id}/review", ReviewEnvelope
        )
        review = envelope.review
        expected_review_id = f"review-{safe_run_id}"
        packet_run_id = review.packet.get("run_id") if review.packet is not None else None
        packet_case_id = review.packet.get("case_id") if review.packet is not None else None
        packet_workspace_id = (
            review.packet.get("workspace_id") if review.packet is not None else None
        )
        decision = review.decision
        _require_contract_identity(
            envelope.run_id in {None, safe_run_id}
            and review.run_id == safe_run_id
            and (review.review_id is None or review.review_id == expected_review_id)
            and (
                review.packet is None
                or "run_id" not in review.packet
                or packet_run_id == safe_run_id
            )
            and (
                review.packet is None
                or "case_id" not in review.packet
                or (
                    review.case_id is not None
                    and packet_case_id == review.case_id
                )
            )
            and (
                review.packet is None
                or "workspace_id" not in review.packet
                or (
                    review.workspace_id is not None
                    and packet_workspace_id == review.workspace_id
                )
            )
            and (
                decision is None
                or (
                    decision.run_id == safe_run_id
                    and decision.review_id == expected_review_id
                )
            )
        )
        return review

    def get_run_review(self, run_id: str) -> Review:
        """Compatibility alias used by existing Hermes callers."""

        return self.get_run_review_snapshot(run_id)

    def get_artifact_projection(self, run_id: str, artifact_id: str) -> ArtifactProjection:
        safe_run_id = _identifier(run_id, field_name="run_id")
        safe_artifact_id = _identifier(artifact_id, field_name="artifact_id")
        projection = self._request_model(
            "GET",
            f"/runs/{safe_run_id}/artifacts/{safe_artifact_id}/projection",
            ArtifactProjection,
        )
        expected_spec = ARTIFACT_PROJECTION_SPECS.get(safe_artifact_id)
        data_run_id = projection.data.get("run_id")
        data_case_id = projection.data.get("case_id")
        data_tool_id = projection.data.get("tool_id")
        _require_contract_identity(
            expected_spec is not None
            and projection.run_id == safe_run_id
            and projection.artifact_id == safe_artifact_id
            and projection.provenance == expected_spec.provenance
            and projection.case_id is not None
            and data_case_id == projection.case_id
            and (data_run_id is None or data_run_id == safe_run_id)
            and (
                data_tool_id is None
                or (
                    projection.tool_id is not None
                    and data_tool_id == projection.tool_id
                )
            )
            and (
                safe_artifact_id != "deterministic_result"
                or data_tool_id is not None
                or (
                    projection.tool_id is not None
                    and projection.data.get("method") == projection.tool_id
                )
            )
        )
        return projection

    def wait_for_terminal_run(
        self,
        run_id: str,
        *,
        poll_interval_seconds: float = 0.0,
        max_polls: int = 20,
    ) -> AgentRunSummary:
        """Poll read-only run surfaces until a terminal summary is observed."""

        if max_polls < 1:
            raise ValueError("max_polls must be at least 1")
        for attempt in range(max_polls):
            summary = self.summarize_run(run_id)
            if summary.terminal:
                return summary
            if poll_interval_seconds > 0 and attempt + 1 < max_polls:
                time.sleep(poll_interval_seconds)
        return summary

    def summarize_run(self, run_id: str) -> AgentRunSummary:
        """Combine authoritative read endpoints into the legacy run summary."""

        run = self.get_run(run_id)
        events = self.get_run_events(run_id)
        _require_contract_identity(
            not events or events[-1].status == run.status
        )
        artifacts = self.get_run_artifacts(run_id)
        review = self.get_run_review(run_id)
        return AgentRunSummary(
            run_id=run.run_id,
            case_id=run.case_id,
            status=run.status,
            summary=run.summary,
            terminal=is_terminal_run_status(run.status),
            event_count=len(events),
            last_event_type=(events[-1].type if events else None),
            artifact_ids=[artifact.artifact_id for artifact in artifacts],
            review_status=review.status,
            review_required=bool(run.review_required or review.review_required),
        )

    def _request_model(
        self,
        method: str,
        path: str,
        model: type[_ModelT],
        **kwargs: Any,
    ) -> _ModelT:
        payload = self._request_json(method, path, **kwargs)
        return self._validate_contract(model, payload)

    def _validate_contract(self, model: type[_ModelT], payload: Any) -> _ModelT:
        try:
            validated = model.model_validate(payload, strict=True)
            if isinstance(validated, ArtifactProjection):
                validate_projected_artifact_payload_schema(
                    validated.artifact_id,
                    validated.data,
                )
            return validated
        except (ArtifactProjectionReadError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ControlPlaneContractError(
                code="invalid_contract",
                message="Control plane returned an invalid response contract.",
            ) from exc

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        normalized_method = method.upper()
        attempts = self._max_get_attempts if normalized_method == "GET" else 1
        for attempt in range(attempts):
            try:
                return self._request_json_once(normalized_method, path, **kwargs)
            except ControlPlaneError as exc:
                if not exc.retryable or attempt + 1 >= attempts:
                    raise
                if self._retry_backoff_seconds:
                    time.sleep(self._retry_backoff_seconds * (attempt + 1))
        raise AssertionError("bounded request loop exhausted")

    def _request_json_once(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        request_headers = httpx.Headers(kwargs.pop("headers", None))
        if "accept-encoding" not in request_headers:
            request_headers["accept-encoding"] = "identity"
        try:
            with self._client.stream(method, path, headers=request_headers, **kwargs) as response:
                if response.status_code >= 400:
                    raise error_for_status(response.status_code)
                content_encoding = response.headers.get("content-encoding")
                if content_encoding is not None and content_encoding.strip().casefold() != "identity":
                    raise ControlPlaneResponseError(
                        code="unsupported_content_encoding",
                        message="Control plane returned an unsupported content encoding.",
                    )
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        length = int(declared_length)
                    except ValueError as exc:
                        raise ControlPlaneResponseError(
                            code="invalid_response",
                            message="Control plane returned invalid response metadata.",
                        ) from exc
                    if length < 0 or length > self._max_response_bytes:
                        raise ControlPlaneResponseError(
                            code="response_too_large",
                            message="Control plane response exceeded the bounded response limit.",
                        )
                content = bytearray()
                if response.is_stream_consumed:
                    chunks = (response.content,)
                else:
                    chunk_size = min(64 * 1024, self._max_response_bytes + 1)
                    chunks = response.iter_raw(chunk_size=chunk_size)
                for chunk in chunks:
                    if len(content) + len(chunk) > self._max_response_bytes:
                        raise ControlPlaneResponseError(
                            code="response_too_large",
                            message="Control plane response exceeded the bounded response limit.",
                        )
                    content.extend(chunk)
        except ControlPlaneError:
            raise
        except httpx.TimeoutException as exc:
            raise ControlPlaneTransportError(
                code="timeout",
                message="Control plane request timed out.",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ControlPlaneTransportError(
                code="connection_failed",
                message="Control plane connection failed.",
                retryable=True,
            ) from exc

        try:
            text = bytes(content).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ControlPlaneContractError(
                code="invalid_encoding",
                message="Control plane response was not valid UTF-8.",
            ) from exc
        try:
            payload = json.loads(text, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ControlPlaneContractError(
                code="invalid_json",
                message="Control plane response was not valid JSON.",
            ) from exc
        if _contains_non_finite_number(payload):
            raise ControlPlaneContractError(
                code="invalid_json",
                message="Control plane response was not valid JSON.",
            )
        if not isinstance(payload, dict):
            raise ControlPlaneContractError(
                code="invalid_shape",
                message="Control plane response must be a JSON object.",
            )
        return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _contains_non_finite_number(payload: Any) -> bool:
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, float) and not math.isfinite(value):
            return True
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return False


def _identifier(value: str, *, field_name: str) -> str:
    candidate = str(value)
    if not _SAFE_IDENTIFIER.fullmatch(candidate):
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return candidate


def _require_contract_identity(condition: bool) -> None:
    if not condition:
        raise ControlPlaneContractError(
            code="invalid_contract",
            message="Control plane returned an invalid response contract.",
        )


def _event_type_matches_status(event: RunEvent) -> bool:
    suffix = event.type.rsplit(".", 1)[-1]
    expected_status = "running" if suffix == "started" else suffix
    return event.status == expected_status


__all__ = ["ReadOnlyControlPlaneClient"]
