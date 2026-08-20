from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from developer_workflows.ai_actuary_developer import tools as adk_tools
from reserving_workflow.adapters.control_plane import ReadOnlyControlPlaneClient
from reserving_workflow.adapters.control_plane.contracts import (
    ArtifactMetadata,
    ArtifactProjection,
    HealthStatus,
    PreflightStatus,
    ToolSummary,
    Workflow,
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
from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.contracts import Review, Run, RunEvent, is_terminal_run_status
from reserving_workflow.storage.local import LocalRunStore


READ_TOOL_NAMES = [
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
]

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAI_RUNTIME_RUNNER = (
    REPO_ROOT / "workflows" / "agent-runtimes" / "openai-agents" / "runner.py"
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_runtime_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tool_fixture(tmp_path: Path) -> tuple[dict[str, Any], list[Path]]:
    run_id = "run-adk-read-1"
    case_id = "case-adk-read-1"
    sentinel = "SENTINEL-SECRET-ADK-551"
    artifact_root = tmp_path / "artifacts" / run_id
    artifact_root.mkdir(parents=True)
    _write_json(
        artifact_root / "validated_input.json",
        {
            "case_id": case_id,
            "run_id": run_id,
            "tool_id": "chainladder",
            "inputs": {"sample_name": "RAA", "api_key": sentinel, "path": str(tmp_path / "private")},
        },
    )
    _write_json(
        artifact_root / "review_packet.json",
        {
            "case_id": case_id,
            "run_id": run_id,
            "status": "review_required",
            "failed_checks": ["threshold"],
            "json_path": str(tmp_path / "private-review.json"),
        },
    )
    _write_json(
        artifact_root / "run_manifest.json",
        {
            "case_id": case_id,
            "run_id": run_id,
            "created_by": "test",
            "artifact_root": str(artifact_root),
            "artifact_paths": {
                "run_manifest": "run_manifest.json",
                "validated_input": "validated_input.json",
                "review_packet": "review_packet.json",
            },
        },
    )
    registry_path = tmp_path / "registry" / "runs.json"
    store = LocalRunStore(registry_path)
    store.create_run(
        task_id="task-adk",
        case_id=case_id,
        run_id=run_id,
        status="running",
        artifact_root=str(artifact_root),
        summary="running",
        operator_params={"tool_id": "chainladder", "secret": sentinel, "path": str(tmp_path)},
        review_required=False,
        event_type="run.running",
        event_payload={"secret": sentinel, "path": str(tmp_path)},
        workflow_id="chainladder-basic",
        operator_id="local-actuary",
        workspace_id="default-workspace",
    )
    store.update_run_status(
        task_id="task-adk",
        case_id=case_id,
        run_id=run_id,
        status="needs_review",
        artifact_root=str(artifact_root),
        summary="review",
        operator_params={"tool_id": "chainladder", "secret": sentinel, "path": str(tmp_path)},
        review_required=True,
        event_type="run.needs_review",
        event_payload={"secret": sentinel, "path": str(tmp_path)},
        workflow_id="chainladder-basic",
        operator_id="local-actuary",
        workspace_id="default-workspace",
    )
    settings = ApiSettings(
        registry_path=registry_path,
        artifact_root=tmp_path / "unused-artifacts",
        review_store_dir=tmp_path / "reviews-not-created",
    )
    app = create_app(settings=settings)

    def handler(request: httpx.Request) -> httpx.Response:
        async def call() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as local:
                return await local.request(
                    request.method,
                    request.url.raw_path.decode("ascii"),
                    headers=request.headers,
                    content=request.content,
                )

        response = asyncio.run(call())
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
        )

    def client_factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    return {
        "run_id": run_id,
        "sentinel": sentinel,
        "client_factory": client_factory,
        "app": app,
    }, [registry_path.parent, artifact_root, Path(settings.review_store_dir)]


def _call_for(name: str, run_id: str) -> dict[str, Any]:
    calls = {
        "get_health": lambda: adk_tools.get_health(),
        "get_preflight": lambda: adk_tools.get_preflight(),
        "list_tools": lambda: adk_tools.list_tools(),
        "get_tool": lambda: adk_tools.get_tool("chainladder"),
        "list_workflows": lambda: adk_tools.list_workflows(),
        "get_workflow": lambda: adk_tools.get_workflow("chainladder-basic"),
        "list_runs": lambda: adk_tools.list_runs(limit=10, status="needs_review"),
        "get_run": lambda: adk_tools.get_run(run_id),
        "get_run_events": lambda: adk_tools.get_run_events(run_id),
        "get_run_artifacts": lambda: adk_tools.get_run_artifacts(run_id),
        "get_run_review_snapshot": lambda: adk_tools.get_run_review_snapshot(run_id),
        "get_artifact_projection": lambda: adk_tools.get_artifact_projection(run_id, "validated_input"),
    }
    return calls[name]()


@pytest.mark.parametrize("tool_name", READ_TOOL_NAMES)
def test_each_adk_read_tool_is_path_free_secret_free_and_storage_invariant(
    tmp_path: Path,
    tool_name: str,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = _call_for(tool_name, fixture["run_id"])

    assert result["ok"] is True
    serialized = json.dumps(result)
    assert str(tmp_path) not in serialized
    assert fixture["sentinel"] not in serialized
    assert "artifact_root" not in serialized
    assert "registry_path" not in serialized
    assert "record_path" not in serialized
    assert "json_path" not in serialized
    assert "markdown_path" not in serialized
    assert _snapshot(roots) == before
    assert not roots[-1].exists()


def test_adk_tool_module_exposes_exact_read_surface_and_no_path_arguments() -> None:
    assert adk_tools.READ_TOOL_NAMES == tuple(READ_TOOL_NAMES)
    for name in READ_TOOL_NAMES:
        function = getattr(adk_tools, name)
        parameters = inspect.signature(function).parameters
        assert not {"path", "filename", "artifact_root", "manifest_path", "url"}.intersection(parameters)

    forbidden = (
        "create",
        "start",
        "rerun",
        "replay",
        "repeatability",
        "benchmark",
        "report_export",
        "review_decision",
    )
    assert not any(any(word in name for word in forbidden) for name in adk_tools.READ_TOOL_NAMES)


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda: adk_tools.get_run("../escape"), "invalid_argument"),
        (lambda: adk_tools.get_tool("x" * 129), "invalid_argument"),
        (lambda: adk_tools.get_workflow("bad/workflow"), "invalid_argument"),
        (lambda: adk_tools.get_artifact_projection("run-1", "C:/path"), "invalid_argument"),
        (lambda: adk_tools.list_runs(limit=0), "invalid_argument"),
        (lambda: adk_tools.list_runs(limit=101), "invalid_argument"),
        (lambda: adk_tools.list_runs(status="unknown"), "invalid_argument"),
    ],
)
def test_adk_tool_arguments_are_strictly_bounded(call, code: str) -> None:
    result = call()
    assert result == {
        "ok": False,
        "error": {"code": code, "message": "Tool arguments failed validation."},
    }


