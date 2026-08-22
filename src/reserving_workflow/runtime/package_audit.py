"""Installed package resource audit for local workbench entry points."""

from __future__ import annotations

import importlib.resources as resources
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceCheck:
    resource_id: str
    package: str
    relative_path: str


RESOURCE_CHECKS: tuple[ResourceCheck, ...] = (
    ResourceCheck(
        "operator_console",
        "reserving_workflow.interfaces.operator_console",
        "console.html",
    ),
    ResourceCheck(
        "adk_agent_schema",
        "reserving_workflow.adapters.adk",
        "data/AgentConfig-2.7.1.json",
    ),
    ResourceCheck(
        "workflow_lab_example",
        "reserving_workflow.developer_workflows.workflow_lab_example",
        "root_agent.yaml",
    ),
    ResourceCheck(
        "workflow_lab_example_policy",
        "reserving_workflow.developer_workflows.workflow_lab_example",
        "workflow_policy.yaml",
    ),
    ResourceCheck(
        "developer_adk_app",
        "developer_workflows.ai_actuary_developer",
        "agent.py",
    ),
    ResourceCheck(
        "developer_adk_tools",
        "developer_workflows.ai_actuary_developer",
        "tools.py",
    ),
    ResourceCheck(
        "workflow_task_contracts",
        "workflows",
        "agent-runtimes/hermes-worker/task_contracts.py",
    ),
    ResourceCheck(
        "workflow_openai_runner",
        "workflows",
        "agent-runtimes/openai-agents/runner.py",
    ),
)


def audit_package_resources() -> dict[str, Any]:
    """Verify workbench resources without importing the optional ADK runtime."""

    imported_before = set(sys.modules)
    checked = {
        item.resource_id: _check_resource(item)
        for item in RESOURCE_CHECKS
    }
    return {
        "ok": all(item["present"] for item in checked.values()),
        "resources": checked,
        "google_adk_imported": (
            "google.adk" in sys.modules and "google.adk" not in imported_before
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    payload = audit_package_resources()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] and not payload["google_adk_imported"] else 1


def _check_resource(item: ResourceCheck) -> dict[str, Any]:
    try:
        node = resources.files(item.package).joinpath(item.relative_path)
        present = node.is_file()
    except (ImportError, ModuleNotFoundError, FileNotFoundError, AttributeError):
        present = False
    return {
        "package": item.package,
        "relative_path": item.relative_path,
        "present": present,
    }


__all__ = ["audit_package_resources", "main"]
