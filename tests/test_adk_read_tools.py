from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from developer_workflows.ai_actuary_developer import tools as adk_tools
from reserving_workflow.adapters.control_plane import ReadOnlyControlPlaneClient
from reserving_workflow.api.app import ApiSettings, create_app
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


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
                "status": "available",
                "provenance": "deterministic",
                "data": {
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


def _snapshot(roots: list[Path]) -> tuple[tuple[str, bool, tuple[tuple[str, str], ...]], ...]:
    result = []
    for root in roots:
        files = []
        if root.exists():
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                files.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
        result.append((root.name, root.exists(), tuple(files)))
    return tuple(result)