def test_adk_tool_failures_never_return_raw_transport_or_response_details() -> None:
    sentinel = "SENTINEL-RAW-EXCEPTION"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": f"C:/private/{sentinel}"})

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = adk_tools.get_health()

    assert result == {
        "ok": False,
        "error": {
            "code": "service_unavailable",
            "message": "Control plane is temporarily unavailable.",
        },
    }
    serialized = json.dumps(result)
    assert sentinel not in serialized
    assert "C:/private" not in serialized


@pytest.mark.parametrize("tool_name", ["get_run", "get_artifact_projection"])
def test_adk_tools_redact_sensitive_values_even_in_allowed_fields(tool_name: str) -> None:
    sentinel = "SENTINEL-SECRET-IN-ALLOWED-FIELD"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/runs/run-1":
            return httpx.Response(
                200,
                json={
                    "run": {
                        "run_id": "run-1",
                        "case_id": "case-1",
                        "status": "completed",
                        "summary": f"C:/private/{sentinel}",
                    }
                },
            )
        return httpx.Response(
            200,
                json={
                    "run_id": "run-1",
                    "artifact_id": "validated_input",
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "status": "available",
                "provenance": "deterministic",
                "data": {
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "inputs": {"note": f"C:/private/{sentinel}"},
                },
                "errors": [],
            },
        )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        if tool_name == "get_run":
            result = adk_tools.get_run("run-1")
        else:
            result = adk_tools.get_artifact_projection("run-1", "validated_input")

    serialized = json.dumps(result)
    assert result["ok"] is True
    assert sentinel not in serialized
    assert "C:/private" not in serialized


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "client_secret=opaque-value",
        "clientSecret: opaque-value",
        "private_key=opaque-value",
        "-----BEGIN PRIVATE KEY-----",
        "Authorization=opaque-value",
        "X-Auth-Header=opaque-value",
        "the access key is opaque-value",
        "Authorization header is opaque-value",
        "https://service-user:opaque-password@example.test",
    ),
)
def test_actual_adk_projection_envelope_redacts_sensitive_free_map_values(
    unsafe_value: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
                json={
                    "run_id": "run-1",
                    "artifact_id": "validated_input",
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "status": "available",
                "provenance": "deterministic",
                "data": {
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "inputs": {"note": unsafe_value},
                },
                "errors": [],
            },
        )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = adk_tools.get_artifact_projection("run-1", "validated_input")

    assert result["ok"] is True
    assert result["data"]["data"]["inputs"]["note"] == "[redacted]"


