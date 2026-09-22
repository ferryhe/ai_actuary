"""env overrides must honour the same bounds as `ApiSettings`."""

from __future__ import annotations

import pytest

from reserving_workflow.api import app as api_app
from reserving_workflow.api.app import ApiSettings


CREDENTIALS = {
    "AI_ACTUARY_OPERATOR_CREDENTIAL": "operator-secret-that-is-independent",
    "AI_ACTUARY_ADK_CREDENTIAL": "adk-secret-that-is-independent",
    "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN": "single-use-bootstrap",
    "AI_ACTUARY_OPERATOR_ORIGIN": "http://testserver",
}


@pytest.fixture
def credential_env(monkeypatch):
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("raw", ["not-a-number", "1000000000", "-1", "0"])
def test_bootstrap_ttl_env_override_is_validated(credential_env, monkeypatch, raw):
    monkeypatch.setenv("AI_ACTUARY_OPERATOR_BOOTSTRAP_TTL", raw)

    with pytest.raises(ValueError) as excinfo:
        api_app.create_app()

    message = str(excinfo.value)
    assert "AI_ACTUARY_OPERATOR_BOOTSTRAP_TTL" in message
    assert "(0, 3600]" in message


@pytest.mark.parametrize("raw", ["not-a-number", "1000000000", "-1", "0"])
def test_session_ttl_env_override_is_validated(credential_env, monkeypatch, raw):
    monkeypatch.setenv("AI_ACTUARY_OPERATOR_SESSION_TTL", raw)

    with pytest.raises(ValueError) as excinfo:
        api_app.create_app()

    message = str(excinfo.value)
    assert "AI_ACTUARY_OPERATOR_SESSION_TTL" in message
    assert "(0, 18000]" in message


def test_valid_ttl_env_overrides_are_accepted(credential_env, monkeypatch):
    monkeypatch.setenv("AI_ACTUARY_OPERATOR_BOOTSTRAP_TTL", "1800")
    monkeypatch.setenv("AI_ACTUARY_OPERATOR_SESSION_TTL", "900")

    authority = api_app.create_app().state.capability_authority

    assert authority._bootstrap_ttl_seconds == 1800.0
    assert authority._session_ttl_seconds == 900.0


def test_ttl_defaults_survive_blank_env(credential_env, monkeypatch):
    monkeypatch.setenv("AI_ACTUARY_OPERATOR_BOOTSTRAP_TTL", "")
    monkeypatch.setenv("AI_ACTUARY_OPERATOR_SESSION_TTL", "")

    authority = api_app.create_app().state.capability_authority

    assert authority._bootstrap_ttl_seconds == ApiSettings().operator_bootstrap_ttl_seconds
    assert authority._session_ttl_seconds == ApiSettings().operator_session_ttl_seconds
