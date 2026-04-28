"""Planner-side agent definitions for the minimal governed OpenAI workflow."""

from dataclasses import asdict
from pathlib import Path
from typing import Any
import importlib.util

from pydantic import BaseModel, Field


class PlannerAgentRole(BaseModel):
    name: str
    responsibility: str
    outputs: list[str] = Field(default_factory=list)


class GovernedCaseSummary(BaseModel):
    case_id: str
    worker_status: str
    deterministic_method: str
    cited_values: dict[str, float] = Field(default_factory=dict)
    review_reasons: list[str] = Field(default_factory=list)
    artifact_manifest_path: str | None = None
    narrative_summary: str


PLANNER_AGENT_ROLES = [
    PlannerAgentRole(
        name="workflow_manager",
        responsibility="Own governed intake, tool invocation, and structured collection.",
        outputs=["structured_case_summary"],
    ),
    PlannerAgentRole(
        name="triage_router",
        responsibility="Keep manager-style routing decisions centralized before worker execution.",
        outputs=["route_decision"],
    ),
    PlannerAgentRole(
        name="review_router",
        responsibility="Surface review-required worker outcomes without fabricating actuarial truth.",
        outputs=["review_signal"],
    ),
    PlannerAgentRole(
        name="narrative_planner",
        responsibility="Constrain narrative framing while numeric truth stays in worker outputs.",
        outputs=["narrative_guidance"],
    ),
]


def get_planner_agent_configs() -> list[dict[str, object]]:
    return [role.model_dump(mode="json") for role in PLANNER_AGENT_ROLES]


def build_workflow_manager_agent(task: Any, *, agents_module=None, model: str | None = None):
    agents_sdk = agents_module or _import_agents_sdk()
    tools_module = _load_sibling_module("tools.py", "openai_tools_runtime")
    config_module = _load_sibling_module("config.py", "openai_config_runtime")
    tool = tools_module.build_openai_case_worker_tool(task, agents_module=agents_sdk)
    return agents_sdk.Agent(
        name="Workflow Manager Agent",
        instructions=(
            "You are the governed workflow manager for AI Actuary. "
            "Always call the bound case-worker tool before answering. "
            "Return a structured summary only. "
            "Never invent key reserve numbers; cite only values returned by the worker tool."
        ),
        model=model or config_module.DEFAULT_PLANNER_CONFIG["model"],
        tools=[tool],
        output_type=GovernedCaseSummary,
    )


def _import_agents_sdk():
    try:
        import agents  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI Agents SDK is required for Prompt 6. Install it with `pip install openai-agents`."
        ) from exc
    return agents


def _load_sibling_module(filename: str, module_name: str):
    module_path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