def test_actual_adk_projection_envelope_normalizes_sensitive_key_styles() -> None:
    sentinel = "opaque-3b978cd1"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
                json={
                    "run_id": "run-1",
                    "artifact_id": "validated_input",
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "status": "available",
                "provenance": "deterministic",
                "data": {
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "inputs": {
                        "secret_key": sentinel,
                        "secretKey": sentinel,
                        "TOKEN-VALUE": sentinel,
                        "authToken": sentinel,
                        "SECRETKEY": sentinel,
                        "TOKENVALUE": sentinel,
                        "AUTHTOKEN": sentinel,
                        "x_api_key": sentinel,
                        "xApiKey": sentinel,
                        "XAPIKEY": sentinel,
                        "apiKeyValue": sentinel,
                        "api-key-value": sentinel,
                        "personalAccessKey": sentinel,
                        "awsAccessKeyId": sentinel,
                        "note": f"x_api_key={sentinel}",
                        "usage": "Token count is 120 for this model.",
                        "secretaryName": "ordinary-business-value",
                        "tokenizationMethod": "ordinary-business-value",
                        "authenticityScore": "ordinary-business-value",
                    },
                },
                "errors": [],
            },
        )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = adk_tools.get_artifact_projection("run-1", "validated_input")

    assert result["ok"] is True
    inputs = result["data"]["data"]["inputs"]
    assert sentinel not in json.dumps(result)
    assert inputs == {
        "note": "[redacted]",
        "usage": "Token count is 120 for this model.",
        "secretaryName": "ordinary-business-value",
        "tokenizationMethod": "ordinary-business-value",
        "authenticityScore": "ordinary-business-value",
    }


def test_actual_adk_review_envelope_rejects_packet_case_identity_mismatch() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "run_id": "run-1",
                "review": {
                    "review_id": "review-run-1",
                    "run_id": "run-1",
                    "case_id": "case-1",
                    "status": "review_required",
                    "review_required": True,
                    "packet": {
                        "run_id": "run-1",
                        "case_id": "other-case",
                        "status": "review_required",
                    },
                },
            },
        )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = adk_tools.get_run_review_snapshot("run-1")

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_contract",
            "message": "Control plane returned an invalid response contract.",
        },
    }


