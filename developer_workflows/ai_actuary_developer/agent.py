"""Minimal read-only ADK agent for local control-plane health inspection."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from google.adk.agents import Agent


_CONTROL_PLANE_BASE_URL = "http://127.0.0.1:8000"
_CONSOLE_URL = f"{_CONTROL_PLANE_BASE_URL}/console"
_REQUEST_TIMEOUT_SECONDS = 2.0
_MODEL_NAME = "gemini-2.5-flash"


def describe_development_environment() -> dict[str, str]:
    """Describe this local development surface and its intentionally narrow scope."""

    return {
        "scope": "development-only",
        "console_url": _CONSOLE_URL,
        "control_plane_url": _CONTROL_PLANE_BASE_URL,
        "allowed_checks": "/health, /health/preflight",
        "model": _MODEL_NAME,
    }


def check_control_plane_health() -> dict[str, Any]:
    """Read the fixed loopback control-plane /health endpoint."""

    return _read_health_endpoint("/health")


def check_control_plane_preflight() -> dict[str, Any]:
    """Read the fixed loopback control-plane /health/preflight endpoint."""

    return _read_health_endpoint("/health/preflight")


def _read_health_endpoint(path: str) -> dict[str, Any]:
    url = f"{_CONTROL_PLANE_BASE_URL}{path}"
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("health response must be a JSON object")
            return {
                "endpoint": path,
                "http_status": response.status,
                "payload": payload,
            }
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "endpoint": path,
            "http_status": None,
            "payload": {"ok": False},
            "error": f"Control plane unavailable: {type(exc).__name__}",
        }


root_agent = Agent(
    name="ai_actuary_developer",
    model=_MODEL_NAME,
    description=(
        "Development-only AI Actuary health inspector. Operator Console: "
        f"{_CONSOLE_URL}"
    ),
    instruction=(
        "You are a development-only environment guide. You may describe the local "
        "workbench and call only the fixed loopback /health and /health/preflight "
        f"checks. The Operator Console is {_CONSOLE_URL}. Do not invoke actuarial "
        "tools, workflows, runs, reviews, registries, or any write operation."
    ),
    tools=[
        describe_development_environment,
        check_control_plane_health,
        check_control_plane_preflight,
    ],
)
