from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAI_RUNTIME_DIR = REPO_ROOT / "workflows" / "agent-runtimes" / "openai-agents"
HERMES_WORKER_DIR = REPO_ROOT / "workflows" / "agent-runtimes" / "hermes-worker"


class FakeRunConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs.get("name")
        self.instructions = kwargs.get("instructions")
        self.model = kwargs.get("model")
        self.tools = kwargs.get("tools", [])
        self.output_type = kwargs.get("output_type")


class FakeRunner:
    @staticmethod
    def run_sync(agent, prompt, run_config=None):
        tool_output = agent.tools[0]()
        worker_status = tool_output["status"]
        final_output = agent.output_type(
            case_id=tool_output["case_id"],
            worker_status=worker_status,
            deterministic_method=tool_output["deterministic_result"]["method"],
            cited_values=tool_output["deterministic_result"]["reserve_summary"],
            review_reasons=tool_output["review_reasons"],
            artifact_manifest_path=tool_output["artifact_paths"].get("run_manifest"),
            narrative_summary=tool_output["narrative_draft"]["summary"],
        )
        return types.SimpleNamespace(final_output=final_output, prompt=prompt, run_config=run_config)


def fake_function_tool(fn):
    fn._is_fake_tool = True
    return fn


def _install_fake_agents_module(monkeypatch):
    fake_agents = types.SimpleNamespace(
        Agent=FakeAgent,
        Runner=FakeRunner,
        RunConfig=FakeRunConfig,
        function_tool=fake_function_tool,
        trace=lambda workflow_name: _NullContext(workflow_name),
        set_tracing_disabled=lambda disabled: disabled,
        ModelSettings=lambda **kwargs: kwargs,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    return fake_agents


class _NullContext:
    def __init__(self, workflow_name: str):
        self.workflow_name = workflow_name

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_task_contracts():
    return _load_module("task_contracts_runtime_test", HERMES_WORKER_DIR / "task_contracts.py")


def _make_task(tmp_path: Path, review_thresholds: dict | None = None):
    task_contracts = _load_task_contracts()
    return task_contracts.WorkerTask(
        task_id="openai-runtime-001",
        task_kind="run_case",
        case_ref="openai-runtime-case",
        objective="Run governed workflow through OpenAI Agents SDK",
        inputs={
            "artifact_dir": str(tmp_path / "artifacts"),
            "case_payload": {
                "case_id": "openai-runtime-case",
                "metadata": {"chainladder_sample": "RAA"},
                "run_config": {
                    "method": "chainladder",
                    "required_artifacts": [
                        "case_input",
                        "deterministic_result",
                        "narrative_draft",
                        "constitution_check",
                        "run_manifest",
                    ],
                    **({"review_thresholds": review_thresholds} if review_thresholds else {}),
                },
            },
        },
    )


def test_build_workflow_manager_agent_uses_sdk_agent_and_structured_output(monkeypatch, tmp_path):
    _install_fake_agents_module(monkeypatch)
    agents_module = _load_module("openai_agents_agents_test", OPENAI_RUNTIME_DIR / "agents.py")
    task = _make_task(tmp_path)

    workflow_agent = agents_module.build_workflow_manager_agent(task)

    assert workflow_agent.name == "Workflow Manager Agent"
    assert workflow_agent.model is not None
    assert workflow_agent.tools
    assert workflow_agent.output_type.__name__ == "GovernedCaseSummary"


def test_openai_case_worker_tool_returns_worker_result_payload(monkeypatch, tmp_path):
    _install_fake_agents_module(monkeypatch)
    tools_module = _load_module("openai_agents_tools_test", OPENAI_RUNTIME_DIR / "tools.py")
    task = _make_task(tmp_path)

    tool = tools_module.build_openai_case_worker_tool(task)
    result = tool()

    assert result["status"] == "completed"
    assert result["case_id"] == "openai-runtime-case"
    assert result["deterministic_result"]["reserve_summary"]["ibnr"] >= 0


def test_run_openai_governed_workflow_returns_structured_summary_and_trace_config(monkeypatch, tmp_path):
    _install_fake_agents_module(monkeypatch)
    runner_module = _load_module("openai_agents_runner_test", OPENAI_RUNTIME_DIR / "runner.py")
    task = _make_task(tmp_path)

    result = runner_module.run_openai_governed_workflow(task)

    assert result["route"]["mode"] == "governed"
    assert result["final_output"]["case_id"] == "openai-runtime-case"
    assert result["final_output"]["deterministic_method"] == "chainladder"
    assert "ibnr" in result["final_output"]["cited_values"]
    assert result["trace"]["workflow_name"] == "ai-actuary-governed-workflow"


def test_run_openai_governed_workflow_keeps_numeric_truth_on_review_case(monkeypatch, tmp_path):
    _install_fake_agents_module(monkeypatch)
    runner_module = _load_module("openai_agents_runner_review_test", OPENAI_RUNTIME_DIR / "runner.py")
    task = _make_task(tmp_path, review_thresholds={"origin_count": 5})

    result = runner_module.run_openai_governed_workflow(task)

    assert result["worker_result"]["status"] == "needs_review"
    assert result["final_output"]["review_reasons"]
    assert result["final_output"]["cited_values"] == result["worker_result"]["deterministic_result"]["reserve_summary"]


def test_build_openai_run_config_allows_tracing_toggle(monkeypatch):
    _install_fake_agents_module(monkeypatch)
    config_module = _load_module("openai_agents_config_test", OPENAI_RUNTIME_DIR / "config.py")

    run_config = config_module.build_openai_run_config(
        workflow_name="ai-actuary-governed-workflow",
        tracing_disabled=True,
        trace_metadata={"task_id": "task-123"},
    )

    assert run_config.kwargs["workflow_name"] == "ai-actuary-governed-workflow"
    assert run_config.kwargs["tracing_disabled"] is True
    assert run_config.kwargs["trace_metadata"]["task_id"] == "task-123"
