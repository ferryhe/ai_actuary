"""Planner runners for offline and real OpenAI Agents SDK governed workflows."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

WORKFLOW_STAGES = ["intake", "route", "dispatch", "collect"]


def run_planner_workflow(task: Any) -> dict[str, Any]:
    routing_module = _load_sibling_module("routing.py", "openai_routing")
    tools_module = _load_sibling_module("tools.py", "openai_tools")

    intake = {
        "task_id": getattr(task, "task_id", None),
        "task_kind": getattr(task, "task_kind", None),
        "objective": getattr(task, "objective", None),
    }
    route = routing_module.route_case_task(task)
    tool_registry = tools_module.get_tool_registry()
    worker_result = tool_registry[route.worker_action](task)

    worker_result_payload = (
        worker_result.model_dump(mode="json")
        if hasattr(worker_result, "model_dump")
        else dict(worker_result)
    )
    planner_summary = (
        "Planner collected worker result for "
        f"task {intake['task_id']} via {route.mode} route with status {worker_result_payload['status']}."
    )

    return {
        "stage": "collect",
        "workflow_stages": list(WORKFLOW_STAGES),
        "intake": intake,
        "route": route.to_dict(),
        "dispatch": {"tool_name": route.worker_action},
        "worker_result": worker_result_payload,
        "planner_summary": planner_summary,
    }


def run_openai_governed_workflow(task: Any, *, agents_module=None, user_prompt: str | None = None) -> dict[str, Any]:
    routing_module = _load_sibling_module("routing.py", "openai_routing_sdk")
    config_module = _load_sibling_module("config.py", "openai_config_sdk")
    agents_file = _load_sibling_module("agents.py", "openai_agents_sdk")
    route = routing_module.route_case_task(task)
    if route.mode != "governed":
        raise ValueError(f"Prompt 6 only supports governed workflow, got route {route.mode!r}")

    sdk = agents_module or _import_agents_sdk()
    agent = agents_file.build_workflow_manager_agent(task, agents_module=sdk)
    run_config = config_module.build_openai_run_config(
        workflow_name=config_module.DEFAULT_PLANNER_CONFIG["workflow_name"],
        tracing_disabled=config_module.DEFAULT_PLANNER_CONFIG["tracing_disabled"],
        trace_metadata={
            "task_id": getattr(task, "task_id", None),
            "case_id": getattr(task, "case_ref", None),
        },
        agents_module=sdk,
    )
    prompt = user_prompt or (
        "Run the governed reserving workflow for this task. "
        "Use the provided case-worker tool, then return only the structured summary. "
        "Do not invent numeric values; cite only the worker result."
    )
    result = sdk.Runner.run_sync(agent, prompt, run_config=run_config)
    final_output = result.final_output
    final_output_payload = final_output.model_dump(mode="json") if hasattr(final_output, "model_dump") else final_output
    tool_state = getattr(agent.tools[0], "_tool_state", {})
    worker_result = tool_state.get("last_result")
    if worker_result is None:
        raise RuntimeError("Case-worker tool did not record a worker result during the OpenAI run.")

    return {
        "stage": "collect",
        "route": route.to_dict(),
        "prompt": prompt,
        "agent": {"name": agent.name, "model": getattr(agent, "model", None)},
        "trace": {
            "workflow_name": run_config.kwargs["workflow_name"] if hasattr(run_config, "kwargs") else config_module.DEFAULT_PLANNER_CONFIG["workflow_name"],
            "tracing_disabled": run_config.kwargs["tracing_disabled"] if hasattr(run_config, "kwargs") else config_module.DEFAULT_PLANNER_CONFIG["tracing_disabled"],
        },
        "worker_result": worker_result,
        "final_output": final_output_payload,
    }


def _import_agents_sdk():
    try:
        import agents  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI Agents SDK is required for Prompt 6. Install it with `pip install openai-agents` and set OPENAI_API_KEY."
        ) from exc
    return agents


def _load_sibling_module(filename: str, module_name: str):
    module_path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
