from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from reserving_workflow.api.app import ApiSettings, _load_batch_runner_module, create_app
from reserving_workflow.schemas import RunArtifactManifest


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


def _client(
    tmp_path,
    runner_module=FakeRunnerModule,
    replay_module=FakeReplayModule,
    batch_runner_module=FakeBatchRunnerModule,
    background_task_runner=None,
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
    )
    return TestClient(app)


def test_post_run_records_registry_and_returns_operator_contract(tmp_path):
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


def test_post_run_can_accept_background_execution_and_poll_events(tmp_path):
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
    client = _client(tmp_path)

    response = client.post("/runs", json={"case_id": "../escape"})

    assert response.status_code == 400
    assert "Invalid case_id" in response.json()["detail"]
    assert not (tmp_path / "escape").exists()


def test_post_run_rejects_unsafe_case_id_even_with_explicit_artifact_dir(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/runs",
        json={"case_id": "bad/case", "artifact_dir": str(tmp_path / "explicit-artifacts"), "background": True},
    )

    assert response.status_code == 400
    assert "Invalid case_id" in response.json()["detail"]
    assert not (tmp_path / "run-registry.json").exists()


def test_run_detail_exposes_symphony_style_events_and_artifacts(tmp_path):
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
    assert detail["artifact_manifest"]["case_id"] == "detail-case"


def test_console_shell_serves_operator_console_html(tmp_path):
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
    assert "Action Panel" in html
    assert "/console/state" in html
    assert "renderConsoleError" in html
    assert "response.ok" in html
    assert "JSON.parse" in html


def test_console_state_exposes_symphony_style_panels(tmp_path):
    client = _client(tmp_path, runner_module=ReviewRunnerModule)
    run = client.post("/runs", json={"case_id": "console-case"}).json()

    response = client.get(f"/console/state?run_id={run['run_id']}")

    assert response.status_code == 200
    state = response.json()
    assert state["console"]["title"] == "AI Actuary Operator Console"
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
    assert state["review_panel"]["present"] is True
    assert state["review_panel"]["status"] == "review_required"
    assert [action["action_id"] for action in state["action_panel"]["actions"]] == ["rerun"]


def test_console_state_defaults_to_latest_run(tmp_path):
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
    client = _client(tmp_path)
    original = client.post("/runs", json={"case_id": "rerun-case"}).json()

    rerun = client.post(f"/runs/{original['run_id']}/rerun", json={}).json()

    assert rerun["status"] == "completed"
    assert rerun["run_id"] != original["run_id"]
    runs = client.get("/runs").json()["runs"]
    assert {item["run_id"] for item in runs} == {original["run_id"], rerun["run_id"]}


def test_review_packet_endpoint_returns_packet_metadata(tmp_path):
    client = _client(tmp_path, runner_module=ReviewRunnerModule)
    run = client.post("/runs", json={"case_id": "review-case"}).json()

    response = client.get(f"/runs/{run['run_id']}/review-packet")

    assert response.status_code == 200
    payload = response.json()
    assert payload["present"] is True
    assert payload["packet"]["status"] == "review_required"
    assert payload["markdown_path"].endswith("review_packet.md")


def test_replay_and_repeatability_endpoints_wrap_existing_helpers(tmp_path):
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
    client = _client(tmp_path, replay_module=ValidationErrorReplayModule)

    replay = client.post("/replay", json={"manifest_path": "./tmp/bad_manifest.json"})
    repeatability = client.post("/repeatability", json={"manifest_paths": ["./tmp/bad_manifest.json"]})

    assert replay.status_code == 400
    assert repeatability.status_code == 400


def test_default_batch_runner_loader_finds_repo_runner():
    module = _load_batch_runner_module()

    assert hasattr(module, "run_batch_benchmark")


def test_batch_benchmark_endpoint_wraps_existing_runner(tmp_path):
    client = _client(tmp_path)

    result = client.post(
        "/benchmarks/batch",
        json={"cases": [{"case_id": "batch-a"}], "artifact_root": str(tmp_path / "batch")},
    ).json()

    assert result["case_count"] == 1
    assert result["artifact_root"] == str(tmp_path / "batch")
