from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.api import app as api_app
from reserving_workflow.api import capabilities
from reserving_workflow.api.capabilities import assert_route_matrix_complete


class Client:
    def __init__(self, app):
        self.app = app
        self.cookies = httpx.Cookies()

    def request(self, method: str, path: str, **kwargs):
        async def call():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
                cookies=self.cookies,
            ) as client:
                response = await client.request(method, path, **kwargs)
                self.cookies.update(response.cookies)
                return response

        return asyncio.run(call())


def settings(tmp_path):
    return ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="single-use-bootstrap",
        operator_origin="http://testserver",
    )


def _storage_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_transport_adr_and_route_matrix_are_frozen(tmp_path):
    adr = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "adr-0003-local-capability-credential-transport.md"
    decision = adr.read_text(encoding="utf-8")
    assert "Authorization: Bearer" in decision
    assert "HttpOnly" in decision
    assert "Idempotency-Key" in decision
    app = create_app(settings=settings(tmp_path))
    assert_route_matrix_complete(app)
    paths = {route.path for route in app.routes}
    assert "/docs" not in paths
    assert "/redoc" not in paths
    assert "/openapi.json" not in paths


def test_runtime_app_fails_closed_when_enforcement_is_disabled_or_secrets_are_missing(tmp_path):
    assert not hasattr(api_app, "create_test_app")
    configured = settings(tmp_path)
    configured.capability_enforcement = False
    with pytest.raises(ValueError, match="cannot be disabled"):
        create_app(settings=configured)

    with pytest.raises(ValueError, match="credentials"):
        create_app(
            settings=ApiSettings(
                registry_path=tmp_path / "unprotected-registry.json",
                artifact_root=tmp_path / "unprotected-artifacts",
                review_store_dir=tmp_path / "unprotected-reviews",
            )
        )


