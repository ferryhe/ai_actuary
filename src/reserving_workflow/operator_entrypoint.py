"""Operator-facing entrypoint helpers for running the governed OpenAI workflow."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any

DEFAULT_REQUIRED_ARTIFACTS = [
    "case_input",
    "deterministic_result",
    "narrative_draft",
    "constitution_check",
    "run_manifest",
]


def build_operator_task(
    *,
    case_id: str,
    artifact_dir: str | Path,
    objective: str,
    sample_name: str = "RAA",
    method: str = "chainladder",
    review_threshold_origin_count: int | None = None,
    task_contracts_module=None,
):
    task_contracts = task_contracts_module or _load_task_contracts_module()
    run_config: dict[str, Any] = {
        "method": method,
        "required_artifacts": list(DEFAULT_REQUIRED_ARTIFACTS),
    }
    if review_threshold_origin_count is not None:
        run_config["review_thresholds"] = {"origin_count": review_threshold_origin_count}

    return task_contracts.WorkerTask(
        task_id=f"operator-{case_id}",
        task_kind="run_case",
        case_ref=case_id,
        objective=objective,
        inputs={
            "artifact_dir": str(Path(artifact_dir)),
            "case_payload": {
                "case_id": case_id,
                "metadata": {"chainladder_sample": sample_name},
                "run_config": run_config,
            },
        },
        required_artifacts=list(DEFAULT_REQUIRED_ARTIFACTS),
    )


def run_operator_flow(
    *,
    case_id: str,
    artifact_dir: str | Path,
    objective: str,
    sample_name: str = "RAA",
    method: str = "chainladder",
    review_threshold_origin_count: int | None = None,
    user_prompt: str | None = None,
    runner_module=None,
    task_contracts_module=None,
):
    task = build_operator_task(
        case_id=case_id,
        artifact_dir=artifact_dir,
        objective=objective,
        sample_name=sample_name,
        method=method,
        review_threshold_origin_count=review_threshold_origin_count,
        task_contracts_module=task_contracts_module,
    )
    runner = runner_module or _load_runner_module()
    return runner.run_openai_governed_workflow(task, user_prompt=user_prompt)


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one governed AI Actuary case as an operator.")
    parser.add_argument("--case-id", required=True, help="Logical case identifier.")
    parser.add_argument("--artifact-dir", required=True, help="Directory where run artifacts will be written.")
    parser.add_argument("--objective", default="Operator-triggered governed workflow run", help="Human-readable run objective.")
    parser.add_argument("--sample-name", default="RAA", help="chainladder sample name for the deterministic worker.")
    parser.add_argument("--method", default="chainladder", help="Deterministic reserving method.")
    parser.add_argument("--review-threshold-origin-count", type=int, default=None, help="Optional origin_count threshold to intentionally trigger review.")
    parser.add_argument("--user-prompt", default=None, help="Optional custom prompt for the OpenAI workflow manager.")
    return parser


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    return run_operator_flow(
        case_id=args.case_id,
        artifact_dir=args.artifact_dir,
        objective=args.objective,
        sample_name=args.sample_name,
        method=args.method,
        review_threshold_origin_count=args.review_threshold_origin_count,
        user_prompt=args.user_prompt,
    )


def _load_task_contracts_module():
    return _load_module(
        "operator_task_contracts",
        Path(__file__).resolve().parents[2] / "workflows" / "agent-runtimes" / "hermes-worker" / "task_contracts.py",
    )


def _load_runner_module():
    return _load_module(
        "operator_openai_runner",
        Path(__file__).resolve().parents[2] / "workflows" / "agent-runtimes" / "openai-agents" / "runner.py",
    )


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
