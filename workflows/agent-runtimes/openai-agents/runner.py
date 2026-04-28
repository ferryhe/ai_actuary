"""Offline planner workflow skeleton: intake → route → dispatch worker → collect result."""

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


def _load_sibling_module(filename: str, module_name: str):
    module_path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