def test_missing_invalid_and_forbidden_credentials_are_distinct(tmp_path):
    client = Client(create_app(settings=settings(tmp_path)))
    assert client.request("GET", "/tools").status_code == 401
    assert client.request("GET", "/tools", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.request(
        "POST",
        "/reviews/review-real/decision",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
        json={"decision": "approved"},
    )
    assert response.status_code == 403
    assert "secret" not in response.text.lower()


def test_operator_can_review_adk_runs_but_adk_cannot_read_operator_runs():
    authority = capabilities.CapabilityAuthority(
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="single-use-bootstrap",
    )
    operator = authority.authenticate_bearer("Bearer operator-secret-that-is-independent")
    adk = authority.authenticate_bearer("Bearer adk-secret-that-is-independent")

    assert operator is not None
    assert adk is not None
    assert capabilities.object_in_scope(
        operator,
        {"workspace_id": "adk-development", "source": "adk-developer"},
    )
    assert not capabilities.object_in_scope(
        adk,
        {"workspace_id": "default-workspace", "source": "operator-console"},
    )


def test_operator_bootstrap_is_single_use_and_mutations_require_csrf_origin_and_host(tmp_path):
    app = create_app(settings=settings(tmp_path))
    client = Client(app)
    response = client.request(
        "POST",
        "/auth/operator/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"bootstrap_token": "single-use-bootstrap"},
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    csrf = response.json()["csrf_token"]
    assert client.request(
        "POST",
        "/runs",
        headers={"Origin": "http://testserver"},
        json={"case_id": "missing-csrf", "tool_id": "chainladder", "inputs": {}},
    ).status_code == 403
    accepted = client.request(
        "POST",
        "/runs",
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        json={"case_id": "operator-case", "tool_id": "chainladder", "inputs": {}, "background": True},
    )
    assert accepted.status_code == 202
    replay = Client(app).request(
        "POST",
        "/auth/operator/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"bootstrap_token": "single-use-bootstrap"},
    )
    assert replay.status_code == 401


@pytest.mark.parametrize(
    "request_headers",
    (
        {
            "Authorization": "Bearer operator-secret-that-is-independent",
            "Origin": "http://testserver",
            "Host": "attacker.invalid",
        },
        {
            "Authorization": "Bearer operator-secret-that-is-independent",
            "Origin": "http://attacker.invalid",
        },
    ),
)
def test_wrong_host_or_origin_mutation_is_stable_and_storage_invariant(
    tmp_path, request_headers
):
    app = create_app(settings=settings(tmp_path))
    before = _storage_snapshot(tmp_path)

    rejected = Client(app).request(
        "POST",
        "/runs",
        headers=request_headers,
        json={"case_id": "request-context-attack", "inputs": {}},
    )

    assert rejected.status_code == 403
    assert rejected.json() == {
        "detail": {
            "code": "request_context_forbidden",
            "message": "Request context is not allowed.",
        }
    }
    assert _storage_snapshot(tmp_path) == before


def test_rotation_revokes_bearer_and_sessions_without_removing_runs(tmp_path):
    configured = settings(tmp_path)
    app = create_app(settings=configured)
    from reserving_workflow.storage.local import LocalRunStore

    LocalRunStore(configured.registry_path).create_run(
        task_id="operator-existing",
        case_id="operator-existing",
        run_id="operator-existing",
        status="running",
        workspace_id="default-workspace",
    )
    client = Client(app)
    boot = client.request(
        "POST", "/auth/operator/bootstrap", headers={"Origin": "http://testserver"},
        json={"bootstrap_token": "single-use-bootstrap"},
    )
    assert boot.status_code == 200
    app.state.capability_authority.rotate("operator-console", "rotated-operator-secret")
    assert client.request("GET", "/runs").status_code == 401
    assert Client(app).request(
        "GET", "/runs", headers={"Authorization": "Bearer operator-secret-that-is-independent"}
    ).status_code == 401
    adk_client = Client(app)
    assert adk_client.request(
        "GET", "/tools", headers={"Authorization": "Bearer adk-secret-that-is-independent"}
    ).status_code == 200
    app.state.capability_authority.rotate("adk-developer", "rotated-adk-secret")
    assert adk_client.request(
        "GET", "/tools", headers={"Authorization": "Bearer adk-secret-that-is-independent"}
    ).status_code == 401
    assert adk_client.request(
        "GET", "/tools", headers={"Authorization": "Bearer rotated-adk-secret"}
    ).status_code == 200
    assert LocalRunStore(configured.registry_path).get_run("operator-existing")["status"] == "running"


def test_browser_smoke_rotation_route_is_gated_and_revokes_former_adk_credential(
    tmp_path, monkeypatch
):
    configured = settings(tmp_path)
    app = create_app(settings=configured)
    client = Client(app)
    disabled = client.request(
        "POST",
        "/adk/browser-smoke/rotate-credential",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
        json={"new_credential": "rotated-adk-secret"},
    )
    assert disabled.status_code == 404
    assert client.request(
        "GET", "/tools", headers={"Authorization": "Bearer adk-secret-that-is-independent"}
    ).status_code == 200

    monkeypatch.setenv("AI_ACTUARY_BROWSER_SMOKE_RUNNER", "1")
    smoke_app = create_app(settings=settings(tmp_path / "smoke"))
    smoke_client = Client(smoke_app)
    rotated = smoke_client.request(
        "POST",
        "/adk/browser-smoke/rotate-credential",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
        json={"new_credential": "rotated-adk-secret"},
    )

    assert rotated.status_code == 200
    assert smoke_client.request(
        "GET", "/tools", headers={"Authorization": "Bearer adk-secret-that-is-independent"}
    ).status_code == 401
    assert smoke_client.request(
        "GET", "/tools", headers={"Authorization": "Bearer rotated-adk-secret"}
    ).status_code == 200


def test_console_get_and_adk_process_cannot_acquire_operator_session(tmp_path):
    configured = settings(tmp_path)
    app = create_app(settings=configured)
    client = Client(app)
    response = client.request("GET", "/console")
    assert response.status_code == 200
    assert configured.operator_credential not in response.text
    assert configured.adk_credential not in response.text
    assert configured.operator_bootstrap_token not in response.text
    assert "bootstrap_token" not in response.text
    assert "localStorage" not in response.text
    assert "sessionStorage" not in response.text
    assert "set-cookie" not in response.headers
    assert client.request("GET", "/runs").status_code == 401

    adk_client = Client(app)
    adk_console = adk_client.request(
        "GET",
        "/console",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
    )
    assert "set-cookie" not in adk_console.headers
    denied = adk_client.request(
        "POST",
        "/auth/operator/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"bootstrap_token": "adk-secret-that-is-independent"},
    )
    assert denied.status_code == 401
    assert "set-cookie" not in denied.headers
    assert configured.adk_credential not in denied.text

    browser = Client(app)
    accepted = browser.request(
        "POST",
        "/auth/operator/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"bootstrap_token": "single-use-bootstrap"},
    )
    assert accepted.status_code == 200
    assert "HttpOnly" in accepted.headers["set-cookie"]
    assert browser.request("GET", "/runs").status_code == 200


def test_operator_bootstrap_expiry_is_safe_and_does_not_consume_response_secret(
    tmp_path, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(capabilities.time, "monotonic", lambda: clock[0])
    configured = settings(tmp_path)
    configured.operator_bootstrap_ttl_seconds = 1.0
    app = create_app(settings=configured)
    clock[0] = 102.0

    expired = Client(app).request(
        "POST",
        "/auth/operator/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"bootstrap_token": "single-use-bootstrap"},
    )

    assert expired.status_code == 401
    assert expired.json()["detail"]["code"] == "bootstrap_invalid"
    assert "set-cookie" not in expired.headers
    assert configured.operator_bootstrap_token not in expired.text


def test_launcher_approved_browser_handoff_binds_claim_and_rejects_adk_or_replay(
    tmp_path
):
    configured = settings(tmp_path)
    app = create_app(settings=configured)
    browser_claim = "browser-generated-private-claim-token-0001"
    requested = Client(app).request(
        "POST",
        "/auth/operator/handoff/request",
        headers={"Origin": "http://testserver"},
        json={"claim_token": browser_claim},
    )
    assert requested.status_code == 200
    handoff_id = requested.json()["handoff_id"]
    assert browser_claim not in requested.text

    adk = Client(app)
    denied = adk.request(
        "POST",
        "/auth/operator/handoff/claim",
        headers={
            "Authorization": "Bearer adk-secret-that-is-independent",
            "Origin": "http://testserver",
        },
        json={
            "handoff_id": handoff_id,
            "claim_token": "adk-cannot-claim-this-handoff-token-0001",
        },
    )
    assert denied.status_code == 401
    assert "set-cookie" not in denied.headers

    approved = Client(app).request(
        "POST",
        "/auth/operator/handoff/approve",
        headers={"Origin": "http://testserver"},
        json={
            "handoff_id": handoff_id,
            "bootstrap_token": "single-use-bootstrap",
        },
    )
    assert approved.status_code == 200
    assert "set-cookie" not in approved.headers
    assert configured.operator_bootstrap_token not in approved.text

    browser = Client(app)
    claimed = browser.request(
        "POST",
        "/auth/operator/handoff/claim",
        headers={"Origin": "http://testserver"},
        json={"handoff_id": handoff_id, "claim_token": browser_claim},
    )
    assert claimed.status_code == 200
    assert "HttpOnly" in claimed.headers["set-cookie"]
    assert browser.request("GET", "/runs").status_code == 200

    replay = Client(app).request(
        "POST",
        "/auth/operator/handoff/claim",
        headers={"Origin": "http://testserver"},
        json={"handoff_id": handoff_id, "claim_token": browser_claim},
    )
    assert replay.status_code == 401
    assert "set-cookie" not in replay.headers


def test_browser_handoff_expires_and_operator_rotation_revokes_pending_claim(
    tmp_path, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(capabilities.time, "monotonic", lambda: clock[0])
    configured = settings(tmp_path)
    configured.operator_bootstrap_ttl_seconds = 1.0
    app = create_app(settings=configured)
    claim_token = "browser-generated-private-claim-token-0002"
    request = Client(app).request(
        "POST",
        "/auth/operator/handoff/request",
        headers={"Origin": "http://testserver"},
        json={"claim_token": claim_token},
    )
    assert request.status_code == 200
    handoff_id = request.json()["handoff_id"]
    clock[0] = 102.0

    expired = Client(app).request(
        "POST",
        "/auth/operator/handoff/approve",
        headers={"Origin": "http://testserver"},
        json={
            "handoff_id": handoff_id,
            "bootstrap_token": configured.operator_bootstrap_token,
        },
    )
    assert expired.status_code == 401
    assert expired.json()["detail"]["code"] == "bootstrap_invalid"
    assert "set-cookie" not in expired.headers
    assert configured.operator_bootstrap_token not in expired.text

    rotated_settings = settings(tmp_path / "rotated")
    rotated_app = create_app(settings=rotated_settings)
    pending = Client(rotated_app).request(
        "POST",
        "/auth/operator/handoff/request",
        headers={"Origin": "http://testserver"},
        json={"claim_token": claim_token},
    )
    assert pending.status_code == 200
    rotated_app.state.capability_authority.rotate(
        "operator-console", "rotated-operator-secret"
    )
    revoked = Client(rotated_app).request(
        "POST",
        "/auth/operator/handoff/claim",
        headers={"Origin": "http://testserver"},
        json={
            "handoff_id": pending.json()["handoff_id"],
            "claim_token": claim_token,
        },
    )
    assert revoked.status_code == 401
    assert revoked.json()["detail"]["code"] == "bootstrap_invalid"
    assert "set-cookie" not in revoked.headers
