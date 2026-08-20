from __future__ import annotations

import gzip
import json
from typing import Iterator

import httpx
import pytest

from reserving_workflow.adapters.control_plane import (
    ControlPlaneContractError,
    ControlPlaneError,
    ReadOnlyControlPlaneClient,
)


class ChunkedStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


class GzipBombStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.iterated = False
        self._compressed = gzip.compress(b'{"ok":true,"padding":"' + b"x" * 1_000_000 + b'"}')

    def __iter__(self) -> Iterator[bytes]:
        self.iterated = True
        yield self._compressed


def _response_for(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/health":
        return httpx.Response(200, json={"ok": True, "service": "control-plane"})
    if path == "/health/preflight":
        return httpx.Response(
            200,
            json={
                "ok": True,
                "service": "control-plane",
                "status": "ok",
                "readiness": "ready",
                "warnings": [],
                "errors": [],
                "summary": {"check_count": 1, "ok_count": 1, "warning_count": 0, "error_count": 0},
                "configuration": {"catalog": {"tool_ids": ["chainladder"], "workflow_ids": ["chainladder-basic"]}},
                "runtime": {"python_version": "3.11.9"},
                "checks": [{"check_id": "tool_catalog", "status": "ok", "summary": "loaded", "details": {}}],
            },
        )
    if path == "/tools":
        return httpx.Response(
            200,
            json={
                "tool_count": 1,
                "tools": [
                    {
                        "tool_id": "chainladder",
                        "method": "chainladder",
                        "title": "Chainladder",
                        "description": "Deterministic reserving",
                        "builtin": True,
                        "tags": ["builtin"],
                        "console_defaults": {},
                    }
                ],
            },
        )
    if path == "/tools/chainladder":
        payload = _response_for(httpx.Request("GET", "http://testserver/tools")).json()["tools"][0]
        return httpx.Response(200, json={**payload, "input_schema": {"type": "object"}})
    if path == "/workflows":
        return httpx.Response(
            200,
            json={
                "workflow_count": 1,
                "workflows": [
                    {
                        "workflow_id": "chainladder-basic",
                        "title": "Basic",
                        "description": "Run chainladder",
                        "builtin": True,
                        "step_count": 1,
                    }
                ],
            },
        )
    if path == "/workflows/chainladder-basic":
        return httpx.Response(
            200,
            json={
                "workflow_id": "chainladder-basic",
                "title": "Basic",
                "description": "Run chainladder",
                "builtin": True,
                "step_count": 1,
                "steps": [
                    {
                        "step_id": "execute",
                        "tool_id": "chainladder",
                        "title": "Execute",
                        "step_kind": "execute",
                        "order": 1,
                        "inputs": {},
                    }
                ],
            },
        )
    if path == "/runs":
        return httpx.Response(
            200,
            json={
                "registry_path": "C:/private/run-registry.json",
                "run_count": 1,
                "runs": [
                    {
                        "run_id": "run-1",
                        "case_id": "case-1",
                        "status": "needs_review",
                        "summary": "review",
                        "artifact_root": "C:/private/artifacts/run-1",
                        "review_required": True,
                        "workflow_id": "chainladder-basic",
                    }
                ],
            },
        )
    if path == "/runs/run-1":
        return httpx.Response(
            200,
            json={
                "run": {
                    "run_id": "run-1",
                    "case_id": "case-1",
                    "status": "needs_review",
                    "summary": "review",
                    "artifact_root": "C:/private/artifacts/run-1",
                    "review_required": True,
                    "workflow_id": "chainladder-basic",
                    "operator_params": {"secret": "do-not-return"},
                }
            },
        )
    if path == "/runs/run-1/events":
        return httpx.Response(
            200,
            json={
                "run_id": "run-1",
                "event_count": 2,
                "events": [
                    {"type": "run.running", "run_id": "run-1", "status": "running", "payload": {"path": "C:/private"}},
                    {"type": "run.needs_review", "run_id": "run-1", "status": "needs_review", "payload": {"secret": "do-not-return"}},
                ],
            },
        )
    if path == "/runs/run-1/artifacts":
        return httpx.Response(
            200,
            json={
                "run_id": "run-1",
                "artifact_root": "C:/private/artifacts/run-1",
                "artifact_paths": {"run_manifest": "C:/private/artifacts/run-1/run_manifest.json"},
                "artifacts": [
                    {
                        "artifact_id": "run_manifest",
                        "label": "run manifest",
                        "path": "C:/private/artifacts/run-1/run_manifest.json",
                        "present": True,
                        "provenance": "system_manifest",
                        "category": "system",
                    }
                ],
            },
        )
    if path == "/runs/run-1/review":
        return httpx.Response(
            200,
            json={
                "review": {
                    "review_id": "review-run-1",
                    "run_id": "run-1",
                    "case_id": "case-1",
                    "status": "review_required",
                    "review_required": True,
                    "reason_codes": ["threshold"],
                    "packet": {"status": "review_required", "json_path": "C:/private/packet.json"},
                    "record_path": "C:/private/review.json",
                    "review_delivery": {"token": "do-not-return"},
                }
            },
        )
    if path == "/runs/run-1/artifacts/validated_input/projection":
        return httpx.Response(
            200,
            json={
                "run_id": "run-1",
                "artifact_id": "validated_input",
                "status": "available",
                "provenance": "deterministic",
                "data": {"case_id": "case-1", "tool_id": "chainladder", "inputs": {"sample_name": "RAA"}},
                "errors": [],
            },
        )
    raise AssertionError(f"unexpected request {request.method} {path}")


def test_read_only_client_covers_all_public_read_surfaces_with_typed_contracts() -> None:
    with ReadOnlyControlPlaneClient(
        "http://testserver/",
        transport=httpx.MockTransport(_response_for),
    ) as client:
        assert client.get_health().ok is True
        assert client.get_preflight().readiness == "ready"
        assert client.list_tools()[0].tool_id == "chainladder"
        assert client.get_tool("chainladder").input_schema == {"type": "object"}
        assert client.list_workflows()[0].workflow_id == "chainladder-basic"
        assert client.get_workflow("chainladder-basic").steps[0].tool_id == "chainladder"
        assert client.list_runs(limit=10)[0].run_id == "run-1"
        assert client.get_run("run-1").status == "needs_review"
        assert [event.status for event in client.get_run_events("run-1")] == ["running", "needs_review"]
        assert client.get_run_artifacts("run-1")[0].artifact_id == "run_manifest"
        assert client.get_run_review_snapshot("run-1").review_id == "review-run-1"
        assert client.get_artifact_projection("run-1", "validated_input").data["case_id"] == "case-1"
        assert not hasattr(client, "create_run")


@pytest.mark.parametrize(
    ("method_name", "arguments", "field_path", "wrong_value"),
    (
        ("get_tool", ("chainladder",), ("tool_id",), "other-tool"),
        ("get_workflow", ("chainladder-basic",), ("workflow_id",), "other-workflow"),
        ("get_run", ("run-1",), ("run", "run_id"), "run-2"),
        ("get_run_events", ("run-1",), ("run_id",), "run-2"),
        ("get_run_events", ("run-1",), ("events", 0, "run_id"), "run-2"),
        ("get_run_artifacts", ("run-1",), ("run_id",), "run-2"),
        ("get_run_review_snapshot", ("run-1",), ("review", "run_id"), "run-2"),
        (
            "get_artifact_projection",
            ("run-1", "validated_input"),
            ("run_id",),
            "run-2",
        ),
        (
            "get_artifact_projection",
            ("run-1", "validated_input"),
            ("artifact_id",),
            "deterministic_result",
        ),
        (
            "get_artifact_projection",
            ("run-1", "validated_input"),
            ("provenance",),
            "model_generated",
        ),
    ),
)
def test_parameterized_reads_bind_response_identity_to_the_request(
    method_name: str,
    arguments: tuple[str, ...],
    field_path: tuple[str | int, ...],
    wrong_value: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _response_for(request).json()
        target = payload
        for key in field_path[:-1]:
            target = target[key]
        target[field_path[-1]] = wrong_value
        return httpx.Response(200, json=payload)

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_get_attempts=1,
    )

    with pytest.raises(ControlPlaneContractError) as exc_info:
        getattr(client, method_name)(*arguments)

    assert exc_info.value.code == "invalid_contract"
    assert exc_info.value.to_envelope() == {
        "ok": False,
        "error": {
            "code": "invalid_contract",
            "message": "Control plane returned an invalid response contract.",
        },
    }


@pytest.mark.parametrize(
    "requested_filters",
    (
        {"operator_id": "operator-1"},
        {"workspace_id": "workspace-1"},
        {"operator_id": "operator-1", "workspace_id": "workspace-1"},
    ),
)
def test_list_runs_validates_requested_identity_before_status_and_limit(
    requested_filters: dict[str, str],
) -> None:
    matching_run = {
        "run_id": "run-match",
        "case_id": "case-1",
        "status": "completed",
        "operator_id": "operator-1",
        "workspace_id": "workspace-1",
    }
    mismatched_run = {
        "run_id": "run-hidden-mismatch",
        "case_id": "case-2",
        "status": "failed",
        "operator_id": "other-operator",
        "workspace_id": "other-workspace",
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"run_count": 2, "runs": [matching_run, mismatched_run]},
        )

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_get_attempts=1,
    )

    with pytest.raises(ControlPlaneContractError) as exc_info:
        client.list_runs(
            limit=1,
            status="completed",
            **requested_filters,
        )

    assert exc_info.value.code == "invalid_contract"


