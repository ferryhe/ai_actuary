from __future__ import annotations

import importlib.util
import asyncio
import json
from pathlib import Path

import httpx

from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.contracts import AgentExecutionPlan


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_MODULE_PATH = REPO_ROOT / "workflows" / "agent-runtimes" / "hermes-worker" / "control_plane_client.py"


class FakeWorkerTask:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeTaskContractsModule:
    WorkerTask = FakeWorkerTask


class FakeRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        artifact_dir = Path(task.inputs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run_manifest = artifact_dir / "run_manifest.json"
        run_manifest.write_text(
            json.dumps(
                {
                    "case_id": task.case_ref,
                    "run_id": task.run_id,
                    "artifact_root": str(artifact_dir),
                    "artifact_paths": {"run_manifest": str(run_manifest)},
                }
            ),
            encoding="utf-8",
        )
        return {
            "route": {"mode": "governed"},
            "trace": {"workflow_name": "test-workflow"},
            "worker_result": {
                "status": "completed",
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "summary": "worker complete",
                "artifact_paths": {"run_manifest": str(run_manifest)},
                "metrics": {},
                "review_reasons": [],
                "errors": [],
                "worker_metadata": {"adapter": "fake"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "completed",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 1.0},
                "review_reasons": [],
                "artifact_manifest_path": str(run_manifest),
                "narrative_summary": "ok",
            },
        }


class LocalApiClient:
    def __init__(self, app):
        self._app = app

    async def _request(self, method: str, path: str, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app), base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    def request(self, method: str, path: str, **kwargs):
        return asyncio.run(self._request(method, path, **kwargs))

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_hermes_control_plane_client_uses_public_http_surfaces_with_mock_transport():
    module = _load_module("control_plane_client_mock", CLIENT_MODULE_PATH)
    requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content.decode() or "{}")))
        if request.method == "POST" and request.url.path == "/runs":
            return httpx.Response(
                202,
                json={
                    "run_id": "run-14",
                    "case_id": "case-14",
                    "status": "accepted",
                    "summary": "accepted",
                    "execution_mode": "background",
                },
            )
        if request.method == "GET" and request.url.path == "/runs/run-14":
            return httpx.Response(
                200,
                json={
                    "run": {
                        "run_id": "run-14",
                        "case_id": "case-14",
                        "status": "needs_review",
                        "summary": "Needs review",
                        "review_required": True,
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/runs/run-14/events":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-14",
                    "event_count": 3,
                    "events": [
                        {"type": "run.accepted", "run_id": "run-14", "status": "accepted"},
                        {"type": "run.running", "run_id": "run-14", "status": "running"},
                        {"type": "run.needs_review", "run_id": "run-14", "status": "needs_review"},
                    ],
                },
            )
        if request.method == "GET" and request.url.path == "/runs/run-14/artifacts":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-14",
                    "artifacts": [
                        {"artifact_id": "run_manifest", "path": "/tmp/run_manifest.json", "present": True},
                        {"artifact_id": "review_packet", "path": "/tmp/review_packet.json", "present": True},
                    ],
                },
            )
        if request.method == "GET" and request.url.path == "/runs/run-14/review":
            return httpx.Response(
                200,
                json={
                    "review": {
                        "status": "review_required",
                        "review_required": True,
                        "review_id": "review-run-14",
                        "run_id": "run-14",
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    client = module.HermesControlPlaneClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
    )
    plan = AgentExecutionPlan(
        case_id="case-14",
        objective="Run case",
        workflow_id="chainladder-basic",
        inputs={"sample_name": "RAA"},
    )

    handle = client.create_run(plan)
    summary = client.wait_for_terminal_run(handle.run_id, max_polls=1)

    assert requests[0] == (
        "POST",
        "/runs",
        {
            "case_id": "case-14",
            "objective": "Run case",
            "workflow_id": "chainladder-basic",
            "inputs": {"sample_name": "RAA"},
            "background": True,
        },
    )
    assert handle.status == "accepted"
    assert summary.status == "needs_review"
    assert summary.terminal is True
    assert summary.artifact_ids == ["run_manifest", "review_packet"]
    assert summary.review_status == "review_required"
    client.close()


def test_public_api_contract_supports_hermes_adapter_regression(tmp_path):
    settings = ApiSettings(
        registry_path=tmp_path / "run-registry.json",
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(
        settings=settings,
        runner_module=FakeRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
    )
    client = LocalApiClient(app)
    create_response = client.post(
        "/runs",
        json={
            "case_id": "case-14",
            "objective": "Run tool",
            "tool_id": "chainladder",
            "inputs": {"sample_name": "RAA"},
            "background": False,
        },
    )
    create_payload = create_response.json()
    run_id = create_payload["run_id"]

    events_payload = client.get(f"/runs/{run_id}/events").json()
    artifacts_payload = client.get(f"/runs/{run_id}/artifacts").json()
    review_payload = client.get(f"/runs/{run_id}/review").json()

    assert create_response.status_code == 200
    assert create_payload["status"] == "completed"
    assert events_payload["events"][-1]["status"] == "completed"
    assert any(item["artifact_id"] == "run_manifest" for item in artifacts_payload["artifacts"])
    assert review_payload["review"]["status"] == "not_required"
