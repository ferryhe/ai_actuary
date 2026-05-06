"""OpenAI Agents planner wrapper for bounded control-plane execution plans."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from reserving_workflow.contracts import AgentExecutionPlan, AgentPlanningRequest


def build_agent_planner(*, agents_module=None, model: str | None = None):
    """Build a planner agent that returns only an execution plan."""

    agents_sdk = agents_module or _import_agents_sdk()
    config_module = _load_sibling_module("config.py", "openai_planner_adapter_config")
    output_type = AgentExecutionPlan
    if hasattr(agents_sdk, "AgentOutputSchema"):
        output_type = agents_sdk.AgentOutputSchema(AgentExecutionPlan, strict_json_schema=True)
    return agents_sdk.Agent(
        name="AI Actuary Planner Adapter",
        instructions=(
            "You are the bounded planner for AI Actuary. "
            "Return only an execution plan for the Hermes control plane. "
            "Choose exactly one of workflow_id or tool_id. "
            "Do not fabricate actuarial values, review judgements, or artifact contents. "
            "Do not claim work is done; produce only the next API-safe plan."
        ),
        model=model or config_module.DEFAULT_PLANNER_CONFIG["model"],
        tools=[],
        output_type=output_type,
    )


def build_planning_prompt(request: AgentPlanningRequest) -> str:
    payload = request.model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True)


def plan_case_run(
    request: AgentPlanningRequest,
    *,
    agents_module=None,
    model: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Run the planner and normalize the returned plan payload."""

    agents_sdk = agents_module or _import_agents_sdk()
    planner = build_agent_planner(agents_module=agents_sdk, model=model)
    planner_prompt = prompt or build_planning_prompt(request)
    result = agents_sdk.Runner.run_sync(planner, planner_prompt)
    final_output = result.final_output
    plan = (
        final_output
        if isinstance(final_output, AgentExecutionPlan)
        else AgentExecutionPlan.model_validate(final_output)
    )
    return {
        "agent": {
            "name": planner.name,
            "model": getattr(planner, "model", None),
        },
        "request": request.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "prompt": planner_prompt,
    }


def _import_agents_sdk():
    try:
        import agents  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI Agents SDK is required for the planner adapter. Install it with `pip install openai-agents`."
        ) from exc
    return agents


def _load_sibling_module(filename: str, module_name: str):
    module_path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
