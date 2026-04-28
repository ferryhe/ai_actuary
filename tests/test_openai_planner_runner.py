from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module(name: str, relative_path: str):
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_run_case_task(*, task_id: str, case_id: str, artifact_dir: Path, review_thresholds: dict | None = None):
    task_contracts = _load_module(
        "task_contracts_for_planner_tests",
        "workflows/agent-runtimes/hermes-worker/task_contracts.py",
    )
    return task_contracts.WorkerTask(
        task_id=task_id,
        task_kind="run_case",
        case_ref=case_id,
        objective="Planner dispatch run case",
        inputs={
            "artifact_dir": str(artifact_dir),
            "case_payload": {
                "case_id": case_id,
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


def test_route_case_task_selects_governed_by_default(tmp_path):
    routing = _load_module(
        "routing_module",
        "workflows/agent-runtimes/openai-agents/routing.py",
    )
    task = _make_run_case_task(
        task_id="route-governed-001",
        case_id="case-governed",
        artifact_dir=tmp_path / "route-governed",
    )

    route = routing.route_case_task(task)

    assert route.mode == "governed"
    assert route.worker_action == "run_case_worker"
    assert route.review_required is False


def test_route_case_task_supports_baseline_and_review_only(tmp_path):
    routing = _load_module(
        "routing_module_review",
        "workflows/agent-runtimes/openai-agents/routing.py",
    )
    baseline_task = _make_run_case_task(
        task_id="route-baseline-001",
        case_id="case-baseline",
        artifact_dir=tmp_path / "route-baseline",
    )
    baseline_task.inputs["mode"] = "baseline"

    review_task = _make_run_case_task(
        task_id="route-review-001",
        case_id="case-review",
        artifact_dir=tmp_path / "route-review",
    )
    review_task.inputs["mode"] = "review_only"

    baseline_route = routing.route_case_task(baseline_task)
    review_route = routing.route_case_task(review_task)

    assert baseline_route.mode == "baseline"
    assert baseline_route.worker_action == "run_case_worker"
    assert review_route.mode == "review_only"
    assert review_route.review_required is True


def test_run_case_worker_tool_invokes_worker_and_returns_result(tmp_path):
    tool_module = _load_module(
        "tools_module",
        "workflows/agent-runtimes/openai-agents/tools.py",
    )
    task = _make_run_case_task(
        task_id="tool-001",
        case_id="tool-case",
        artifact_dir=tmp_path / "tool-case",
    )

    result = tool_module.run_case_worker_tool(task)

    assert result.status == "completed"
    assert result.case_id == "tool-case"
    assert result.constitution_check["status"] == "pass"


def test_planner_runner_executes_intake_route_dispatch_collect(tmp_path):
    runner_module = _load_module(
        "runner_module",
        "workflows/agent-runtimes/openai-agents/runner.py",
    )
    task = _make_run_case_task(
        task_id="runner-001",
        case_id="runner-case",
        artifact_dir=tmp_path / "runner-case",
    )

    workflow_result = runner_module.run_planner_workflow(task)

    assert workflow_result["stage"] == "collect"
    assert workflow_result["route"]["mode"] == "governed"
    assert workflow_result["worker_result"]["status"] == "completed"
    assert workflow_result["worker_result"]["case_id"] == "runner-case"


def test_planner_runner_keeps_planner_worker_boundary_for_review_case(tmp_path):
    runner_module = _load_module(
        "runner_module_review",
        "workflows/agent-runtimes/openai-agents/runner.py",
    )
    task = _make_run_case_task(
        task_id="runner-review-001",
        case_id="runner-review-case",
        artifact_dir=tmp_path / "runner-review-case",
        review_thresholds={"origin_count": 5},
    )

    workflow_result = runner_module.run_planner_workflow(task)

    assert workflow_result["route"]["mode"] == "governed"
    assert workflow_result["worker_result"]["status"] == "needs_review"
    assert workflow_result["worker_result"]["constitution_check"]["status"] == "review_required"
    assert workflow_result["planner_summary"].startswith("Planner collected worker result")