@pytest.mark.parametrize(
    ("artifact_id", "server_provenance"),
    (
        ("validated_input", "review"),
        ("unknown_artifact", "deterministic"),
    ),
)
def test_artifact_metadata_rejects_forged_or_mismatched_provenance(
    artifact_id: str,
    server_provenance: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "run_id": "run-1",
                "artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "present": True,
                        "provenance": server_provenance,
                    }
                ],
            },
        )

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_get_attempts=1,
    )

    with pytest.raises(ControlPlaneContractError) as exc_info:
        client.get_run_artifacts("run-1")

    assert exc_info.value.code == "invalid_contract"


def test_artifact_metadata_derives_known_provenance_and_leaves_unknown_unclaimed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "run_id": "run-1",
                "artifacts": [
                    {"artifact_id": "validated_input", "present": True},
                    {"artifact_id": "unknown_artifact", "present": True},
                ],
            },
        )

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_get_attempts=1,
    )

    artifacts = client.get_run_artifacts("run-1")

    assert artifacts[0].provenance == "deterministic"
    assert artifacts[1].provenance is None


@pytest.mark.parametrize(
    ("field_path", "wrong_value"),
    (
        (("run_id",), "other-run"),
        (("review", "review_id"), "review-other-run"),
        (("review", "packet", "run_id"), "other-run"),
        (("review", "decision", "run_id"), "other-run"),
        (("review", "decision", "review_id"), "review-other-run"),
        (("review", "decision", "review_id"), ""),
    ),
)
def test_review_snapshot_rejects_relational_identity_mismatches(
    field_path: tuple[str, ...],
    wrong_value: str,
) -> None:
    payload = {
        "run_id": "run-1",
        "review": {
            "review_id": "review-run-1",
            "run_id": "run-1",
            "case_id": "case-1",
            "status": "review_decided",
            "review_required": True,
            "packet": {"run_id": "run-1", "status": "review_required"},
            "decision": {
                "review_id": "review-run-1",
                "run_id": "run-1",
                "decision": "approved",
            },
        },
    }
    target = payload
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = wrong_value

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        max_get_attempts=1,
    )

    with pytest.raises(ControlPlaneContractError) as exc_info:
        client.get_run_review_snapshot("run-1")

    assert exc_info.value.code == "invalid_contract"


