from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

from reserving_workflow.contracts import AgentPlanningRequest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLANNER_ADAPTER_PATH = REPO_ROOT / "workflows" / "agent-runtimes" / "openai-agents" / "planner_adapter.py"


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs.get("name")
        self.instructions = kwargs.get("instructions")
        self.model = kwargs.get("model")
        self.output_type = kwargs.get("output_type")


class FakeAgentOutputSchema:
    def __init__(self, output_type, strict_json_schema: bool = False):
        self.output_type = output_type
        self.strict_json_schema = strict_json_schema


class FakeRunner:
    @staticmethod
    def run_sync(agent, prompt, run_config=None):
        request = json.loads(prompt)
        output_type = agent.output_type.output_type if hasattr(agent.output_type, "output_type") else agent.output_type
        workflow_ids = request.get("available_workflow_ids", [])
        tool_ids = request.get("available_tool_ids", [])
        plan = output_type(
            case_id=request["case_id"],
            objective=request["objective"],
            inputs=request.get("inputs", {}),
            workflow_id=(workflow_ids[0] if workflow_ids else None),
            tool_id=(tool_ids[0] if not workflow_ids and tool_ids else None),
            user_prompt=request.get("user_prompt"),
            background=True,
        )
        return types.SimpleNamespace(final_output=plan, prompt=prompt, run_config=run_config)


def _install_fake_agents_module(monkeypatch):
    fake_agents = types.SimpleNamespace(
        Agent=FakeAgent,
        AgentOutputSchema=FakeAgentOutputSchema,
        Runner=FakeRunner,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    return fake_agents


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_agent_planner_uses_structured_plan_schema(monkeypatch):
    _install_fake_agents_module(monkeypatch)
    module = _load_module("planner_adapter_build", PLANNER_ADAPTER_PATH)

    agent = module.build_agent_planner()

    assert agent.name == "AI Actuary Planner Adapter"
    assert agent.output_type.output_type.__name__ == "AgentExecutionPlan"
    assert agent.output_type.strict_json_schema is True
    assert "Do not fabricate actuarial values" in agent.instructions


def test_plan_case_run_returns_json_serializable_execution_plan(monkeypatch):
    _install_fake_agents_module(monkeypatch)
    module = _load_module("planner_adapter_run", PLANNER_ADAPTER_PATH)
    request = AgentPlanningRequest(
        case_id="case-14",
        objective="Run the governed workflow",
        inputs={"sample_name": "RAA"},
        available_tool_ids=["chainladder"],
        available_workflow_ids=["chainladder-basic"],
        user_prompt="Use the standard governed path.",
    )

    payload = module.plan_case_run(request)

    assert payload["agent"]["name"] == "AI Actuary Planner Adapter"
    assert payload["request"]["case_id"] == "case-14"
    assert payload["plan"]["workflow_id"] == "chainladder-basic"
    assert payload["plan"]["tool_id"] is None
    assert payload["plan"]["inputs"] == {"sample_name": "RAA"}