@pytest.mark.parametrize(
    ("field_path", "wrong_value"),
    (
        (("data", "run_id"), "other-run"),
        (("data", "case_id"), "other-case"),
        (("data", "tool_id"), "other-tool"),
    ),
)
def test_actual_adk_projection_rejects_inner_identity_mismatch_safely(
    field_path: tuple[str, str],
    wrong_value: str,
) -> None:
    payload = {
        "run_id": "run-1",
        "artifact_id": "validated_input",
        "case_id": "case-1",
        "tool_id": "chainladder",
        "status": "available",
        "provenance": "deterministic",
        "data": {
            "case_id": "case-1",
            "run_id": "run-1",
            "tool_id": "chainladder",
            "inputs": {"sample_name": "RAA"},
        },
        "errors": [],
    }
    payload[field_path[0]][field_path[1]] = wrong_value

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = adk_tools.get_artifact_projection("run-1", "validated_input")

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_contract",
            "message": "Control plane returned an invalid response contract.",
        },
    }


def test_actual_adk_review_packet_case_conflict_is_storage_invariant_and_keeps_review_root_missing(
    tmp_path: Path,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    packet_path = roots[1] / "review_packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["case_id"] = "other-case"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = adk_tools.get_run_review_snapshot(fixture["run_id"])

    assert result == {
        "ok": False,
        "error": {
            "code": "http_error",
            "message": "Control plane rejected the request.",
        },
    }
    assert _snapshot(roots) == before
    assert not roots[-1].exists()


@pytest.mark.parametrize(
    ("relation", "identity_field"),
    (
        ("outer", "workspace_id"),
        ("packet", "workspace_id"),
        ("decision", "review_id"),
        ("decision", "run_id"),
    ),
)
def test_actual_adk_rejects_persisted_review_identity_conflicts_without_storage_changes(
    tmp_path: Path,
    relation: str,
    identity_field: str,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    run_id = fixture["run_id"]
    review_id = f"review-{run_id}"
    record = {
        "review_id": review_id,
        "run_id": run_id,
        "case_id": "case-adk-read-1",
        "workspace_id": "default-workspace",
        "status": "review_required",
        "reason_codes": ["threshold"],
        "packet": {
            "run_id": run_id,
            "case_id": "case-adk-read-1",
            "workspace_id": "default-workspace",
            "status": "review_required",
        },
        "decision": {
            "review_id": review_id,
            "run_id": run_id,
            "decision": "approved",
        },
    }
    if relation == "outer":
        record[identity_field] = f"mismatched-{identity_field}"
    else:
        record[relation][identity_field] = f"mismatched-{identity_field}"
    _write_json(roots[-1] / review_id / "review_record.json", record)
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = adk_tools.get_run_review_snapshot(run_id)

    assert result == {
        "ok": False,
        "error": {
            "code": "http_error",
            "message": "Control plane rejected the request.",
        },
    }
    assert _snapshot(roots) == before


def test_actual_asgi_adk_projection_redacts_natural_language_secrets_and_preserves_token_counts(
    tmp_path: Path,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    opaque = "opaque-natural-language-value"
    target = roots[1] / "validated_input.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["inputs"] = {
        "first_note": f"the access key is {opaque}",
        "second_note": f"Authorization header is {opaque}",
        "compact_api": f"APIKEY {opaque}",
        "compact_access": f"ACCESSKEY {opaque}",
        "third_note": f"AUTHHEADER {opaque}",
        "fourth_note": f"Proxy-Authorization Digest {opaque}",
        "fifth_note": f"private key is {opaque}",
        "mixed_usage": f"TokenCount 42; APIKEY {opaque}",
        "usage_space": "Token count is 42",
        "usage_dash": "token-count is 42",
        "usage_dot": "TOKEN.COUNT: 42",
        "usage_underscore": "token_count=42",
        "usage_compact": "TokenCount 42",
    }
    _write_json(target, payload)
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = adk_tools.get_artifact_projection(fixture["run_id"], "validated_input")

    assert result["ok"] is True
    inputs = result["data"]["data"]["inputs"]
    assert inputs == {
        "first_note": "[redacted]",
        "second_note": "[redacted]",
        "compact_api": "[redacted]",
        "compact_access": "[redacted]",
        "third_note": "[redacted]",
        "fourth_note": "[redacted]",
        "fifth_note": "[redacted]",
        "mixed_usage": "[redacted]",
        "usage_space": "Token count is 42",
        "usage_dash": "token-count is 42",
        "usage_dot": "TOKEN.COUNT: 42",
        "usage_underscore": "token_count=42",
        "usage_compact": "TokenCount 42",
    }
    assert opaque not in json.dumps(result)
    assert _snapshot(roots) == before


@pytest.mark.parametrize(
    ("summary", "expected"),
    (
        ("APIKEY opaque-run-summary", "[redacted]"),
        ("TokenCount 42", "TokenCount 42"),
    ),
)
def test_actual_asgi_adk_get_run_applies_shared_value_semantics(
    tmp_path: Path,
    summary: str,
    expected: str,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    registry = json.loads((roots[0] / "runs.json").read_text(encoding="utf-8"))
    registry["runs"][0]["summary"] = summary
    _write_json(roots[0] / "runs.json", registry)
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = adk_tools.get_run(fixture["run_id"])

    assert result["ok"] is True
    assert result["data"]["summary"] == expected
    assert _snapshot(roots) == before


@pytest.mark.parametrize(
    ("filename", "tool_name"),
    (
        ("run_manifest.json", "get_run"),
        ("run_manifest.json", "get_run_artifacts"),
        ("review_packet.json", "get_run_review_snapshot"),
    ),
)
def test_actual_adk_fixed_json_symlinks_fail_safely_and_preserve_storage(
    tmp_path: Path,
    filename: str,
    tool_name: str,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    sentinel = "ADK-OUTSIDE-FIXED-JSON-SENTINEL"
    outside = tmp_path / f"outside-{filename}"
    _write_json(
        outside,
        {
            "case_id": "case-adk-read-1",
            "run_id": fixture["run_id"],
            "tool_id": "chainladder",
            "status": "review_required",
            "artifact_paths": {},
            "sentinel": sentinel,
        },
    )
    target = roots[1] / filename
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = _call_for(tool_name, fixture["run_id"])

    assert result["ok"] is False
    serialized = json.dumps(result)
    assert sentinel not in serialized
    assert str(outside) not in serialized
    assert _snapshot(roots) == before


@pytest.mark.parametrize(
    "mutation",
    (
        {"run_id": "other-run"},
        {"case_id": "other-case"},
        {"artifact_paths": []},
    ),
)
@pytest.mark.parametrize("tool_name", ("get_run", "get_run_artifacts"))
def test_actual_adk_manifest_identity_and_shape_fail_safely_without_storage_changes(
    tmp_path: Path,
    mutation: dict[str, Any],
    tool_name: str,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    manifest_path = roots[1] / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(mutation)
    _write_json(manifest_path, manifest)
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = _call_for(tool_name, fixture["run_id"])

    assert result == {
        "ok": False,
        "error": {
            "code": "http_error",
            "message": "Control plane rejected the request.",
        },
    }
    assert _snapshot(roots) == before


def test_actual_adk_list_runs_reports_filtered_identity_mismatch_safely() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "run_count": 1,
                "runs": [
                    {
                        "run_id": "run-1",
                        "status": "completed",
                        "operator_id": "other-operator",
                    }
                ],
            },
        )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = adk_tools.list_runs(operator_id="operator-1")

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_contract",
            "message": "Control plane returned an invalid response contract.",
        },
    }


def test_actual_api_and_adk_round_trip_legacy_identity_filters(tmp_path: Path) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = adk_tools.list_runs(
            operator_id="local-actuary",
            workspace_id="default-workspace",
        )

    assert result["ok"] is True
    assert len(result["data"]) == 1
    assert result["data"][0]["operator_id"] == "local-actuary"
    assert result["data"][0]["workspace_id"] == "default-workspace"
    assert _snapshot(roots) == before


def test_actual_adk_envelope_reports_response_identity_mismatch_safely() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "run": {
                    "run_id": "cross-run",
                    "case_id": "case-1",
                    "status": "completed",
                }
            },
        )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = adk_tools.get_run("run-1")

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_contract",
            "message": "Control plane returned an invalid response contract.",
        },
    }