def test_read_only_client_owns_shared_run_polling_and_summary_behavior() -> None:
    with ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(_response_for),
    ) as client:
        summary = client.summarize_run("run-1")
        waited = client.wait_for_terminal_run("run-1", max_polls=1)

    assert summary.status == "needs_review"
    assert summary.terminal is True
    assert summary.event_count == 2
    assert summary.last_event_type == "run.needs_review"
    assert summary.artifact_ids == ["run_manifest"]
    assert summary.review_status == "review_required"
    assert summary.review_required is True
    assert waited == summary


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(404, json={"detail": "C:/private/secret-token"}), "not_found"),
        (httpx.Response(500, json={"detail": "C:/private/secret-token"}), "server_error"),
        (httpx.Response(503, json={"detail": "C:/private/secret-token"}), "service_unavailable"),
        (httpx.Response(200, content=b"\xff"), "invalid_encoding"),
        (httpx.Response(200, text="not-json"), "invalid_json"),
        (httpx.Response(200, content=(b"[" * 2_000) + b"0" + (b"]" * 2_000)), "invalid_json"),
        (httpx.Response(200, json=[]), "invalid_shape"),
        (httpx.Response(200, json={"service": "missing-ok"}), "invalid_contract"),
        (httpx.Response(200, json={"ok": "true"}), "invalid_contract"),
    ],
)
def test_client_errors_are_stable_and_redacted(response: httpx.Response, expected_code: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return response

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_get_attempts=1,
    )
    with pytest.raises(ControlPlaneError) as exc_info:
        client.get_health()

    error = exc_info.value
    assert error.code == expected_code
    serialized = json.dumps(error.to_envelope())
    assert "C:/private" not in serialized
    assert "secret-token" not in serialized
    assert response.text not in str(error)


