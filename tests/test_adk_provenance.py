from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.runtime.run_registry import get_run


def _provenance(invocation_id="invocation-lock", correlation_id="corr_" + "a" * 32):
    return {
        "provenance_schema_version": "1.0",
        "source": "adk-developer",
        "workflow_origin": "published",
        "workflow_id": "chainladder-basic",
        "workflow_digest": "a" * 64,
        "input_digest": "b" * 64,
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-lock",
        "adk_invocation_id": invocation_id,
        "correlation_id": correlation_id,
        "capability_class": "adk-developer",
    }


def _request(app, payload, key="provenance-idempotency-key"):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    grant = hmac.new(b"adk-secret", f"{key}:{fingerprint}".encode(), hashlib.sha256).hexdigest()

    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.post(
                "/adk/runs",
                headers={"Authorization": "Bearer adk-secret", "Idempotency-Key": key, "X-ADK-Confirmation": grant},
                json=payload,
            )
    return asyncio.run(call())


def _settings(tmp_path):
    return ApiSettings(
        registry_path=tmp_path / "registry.json", artifact_root=tmp_path / "operator",
        adk_artifact_root=tmp_path / "developer", review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret", adk_credential="adk-secret",
        operator_bootstrap_token="bootstrap", operator_origin="http://testserver",
    )


