from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx

import reserving_workflow.api.app as app_module
import reserving_workflow.evaluation.adk_lanes as adk_lanes
from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.evaluation.adk_lanes import (
    run_offline_evaluation_lane,
    run_real_model_evaluation_lane,
)


ADK_SECRET = "adk-secret-that-is-independent"
OPERATOR_SECRET = "operator-secret-that-is-independent"


def _request(app, method: str, path: str, raise_app_exceptions: bool = True, **kwargs):
    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=raise_app_exceptions,
            ),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(call())


def _settings(tmp_path):
    return ApiSettings(
        registry_path=tmp_path / "business" / "registry.json",
        artifact_root=tmp_path / "business" / "artifacts",
        review_store_dir=tmp_path / "business" / "reviews",
        adk_artifact_root=tmp_path / "adk-artifacts",
        evaluation_state_root=tmp_path / "eval-state",
        operator_credential=OPERATOR_SECRET,
        adk_credential=ADK_SECRET,
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )


def _snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _debug_headers(
    *,
    action: str,
    object_id: str,
    payload: dict[str, Any],
    key: str = "opaque-eval-key",
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


def test_offline_evaluation_lane_writes_isolated_path_free_evidence(tmp_path):
    business_root = tmp_path / "business"
    before = _snapshot(business_root)

    result = run_offline_evaluation_lane(
        case_pack_id="deterministic-v1",
        state_root=tmp_path / "eval-state",
        business_roots=[business_root],
    )

    assert result["ok"] is True
    assert result["lane"] == "offline"
    assert result["status"] == "completed"
    assert result["case_pack_id"] == "deterministic-v1"
    assert result["case_count"] > 0
    assert result["business_storage_changed"] is False
    assert str(tmp_path) not in json.dumps(result)
    assert _snapshot(business_root) == before

    evidence_files = list((tmp_path / "eval-state").glob("*/evidence.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["lane"] == "offline"
    assert evidence["case_count"] == result["case_count"]


def test_real_model_lane_records_skipped_evidence_without_credentials(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    business_root = tmp_path / "business"
    before = _snapshot(business_root)

    result = run_real_model_evaluation_lane(
        case_pack_id="deterministic-v1",
        state_root=tmp_path / "real-model-eval",
        business_roots=[business_root],
    )

    assert result["ok"] is True
    assert result["lane"] == "real_model"
    assert result["status"] == "skipped"
    assert result["not_run_reason"] == "credentials_missing"
    assert result["business_storage_changed"] is False
    assert str(tmp_path) not in json.dumps(result)
    assert _snapshot(business_root) == before

    evidence_files = list((tmp_path / "real-model-eval").glob("*/evidence.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "skipped"
    assert evidence["not_run_reason"] == "credentials_missing"


def test_bounded_benchmark_api_uses_case_pack_ids_and_separate_eval_state(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)

    rejected = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={
            "case_pack_id": "deterministic-v1",
            "artifact_root": str(tmp_path / "business" / "artifacts"),
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "request_invalid"
    assert not settings.evaluation_state_root.exists()

    too_large = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        headers={"Authorization": f"Bearer {ADK_SECRET}", "Origin": "http://testserver"},
        json={"case_pack_id": "deterministic-v1", "lane": "offline", "case_limit": 99},
    )
    assert too_large.status_code == 400
    assert too_large.json()["detail"]["code"] == "request_invalid"
    assert not settings.evaluation_state_root.exists()

    request_payload = {
        "case_pack_id": "deterministic-v1",
        "lane": "offline",
        "case_limit": 1,
    }
    accepted = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="deterministic-v1",
            payload=request_payload,
            key="benchmark-idempotency-key",
        ),
        json=request_payload,
    )
    retry = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="deterministic-v1",
            payload=request_payload,
            key="benchmark-idempotency-key",
        ),
        json=request_payload,
    )

    assert accepted.status_code == retry.status_code == 202
    assert retry.json()["operation_id"] == accepted.json()["operation_id"]
    assert retry.json()["idempotent_replay"] is True
    operation_id = accepted.json()["operation_id"]
    payload = accepted.json()["benchmark"]
    assert payload["lane"] == "offline"
    assert payload["case_pack_id"] == "deterministic-v1"
    assert payload["case_count"] == 1
    assert payload["business_storage_changed"] is False
    serialized = json.dumps(payload)
    assert str(tmp_path) not in serialized
    assert "registry.json" not in serialized
    assert settings.evaluation_state_root.exists()
    assert (settings.evaluation_state_root / operation_id / "evidence.json").is_file()
    assert len(list(settings.evaluation_state_root.glob("*/evidence.json"))) == 1
    assert not settings.registry_path.exists()
    assert not settings.artifact_root.exists()
    assert not settings.review_store_dir.exists()

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
    assert status.json()["operation"]["operation_id"] == operation_id
    assert waited.json()["operation"]["status"] == "completed"
    assert str(tmp_path) not in json.dumps(status.json())


def test_bounded_benchmark_rejects_quota_expansion_before_operation_storage(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    request_payload = {
        "case_pack_id": "deterministic-v1",
        "lane": "offline",
        "input_byte_limit": 10_000_000,
        "wall_time_seconds": 999,
        "output_byte_limit": 10_000_000,
        "temp_storage_bytes": 10_000_000,
    }

    rejected = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="deterministic-v1",
            payload=request_payload,
            key="quota-idempotency-key",
        ),
        json=request_payload,
    )

    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "benchmark_quota_exceeded"
    assert not settings.evaluation_state_root.exists()


def test_bounded_benchmark_rejects_unknown_case_pack_before_operation_storage(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    request_payload = {
        "case_pack_id": "missing-v1",
        "lane": "offline",
    }

    rejected = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        raise_app_exceptions=False,
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="missing-v1",
            payload=request_payload,
            key="missing-case-pack-key",
        ),
        json=request_payload,
    )

    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "case_pack_invalid"
    assert not settings.evaluation_state_root.exists()
    assert not settings.registry_path.exists()
    assert not settings.artifact_root.exists()
    assert not settings.review_store_dir.exists()


