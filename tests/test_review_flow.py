from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_WORKER_DIR = REPO_ROOT / "workflows" / "agent-runtimes" / "hermes-worker"
OPENAI_RUNTIME_DIR = REPO_ROOT / "workflows" / "agent-runtimes" / "openai-agents"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_review_worker_result(tmp_path: Path):
    task_contracts = _load_module("review_task_contracts", HERMES_WORKER_DIR / "task_contracts.py")
    artifact_paths = {
        "case_input": str(tmp_path / "case_input.json"),
        "deterministic_result": str(tmp_path / "deterministic_result.json"),
        "narrative_draft": str(tmp_path / "narrative_draft.json"),
        "constitution_check": str(tmp_path / "constitution_check.json"),
        "run_manifest": str(tmp_path / "run_manifest.json"),
    }
    for name, path in artifact_paths.items():
        Path(path).write_text(json.dumps({"artifact": name}), encoding="utf-8")
    return task_contracts.WorkerResult(
        task_id="review-task-001",
        task_kind="run_case",
        case_id="review-case-001",
        run_id="review-run-001",
        status="needs_review",
        summary="Review required for case",
        artifact_paths=artifact_paths,
        review_reasons=["diagnostic_threshold:origin_count:value=10.0:threshold=5.0"],
        deterministic_result={
            "case_id": "review-case-001",
            "method": "chainladder",
            "reserve_summary": {"ibnr": 12.0, "ultimate": 100.0},
        },
        narrative_draft={
            "case_id": "review-case-001",
            "summary": "Draft summary",
            "key_points": ["Review needed"],
        },
        constitution_check={
            "case_id": "review-case-001",
            "status": "review_required",
            "hard_constraints": [],
            "review_triggers": ["diagnostic_threshold:origin_count:value=10.0:threshold=5.0"],
        },
        artifact_manifest={
            "case_id": "review-case-001",
            "run_id": "review-run-001",
            "artifact_paths": artifact_paths,
        },
    )


def test_review_worker_builds_json_and_markdown_packets(tmp_path):
    review_worker = _load_module("review_worker_test", HERMES_WORKER_DIR / "review_worker.py")
    worker_result = _make_review_worker_result(tmp_path)

    packet = review_worker.build_review_packet(worker_result, output_dir=tmp_path)

    assert packet["case_id"] == "review-case-001"
    assert packet["status"] == "review_required"
    assert packet["packet_paths"]["json"].endswith("review_packet.json")
    assert packet["packet_paths"]["markdown"].endswith("review_packet.md")
    assert Path(packet["packet_paths"]["json"]).exists()
    assert Path(packet["packet_paths"]["markdown"]).exists()


def test_openai_runner_adds_review_packet_when_worker_needs_review(tmp_path):
    runner_module = _load_module("runner_review_flow", OPENAI_RUNTIME_DIR / "runner.py")

    class FakeRoute:
        mode = "governed"
        worker_action = "run_case_worker"
        review_required = False
        def to_dict(self):
            return {"mode": self.mode, "worker_action": self.worker_action, "review_required": self.review_required}

    class FakeRoutingModule:
        @staticmethod
        def route_case_task(task):
            return FakeRoute()

    class FakeConfigModule:
        DEFAULT_PLANNER_CONFIG = {"workflow_name": "wf", "tracing_disabled": False}
        @staticmethod
        def build_openai_run_config(**kwargs):
            class RC:
                def __init__(self, kwargs):
                    self.kwargs = kwargs
            return RC(kwargs)

    class FakeAgent:
        def __init__(self):
            self.name = "Workflow Manager Agent"
            self.model = "fake-model"
            self.tools = [type("Tool", (), {"_tool_state": {"last_result": _make_review_worker_result(tmp_path).model_dump(mode='json')}})()]

    class FakeAgentsFile:
        @staticmethod
        def build_workflow_manager_agent(task, agents_module=None):
            return FakeAgent()

    class FakeSDK:
        class Runner:
            @staticmethod
            def run_sync(agent, prompt, run_config=None):
                return type("Result", (), {"final_output": type("Final", (), {"model_dump": lambda self, mode='json': {
                    "case_id": "review-case-001",
                    "worker_status": "needs_review",
                    "deterministic_method": "chainladder",
                    "cited_values": {"ibnr": 12.0, "ultimate": 100.0},
                    "review_reasons": ["diagnostic_threshold:origin_count:value=10.0:threshold=5.0"],
                    "artifact_manifest_path": str(tmp_path / 'run_manifest.json'),
                    "narrative_summary": "Draft summary",
                }})()})()

    review_worker_module = _load_module("review_worker_runtime", HERMES_WORKER_DIR / "review_worker.py")

    original_loader = runner_module._load_sibling_module
    def fake_loader(filename: str, module_name: str):
        if filename == "routing.py":
            return FakeRoutingModule
        if filename == "config.py":
            return FakeConfigModule
        if filename == "agents.py":
            return FakeAgentsFile
        if filename == "tools.py":
            return object()
        raise AssertionError(filename)
    runner_module._load_sibling_module = fake_loader
    runner_module._load_review_worker_module = lambda: review_worker_module
    runner_module._import_agents_sdk = lambda: FakeSDK
    try:
        task_contracts = _load_module("review_task_contracts_runtime", HERMES_WORKER_DIR / "task_contracts.py")
        task = task_contracts.WorkerTask(
            task_id="review-flow-001",
            task_kind="run_case",
            case_ref="review-case-001",
            objective="Review flow",
            inputs={"artifact_dir": str(tmp_path)},
        )
        result = runner_module.run_openai_governed_workflow(task)
    finally:
        runner_module._load_sibling_module = original_loader

    assert result["worker_result"]["status"] == "needs_review"
    assert result["review_packet"]["status"] == "review_required"
    assert Path(result["review_packet"]["packet_paths"]["json"]).exists()
