"""Development-only ADK agent for bounded reads and confirmed workflow starts."""

from __future__ import annotations

from google.adk.agents import Agent

from . import tools as read_tools

_CONTROL_PLANE_BASE_URL = read_tools.CONTROL_PLANE_BASE_URL
_CONSOLE_URL = f"{_CONTROL_PLANE_BASE_URL}/console"
_MODEL_NAME = "gemini-2.5-flash"


def describe_development_environment() -> dict[str, str]:
    """Describe this local development surface and its intentionally narrow scope."""

    return {
        "scope": "development-only",
        "console_url": _CONSOLE_URL,
        "control_plane_url": _CONTROL_PLANE_BASE_URL,
        "capability": "read-only control-plane inspection",
        "execution_capability": "isolated, confirmed published-workflow execution",
        "model": _MODEL_NAME,
    }


check_control_plane_health = read_tools.get_health
check_control_plane_preflight = read_tools.get_preflight


root_agent = Agent(
    name="ai_actuary_developer",
    model=_MODEL_NAME,
    description=(
        "Development-only AI Actuary control-plane assistant. Operator Console: "
        f"{_CONSOLE_URL}"
    ),
    instruction=(
        "You are a development-only environment guide. Use the registered tools only "
        "to inspect the fixed loopback control plane or, after explicit ADK confirmation, "
        "start one of the two published Chainladder workflows in adk-development. "
        f"The Operator Console is {_CONSOLE_URL}. Never start a direct tool run, rerun, "
        "replay, benchmark, run a repeatability check, export a report, or submit a review "
        "decision. Never claim that a poll timeout cancelled a business run. Do not request "
        "or reveal filesystem paths, "
        "registry internals, artifact roots, credentials, secrets, or raw exceptions."
    ),
    tools=[
        getattr(read_tools, name)
        for name in read_tools.READ_TOOL_NAMES + read_tools.EXECUTION_TOOL_NAMES
    ],
)