def test_bounded_benchmark_post_acceptance_failure_finalizes_operation(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    request_payload = {
        "case_pack_id": "deterministic-v1",
        "lane": "offline",
        "case_limit": 1,
    }

    def fail_lane(**_kwargs):
        raise RuntimeError("forced benchmark failure with C:/secret/path")

    monkeypatch.setattr(app_module, "run_offline_evaluation_lane", fail_lane)
    failed = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        raise_app_exceptions=False,
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="deterministic-v1",
            payload=request_payload,
            key="benchmark-failure-key",
        ),
        json=request_payload,
    )

    assert failed.status_code == 202
    assert failed.json()["status"] == "failed"
    assert failed.json()["benchmark"]["failure_class"] == "benchmark_execution_failed"
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
        "/adk/benchmarks/bounded",
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="deterministic-v1",
            payload=request_payload,
            key="benchmark-failure-key",
        ),
        json=request_payload,
    )

    assert status.status_code == 200
    assert status.json()["operation"]["status"] == "failed"
    assert retry.status_code == 202
    assert retry.json()["idempotent_replay"] is True
    assert retry.json()["status"] == "failed"


def test_offline_evaluation_lane_runs_cases_and_reports_partial_failures(tmp_path):
    business_root = tmp_path / "business"
    case_pack = {
        "case_pack_id": "custom-pack",
        "cases": [
            {
                "case_id": "valid-case",
                "case_payload": {
                    "case_id": "valid-case",
                    "metadata": {"chainladder_sample": "RAA"},
                    "run_config": {"method": "chainladder"},
                },
            },
            {
                "case_id": "invalid-case",
                "case_payload": {
                    "case_id": "invalid-case",
                    "metadata": {"chainladder_sample": "not-a-sample"},
                    "run_config": {"method": "chainladder"},
                },
            },
        ],
    }

    result = run_offline_evaluation_lane(
        case_pack_id="custom-pack",
        case_pack=case_pack,
        state_root=tmp_path / "eval-state",
        business_roots=[business_root],
        budget={"case_limit": 2},
    )

    assert result["status"] == "partial"
    assert result["case_count"] == 2
    assert result["completed_count"] == 1
    assert result["failed_count"] == 1
    assert [case["status"] for case in result["cases"]] == ["completed", "failed"]
    assert result["cases"][1]["failure_class"] == "case_evaluation_failed"
    assert result["business_storage_changed"] is False
    assert str(tmp_path) not in json.dumps(result)