@pytest.mark.parametrize("relation", ("count", "event"))
def test_actual_adk_reports_shared_envelope_relation_failures_safely(
    relation: str,
) -> None:
    payload = (
        {
            "tool_count": 2,
            "tools": [
                {
                    "tool_id": "chainladder",
                    "method": "chainladder",
                    "title": "Chainladder",
                    "description": "Deterministic reserving",
                }
            ],
        }
        if relation == "count"
        else {
            "run_id": "run-1",
            "event_count": 1,
            "events": [
                {
                    "type": "run.completed",
                    "run_id": "run-1",
                    "status": "running",
                    "payload": {},
                }
            ],
        }
    )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=payload)
            ),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(factory):
        result = (
            adk_tools.list_tools()
            if relation == "count"
            else adk_tools.get_run_events("run-1")
        )

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_contract",
            "message": "Control plane returned an invalid response contract.",
        },
    }


def test_adk_run_manifest_accepts_the_server_safe_projection_without_raw_paths(
    tmp_path: Path,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    before = _snapshot(roots)

    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        result = adk_tools.get_artifact_projection(fixture["run_id"], "run_manifest")

    assert result == {
        "ok": True,
        "data": {
            "run_id": fixture["run_id"],
            "artifact_id": "run_manifest",
            "status": "available",
            "provenance": "system_manifest",
            "data": {
                "case_id": "case-adk-read-1",
                "run_id": fixture["run_id"],
                "created_by": "test",
            },
            "errors": [],
        },
    }
    assert _snapshot(roots) == before


def test_all_projection_entry_points_redact_embedded_paths_and_credentials() -> None:
    unsafe_values = (
        "prefix C:\\private\\record.json suffix",
        "prefix \\\\server\\share\\record.json suffix",
        "prefix file:///var/lib/private/record.json suffix",
        "prefix /var/lib/private/record.json suffix",
        "ghp_FAKE0000000000000000000000000000000000",
        "Bearer FAKE000000000000000000000000",
        "sessionid=FAKE000000000000000000000000",
        "Authorization: Basic ZmFrZTpzZWNyZXQ=",
        "Basic ZmFrZTpzZWNyZXQ=",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlLXVzZXIifQ.FAKESIGNATURE000000000000",
        r"\Users\private\record.json",
        r"\Device\HarddiskVolume1\private\record.json",
        r"\\?\C:\private\record.json",
        r"\??\C:\private\record.json",
    )
    outputs = (
        project_health(HealthStatus(ok=True, service=unsafe_values[12])),
        project_preflight(
            PreflightStatus(
                ok=True,
                service="control-plane",
                status="ok",
                readiness="ready",
                warnings=[],
                errors=[],
                summary={},
                configuration={
                    "catalog": {
                        "accessToken": "SENTINEL-CATALOG-ACCESS-TOKEN",
                        "note": unsafe_values[13],
                    }
                },
                runtime={},
                checks=[],
            )
        ),
        project_run(Run(run_id="run-1", status="completed", summary=unsafe_values[0])),
        project_event(
            RunEvent(
                type="run.completed",
                run_id="run-1",
                status="completed",
                summary=unsafe_values[1],
            )
        ),
        project_review(
            Review(
                status="review_required",
                review_required=True,
                reason_codes=[unsafe_values[2], unsafe_values[7]],
            )
        ),
        project_tool(
            ToolSummary(
                tool_id="tool-1",
                method="method-1",
                title="Title",
                description="Description",
                tags=[unsafe_values[3], unsafe_values[5], unsafe_values[9]],
            )
        ),
        project_workflow(
            Workflow(
                workflow_id="workflow-1",
                title="Title",
                description=unsafe_values[10],
                step_count=0,
            )
        ),
        project_artifact_metadata(
            ArtifactMetadata(
                artifact_id="validated_input",
                label=unsafe_values[11],
                present=True,
            )
        ),
        project_artifact_projection(
            ArtifactProjection(
                run_id="run-1",
                artifact_id="validated_input",
                status="available",
                provenance="deterministic",
                data={
                    "case_id": "case-1",
                    "tool_id": "chainladder",
                    "inputs": {
                        "note": unsafe_values[6],
                        "rooted": unsafe_values[4],
                        "basic": unsafe_values[8],
                    },
                },
            )
        ),
    )

    serialized = json.dumps(outputs)
    assert not any(value in serialized for value in unsafe_values)
    assert "SENTINEL-CATALOG-ACCESS-TOKEN" not in serialized
    assert "accessToken" not in serialized
    assert serialized.count("[redacted]") == len(unsafe_values)


def test_isolated_asgi_console_api_and_adk_share_authoritative_read_state(
    tmp_path: Path,
) -> None:
    fixture, roots = _tool_fixture(tmp_path)
    run_id = fixture["run_id"]
    before = _snapshot(roots)

    async def fetch(path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fixture["app"]),
            base_url="http://testserver",
        ) as client:
            response = await client.get(path)
            assert response.status_code == 200
            return response.json()

    api_run = asyncio.run(fetch(f"/runs/{run_id}"))
    api_events = asyncio.run(fetch(f"/runs/{run_id}/events"))
    api_artifacts = asyncio.run(fetch(f"/runs/{run_id}/artifacts"))
    api_review = asyncio.run(fetch(f"/runs/{run_id}/review"))
    console = asyncio.run(fetch(f"/console/state?run_id={run_id}"))
    with adk_tools.use_read_client_factory(fixture["client_factory"]):
        adk_workflows = adk_tools.list_workflows()
        adk_registry_tools = adk_tools.list_tools()
        adk_run = adk_tools.get_run(run_id)
        adk_events = adk_tools.get_run_events(run_id)
        adk_artifacts = adk_tools.get_run_artifacts(run_id)
        adk_review = adk_tools.get_run_review_snapshot(run_id)

    assert {"chainladder-basic", "chainladder-validated"} <= {
        item["workflow_id"] for item in adk_workflows["data"]
    }
    assert {"chainladder", "minimax_experience_study_tool"} <= {
        item["tool_id"] for item in adk_registry_tools["data"]
    }
    assert (
        api_run["run"]["status"]
        == console["selected_run"]["status"]
        == adk_run["data"]["status"]
        == "needs_review"
    )
    api_event_types = [item["type"] for item in api_events["events"]]
    assert api_event_types == [item["event_type"] for item in console["timeline"]]
    assert api_event_types == [item["type"] for item in adk_events["data"]]
    assert api_events["events"][-1]["status"] == adk_run["data"]["status"]
    assert is_terminal_run_status(api_events["events"][-1]["status"])
    api_artifact_ids = {
        item["artifact_id"] for item in api_artifacts["artifacts"] if item["present"]
    }
    console_artifact_ids = {
        item["artifact_id"]
        for item in console["artifact_panel"]["evidence_items"]
        if item["present"]
    }
    adk_artifact_ids = {
        item["artifact_id"] for item in adk_artifacts["data"] if item["present"]
    }
    assert api_artifact_ids == console_artifact_ids == adk_artifact_ids
    assert (
        api_review["review"]["status"]
        == console["review_panel"]["status"]
        == adk_review["data"]["status"]
        == "review_required"
    )
    assert _snapshot(roots) == before
    assert not roots[-1].exists()


