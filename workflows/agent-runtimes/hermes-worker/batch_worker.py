"""Batch Hermes worker implementation for Prompt 8 benchmark runs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

SUPPORTED_TASK = "run_batch"


def run_batch_worker(task: Any, *, batch_runner_module=None):
    if getattr(task, "task_kind", None) != SUPPORTED_TASK:
        raise ValueError(f"Unsupported task_kind for batch worker: {getattr(task, 'task_kind', None)!r}")

    task_contracts = _load_sibling_module("batch_task_contracts", "task_contracts.py")
    WorkerResult = task_contracts.WorkerResult
    runner_module = batch_runner_module or _load_repo_module(
        "benchmark_batch_runner",
        Path("benchmarks/runners/batch_runner.py"),
    )

    inputs = getattr(task, "inputs", {}) or {}
    artifact_root = inputs.get("artifact_root") or getattr(task, "artifact_root", None) or (Path.cwd() / "artifacts" / "batch-runs")
    try:
        report = runner_module.run_batch_benchmark(
            cases=list(inputs.get("cases", [])),
            artifact_root=artifact_root,
        )
    except Exception as exc:
        return WorkerResult(
            task_id=getattr(task, "task_id", "unknown-batch-task"),
            task_kind=SUPPORTED_TASK,
            case_id=None,
            run_id=getattr(task, "run_id", None),
            status="failed",
            summary="Batch benchmark failed.",
            errors=[str(exc)],
            artifact_paths={"artifact_root": str(artifact_root)},
            metrics={},
            artifact_manifest={},
            worker_metadata={"adapter": "local-callable-batch"},
        )
    return WorkerResult(
        task_id=getattr(task, "task_id", "unknown-batch-task"),
        task_kind=SUPPORTED_TASK,
        case_id=None,
        run_id=getattr(task, "run_id", None),
        status="completed",
        summary=f"Batch benchmark completed for {report['case_count']} cases across {len(report['modes'])} modes.",
        artifact_paths={"comparison_report": report["comparison_report_path"]},
        metrics={
            "case_count": report["case_count"],
            "mode_count": len(report["modes"]),
        },
        artifact_manifest={
            "mode_artifact_manifests": report.get("mode_artifact_manifests", {}),
        },
        worker_metadata={"adapter": "local-callable-batch"},
    )



def _load_sibling_module(name: str, filename: str):
    module_path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _load_repo_module(name: str, relative_path: Path):
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
