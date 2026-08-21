from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import reserving_workflow.api.app as app_module
from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.calculators import ChainladderAdapter
from reserving_workflow.runtime.run_registry import get_run, list_runs
from reserving_workflow.schemas import ReservingCaseInput


ADK_SECRET = "adk-secret-that-is-independent"
OPERATOR_SECRET = "operator-secret-that-is-independent"


class _FakeWorkerTask:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeTaskContracts:
    WorkerTask = _FakeWorkerTask


class _CompletedRunner:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        del user_prompt
        artifact_dir = Path(task.inputs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        deterministic_result = artifact_dir / "deterministic_result.json"
        deterministic_result.write_text(
            json.dumps(
                {
                    "case_id": task.case_ref,
                    "run_id": task.run_id,
                    "method": "chainladder",
                    "reserve_summary": {"ibnr": 12.5, "ultimate": 42.0},
                }
            ),
            encoding="utf-8",
        )
        step_manifest = artifact_dir / "run_manifest.json"
        step_manifest.write_text(
            json.dumps(
                {
                    "case_id": task.case_ref,
                    "run_id": task.run_id,
                    "artifact_paths": {
                        "run_manifest": "run_manifest.json",
                        "deterministic_result": "deterministic_result.json",
                    },
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
                "summary": "completed",
                "artifact_paths": {"run_manifest": str(step_manifest)},
                "metrics": {},
                "review_reasons": [],
                "errors": [],
                "worker_metadata": {"adapter": "fake"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "worker_status": "completed",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 12.5},
                "review_reasons": [],
                "artifact_manifest_path": str(step_manifest),
                "narrative_summary": "completed",
            },
        }


class _ReplayableRunner:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        del user_prompt
        artifact_dir = Path(task.inputs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        case_payload = dict(task.inputs["case_payload"])
        case_input = ReservingCaseInput.model_validate(case_payload)
        deterministic_result = ChainladderAdapter().calculate(case_input).model_dump(
            mode="json"
        )
        (artifact_dir / "case_input.json").write_text(
            json.dumps(case_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        (artifact_dir / "deterministic_result.json").write_text(
            json.dumps(deterministic_result, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = {
            "case_id": task.case_ref,
            "run_id": task.run_id,
            "artifact_paths": {
                "case_input": "case_input.json",
                "deterministic_result": "deterministic_result.json",
                "run_manifest": "run_manifest.json",
            },
        }
        manifest_path = artifact_dir / "run_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "route": {"mode": "governed"},
            "trace": {"workflow_name": "test-workflow"},
            "worker_result": {
                "status": "completed",
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "summary": "completed",
                "artifact_paths": {"run_manifest": str(manifest_path)},
                "metrics": {},
                "review_reasons": [],
                "errors": [],
                "worker_metadata": {"adapter": "fake"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "worker_status": "completed",
                "deterministic_method": deterministic_result["method"],
                "cited_values": dict(deterministic_result["reserve_summary"]),
                "review_reasons": [],
                "artifact_manifest_path": str(manifest_path),
                "narrative_summary": "completed",
            },
        }


def _request(app, method: str, path: str, **kwargs):
    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(call())


def _settings(tmp_path):
    return ApiSettings(
        registry_path=tmp_path / "business" / "registry.json",
        artifact_root=tmp_path / "business" / "artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "business" / "reviews",
        evaluation_state_root=tmp_path / "adk-evaluations",
        operator_credential=OPERATOR_SECRET,
        adk_credential=ADK_SECRET,
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )


def _app(tmp_path, scheduler=lambda fn, params: None):
    settings = _settings(tmp_path)
    return (
        create_app(
            settings=settings,
            runner_module=_CompletedRunner,
            task_contracts_module=_FakeTaskContracts,
            background_task_runner=scheduler,
        ),
        settings,
    )


def _replayable_app(tmp_path):
    settings = _settings(tmp_path)
    return (
        create_app(
            settings=settings,
            runner_module=_ReplayableRunner,
            task_contracts_module=_FakeTaskContracts,
            background_task_runner=lambda fn, params: fn(params),
        ),
        settings,
    )


def _payload(**changes: Any) -> dict[str, Any]:
    payload = {
        "workflow_id": "chainladder-basic",
        "case_id": "developer-case",
        "inputs": {"sample_name": "RAA", "method_variant": "chainladder"},
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "invocation-1",
    }
    payload.update(changes)
    return payload


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _start_headers(payload: dict[str, Any], key: str = "opaque-start-key"):
    grant = hmac.new(
        ADK_SECRET.encode("utf-8"),
        f"{key}:{_fingerprint(payload)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"Bearer {ADK_SECRET}",
        "Idempotency-Key": key,
        "X-ADK-Confirmation": grant,
    }


def _debug_headers(
    *,
    action: str,
    object_id: str,
    payload: dict[str, Any],
    key: str = "opaque-debug-key",
):
    from reserving_workflow.runtime.adk_execution import adk_debug_request_fingerprint

    fingerprint = adk_debug_request_fingerprint(
        action=action,
        object_id=object_id,
        request=payload,
    )
    grant = hmac.new(
        ADK_SECRET.encode("utf-8"),
        f"{key}:{fingerprint}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"Bearer {ADK_SECRET}",
        "Origin": "http://testserver",
        "Idempotency-Key": key,
        "X-ADK-Confirmation": grant,
    }


def _start_adk_run(app, **payload_changes):
    payload = _payload(**payload_changes)
    response = _request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, f"start-{payload['adk_invocation_id']}"),
        json=payload,
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_adk_start_persists_bidirectional_trace_and_run_correlation(tmp_path):
    app, settings = _app(tmp_path)

    started = _start_adk_run(app)
    run_id = started["run_id"]
    entry = get_run(settings.registry_path, run_id)
    provenance = entry["provenance"]

    assert provenance["run_id"] == run_id
    assert provenance["correlation_id"] == started["correlation_id"]
    assert provenance["workflow_id"] == "chainladder-basic"
    assert provenance["adk_app"] == "ai_actuary_developer"
    assert provenance["adk_session_id"] == "session-1"
    assert provenance["adk_invocation_id"] == "invocation-1"
    assert len(provenance["workflow_digest"]) == 64
    assert provenance["trace_id"] == (
        "ai_actuary_developer:session-1:invocation-1"
    )

    manifest = json.loads(
        (settings.adk_artifact_root / run_id / "run_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    for key in (
        "run_id",
        "correlation_id",
        "workflow_id",
        "adk_app",
        "adk_session_id",
        "adk_invocation_id",
        "workflow_digest",
        "trace_id",
    ):
        assert manifest[key] == provenance[key]

    visible = _request(
        app,
        "GET",
        f"/runs/{run_id}",
        headers={"Authorization": f"Bearer {ADK_SECRET}"},
    )
    assert visible.status_code == 200
    visible_run = visible.json()["run"]
    assert visible_run["run_id"] == run_id
    assert visible_run["provenance"] == provenance
    assert str(tmp_path) not in json.dumps(visible_run)

    same_session_second = _start_adk_run(
        app,
        adk_invocation_id="invocation-2",
        case_id="developer-case-2",
    )
    assert same_session_second["run_id"] != run_id
    assert len(list_runs(settings.registry_path)) == 2


def test_rerun_run_creates_child_with_new_correlation_and_frozen_lineage(tmp_path):
    app, settings = _app(tmp_path)
    source = _start_adk_run(app)
    source_entry = get_run(settings.registry_path, source["run_id"])
    source_provenance = source_entry["provenance"]

    payload = {
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "rerun-invocation-1",
    }
    first = _request(
        app,
        "POST",
        f"/adk/runs/{source['run_id']}/rerun",
        headers=_debug_headers(
            action="rerun_run",
            object_id=source["run_id"],
            payload=payload,
            key="rerun-idempotency-key",
        ),
        json=payload,
    )
    replay = _request(
        app,
        "POST",
        f"/adk/runs/{source['run_id']}/rerun",
        headers=_debug_headers(
            action="rerun_run",
            object_id=source["run_id"],
            payload=payload,
            key="rerun-idempotency-key",
        ),
        json=payload,
    )

    assert first.status_code == replay.status_code == 202
    child = first.json()
    assert replay.json()["run_id"] == child["run_id"]
    assert child["run_id"] != source["run_id"]
    assert child["correlation_id"] != source["correlation_id"]
    assert child["parent_run_id"] == source["run_id"]
    assert child["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True

    child_entry = get_run(settings.registry_path, child["run_id"])
    child_provenance = child_entry["provenance"]
    assert child_provenance["run_id"] == child["run_id"]
    assert child_provenance["parent_run_id"] == source["run_id"]
    assert child_provenance["source_run_id"] == source["run_id"]
    assert child_provenance["lineage"]["parent_run_id"] == source["run_id"]
    assert child_provenance["lineage"]["root_run_id"] == source["run_id"]
    assert child_provenance["workflow_id"] == source_provenance["workflow_id"]
    assert child_provenance["workflow_digest"] == source_provenance["workflow_digest"]
    assert child_entry["operator_params"]["workflow_inputs"] == (
        source_entry["operator_params"]["workflow_inputs"]
    )


def test_rerun_operation_id_is_queryable_by_status_and_wait(tmp_path):
    app, settings = _app(tmp_path)
    source = _start_adk_run(app)
    payload = {
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "rerun-operation-status",
    }

    rerun = _request(
        app,
        "POST",
        f"/adk/runs/{source['run_id']}/rerun",
        headers=_debug_headers(
            action="rerun_run",
            object_id=source["run_id"],
            payload=payload,
            key="rerun-operation-status-key",
        ),
        json=payload,
    )

    assert rerun.status_code == 202, rerun.text
    operation_id = rerun.json()["operation_id"]
    child_run_id = rerun.json()["run_id"]
    status = _request(
        app,
        "GET",
        f"/adk/operations/{operation_id}",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
    )
    waited = _request(
        app,
        "POST",
        f"/adk/operations/{operation_id}/wait",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={"timeout_seconds": 0.1},
    )

    assert status.status_code == waited.status_code == 200
    for response in (status.json()["operation"], waited.json()["operation"]):
        assert response["operation_id"] == operation_id
        assert response["action"] == "rerun_run"
        assert response["status"] == get_run(settings.registry_path, child_run_id)["status"]
        assert response["run"]["run_id"] == child_run_id
        assert response["run"]["parent_run_id"] == source["run_id"]
        assert str(settings.adk_artifact_root) not in json.dumps(response)


def test_rerun_rejects_workflow_digest_mismatch_with_zero_side_effects(tmp_path):
    app, settings = _app(tmp_path)
    source = _start_adk_run(app)
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    source_entry = registry["runs"][0]
    source_entry["provenance"]["workflow_digest"] = "f" * 64
    settings.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    manifest_path = settings.adk_artifact_root / source["run_id"] / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow_digest"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before_registry = settings.registry_path.read_text(encoding="utf-8")
    before_dirs = sorted(path.name for path in settings.adk_artifact_root.iterdir())
    payload = {
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "rerun-digest-mismatch",
    }

    rejected = _request(
        app,
        "POST",
        f"/adk/runs/{source['run_id']}/rerun",
        headers=_debug_headers(
            action="rerun_run",
            object_id=source["run_id"],
            payload=payload,
            key="digest-mismatch-key",
        ),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "workflow_version_unavailable"
    assert settings.registry_path.read_text(encoding="utf-8") == before_registry
    assert sorted(path.name for path in settings.adk_artifact_root.iterdir()) == before_dirs


@pytest.mark.parametrize("mutation", ("missing", "invalid"))
def test_rerun_rejects_unavailable_frozen_inputs_with_zero_side_effects(tmp_path, mutation):
    app, settings = _app(tmp_path)
    source = _start_adk_run(app)
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    source_entry = next(run for run in registry["runs"] if run["run_id"] == source["run_id"])
    source_operator_params = source_entry["operator_params"]
    if mutation == "missing":
        source_operator_params.pop("workflow_inputs", None)
    else:
        source_operator_params["workflow_inputs"] = ["not", "a", "frozen-input-object"]
    settings.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    before_registry = settings.registry_path.read_text(encoding="utf-8")
    before_dirs = sorted(path.name for path in settings.adk_artifact_root.iterdir())
    payload = {
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": f"rerun-frozen-inputs-{mutation}",
    }

    rejected = _request(
        app,
        "POST",
        f"/adk/runs/{source['run_id']}/rerun",
        headers=_debug_headers(
            action="rerun_run",
            object_id=source["run_id"],
            payload=payload,
            key=f"frozen-inputs-{mutation}-key",
        ),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "workflow_inputs_unavailable"
    assert settings.registry_path.read_text(encoding="utf-8") == before_registry
    assert sorted(path.name for path in settings.adk_artifact_root.iterdir()) == before_dirs


def test_action_scoped_idempotency_allows_same_key_for_start_and_rerun(tmp_path):
    app, settings = _app(tmp_path)
    start_payload = _payload()
    started = _request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(start_payload, key="shared-action-key"),
        json=start_payload,
    )
    assert started.status_code == 202
    rerun_payload = {
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "shared-key-rerun",
    }

    rerun = _request(
        app,
        "POST",
        f"/adk/runs/{started.json()['run_id']}/rerun",
        headers=_debug_headers(
            action="rerun_run",
            object_id=started.json()["run_id"],
            payload=rerun_payload,
            key="shared-action-key",
        ),
        json=rerun_payload,
    )

    assert rerun.status_code == 202, rerun.text
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    assert {operation["action"] for operation in registry["adk_operations"]} == {
        "start_workflow_run",
        "rerun_run",
    }


def test_replay_and_repeatability_fail_closed_on_incomplete_or_incompatible_runs(tmp_path):
    app, settings = _app(tmp_path)
    first = _start_adk_run(app)
    replay = _request(
        app,
        "POST",
        f"/adk/runs/{first['run_id']}/replay",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "not_replayable"

    second = _start_adk_run(
        app,
        adk_invocation_id="repeatability-incompatible",
        case_id="developer-case",
    )
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    second_entry = next(run for run in registry["runs"] if run["run_id"] == second["run_id"])
    second_entry["provenance"]["workflow_digest"] = "e" * 64
    settings.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    second_manifest_path = settings.adk_artifact_root / second["run_id"] / "run_manifest.json"
    second_manifest = json.loads(second_manifest_path.read_text(encoding="utf-8"))
    second_manifest["workflow_digest"] = "e" * 64
    second_manifest_path.write_text(json.dumps(second_manifest), encoding="utf-8")

    repeatability = _request(
        app,
        "POST",
        "/adk/repeatability",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={"run_ids": [first["run_id"], second["run_id"]]},
    )
    assert repeatability.status_code == 409
    assert repeatability.json()["detail"]["code"] == "repeatability_incompatible"


def test_replay_and_repeatability_rerun_deterministic_step_evidence(tmp_path):
    app, settings = _replayable_app(tmp_path)
    first = _start_adk_run(app)
    second = _start_adk_run(
        app,
        adk_invocation_id="replay-compatible-second",
    )

    replay = _request(
        app,
        "POST",
        f"/adk/runs/{first['run_id']}/replay",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={},
    )

    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()["replay"]
    assert replay_payload["replay_status"] == "available"
    assert replay_payload["replay_match"] is True
    assert replay_payload["saved_result_digest"] == replay_payload["replayed_result_digest"]
    assert replay_payload["evidence"]["case_input"] is True
    assert replay_payload["evidence"]["validated_input"] is True
    assert replay_payload["evidence"]["deterministic_result"] is True
    assert str(settings.adk_artifact_root) not in json.dumps(replay_payload)

    repeatability = _request(
        app,
        "POST",
        "/adk/repeatability",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={"run_ids": [first["run_id"], second["run_id"]]},
    )
    assert repeatability.status_code == 200, repeatability.text
    repeatability_payload = repeatability.json()["repeatability"]
    assert repeatability_payload["repeatability_status"] == "repeatable"
    assert repeatability_payload["result_digest_match"] is True


def test_replay_rejects_tampered_step_evidence_identity_and_digests(tmp_path):
    app, settings = _replayable_app(tmp_path)
    first = _start_adk_run(app)
    artifact_root = settings.adk_artifact_root / first["run_id"]

    case_input_path = artifact_root / "chainladder" / "case_input.json"
    case_input = json.loads(case_input_path.read_text(encoding="utf-8"))
    case_input["case_id"] = "swapped-case"
    case_input_path.write_text(json.dumps(case_input), encoding="utf-8")
    replay = _request(
        app,
        "POST",
        f"/adk/runs/{first['run_id']}/replay",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "not_replayable"

    app, settings = _replayable_app(tmp_path / "result-tamper")
    second = _start_adk_run(app)
    result_path = settings.adk_artifact_root / second["run_id"] / "chainladder" / "deterministic_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["reserve_summary"]["ibnr"] = result["reserve_summary"]["ibnr"] + 1
    result_path.write_text(json.dumps(result), encoding="utf-8")
    replay = _request(
        app,
        "POST",
        f"/adk/runs/{second['run_id']}/replay",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "not_replayable"

    app, settings = _replayable_app(tmp_path / "digest-tamper")
    third = _start_adk_run(app)
    step_manifest_path = settings.adk_artifact_root / third["run_id"] / "chainladder" / "run_manifest.json"
    step_manifest = json.loads(step_manifest_path.read_text(encoding="utf-8"))
    step_manifest.setdefault("artifact_digests", {})["case_input"] = "0" * 64
    step_manifest_path.write_text(json.dumps(step_manifest), encoding="utf-8")
    replay = _request(
        app,
        "POST",
        f"/adk/runs/{third['run_id']}/replay",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "not_replayable"


def test_export_run_report_requires_confirmation_and_is_idempotent(tmp_path):
    app, settings = _app(tmp_path)
    started = _start_adk_run(app)
    payload = {
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "report-invocation",
    }
    rejected = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/report-export",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json=payload,
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "idempotency_key_required"

    bad_request = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/report-export",
        headers=_debug_headers(
            action="export_run_report",
            object_id=started["run_id"],
            payload={**payload, "output_dir": str(tmp_path / "reports")},
            key="bad-report-key",
        ),
        json={**payload, "output_dir": str(tmp_path / "reports")},
    )
    assert bad_request.status_code == 400
    assert not (settings.adk_artifact_root / started["run_id"] / "run_report.json").exists()

    first = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/report-export",
        headers=_debug_headers(
            action="export_run_report",
            object_id=started["run_id"],
            payload=payload,
            key="report-idempotency-key",
        ),
        json=payload,
    )
    second = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/report-export",
        headers=_debug_headers(
            action="export_run_report",
            object_id=started["run_id"],
            payload=payload,
            key="report-idempotency-key",
        ),
        json=payload,
    )

    assert first.status_code == second.status_code == 202
    assert second.json()["operation_id"] == first.json()["operation_id"]
    artifact_ids = {
        artifact["artifact_id"] for artifact in first.json()["report"]["artifacts"]
    }
    assert {
        "operator_handoff",
        "reserve_summary_json",
        "reserve_summary_markdown",
    } <= artifact_ids
    serialized = json.dumps(first.json())
    assert str(tmp_path) not in serialized
    manifest = json.loads(
        (settings.adk_artifact_root / started["run_id"] / "run_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["artifact_paths"]["operator_handoff"] == "operator_handoff.md"
    assert manifest["artifact_paths"]["reserve_summary_json"] == "reserve_summary.json"
    assert manifest["artifact_paths"]["reserve_summary_markdown"] == "reserve_summary.md"
    assert (settings.adk_artifact_root / started["run_id"] / "operator_handoff.md").is_file()
    assert (settings.adk_artifact_root / started["run_id"] / "reserve_summary.json").is_file()
    assert (settings.adk_artifact_root / started["run_id"] / "reserve_summary.md").is_file()


def test_export_run_report_failure_finalizes_operation(tmp_path, monkeypatch):
    app, settings = _app(tmp_path)
    started = _start_adk_run(app)
    payload = {
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "report-failure",
    }

    def fail_export(**_kwargs):
        raise RuntimeError("forced report export failure with C:/secret/path")

    monkeypatch.setattr(app_module, "export_run_report", fail_export)
    failed = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/report-export",
        headers=_debug_headers(
            action="export_run_report",
            object_id=started["run_id"],
            payload=payload,
            key="report-failure-key",
        ),
        json=payload,
    )

    assert failed.status_code == 202, failed.text
    assert failed.json()["status"] == "failed"
    assert failed.json()["report"]["failure_class"] == "report_export_failed"
    assert "secret" not in json.dumps(failed.json()).lower()
    operation_id = failed.json()["operation_id"]

    status = _request(
        app,
        "GET",
        f"/adk/operations/{operation_id}",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
    )
    retry = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/report-export",
        headers=_debug_headers(
            action="export_run_report",
            object_id=started["run_id"],
            payload=payload,
            key="report-failure-key",
        ),
        json=payload,
    )

    assert status.status_code == 200
    assert status.json()["operation"]["status"] == "failed"
    assert retry.status_code == 202
    assert retry.json()["idempotent_replay"] is True
    assert retry.json()["status"] == "failed"


def test_adk_debug_surfaces_are_id_only_and_legacy_path_routes_remain_denied(tmp_path):
    app, settings = _app(tmp_path)
    started = _start_adk_run(app)
    before = (
        settings.registry_path.read_text(encoding="utf-8")
        if settings.registry_path.exists()
        else ""
    )

    rejected = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/replay",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={"manifest_path": str(tmp_path / "secret" / "run_manifest.json")},
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "request_invalid"
    assert str(tmp_path) not in rejected.text
    assert settings.registry_path.read_text(encoding="utf-8") == before

    legacy = _request(
        app,
        "POST",
        "/replay",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={"manifest_path": "./tmp/run_manifest.json"},
    )
    assert legacy.status_code == 403

    report = _request(
        app,
        "POST",
        f"/adk/runs/{started['run_id']}/report-export",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={},
    )
    assert report.status_code == 400
    assert report.json()["detail"]["code"] == "request_invalid"
