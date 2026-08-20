from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reserving_workflow.api.app import ApiSettings, create_app  # noqa: E402


TEST_OPERATOR_CREDENTIAL = "test-operator-capability-credential"
TEST_ADK_CREDENTIAL = "test-adk-capability-credential"
TEST_BOOTSTRAP_TOKEN = "test-single-use-bootstrap-token"
TEST_ORIGIN = "http://testserver"


def authenticated_api_settings(settings: ApiSettings | None = None) -> ApiSettings:
    configured = settings or ApiSettings()
    return configured.model_copy(
        update={
            "operator_credential": TEST_OPERATOR_CREDENTIAL,
            "adk_credential": TEST_ADK_CREDENTIAL,
            "operator_bootstrap_token": TEST_BOOTSTRAP_TOKEN,
            "operator_origin": TEST_ORIGIN,
            "capability_enforcement": True,
        }
    )


def create_authenticated_app(*, settings: ApiSettings | None = None, **kwargs: Any):
    return create_app(settings=authenticated_api_settings(settings), **kwargs)


def authenticated_request_kwargs(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    request_kwargs = dict(kwargs)
    headers = dict(request_kwargs.pop("headers", {}) or {})
    headers.setdefault("Authorization", f"Bearer {TEST_OPERATOR_CREDENTIAL}")
    if method.upper() not in {"GET", "HEAD"}:
        headers.setdefault("Origin", TEST_ORIGIN)
    request_kwargs["headers"] = headers
    return request_kwargs