@pytest.mark.parametrize(
    "content",
    [
        b'{"ok":true,"value":' + (b"9" * 5_000) + b"}",
        b'{"ok":true,"value":NaN}',
        b'{"ok":true,"value":Infinity}',
        b'{"ok":true,"value":-Infinity}',
        b'{"ok":true,"value":1e999}',
    ],
    ids=("oversized-integer", "nan", "infinity", "negative-infinity", "overflow"),
)
def test_client_rejects_invalid_and_non_finite_json_numbers(content: bytes) -> None:
    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)),
        max_get_attempts=1,
    )

    with pytest.raises(ControlPlaneError) as exc_info:
        client.get_health()

    assert exc_info.value.code == "invalid_json"


def test_client_maps_timeout_and_connection_failures_without_raw_exception_text() -> None:
    for failure, expected_code in (
        (httpx.ConnectError("secret-token C:/private"), "connection_failed"),
        (httpx.ReadTimeout("secret-token C:/private"), "timeout"),
    ):
        def handler(request: httpx.Request, failure: Exception = failure) -> httpx.Response:
            raise failure

        client = ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )
        with pytest.raises(ControlPlaneError) as exc_info:
            client.get_health()
        assert exc_info.value.code == expected_code
        assert "secret-token" not in str(exc_info.value)
        assert "C:/private" not in str(exc_info.value)


def test_client_rejects_declared_and_streamed_oversized_responses_before_unbounded_read() -> None:
    declared = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-length": "33"}, content=b"{}")
        ),
        max_response_bytes=32,
    )
    with pytest.raises(ControlPlaneError, match="bounded response limit") as declared_error:
        declared.get_health()
    assert declared_error.value.code == "response_too_large"

    streamed = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=ChunkedStream([b"{" + b"x" * 20, b"x" * 20 + b"}"]))
        ),
        max_response_bytes=32,
    )
    with pytest.raises(ControlPlaneError) as streamed_error:
        streamed.get_health()
    assert streamed_error.value.code == "response_too_large"


def test_client_requests_identity_and_rejects_compressed_response_without_iterating() -> None:
    stream = GzipBombStream()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=stream,
        )

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_response_bytes=32,
        max_get_attempts=1,
    )

    with pytest.raises(ControlPlaneError) as exc_info:
        client.get_health()

    assert exc_info.value.code == "unsupported_content_encoding"
    assert stream.iterated is False


def test_client_rejects_unknown_raw_fields_in_safe_artifact_projection() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "run_id": "run-1",
                "artifact_id": "validated_input",
                "status": "available",
                "provenance": "deterministic",
                "data": {
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "inputs": {},
                    "artifact_paths": {"validated_input": "C:/private/input.json"},
                },
                "errors": [],
            },
        )

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_get_attempts=1,
    )

    with pytest.raises(ControlPlaneError) as exc_info:
        client.get_artifact_projection("run-1", "validated_input")

    assert exc_info.value.code == "invalid_contract"


def test_get_retry_is_bounded_and_only_for_transient_failures() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(200, json={"ok": True, "service": "control-plane"})

    client = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        max_get_attempts=3,
        retry_backoff_seconds=0,
    )
    assert client.get_health().ok is True
    assert attempts == 3


def test_close_and_context_manager_only_close_owned_clients() -> None:
    injected = httpx.Client(
        base_url="http://testserver",
        transport=httpx.MockTransport(_response_for),
    )
    client = ReadOnlyControlPlaneClient("http://ignored", client=injected)
    client.close()
    assert not injected.is_closed

    owned = ReadOnlyControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(_response_for),
    )
    with owned:
        assert owned.get_health().ok is True
    assert owned.is_closed is True
    injected.close()