def test_bounded_benchmark_enforces_tightened_runtime_limits(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)

    concurrency_payload = {
        "case_pack_id": "deterministic-v1",
        "lane": "offline",
        "concurrency": 2,
    }
    rejected = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="deterministic-v1",
            payload=concurrency_payload,
            key="concurrency-limit-key",
        ),
        json=concurrency_payload,
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "benchmark_quota_exceeded"

    expectations = [
        ({"input_byte_limit": 1}, "failed", "input_limit_exceeded"),
        ({"total_byte_limit": 1}, "failed", "total_limit_exceeded"),
        ({"output_byte_limit": 1}, "failed", "output_limit_exceeded"),
        ({"temp_storage_bytes": 1}, "failed", "temp_storage_exceeded"),
        ({"wall_time_seconds": 0.000001}, "timeout", "wall_time_exceeded"),
    ]
    for index, (limits, status, failure_class) in enumerate(expectations):
        payload = {
            "case_pack_id": "deterministic-v1",
            "lane": "offline",
            "case_limit": 1,
            **limits,
        }
        response = _request(
            app,
            "POST",
            "/adk/benchmarks/bounded",
            headers=_debug_headers(
                action="run_bounded_benchmark",
                object_id="deterministic-v1",
                payload=payload,
                key=f"runtime-limit-key-{index}",
            ),
            json=payload,
        )
        assert response.status_code == 202, response.text
        benchmark = response.json()["benchmark"]
        assert benchmark["status"] == status
        assert benchmark["cases"][0]["failure_class"] == failure_class
        assert benchmark["business_storage_changed"] is False
        assert str(tmp_path) not in json.dumps(benchmark)


def test_real_model_lane_runs_configured_evaluator_with_budgeted_evidence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_ACTUARY_REAL_MODEL_EVAL_MODE", "local_stub")
    business_root = tmp_path / "business"
    before = _snapshot(business_root)

    result = run_real_model_evaluation_lane(
        case_pack_id="deterministic-v1",
        state_root=tmp_path / "real-model-configured",
        business_roots=[business_root],
        case_limit=1,
    )

    assert result["ok"] is True
    assert result["lane"] == "real_model"
    assert result["status"] == "completed"
    assert result["case_count"] == 1
    assert result["business_storage_changed"] is False
    assert result["config_summary"]["mode"] == "local_stub"
    assert result["budget"]["case_limit"] == 1
    assert len(result["code_sha"]) == 40
    assert str(tmp_path) not in json.dumps(result)
    assert _snapshot(business_root) == before

    evidence_files = list((tmp_path / "real-model-configured").glob("*/evidence.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "completed"
    assert evidence["config_summary"]["mode"] == "local_stub"


def test_real_model_cli_fails_with_credentials_and_unconfigured_evaluator(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AI_ACTUARY_REAL_MODEL_EVAL_MODE", raising=False)

    exit_code = adk_lanes.main(
        [
            "--lane",
            "real-model",
            "--state-root",
            str(tmp_path / "real-model-cli"),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["lane"] == "real_model"
    assert payload["status"] == "failed"
    assert payload["ok"] is False
    assert payload["failure_class"] == "evaluator_not_configured"
    evidence_files = list((tmp_path / "real-model-cli").glob("*/evidence.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["ok"] is False


def test_benchmark_concurrency_and_retention_evidence_are_truthful(tmp_path):
    settings = _settings(tmp_path)
    settings.adk_benchmark_concurrency = 4
    app = create_app(settings=settings, background_task_runner=lambda fn, params: None)
    concurrency_payload = {
        "case_pack_id": "deterministic-v1",
        "lane": "offline",
        "concurrency": 2,
    }

    rejected = _request(
        app,
        "POST",
        "/adk/benchmarks/bounded",
        headers=_debug_headers(
            action="run_bounded_benchmark",
            object_id="deterministic-v1",
            payload=concurrency_payload,
            key="server-concurrency-limit-key",
        ),
        json=concurrency_payload,
    )

    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "benchmark_quota_exceeded"

    result = run_offline_evaluation_lane(
        case_pack_id="deterministic-v1",
        state_root=tmp_path / "retention-eval",
        case_limit=1,
        budget={"case_limit": 1, "retention_days": 2},
    )

    assert result["status"] == "completed"
    assert result["effective_concurrency"] == 1
    assert "cleanup_status" not in result
    assert "retention_days" not in result
    assert result["retention_policy"] == {
        "retention_days": 2,
        "cleanup_enforced": False,
        "cleanup_status": "not_applicable",
    }
