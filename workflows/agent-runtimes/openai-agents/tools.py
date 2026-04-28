"""Planner-facing tool wrappers, including the Prompt 6 OpenAI Agents SDK bridge."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

AVAILABLE_TOOL_WRAPPERS = [
    "run_case_worker_tool",
    "run_batch_worker_tool",
    "build_review_packet_tool",
    "build_openai_case_worker_tool",
]


def run_case_worker_tool(task: Any):
    worker_module = _load_worker_module("case_worker.py", "hermes_case_worker")
    return worker_module.run_case_worker(task)


def build_openai_case_worker_tool(task: Any, *, agents_module=None):
    agents_sdk = agents_module or _import_agents_sdk()
    tool_state = {"last_result": None}

    @agents_sdk.function_tool
    def run_case_worker_tool_bound() -> dict[str, Any]:
        result = run_case_worker_tool(task)
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        tool_state["last_result"] = payload
        return payload

    setattr(run_case_worker_tool_bound, "_tool_state", tool_state)
    return run_case_worker_tool_bound


def run_batch_worker_tool(task: Any):
    raise NotImplementedError("Batch worker wrapper is reserved for Prompt 8.")


def build_review_packet_tool(task: Any):
    raise NotImplementedError("Review packet wrapper is reserved for Prompt 7.")


def get_tool_registry() -> dict[str, Callable[[Any], Any]]:
    return {
        "run_case_worker": run_case_worker_tool,
        "run_batch_worker": run_batch_worker_tool,
        "build_review_packet": build_review_packet_tool,
    }


def _import_agents_sdk():
    try:
        import agents  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI Agents SDK is required for Prompt 6. Install it with `pip install openai-agents`."
        ) from exc
    return agents


def _load_worker_module(filename: str, module_name: str):
    worker_path = Path(__file__).resolve().parents[1] / "hermes-worker" / filename
    spec = importlib.util.spec_from_file_location(module_name, worker_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
