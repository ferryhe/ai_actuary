"""Single-case Hermes worker local callable implementation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from reserving_workflow.calculators import ChainladderAdapter, ChainladderAdapterError
from reserving_workflow.constitution import evaluate_case_constitution
from reserving_workflow.schemas import NarrativeDraft, ReservingCaseInput

SUPPORTED_TASK = "run_case"
DEFAULT_RUN_ID_SUFFIX = "local"


def run_case_worker(task: Any):
    if getattr(task, "task_kind", None) != SUPPORTED_TASK:
        raise ValueError(f"Unsupported task_kind for case worker: {getattr(task, 'task_kind', None)!r}")

    task_contracts = _load_sibling_module("task_contracts", "task_contracts.py")
    artifact_packager = _load_sibling_module("artifact_packager", "artifact_packager.py")
    WorkerResult = task_contracts.WorkerResult

    run_id = getattr(task, "run_id", None) or f"{getattr(task, 'task_id', 'task')}-{DEFAULT_RUN_ID_SUFFIX}"
    case_id = getattr(task, "case_ref", None)

    try:
        inputs = getattr(task, "inputs", {}) or {}
        case_payload = inputs["case_payload"]
        case_input = ReservingCaseInput.model_validate(case_payload)
        case_id = case_input.case_id
        artifact_dir = _resolve_artifact_dir(task=task, case_input=case_input, run_id=run_id)
        required_artifacts = _merge_required_artifacts(task=task, case_input=case_input)

        manifest = artifact_packager.build_run_artifact_manifest(
            case_id=case_input.case_id,
            run_id=run_id,
            artifact_dir=artifact_dir,
            required_artifacts=required_artifacts,
            created_by="local-case-worker",
            metadata={
                "task_id": task.task_id,
                "task_kind": task.task_kind,
                "objective": task.objective,
                "adapter": "local-callable",
            },
        )
        deterministic_result = ChainladderAdapter().calculate(case_input)
        narrative_draft = _build_narrative_draft(case_input, deterministic_result)
        constitution_check = evaluate_case_constitution(
            case_input,
            deterministic_result,
            narrative_draft,
            manifest,
        )

        artifact_packager.write_artifacts(
            manifest,
            {
                "case_input": case_input,
                "deterministic_result": deterministic_result,
                "narrative_draft": narrative_draft,
                "constitution_check": constitution_check,
            },
        )

        status = _map_worker_status(constitution_check.status)
        review_reasons = list(constitution_check.review_triggers)
        errors = list(constitution_check.hard_constraints) if constitution_check.status == "fail" else []
        summary = _build_summary(case_input.case_id, deterministic_result.method, constitution_check.status)

        return WorkerResult(
            task_id=task.task_id,
            task_kind=task.task_kind,
            case_id=case_input.case_id,
            run_id=run_id,
            status=status,
            summary=summary,
            artifact_paths=manifest.artifact_paths,
            metrics={
                "artifact_count": len(manifest.artifact_paths),
                "review_trigger_count": len(constitution_check.review_triggers),
                "hard_constraint_count": len(constitution_check.hard_constraints),
            },
            review_reasons=review_reasons,
            errors=errors,
            deterministic_result=deterministic_result.model_dump(mode="json"),
            narrative_draft=narrative_draft.model_dump(mode="json"),
            constitution_check=constitution_check.model_dump(mode="json"),
            artifact_manifest=manifest.model_dump(mode="json"),
            worker_metadata={
                "adapter": "local-callable",
                "calculator_backend": deterministic_result.metadata.get("backend"),
                "artifact_dir": str(artifact_dir),
            },
        )
    except (ChainladderAdapterError, KeyError, ValidationError, ValueError) as exc:
        return WorkerResult(
            task_id=getattr(task, "task_id", "unknown-task"),
            task_kind=getattr(task, "task_kind", SUPPORTED_TASK),
            case_id=case_id,
            run_id=run_id,
            status="failed",
            summary=f"Case worker failed for task {getattr(task, 'task_id', 'unknown-task')}",
            errors=[str(exc)],
            worker_metadata={
                "adapter": "local-callable",
                "failure_category": _classify_failure_category(exc),
                "failure_stage": "worker_input" if isinstance(exc, (KeyError, ValidationError, ValueError)) else "deterministic_engine",
                "error_type": type(exc).__name__,
            },
        )


def _classify_failure_category(exc: Exception) -> str:
    if isinstance(exc, ChainladderAdapterError):
        return "deterministic_engine"
    if isinstance(exc, (KeyError, ValidationError, ValueError)):
        return "input_validation"
    return "worker_runtime"


def _resolve_artifact_dir(*, task: Any, case_input: ReservingCaseInput, run_id: str) -> Path:
    inputs = getattr(task, "inputs", {}) or {}
    configured = inputs.get("artifact_dir") or getattr(task, "artifact_root", None)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / "artifacts" / case_input.case_id / run_id).resolve()


def _merge_required_artifacts(*, task: Any, case_input: ReservingCaseInput) -> list[str]:
    artifact_names: list[str] = []
    for name in [
        *((getattr(task, "required_artifacts", None) or [])),
        *((case_input.run_config.get("required_artifacts", []) or [])),
    ]:
        if name not in artifact_names:
            artifact_names.append(name)
    return artifact_names


def _build_narrative_draft(case_input: ReservingCaseInput, deterministic_result) -> NarrativeDraft:
    reserve_summary = deterministic_result.reserve_summary or {}
    method = deterministic_result.method
    ultimate = reserve_summary.get("ultimate")
    ibnr = reserve_summary.get("ibnr")
    latest_diagonal = reserve_summary.get("latest_diagonal")

    key_points = [
        f"Deterministic method: {method}",
        f"Case id: {case_input.case_id}",
    ]
    diagnostics = deterministic_result.diagnostics or {}
    if "origin_count" in diagnostics:
        key_points.append(f"Origin periods: {diagnostics['origin_count']}")
    if "development_count" in diagnostics:
        key_points.append(f"Development periods: {diagnostics['development_count']}")

    summary = (
        f"Deterministic {method} run completed for {case_input.case_id}. "
        f"Latest diagonal={latest_diagonal}, ultimate={ultimate}, ibnr={ibnr}."
    )
    return NarrativeDraft(
        case_id=case_input.case_id,
        summary=summary,
        key_points=key_points,
        cited_values={name: float(value) for name, value in reserve_summary.items()},
    )


def _map_worker_status(constitution_status: str) -> str:
    return {
        "pass": "completed",
        "review_required": "needs_review",
        "fail": "failed",
    }.get(constitution_status, "failed")


def _build_summary(case_id: str, method: str, constitution_status: str) -> str:
    if constitution_status == "pass":
        return f"Case {case_id} completed via {method} with constitution pass."
    if constitution_status == "review_required":
        return f"Case {case_id} completed via {method} and requires review."
    return f"Case {case_id} failed constitution checks after {method} run."


def _load_sibling_module(name: str, filename: str):
    module_path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module
