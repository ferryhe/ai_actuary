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
    review_delivery_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
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
    run_id = getattr(task, "run_id", None) or f"{getattr(task, 'task_id', 'task')}-local"
    if getattr(task, "run_id", None) is None:
        setattr(task, "run_id", run_id)
    runner = runner_module or _load_runner_module()
    if registry_path is not None:
        _record_registry_event(
            registry_path=registry_path,
            task=task,
            run_id=run_id,
            status="queued",
            artifact_dir=artifact_dir,
            objective=objective,
            sample_name=sample_name,
            method=method,
            review_threshold_origin_count=review_threshold_origin_count,
            user_prompt=user_prompt,
            review_delivery_dir=review_delivery_dir,
            summary=f"Queued operator run for {case_id}",
        )
        _record_registry_event(
            registry_path=registry_path,
            task=task,
            run_id=run_id,
            status="running",
            artifact_dir=artifact_dir,
            objective=objective,
            sample_name=sample_name,
            method=method,
            review_threshold_origin_count=review_threshold_origin_count,
            user_prompt=user_prompt,
            review_delivery_dir=review_delivery_dir,
            summary=f"Running operator run for {case_id}",
        )
    try:
        raw_result = runner.run_openai_governed_workflow(task, user_prompt=user_prompt)
    except Exception as exc:
        failure_result = _build_operator_failure_result(task, exc)
        if registry_path is not None:
            _record_registry_final_result(registry_path, task, artifact_dir, failure_result)
        return failure_result
    normalized = _normalize_operator_result(task, raw_result)
    if review_delivery_dir is not None and normalized.get("status") == "needs_review" and normalized.get("review_packet"):
        try:
            delivery_module = _load_review_delivery_module()
            normalized["review_delivery"] = delivery_module.deliver_review_packet(
                normalized["review_packet"],
                destination_dir=review_delivery_dir,
            )
        except Exception as exc:
            normalized["review_delivery"] = {
                "ok": False,
                "status": "failed",
                "destination": "local_outbox",
                "destination_dir": str(Path(review_delivery_dir).expanduser().resolve()),
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            normalized.setdefault("errors", []).append(
                f"review_delivery_failed: {type(exc).__name__}: {exc}"
            )
    if registry_path is not None:
        _record_registry_final_result(registry_path, task, artifact_dir, normalized)
    return normalized


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one governed AI Actuary case as an operator.")
    parser.add_argument("--case-id", required=True, help="Logical case identifier.")
    parser.add_argument("--artifact-dir", required=True, help="Directory where run artifacts will be written.")
    parser.add_argument("--objective", default="Operator-triggered governed workflow run", help="Human-readable run objective.")
    parser.add_argument("--sample-name", default="RAA", help="chainladder sample name for the deterministic worker.")
    parser.add_argument("--method", default="chainladder", help="Deterministic reserving method.")
    parser.add_argument("--review-threshold-origin-count", type=int, default=None, help="Optional origin_count threshold to intentionally trigger review.")
    parser.add_argument("--user-prompt", default=None, help="Optional custom prompt for the OpenAI workflow manager.")
    parser.add_argument("--review-delivery-dir", default=None, help="Optional directory where generated review packets should be copied as operator-facing outbox artifacts.")
    parser.add_argument("--registry-path", default=None, help="Optional JSON registry path used to record queued/running/final single-case run states.")
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
        review_delivery_dir=args.review_delivery_dir,
        registry_path=args.registry_path,
    )


def rerun_from_registry(
    run_id: str,
    *,
    registry_path: str | Path,
    artifact_dir: str | Path | None = None,
    review_delivery_dir: str | Path | None = None,
    runner_module=None,
    task_contracts_module=None,
):
    registry_module = _load_run_registry_module()
    entry = registry_module.get_run(registry_path, run_id)
    operator_params = dict(entry.get("operator_params", {}) or {})
    if not operator_params:
        raise ValueError(f"Run registry entry is missing operator_params for rerun: {run_id}")
    if artifact_dir is not None:
        operator_params["artifact_dir"] = str(artifact_dir)
    if review_delivery_dir is not None:
        operator_params["review_delivery_dir"] = str(review_delivery_dir)
    operator_params["registry_path"] = str(registry_path)
    if runner_module is not None:
        operator_params["runner_module"] = runner_module
    if task_contracts_module is not None:
        operator_params["task_contracts_module"] = task_contracts_module
    return run_operator_flow(**operator_params)