def test_real_builtin_workflow_parent_manifest_api_console_and_adk_artifacts_match(
    tmp_path: Path,
) -> None:
    offline_runner = _load_runtime_module(
        "pr2_real_builtin_offline_runner",
        OPENAI_RUNTIME_RUNNER,
    )

    class ModelFreeGovernedRunner:
        @staticmethod
        def run_openai_governed_workflow(task, *, user_prompt=None):
            del user_prompt
            result = offline_runner.run_planner_workflow(task)
            worker = result["worker_result"]
            return {
                "route": result["route"],
                "trace": {"workflow_name": "model-free-real-builtin"},
                "worker_result": worker,
                "final_output": {
                    "case_id": worker["case_id"],
                    "worker_status": worker["status"],
                    "deterministic_method": worker["deterministic_result"]["method"],
                    "cited_values": worker["deterministic_result"]["reserve_summary"],
                    "review_reasons": worker["review_reasons"],
                    "artifact_manifest_path": worker["artifact_paths"]["run_manifest"],
                    "narrative_summary": worker["narrative_draft"]["summary"],
                },
            }

    settings = ApiSettings(
        registry_path=tmp_path / "registry" / "runs.json",
        artifact_root=tmp_path / "artifacts",
        review_store_dir=tmp_path / "reviews-not-created",
    )
    app = create_app(settings=settings, runner_module=ModelFreeGovernedRunner)

    async def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as local:
            return await local.request(method, path, **kwargs)

    run_response = asyncio.run(
        request(
            "POST",
            "/runs",
            json={
                "case_id": "real-workflow-parity",
                "workflow_id": "chainladder-basic",
                "background": False,
            },
        )
    )
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()
    run_id = run["run_id"]
    manifest_path = Path(run["final_output"]["artifact_manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact_paths"]["run_manifest"] == "run_manifest.json"

    child_manifest_path = Path(
        run["worker_result"]["artifact_paths"]["step_chainladder_run_manifest"]
    )
    child_manifest = json.loads(child_manifest_path.read_text(encoding="utf-8"))
    deterministic_result = json.loads(
        (child_manifest_path.parent / child_manifest["artifact_paths"]["deterministic_result"]).read_text(
            encoding="utf-8"
        )
    )
    assert deterministic_result["method"] == "chainladder"
    assert deterministic_result["reserve_summary"]["ibnr"] >= 0

    api_artifacts = asyncio.run(request("GET", f"/runs/{run_id}/artifacts")).json()[
        "artifacts"
    ]
    console = asyncio.run(request("GET", f"/console/state?run_id={run_id}")).json()
    console_artifacts = console["artifact_panel"]["artifacts"]

    def handler(request_value: httpx.Request) -> httpx.Response:
        response = asyncio.run(
            request(
                request_value.method,
                request_value.url.raw_path.decode("ascii"),
                headers=request_value.headers,
                content=request_value.content,
            )
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
        )

    def client_factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://testserver",
            transport=httpx.MockTransport(handler),
            max_get_attempts=1,
        )

    with adk_tools.use_read_client_factory(client_factory):
        adk_artifacts = adk_tools.get_run_artifacts(run_id)
        adk_manifest = adk_tools.get_artifact_projection(run_id, "run_manifest")

    api_ids = [item["artifact_id"] for item in api_artifacts]
    assert api_ids == [item["artifact_id"] for item in console_artifacts]
    assert api_ids == [item["artifact_id"] for item in adk_artifacts["data"]]
    assert api_ids == [
        "run_manifest",
        "step_chainladder_run_manifest",
        "workflow_summary",
    ]
    assert adk_manifest["ok"] is True
    assert adk_manifest["data"]["run_id"] == run_id
    assert adk_manifest["data"]["artifact_id"] == "run_manifest"
    assert adk_manifest["data"]["provenance"] == "system_manifest"
    serialized_metadata = json.dumps(
        [api_artifacts, console_artifacts, adk_artifacts, adk_manifest]
    )
    assert str(tmp_path) not in serialized_metadata
    assert all("path" not in item and "ref" not in item for item in api_artifacts)
    assert all("path" not in item and "ref" not in item for item in console_artifacts)


def _snapshot(roots: list[Path]) -> tuple[tuple[str, bool, tuple[tuple[str, str], ...]], ...]:
    result = []
    for root in roots:
        files = []
        if root.exists():
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                files.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
        result.append((root.name, root.exists(), tuple(files)))
    return tuple(result)