def test_adk_provenance_is_frozen_at_acceptance_in_registry_events_and_manifest(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    payload = {
        "workflow_id": "chainladder-validated", "case_id": "case-1",
        "inputs": {"sample_name": "RAA"}, "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1", "adk_invocation_id": "invocation-1",
    }
    response = _request(app, payload)
    assert response.status_code == 202
    entry = get_run(settings.registry_path, response.json()["run_id"])
    provenance = entry["provenance"]
    assert provenance["provenance_schema_version"] == "1.0"
    assert provenance["source"] == "adk-developer"
    assert provenance["workflow_origin"] == "published"
    assert provenance["workflow_id"] == "chainladder-validated"
    assert provenance["capability_class"] == "adk-developer"
    assert provenance["correlation_id"]
    assert "credential" not in json.dumps(provenance).lower()
    assert entry["status_history"][0]["provenance"] == provenance
    manifest = json.loads((settings.adk_artifact_root / entry["run_id"] / "run_manifest.json").read_text())
    for key, value in provenance.items():
        assert manifest[key] == value


def test_restart_persists_stale_state_and_legacy_runs_remain_readable(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    payload = {
        "workflow_id": "chainladder-basic", "case_id": "case-2", "inputs": {},
        "adk_app": "ai_actuary_developer", "adk_session_id": "session-1",
        "adk_invocation_id": "invocation-2",
    }
    response = _request(app, payload, key="restart-idempotency-key")
    run_id = response.json()["run_id"]
    create_app(settings=settings, background_task_runner=lambda fn, params: None)
    restarted = get_run(settings.registry_path, run_id)
    assert restarted["status"] == "failed"
    assert restarted["recovery_state"] == "stale"

    from reserving_workflow.storage.local import LocalRunStore
    LocalRunStore(settings.registry_path).create_run(
        task_id="legacy", case_id="legacy-case", run_id="legacy-run", status="completed"
    )
    assert get_run(settings.registry_path, "legacy-run")["run_id"] == "legacy-run"


def test_draft_and_caller_correlation_are_rejected_without_a_run(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    base = {
        "workflow_id": "chainladder-basic", "case_id": "case-3", "inputs": {},
        "adk_app": "ai_actuary_developer", "adk_session_id": "session-3",
        "adk_invocation_id": "invocation-3",
    }
    for index, extra in enumerate(({"draft_workflow_digest": "draft"}, {"correlation_id": "caller-value"})):
        payload = {**base, **extra, "adk_invocation_id": f"invocation-{index + 3}"}
        assert _request(app, payload, key=f"reject-{index}").status_code in {400, 422}
    assert not settings.registry_path.exists()


def test_tampered_new_adk_manifest_fails_closed_with_stable_code(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    payload = {
        "workflow_id": "chainladder-basic", "case_id": "case-4", "inputs": {},
        "adk_app": "ai_actuary_developer", "adk_session_id": "session-4",
        "adk_invocation_id": "invocation-4",
    }
    response = _request(app, payload, key="tamper-idempotency-key")
    run_id = response.json()["run_id"]
    manifest_path = settings.adk_artifact_root / run_id / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("correlation_id")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    async def read():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.get("/runs/" + run_id, headers={"Authorization": "Bearer adk-secret"})

    rejected = asyncio.run(read())
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "adk_provenance_invalid"

    async def list_read():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(
                "/runs", headers={"Authorization": "Bearer adk-secret"}
            )

    list_rejected = asyncio.run(list_read())
    assert list_rejected.status_code == 409
    assert list_rejected.json()["detail"]["code"] == "adk_provenance_invalid"


def test_tampered_out_of_scope_adk_objects_match_absent_before_manifest_read(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    payload = {
        "workflow_id": "chainladder-basic",
        "case_id": "out-of-scope-tamper",
        "inputs": {},
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "out-of-scope-session",
        "adk_invocation_id": "out-of-scope-invocation",
    }
    started = _request(app, payload, key="out-of-scope-tamper-key")
    run_id = started.json()["run_id"]
    manifest_path = settings.adk_artifact_root / run_id / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("correlation_id")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    async def operator_request(method, path):
        headers = {"Authorization": "Bearer operator-secret"}
        if method == "POST":
            headers["Origin"] = "http://testserver"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.request(method, path, headers=headers)

    surfaces = (
        ("GET", f"/runs/{run_id}", "/runs/absent-run"),
        ("GET", f"/runs/{run_id}/events", "/runs/absent-run/events"),
        ("GET", f"/runs/{run_id}/artifacts", "/runs/absent-run/artifacts"),
        (
            "GET",
            f"/runs/{run_id}/artifacts/run_manifest/projection",
            "/runs/absent-run/artifacts/run_manifest/projection",
        ),
        ("GET", f"/runs/{run_id}/review", "/runs/absent-run/review"),
        ("POST", f"/runs/{run_id}/report-export", "/runs/absent-run/report-export"),
        ("GET", f"/reviews/review-{run_id}", "/reviews/review-absent-run"),
    )
    for method, known_path, absent_path in surfaces:
        known = asyncio.run(operator_request(method, known_path))
        absent = asyncio.run(operator_request(method, absent_path))
        assert known.status_code == absent.status_code == 404, (
            known_path,
            known.text,
            absent.text,
        )
        assert known.json() == absent.json(), known_path


def test_adk_acceptance_and_status_update_share_one_registry_transaction(
    tmp_path, monkeypatch
):
    from reserving_workflow.runtime import run_registry
    from reserving_workflow.storage import local as local_storage
    from reserving_workflow.storage.local import LocalRunStore

    registry = tmp_path / "registry.json"
    store = LocalRunStore(registry)
    store.create_run(
        task_id="operator-task",
        case_id="operator-case",
        run_id="operator-run",
        status="queued",
    )
    original_read = local_storage._read_registry_payload
    read_started = threading.Event()
    release_read = threading.Event()

    def interleaved_read(path):
        payload = original_read(path)
        if threading.current_thread().name == "status-writer":
            read_started.set()
            assert release_read.wait(5)
        return payload

    monkeypatch.setattr(local_storage, "_read_registry_payload", interleaved_read)

    status_thread = threading.Thread(
        name="status-writer",
        target=lambda: store.update_run_status(
            run_id="operator-run",
            task_id="operator-task",
            case_id="operator-case",
            status="completed",
        ),
    )
    accept_thread = threading.Thread(
        name="accept-writer",
        target=lambda: run_registry.accept_adk_run(
            registry_path=registry,
            idempotency_key="opaque-idempotency-lock-key",
            request_fingerprint="c" * 64,
            confirmation_grant_digest="d" * 64,
            run_id="adk-run-lock",
            operation_id="op_lock",
            task_id="adk-task",
            case_id="adk-case",
            artifact_root=str(tmp_path / "adk-run-lock"),
            workflow_id="chainladder-basic",
            provenance=_provenance(),
        ),
    )
    status_thread.start()
    assert read_started.wait(5)
    accept_thread.start()
    time.sleep(0.1)
    release_read.set()
    status_thread.join(5)
    accept_thread.join(5)
    assert not status_thread.is_alive()
    assert not accept_thread.is_alive()

    payload = original_read(registry)
    assert {run["run_id"] for run in payload["runs"]} == {
        "operator-run",
        "adk-run-lock",
    }
    assert len(payload["adk_operations"]) == 1
    operator = next(run for run in payload["runs"] if run["run_id"] == "operator-run")
    assert [event["status"] for event in operator["status_history"]] == [
        "queued",
        "completed",
    ]


def test_registry_transaction_lock_preserves_concurrent_process_updates(tmp_path):
    registry = tmp_path / "registry.json"
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), environment.get("PYTHONPATH", "")]
    )
    script = """
import sys
from reserving_workflow.storage.local import LocalRunStore

registry, suffix = sys.argv[1:]
LocalRunStore(registry).create_run(
    task_id=f"process-task-{suffix}",
    case_id=f"process-case-{suffix}",
    run_id=f"process-run-{suffix}",
    status="queued",
)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(registry), str(index)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
        )
        for index in range(6)
    ]
    assert [process.wait(timeout=15) for process in processes] == [0] * 6
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert {run["run_id"] for run in payload["runs"]} == {
        f"process-run-{index}" for index in range(6)
    }


@pytest.mark.parametrize("reader", ("list", "get"))
def test_registry_read_returns_the_same_snapshot_that_passed_adk_audit(
    tmp_path, monkeypatch, reader
):
    from reserving_workflow.runtime import run_registry
    from reserving_workflow.storage import local as local_storage

    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"runs": []}', encoding="utf-8")
    audited_payload = {
        "runs": [
            {
                "run_id": "snapshot-run",
                "status": "completed",
                "summary": "audited snapshot",
                "updated_at": "2026-08-20T00:00:00+00:00",
            }
        ],
        "adk_operations": [],
    }
    unaudited_payload = {
        "runs": [
            {
                "run_id": "snapshot-run",
                "status": "completed",
                "summary": "unaudited replacement",
                "source": "adk-developer",
                "provenance": {"source": "adk-developer"},
                "updated_at": "2026-08-20T00:00:01+00:00",
            }
        ],
        "adk_operations": [],
    }
    monkeypatch.setattr(
        run_registry,
        "_read_registry_payload",
        lambda path: audited_payload,
    )
    monkeypatch.setattr(
        local_storage,
        "_read_registry_payload",
        lambda path: unaudited_payload,
    )

    result = (
        run_registry.list_runs(registry_path)[0]
        if reader == "list"
        else run_registry.get_run(registry_path, "snapshot-run")
    )

    assert result["summary"] == "audited snapshot"
    assert result.get("source") is None


@pytest.mark.parametrize("tampered_source", (None, "operator-console"))
def test_operation_bound_adk_run_rejects_missing_or_mismatched_source_on_all_reads(
    tmp_path, tampered_source
):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    payload = {
        "workflow_id": "chainladder-basic",
        "case_id": "source-case",
        "inputs": {},
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "source-session",
        "adk_invocation_id": "source-invocation",
    }
    started = _request(app, payload, key="source-idempotency-key")
    run_id = started.json()["run_id"]
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    run = registry["runs"][0]
    if tampered_source is None:
        run.pop("source")
    else:
        run["source"] = tampered_source
    settings.registry_path.write_text(json.dumps(registry), encoding="utf-8")

    async def read(path):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(path, headers={"Authorization": "Bearer adk-secret"})

    for path in (
        f"/runs/{run_id}",
        "/runs",
        f"/runs/{run_id}/events",
        f"/runs/{run_id}/artifacts",
        f"/runs/{run_id}/review",
        "/reviews",
    ):
        response = asyncio.run(read(path))
        assert response.status_code == 409, (path, response.text)
        assert response.json()["detail"]["code"] == "adk_provenance_invalid"


@pytest.mark.parametrize("duplicate_field", ("correlation_id", "adk_invocation_id"))
def test_registry_wide_adk_cardinality_conflicts_fail_on_read_and_restart(
    tmp_path, duplicate_field
):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    base = {
        "workflow_id": "chainladder-basic",
        "case_id": "cardinality-case",
        "inputs": {},
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "cardinality-session",
    }
    first = _request(
        app,
        {**base, "adk_invocation_id": "cardinality-invocation-1"},
        key="cardinality-key-1",
    ).json()
    second = _request(
        app,
        {**base, "adk_invocation_id": "cardinality-invocation-2"},
        key="cardinality-key-2",
    ).json()
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    runs_by_id = {run["run_id"]: run for run in registry["runs"]}
    first_run = runs_by_id[first["run_id"]]
    second_run = runs_by_id[second["run_id"]]
    duplicate_value = first_run["provenance"][duplicate_field]
    second_run["provenance"][duplicate_field] = duplicate_value
    if duplicate_field == "correlation_id":
        second_operation = next(
            operation
            for operation in registry["adk_operations"]
            if operation["run_id"] == second["run_id"]
        )
        second_operation["correlation_id"] = duplicate_value
    settings.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    second_manifest_path = (
        settings.adk_artifact_root / second["run_id"] / "run_manifest.json"
    )
    second_manifest = json.loads(second_manifest_path.read_text(encoding="utf-8"))
    second_manifest[duplicate_field] = duplicate_value
    second_manifest_path.write_text(json.dumps(second_manifest), encoding="utf-8")

    async def list_read():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(
                "/runs", headers={"Authorization": "Bearer adk-secret"}
            )

    rejected = asyncio.run(list_read())
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "adk_registry_cardinality_conflict"
    with pytest.raises(ValueError, match="adk_registry_cardinality_conflict"):
        create_app(settings=settings, background_task_runner=lambda fn, params: None)