def _normalize_operator_result(task: Any, raw_result: dict[str, Any]) -> dict[str, Any]:
    worker_result = dict(raw_result.get("worker_result", {}) or {})
    final_output = dict(raw_result.get("final_output", {}) or {})
    status = worker_result.get("status") or final_output.get("worker_status") or "failed"
    review_packet = raw_result.get("review_packet") if status == "needs_review" else None
    summary = worker_result.get("summary") or final_output.get("narrative_summary") or f"Operator run for {task.case_ref} finished with status {status}."
    run_id = worker_result.get("run_id") or getattr(task, "run_id", None) or f"{getattr(task, 'task_id', 'task')}-local"
    response = {
        "ok": status != "failed",
        "status": status,
        "case_id": worker_result.get("case_id") or final_output.get("case_id") or getattr(task, "case_ref", None),
        "run_id": run_id,
        "summary": summary,
        "route": raw_result.get("route", {}),
        "trace": raw_result.get("trace", {}),
        "worker_result": worker_result,
        "final_output": final_output,
        "errors": list(worker_result.get("errors", []) or []),
        "error_category": None,
    }
    if review_packet is not None:
        response["review_packet"] = review_packet
    return response


def _build_operator_failure_result(task: Any, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "case_id": getattr(task, "case_ref", None),
        "run_id": getattr(task, "run_id", None),
        "summary": f"Operator run failed for {getattr(task, 'case_ref', 'unknown-case')}",
        "route": {},
        "trace": {},
        "worker_result": {
            "task_id": getattr(task, "task_id", None),
            "task_kind": getattr(task, "task_kind", None),
            "case_id": getattr(task, "case_ref", None),
            "run_id": getattr(task, "run_id", None),
            "status": "failed",
            "summary": f"Planner runtime failed for {getattr(task, 'case_ref', 'unknown-case')}",
            "artifact_paths": {},
            "metrics": {},
            "review_reasons": [],
            "errors": [str(exc)],
            "deterministic_result": {},
            "narrative_draft": {},
            "constitution_check": {},
            "artifact_manifest": {},
            "worker_metadata": {
                "adapter": "operator-entrypoint",
                "failure_category": "planner_runtime",
                "failure_stage": "planner_runtime",
                "error_type": type(exc).__name__,
            },
        },
        "final_output": {},
        "errors": [str(exc)],
        "error_category": "planner_runtime",
    }


def _load_task_contracts_module():
    return _load_module(
        "operator_task_contracts",
        _workflow_source_path("agent-runtimes", "hermes-worker", "task_contracts.py"),
    )


def _load_runner_module():
    return _load_module(
        "operator_openai_runner",
        _workflow_source_path("agent-runtimes", "openai-agents", "runner.py"),
    )


def _load_review_delivery_module():
    return _load_module(
        "operator_review_delivery",
        Path(__file__).resolve().parent / "review" / "delivery.py",
    )


def _load_run_registry_module():
    return _load_module(
        "operator_run_registry",
        Path(__file__).resolve().parent / "runtime" / "run_registry.py",
    )


def _record_registry_event(
    *,
    registry_path: str | Path,
    task: Any,
    run_id: str,
    status: str,
    artifact_dir: str | Path,
    objective: str,
    sample_name: str,
    method: str,
    review_threshold_origin_count: int | None,
    user_prompt: str | None,
    review_delivery_dir: str | Path | None,
    summary: str,
) -> dict[str, Any]:
    registry_module = _load_run_registry_module()
    operator_params = {
        "case_id": getattr(task, "case_ref", None),
        "artifact_dir": str(artifact_dir),
        "objective": objective,
        "sample_name": sample_name,
        "method": method,
        "review_threshold_origin_count": review_threshold_origin_count,
        "user_prompt": user_prompt,
        "review_delivery_dir": str(review_delivery_dir) if review_delivery_dir is not None else None,
    }
    return registry_module.record_run_event(
        registry_path=registry_path,
        task_id=getattr(task, "task_id", "unknown-task"),
        case_id=getattr(task, "case_ref", None),
        run_id=run_id,
        status=status,
        artifact_root=str(Path(artifact_dir).expanduser().resolve()),
        summary=summary,
        operator_params=operator_params,
        review_required=status == "needs_review",
    )


def _record_registry_final_result(registry_path: str | Path, task: Any, artifact_dir: str | Path, result: dict[str, Any]) -> dict[str, Any]:
    registry_module = _load_run_registry_module()
    return registry_module.record_run_event(
        registry_path=registry_path,
        task_id=getattr(task, "task_id", "unknown-task"),
        case_id=result.get("case_id") or getattr(task, "case_ref", None),
        run_id=result.get("run_id") or f"{getattr(task, 'task_id', 'task')}-local",
        status=result.get("status", "failed"),
        artifact_root=str(Path(artifact_dir).expanduser().resolve()),
        summary=result.get("summary"),
        review_required=result.get("status") == "needs_review",
        error_category=result.get("error_category"),
        errors=list(result.get("errors", []) or []),
        review_delivery=result.get("review_delivery"),
    )


def _workflow_source_path(*relative_parts: str) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root.joinpath("workflows", *relative_parts)
    if not path.is_file():
        raise FileNotFoundError(
            "Required workflow source file is not available at "
            f"{path}. This operator entrypoint loads workflow modules from the repository's "
            "workflows/ directory, which may be missing in an installed package. Run from a "
            "repository checkout or install the project in editable mode so workflows/ is present."
        )
    return path


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
