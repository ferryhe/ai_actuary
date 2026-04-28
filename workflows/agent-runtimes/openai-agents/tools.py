"""Planner-facing tool wrappers for the offline runner skeleton."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

AVAILABLE_TOOL_WRAPPERS = [
    "run_case_worker_tool",
    "run_batch_worker_tool",
    "build_review_packet_tool",
]


def run_case_worker_tool(task: Any):
    worker_module = _load_worker_module("case_worker.py", "hermes_case_worker")
    return worker_module.run_case_worker(task)


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


def _load_worker_module(filename: str, module_name: str):
    worker_path = Path(__file__).resolve().parents[1] / "hermes-worker" / filename
    spec = importlib.util.spec_from_file_location(module_name, worker_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
