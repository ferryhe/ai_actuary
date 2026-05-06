from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from reserving_workflow.api.app import (
    ApiSettings,
    _load_batch_runner_module,
    create_app,
)
from reserving_workflow.schemas import RunArtifactManifest
from reserving_workflow.workflows import WorkflowCatalog, WorkflowCatalogEntry, WorkflowStepEntry


class FakeWorkerTask:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeTaskContractsModule:
    WorkerTask = FakeWorkerTask


class FakeRunnerModule:
    calls = []

    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        FakeRunnerModule.calls.append(
            {
                "run_id": task.run_id,
                "case_id": task.case_ref,
                "artifact_dir": task.inputs["artifact_dir"],
                "required_artifacts": list(getattr(task, "required_artifacts", [])),
                "user_prompt": user_prompt,
            }
        )
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


class RelativeArtifactPathRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        artifact_dir = Path(task.inputs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        deterministic_result = artifact_dir / "deterministic_result.json"
        deterministic_result.write_text('{"ibnr": 1.0}', encoding="utf-8")
        run_manifest = artifact_dir / "run_manifest.json"
        run_manifest.write_text(
            json.dumps(
                {
                    "case_id": task.case_ref,
                    "run_id": task.run_id,
                    "artifact_root": str(artifact_dir),
                    "artifact_paths": {"deterministic_result": "deterministic_result.json"},
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


class ReviewRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        artifact_dir = Path(task.inputs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        packet_json = artifact_dir / "review_packet.json"
        packet_md = artifact_dir / "review_packet.md"
        packet_json.write_text('{"status":"review_required","failed_checks":["threshold"]}', encoding="utf-8")
        packet_md.write_text("# Review Packet\n", encoding="utf-8")
        return {
            "route": {"mode": "governed"},
            "trace": {"workflow_name": "test-workflow"},
            "worker_result": {
                "status": "needs_review",
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "summary": "needs review",
                "artifact_paths": {},
                "metrics": {},
                "review_reasons": ["threshold"],
                "errors": [],
                "worker_metadata": {"adapter": "fake"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "needs_review",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 1.0},
                "review_reasons": ["threshold"],
                "artifact_manifest_path": None,
                "narrative_summary": "needs review",
            },
            "review_packet": {
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "status": "review_required",
                "failed_checks": ["threshold"],
                "packet_paths": {"json": str(packet_json), "markdown": str(packet_md)},
            },
        }


class FakeReplayModule:
    @staticmethod
    def replay_case_from_manifest(manifest_path):
        return {"case_id": "replay-case", "manifest_path": str(manifest_path), "matches_saved_result": True}

    @staticmethod
    def compare_repeatability(manifest_paths):
        return {"case_id": "repeat-case", "run_count": len(manifest_paths), "stable_ibnr": True}


class ValidationErrorReplayModule:
    @staticmethod
    def replay_case_from_manifest(manifest_path):
        return RunArtifactManifest.model_validate({})

    @staticmethod
    def compare_repeatability(manifest_paths):
        return RunArtifactManifest.model_validate({})


class FakeBatchRunnerModule:

    @staticmethod
    def run_batch_benchmark(*, cases, artifact_root):
        return {"case_count": len(cases), "artifact_root": str(artifact_root), "comparison_report_path": str(Path(artifact_root) / "comparison_report.json")}


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


def _client(
    tmp_path,
    runner_module=FakeRunnerModule,
    replay_module=FakeReplayModule,
    batch_runner_module=FakeBatchRunnerModule,
    background_task_runner=None,
    workflow_catalog=None,
):
    settings = ApiSettings(
        registry_path=tmp_path / "run-registry.json",
        artifact_root=tmp_path / "artifacts",
    )
    app = create_app(
        settings=settings,
        runner_module=runner_module,
        task_contracts_module=FakeTaskContractsModule,
        replay_module=replay_module,
        batch_runner_module=batch_runner_module,
        background_task_runner=background_task_runner,
        workflow_catalog=workflow_catalog,
    )
    return LocalApiClient(app)


def _reset_fake_runner_calls():
    FakeRunnerModule.calls.clear()


def test_post_run_records_registry_and_returns_operator_contract(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post("/runs", json={"case_id": "api-case"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["case_id"] == "api-case"
    assert payload["run_id"].startswith("operator-api-case-")

    list_payload = client.get("/runs").json()
    assert list_payload["run_count"] == 1
    assert list_payload["runs"][0]["run_id"] == payload["run_id"]


def test_post_run_normalizes_tool_backed_request_and_writes_validated_input_artifact(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post(
        "/runs",
        json={
            "case_id": "tool-backed-case",
            "tool_id": "chainladder",
            "inputs": {"sample_name": "RAA", "method": "chainladder", "review_threshold_origin_count": 3},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    validated_input_path = Path(payload["worker_result"]["artifact_paths"]["validated_input"])
    assert validated_input_path.exists()
    validated_input = json.loads(validated_input_path.read_text(encoding="utf-8"))
    assert validated_input == {
        "case_id": "tool-backed-case",
        "tool_id": "chainladder",
        "inputs": {
            "sample_name": "RAA",
            "method_variant": "chainladder",
            "review_threshold_origin_count": 3,
        },
    }

    run_manifest = Path(payload["final_output"]["artifact_manifest_path"])
    manifest_payload = json.loads(run_manifest.read_text(encoding="utf-8"))
    assert manifest_payload["artifact_paths"]["validated_input"] == str(validated_input_path)


def test_post_run_normalizes_legacy_method_alias_into_tool_backed_validated_input(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post(
        "/runs",
        json={
            "case_id": "legacy-method-case",
            "sample_name": "RAA",
            "method": "chainladder",
            "review_threshold_origin_count": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    validated_input_path = Path(payload["worker_result"]["artifact_paths"]["validated_input"])
    validated_input = json.loads(validated_input_path.read_text(encoding="utf-8"))
    assert validated_input["tool_id"] == "chainladder"
    assert validated_input["inputs"]["sample_name"] == "RAA"
    assert validated_input["inputs"]["method_variant"] == "chainladder"
    assert validated_input["inputs"]["review_threshold_origin_count"] == 2


def test_post_run_rejects_unknown_tool_id_with_http_400(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post("/runs", json={"case_id": "bad-tool-case", "tool_id": "unknown-tool"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown tool_id: unknown-tool"


def test_post_run_rejects_invalid_chainladder_method_variant_with_http_400(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post(
        "/runs",
        json={"case_id": "bad-variant-case", "tool_id": "chainladder", "inputs": {"method": "mack"}},
    )

    assert response.status_code == 400
    assert "chainladder" in str(response.json()["detail"])


def test_tool_catalog_endpoints_expose_builtin_chainladder(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    tools = client.get("/tools")
    tool = client.get("/tools/chainladder")

    assert tools.status_code == 200
    assert tools.json()["tool_count"] == 1
    assert tools.json()["tools"][0]["tool_id"] == "chainladder"
    assert tool.status_code == 200
    assert tool.json()["method"] == "chainladder"
    assert tool.json()["input_schema"]["properties"]["sample_name"]["default"] == "RAA"
    assert tool.json()["input_schema"]["properties"]["method_variant"]["const"] == "chainladder"


def test_post_run_can_accept_background_execution_and_poll_events(tmp_path):
    _reset_fake_runner_calls()
    scheduled = []

    def capture_background_task(fn, *args, **kwargs):
        scheduled.append((fn, args, kwargs))

    client = _client(tmp_path, background_task_runner=capture_background_task)

    response = client.post("/runs", json={"case_id": "background-case", "background": True})

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "accepted"
    assert accepted["case_id"] == "background-case"
    assert accepted["run_id"].startswith("operator-background-case-")
    assert len(scheduled) == 1

    initial_events = client.get(f"/runs/{accepted['run_id']}/events").json()["events"]
    assert [event["event_type"] for event in initial_events] == ["run.accepted"]

    fn, args, kwargs = scheduled.pop()
    fn(*args, **kwargs)

    events = client.get(f"/runs/{accepted['run_id']}/events").json()["events"]
    assert [event["event_type"] for event in events] == [
        "run.accepted",
        "run.queued",
        "run.running",
        "run.completed",
    ]


def test_post_run_rejects_unsafe_default_artifact_case_id(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post("/runs", json={"case_id": "../escape"})

    assert response.status_code == 400
    assert "Invalid case_id" in response.json()["detail"]
    assert not (tmp_path / "escape").exists()


def test_post_run_rejects_unsafe_case_id_even_with_explicit_artifact_dir(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post(
        "/runs",
        json={"case_id": "bad/case", "artifact_dir": str(tmp_path / "explicit-artifacts"), "background": True},
    )

    assert response.status_code == 400
    assert "Invalid case_id" in response.json()["detail"]
    assert not (tmp_path / "run-registry.json").exists()


def test_run_detail_exposes_symphony_style_events_and_artifacts(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)
    run = client.post("/runs", json={"case_id": "detail-case"}).json()

    response = client.get(f"/runs/{run['run_id']}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["run"]["run_id"] == run["run_id"]
    assert [event["event_type"] for event in detail["events"]] == [
        "run.queued",
        "run.running",
        "run.completed",
    ]
    assert [event["type"] for event in detail["events"]] == [
        "run.queued",
        "run.running",
        "run.completed",
    ]
    assert detail["artifact_manifest"]["case_id"] == "detail-case"
    assert detail["artifacts"][0]["artifact_id"] == "run_manifest"


def test_run_detail_resolves_relative_manifest_artifact_paths(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path, runner_module=RelativeArtifactPathRunnerModule)
    run = client.post("/runs", json={"case_id": "relative-artifact-case"}).json()

    response = client.get(f"/runs/{run['run_id']}")

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    assert artifact["artifact_id"] == "deterministic_result"
    assert artifact["present"] is True
    assert artifact["path"].endswith("deterministic_result.json")
    assert Path(artifact["path"]).is_absolute()


def test_console_shell_serves_operator_console_html(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.get("/console")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "AI Actuary Operator Console" in html
    assert "Run Queue" in html
    assert "Timeline" in html
    assert "Artifact Panel" in html
    assert "Review Panel" in html
    assert "Review Inbox" in html
    assert "Action Panel" in html
    assert "/console/state" in html
    assert "renderConsoleError" in html
    assert "response.ok" in html
    assert "JSON.parse" in html
    assert "Create Governed Run" in html
    assert "case_id" in html
    assert "sample_name" in html
    assert "tool-selector" in html
    assert "review_threshold_origin_count" in html
    assert "background" in html
    assert "createRun(" in html
    assert "pollRunEvents(" in html
    assert "runConsoleAction(" in html
    assert "submitReviewDecision(" in html
    assert "loadToolCatalog()" in html
    assert "fetch(\"/tools\")" in html
    assert "fetch(`/reviews/${encodeURIComponent(reviewId)}/decision`" in html
    assert "Tool catalog unavailable; using default tool." in html
    assert "fallback.selected = true" in html


def test_console_actionable_html_exposes_ai_facing_operation_contracts(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    html = client.get("/console").text

    assert "name=\"sample_name\"" in html
    assert "value=\"RAA\"" in html
    assert "name=\"tool_id\"" in html
    assert "data-default-tool-id=\"chainladder\"" in html
    assert "name=\"background\"" in html
    assert "checked" in html
    assert "fetch(\"/runs\"" in html
    assert "tool_id:" in html
    assert "inputs:" in html
    assert "method: String(formData.get(\"tool_id\") || \"chainladder\")" in html
    assert "Tool catalog" in html
    assert "fetch(`/runs/${encodeURIComponent(runId)}/events`)" in html
    assert "review-inbox" in html
    assert "review-id-input" in html
    assert "changes_requested" in html
    assert "run.accepted" in html
    assert "run.queued" in html
    assert "run.running" in html
    assert "run.completed" in html
    assert "run.needs_review" in html
    assert "run.failed" in html
    assert "formatResponseDetail(payload.detail)" in html
    assert "JSON.stringify(detail)" in html
    assert "Number.isInteger(thresholdNumber)" in html
    assert "review_threshold_origin_count must be a non-negative integer" in html
    assert "pollGeneration += 1" in html
    assert "runId === selectedRunId" in html
    assert "if (!isCurrentPoll()) return" in html
    assert "No review selected for decision submission." in html


def test_console_state_exposes_symphony_style_panels(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path, runner_module=ReviewRunnerModule)
    run = client.post("/runs", json={"case_id": "console-case"}).json()

    response = client.get(f"/console/state?run_id={run['run_id']}")

    assert response.status_code == 200
    state = response.json()
    assert state["console"]["title"] == "AI Actuary Operator Console"
    assert state["tool_catalog"]["tools"][0]["tool_id"] == "chainladder"
    assert state["selected_run_id"] == run["run_id"]
    assert state["selected_run"] == {
        "run_id": run["run_id"],
        "case_id": "console-case",
        "status": "needs_review",
        "summary": "needs review",
        "created_at": state["selected_run"]["created_at"],
        "updated_at": state["selected_run"]["updated_at"],
        "artifact_root": state["selected_run"]["artifact_root"],
        "review_required": True,
    }
    assert "operator_params" not in state["selected_run"]
    assert "errors" not in state["selected_run"]
    assert state["run_cards"][0]["run_id"] == run["run_id"]
    assert state["run_cards"][0]["needs_review"] is True
    assert [event["event_type"] for event in state["timeline"]] == [
        "run.queued",
        "run.running",
        "run.needs_review",
    ]
    assert state["artifact_panel"]["artifact_root"]
    assert state["review_inbox"][0]["review_id"].startswith("review-")
    assert state["review_inbox"][0]["status"] == "review_required"
    assert state["review_panel"]["present"] is True
    assert state["review_panel"]["review_id"].startswith("review-")
    assert state["review_panel"]["status"] == "review_required"
    assert [action["action_id"] for action in state["action_panel"]["actions"]] == ["rerun"]
    assert state["action_panel"]["actions"][0]["semantics"]["creates_distinct_run"] is True


def test_console_state_defaults_to_latest_run(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)
    older = client.post("/runs", json={"case_id": "older-console-case"}).json()
    newer = client.post("/runs", json={"case_id": "newer-console-case"}).json()

    response = client.get("/console/state")

    assert response.status_code == 200
    state = response.json()
    assert state["selected_run_id"] == newer["run_id"]
    assert state["selected_run"]["run_id"] == newer["run_id"]
    assert {card["run_id"] for card in state["run_cards"]} == {older["run_id"], newer["run_id"]}


def test_rerun_endpoint_creates_distinct_run_id(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)
    original = client.post(
        "/runs",
        json={
            "case_id": "rerun-case",
            "tool_id": "chainladder",
            "inputs": {"sample_name": "RAA", "review_threshold_origin_count": 4},
        },
    ).json()

    registry = json.loads((tmp_path / "run-registry.json").read_text(encoding="utf-8"))
    original_entry = next(item for item in registry["runs"] if item["run_id"] == original["run_id"])
    assert original_entry["operator_params"]["validated_input"] == {
        "case_id": "rerun-case",
        "tool_id": "chainladder",
        "inputs": {
            "sample_name": "RAA",
            "method_variant": "chainladder",
            "review_threshold_origin_count": 4,
        },
    }

    rerun = client.post(f"/runs/{original['run_id']}/rerun", json={}).json()

    assert rerun["status"] == "completed"
    assert rerun["run_id"] != original["run_id"]
    assert rerun["rerun"]["source_run_id"] == original["run_id"]
    rerun_validated_input_path = Path(rerun["worker_result"]["artifact_paths"]["validated_input"])
    assert json.loads(rerun_validated_input_path.read_text(encoding="utf-8")) == original_entry["operator_params"]["validated_input"]
    runs = client.get("/runs").json()["runs"]
    assert {item["run_id"] for item in runs} == {original["run_id"], rerun["run_id"]}


def test_review_packet_endpoint_returns_packet_metadata(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path, runner_module=ReviewRunnerModule)
    run = client.post("/runs", json={"case_id": "review-case"}).json()

    response = client.get(f"/runs/{run['run_id']}/review-packet")

    assert response.status_code == 200
    payload = response.json()
    assert payload["present"] is True
    assert payload["packet"]["status"] == "review_required"
    assert payload["markdown_path"].endswith("review_packet.md")


def test_review_endpoints_materialize_review_records_and_list_inbox(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path, runner_module=ReviewRunnerModule)
    run = client.post("/runs", json={"case_id": "review-inbox-case"}).json()

    run_review = client.get(f"/runs/{run['run_id']}/review")
    reviews = client.get("/reviews")
    review_id = run_review.json()["review"]["review_id"]
    detail = client.get(f"/reviews/{review_id}")

    assert run_review.status_code == 200
    assert run_review.json()["review"]["status"] == "review_required"
    assert reviews.status_code == 200
    assert reviews.json()["review_count"] == 1
    assert reviews.json()["reviews"][0]["review_id"] == review_id
    assert detail.status_code == 200
    assert detail.json()["review"]["run_id"] == run["run_id"]


def test_review_decision_submission_writes_independent_artifacts_without_mutating_run_status(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path, runner_module=ReviewRunnerModule)
    run = client.post("/runs", json={"case_id": "decision-case"}).json()
    review = client.get(f"/runs/{run['run_id']}/review").json()["review"]

    response = client.post(
        f"/reviews/{review['review_id']}/decision",
        json={"decision": "changes_requested", "comment": "Please rerun with updated assumptions.", "decided_by": "actuary-001"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"]["decision"] == "changes_requested"
    assert payload["run_status"] == "needs_review"
    assert payload["review"]["status"] == "review_decided"
    run_detail = client.get(f"/runs/{run['run_id']}").json()
    decision_json_path = Path(run_detail["run"]["artifact_root"]) / "review_decision.json"
    decision_md_path = Path(run_detail["run"]["artifact_root"]) / "review_decision.md"
    assert json.loads(decision_json_path.read_text(encoding="utf-8"))["decision"] == "changes_requested"
    assert "updated assumptions" in decision_md_path.read_text(encoding="utf-8")
    assert run_detail["run"]["status"] == "needs_review"
    assert run_detail["artifact_manifest"]["artifact_paths"]["review_decision"] == str(decision_json_path)


def test_review_decision_endpoint_rejects_invalid_decision_values(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path, runner_module=ReviewRunnerModule)
    run = client.post("/runs", json={"case_id": "bad-decision-case"}).json()
    review = client.get(f"/runs/{run['run_id']}/review").json()["review"]

    response = client.post(f"/reviews/{review['review_id']}/decision", json={"decision": "pending"})

    assert response.status_code == 400


def test_replay_and_repeatability_endpoints_wrap_existing_helpers(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    replay = client.post("/replay", json={"manifest_path": "./tmp/run_manifest.json"}).json()
    repeatability = client.post(
        "/repeatability",
        json={"manifest_paths": ["./tmp/a/run_manifest.json", "./tmp/b/run_manifest.json"]},
    ).json()

    assert replay["matches_saved_result"] is True
    assert replay["manifest_path"] == "./tmp/run_manifest.json"
    assert repeatability["run_count"] == 2
    assert repeatability["stable_ibnr"] is True


def test_replay_and_repeatability_validation_errors_return_400(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path, replay_module=ValidationErrorReplayModule)

    replay = client.post("/replay", json={"manifest_path": "./tmp/bad_manifest.json"})
    repeatability = client.post("/repeatability", json={"manifest_paths": ["./tmp/bad_manifest.json"]})

    assert replay.status_code == 400
    assert repeatability.status_code == 400


def test_default_batch_runner_loader_finds_repo_runner():
    module = _load_batch_runner_module()

    assert hasattr(module, "run_batch_benchmark")


def test_batch_benchmark_endpoint_wraps_existing_runner(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    result = client.post(
        "/benchmarks/batch",
        json={"cases": [{"case_id": "batch-a"}], "artifact_root": str(tmp_path / "batch")},
    ).json()

    assert result["case_count"] == 1
    assert result["artifact_root"] == str(tmp_path / "batch")


def test_workflow_catalog_endpoints_expose_builtin_workflow(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    workflows = client.get("/workflows")
    workflow = client.get("/workflows/chainladder-basic")

    assert workflows.status_code == 200
    assert workflows.json()["workflow_count"] == 1
    assert workflows.json()["workflows"][0]["workflow_id"] == "chainladder-basic"
    assert workflows.json()["workflows"][0]["step_count"] == 1
    assert workflow.status_code == 200
    assert workflow.json()["workflow_id"] == "chainladder-basic"
    assert workflow.json()["steps"][0]["tool_id"] == "chainladder"


def test_post_run_with_workflow_id_executes_steps_sequentially_and_records_workflow_events(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)

    response = client.post(
        "/runs",
        json={"case_id": "workflow-case", "workflow_id": "chainladder-basic", "background": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["workflow"]["workflow_id"] == "chainladder-basic"
    assert payload["workflow"]["step_count"] == 1
    assert payload["workflow"]["steps"][0]["status"] == "completed"
    assert len(FakeRunnerModule.calls) == 1
    assert Path(FakeRunnerModule.calls[0]["artifact_dir"]).name == "chainladder"

    run_id = payload["run_id"]
    events = client.get(f"/runs/{run_id}/events").json()["events"]
    assert [event["event_type"] for event in events] == [
        "run.queued",
        "run.running",
        "workflow.started",
        "workflow.step.started",
        "workflow.step.completed",
        "workflow.completed",
        "run.completed",
    ]
    assert events[2]["payload"]["workflow_id"] == "chainladder-basic"
    assert events[3]["payload"]["step_id"] == "chainladder"

    detail = client.get(f"/runs/{run_id}").json()
    assert detail["run"]["workflow_id"] == "chainladder-basic"
    assert detail["artifact_manifest"]["workflow_id"] == "chainladder-basic"
    assert detail["artifact_manifest"]["artifact_paths"]["workflow_summary"].endswith("workflow_summary.json")
    assert any(artifact["artifact_id"] == "step_chainladder_run_manifest" for artifact in detail["artifacts"])


def test_post_run_with_workflow_id_accepts_background_mode_without_changing_legacy_background_contract(tmp_path):
    _reset_fake_runner_calls()
    scheduled = []

    def capture_background_task(fn, *args, **kwargs):
        scheduled.append((fn, args, kwargs))

    client = _client(tmp_path, background_task_runner=capture_background_task)

    response = client.post("/runs", json={"case_id": "workflow-background", "workflow_id": "chainladder-basic", "background": True})

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "accepted"
    assert accepted["execution_mode"] == "background"
    assert len(scheduled) == 1

    fn, args, kwargs = scheduled.pop()
    fn(*args, **kwargs)

    events = client.get(f"/runs/{accepted['run_id']}/events").json()["events"]
    assert events[-1]["event_type"] == "run.completed"
    assert "workflow.completed" in [event["event_type"] for event in events]


def test_rerun_endpoint_supports_workflow_backed_parent_runs(tmp_path):
    _reset_fake_runner_calls()
    client = _client(tmp_path)
    original = client.post("/runs", json={"case_id": "workflow-rerun", "workflow_id": "chainladder-basic"}).json()

    rerun = client.post(f"/runs/{original['run_id']}/rerun", json={}).json()

    assert rerun["status"] == "completed"
    assert rerun["run_id"] != original["run_id"]
    assert rerun["workflow"]["workflow_id"] == "chainladder-basic"
    assert rerun["rerun"]["source_run_id"] == original["run_id"]
    runs = client.get("/runs").json()["runs"]
    assert {item["run_id"] for item in runs} == {original["run_id"], rerun["run_id"]}


def test_post_run_with_injected_workflow_catalog_uses_selected_workflow_detail(tmp_path):
    _reset_fake_runner_calls()
    custom_catalog = WorkflowCatalog(
        entries=[
            WorkflowCatalogEntry(
                workflow_id="custom-chainladder",
                title="Custom Chainladder",
                description="Injected workflow used by API tests.",
                steps=[
                    WorkflowStepEntry(
                        step_id="custom-step",
                        tool_id="chainladder",
                        title="Custom step",
                        inputs={"sample_name": "RAA"},
                    )
                ],
            )
        ]
    )
    client = _client(tmp_path, workflow_catalog=custom_catalog)

    response = client.post(
        "/runs",
        json={"case_id": "workflow-custom", "workflow_id": "custom-chainladder", "background": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workflow"]["workflow_id"] == "custom-chainladder"
    assert payload["workflow"]["steps"][0]["step_id"] == "custom-step"
    assert Path(FakeRunnerModule.calls[0]["artifact_dir"]).name == "custom-step"


def test_workflow_needs_review_status_uses_review_event_types(tmp_path):
    client = _client(tmp_path, runner_module=ReviewRunnerModule)

    response = client.post("/runs", json={"case_id": "workflow-review", "workflow_id": "chainladder-basic"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_review"
    events = client.get(f"/runs/{payload['run_id']}/events").json()["events"]
    event_types = [event["event_type"] for event in events]
    assert "workflow.step.needs_review" in event_types
    assert "workflow.needs_review" in event_types
    assert "workflow.step.failed" not in event_types
    assert "workflow.failed" not in event_types
