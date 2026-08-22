"""Development-only ADK agent for bounded reads and confirmed workflow starts."""

from __future__ import annotations

import os
from typing import Any, AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.events import Event
from google.genai import types

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


class BrowserSmokeAgent(BaseAgent):
    """Model-free ADK protocol agent used only by the deterministic browser smoke."""

    async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Event, None]:
        inspect_request = _browser_smoke_inspect_request(ctx)
        if inspect_request is not None:
            run_id = str(inspect_request["run_id"])
            yield _browser_smoke_function_response_event(
                self.name,
                ctx,
                "summarize_run",
                {
                    "ok": True,
                    "run_id": run_id,
                    "summary": read_tools.summarize_run(run_id),
                },
            )
            return
        confirmation = _browser_smoke_confirmation(ctx)
        if confirmation is None:
            yield _browser_smoke_confirmation_event(self.name, ctx)
            return
        if not bool(confirmation.get("confirmed")):
            yield _browser_smoke_function_response_event(
                self.name,
                ctx,
                "start_workflow_run",
                {
                    "ok": False,
                    "status": "rejected",
                },
            )
            return
        request = confirmation.get("payload")
        if not isinstance(request, dict):
            request = _browser_smoke_request(ctx)
        result = read_tools._default_execution_client_factory().start_workflow_run(
            workflow_id=str(request["workflow_id"]),
            case_id=str(request["case_id"]),
            inputs=dict(request["inputs"]),
            adk_app="ai_actuary_developer",
            adk_session_id=str(ctx.session.id),
            adk_invocation_id=str(ctx.invocation_id),
            idempotency_key=f"browser-smoke-{ctx.session.id}-{ctx.invocation_id}",
        )
        read_tools._record_start_workflow_trace_span(
            session_id=str(ctx.session.id),
            invocation_id=str(ctx.invocation_id),
            workflow_id=str(request["workflow_id"]),
            case_id=str(request["case_id"]),
            result={"ok": True, "data": result},
        )
        yield _browser_smoke_function_response_event(
            self.name,
            ctx,
            "start_workflow_run",
            {"ok": True, **result},
        )


def _browser_smoke_request(ctx: Any) -> dict[str, Any]:
    state = getattr(getattr(ctx, "session", None), "state", {}) or {}
    request = state.get("browser_smoke_start")
    if not isinstance(request, dict):
        raise ValueError("Browser smoke ADK request state is unavailable.")
    return request


def _browser_smoke_inspect_request(ctx: Any) -> dict[str, Any] | None:
    state = getattr(getattr(ctx, "session", None), "state", {}) or {}
    request = state.get("browser_smoke_inspect")
    if request is None:
        return None
    if not isinstance(request, dict) or not isinstance(request.get("run_id"), str):
        raise ValueError("Browser smoke ADK inspect state is unavailable.")
    return request


def _browser_smoke_confirmation(ctx: Any) -> dict[str, Any] | None:
    content = getattr(ctx, "user_content", None)
    for part in getattr(content, "parts", []) or []:
        function_response = getattr(part, "function_response", None)
        if (
            function_response is not None
            and function_response.name == "adk_request_confirmation"
            and isinstance(function_response.response, dict)
        ):
            return function_response.response
    return None


def _browser_smoke_confirmation_event(agent_name: str, ctx: Any) -> Event:
    request = _browser_smoke_request(ctx)
    confirmation_id = f"{ctx.invocation_id}-confirm"
    start_call_id = f"{ctx.invocation_id}-start"
    confirmation_payload = {
        "workflow_id": request["workflow_id"],
        "case_id": request["case_id"],
        "inputs": request["inputs"],
        "workspace_id": "adk-development",
        "expected_artifact_types": list(
            read_tools.EXPECTED_ARTIFACT_TYPES[str(request["workflow_id"])]
        ),
    }
    return Event(
        invocation_id=ctx.invocation_id,
        author=agent_name,
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="adk_request_confirmation",
                        id=confirmation_id,
                        args={
                            "originalFunctionCall": {
                                "name": "start_workflow_run",
                                "id": start_call_id,
                                "args": {
                                    "workflow_id": request["workflow_id"],
                                    "case_id": request["case_id"],
                                    "inputs": request["inputs"],
                                },
                            },
                            "toolConfirmation": {
                                "hint": (
                                    "Start the deterministic browser-smoke ADK "
                                    "workflow in adk-development?"
                                ),
                                "payload": confirmation_payload,
                            },
                        },
                    )
                )
            ],
        ),
        long_running_tool_ids={confirmation_id},
    )


def _browser_smoke_function_response_event(
    agent_name: str,
    ctx: Any,
    function_name: str,
    response: dict[str, Any],
) -> Event:
    return Event(
        invocation_id=ctx.invocation_id,
        author=agent_name,
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name=function_name,
                        id=f"{ctx.invocation_id}-{function_name}",
                        response=response,
                    )
                )
            ],
        ),
    )


root_agent = BrowserSmokeAgent(
    name="ai_actuary_developer",
    description="Model-free deterministic browser smoke ADK protocol agent.",
) if os.environ.get("AI_ACTUARY_BROWSER_SMOKE_RUNNER") == "1" else Agent(
    name="ai_actuary_developer",
    model=_MODEL_NAME,
    description=(
        "Development-only AI Actuary control-plane assistant. Operator Console: "
        f"{_CONSOLE_URL}"
    ),
    instruction=(
        "You are a development-only environment guide. Use the registered tools only "
        "to inspect the fixed loopback control plane or, after explicit ADK confirmation, "
        "start one of the two published Chainladder workflows, rerun an ADK run, or run "
        "an isolated bounded benchmark in adk-development. "
        f"The Operator Console is {_CONSOLE_URL}. Never start a direct tool run, call legacy "
        "path-based replay, benchmark, repeatability, or report APIs, or submit a review "
        "decision. Use replay, repeatability, and report tools only by trusted run IDs. "
        "Never claim that a poll timeout cancelled a business run. Do not request "
        "or reveal filesystem paths, "
        "registry internals, artifact roots, credentials, secrets, or raw exceptions."
    ),
    tools=[
        getattr(read_tools, name)
        for name in read_tools.READ_TOOL_NAMES
        + read_tools.EXECUTION_TOOL_NAMES
        + read_tools.DEBUG_TOOL_NAMES
    ],
)
