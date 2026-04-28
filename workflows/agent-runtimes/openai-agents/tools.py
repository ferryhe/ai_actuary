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
    worker_module = _load_worker_module("batch_worker.py", "hermes_batch_worker")
    return worker_module.run_batch_worker(task)


def build_review_packet_tool(task: Any, worker_result: Any):
    review_worker = _load_worker_module("review_worker.py", "hermes_review_worker")
    output_dir = ((getattr(task, "inputs", {}) or {}).get("artifact_dir") if task is not None else None)
    return review_worker.build_review_packet(worker_result, output_dir=output_dir)


def get_tool_registry() -> dict[str, Callable[[Any], Any]]:
    return {
        "run_case_worker": run_case_worker_tool,
        "run_batch_worker": run_batch_worker_tool,
    }


def _import_agents_sdk():
    try:
        import agents  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI Agents SDK is required for Prompt 6. Install it with `pip install openai-agents`."
        ) from exc
    return agents


def _load_review_worker_module():
    return _load_worker_module("review_worker.py", "hermes_review_worker")


def _load_worker_module(filename: str, module_name: str):
    worker_path = Path(__file__).resolve().parents[1] / "hermes-worker" / filename
    if not worker_path.is_file():
        raise FileNotFoundError(f"Worker module file not found: {worker_path}")
    spec = importlib.util.spec_from_file_location(module_name, worker_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load worker module spec for {worker_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
