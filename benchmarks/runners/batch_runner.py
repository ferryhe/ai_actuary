"""Benchmark batch runner for baseline and governed workflow comparisons."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

from reserving_workflow.evaluation import resolve_case_definition
from reserving_workflow.runtime import run_registry
from reserving_workflow.schemas import RunArtifactManifest

MODES = ("baseline_prompt", "governed_workflow")


def run_batch_benchmark(
    *,
    cases: list[dict[str, Any]],
    artifact_root: str | Path,
    modes: tuple[str, ...] = MODES,
    governed_runner: Callable[..., dict[str, Any]] | None = None,
    registry_path: str | Path | None = None,
    case_pack_id: str | None = None,
) -> dict[str, Any]:
    root = Path(artifact_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    resolved_registry_path = Path(registry_path).expanduser().resolve() if registry_path is not None else root / "run-registry.json"

    operator_entrypoint = _load_repo_module(
        "batch_operator_entrypoint",
        Path("src/reserving_workflow/operator_entrypoint.py"),
    )
    case_worker_module = _load_repo_module(
        "batch_case_worker",
        Path("workflows/agent-runtimes/hermes-worker/case_worker.py"),
    )
    comparison_module = _load_repo_module(
        "batch_comparison",
        Path("src/reserving_workflow/evaluation/comparison.py"),
    )
    governed_runner_fn = governed_runner or _load_repo_module(
        "batch_governed_runner",
        Path("workflows/agent-runtimes/openai-agents/runner.py"),
    ).run_openai_governed_workflow

    mode_results: dict[str, list[dict[str, Any]]] = {mode: [] for mode in modes}
    mode_artifact_manifests: dict[str, list[str]] = {mode: [] for mode in modes}
    resolved_cases = [resolve_case_definition(case) for case in cases]
    case_pack_path = root / "case_pack_resolved.json"
    batch_manifest_path = root / "batch_manifest.json"
    case_pack_path.write_text(
        json.dumps(
            {
                "case_pack_id": case_pack_id,
                "case_count": len(resolved_cases),
                "cases": resolved_cases,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    for case in resolved_cases:
        case_id = str(case["case_id"])
        sample_name = str(case.get("sample_name", "RAA"))
        review_threshold_origin_count = case.get("review_threshold_origin_count")
        for mode in modes:
            artifact_dir = root / mode / case_id
            manifest_path = None
            try:
                task = operator_entrypoint.build_operator_task(
                    case_id=case_id,
                    artifact_dir=artifact_dir,
                    objective=f"Batch benchmark run via {mode}",
                    sample_name=sample_name,
                    review_threshold_origin_count=review_threshold_origin_count,
                    case_payload=case.get("case_payload"),
                )
                task.run_id = _benchmark_run_id(mode=mode, case_id=case_id)
                task.task_id = f"benchmark-{mode}-{case_id}"
                if mode == "baseline_prompt":
                    worker_result = case_worker_module.run_case_worker(task)
                    manifest_path = worker_result.artifact_paths.get("run_manifest")
                    manifest = _load_manifest(manifest_path)
                    result_summary = {
                        "case_id": worker_result.case_id,
                        "run_id": worker_result.run_id,
                        "status": worker_result.status,
                        "reserve_summary": dict(worker_result.deterministic_result.get("reserve_summary", {})),
                        "artifact_manifest_path": manifest_path,
                        "artifact_root": manifest.artifact_root,
                        "review_reasons": list(worker_result.review_reasons),
                        "input_source": _input_source(case),
                    }
                elif mode == "governed_workflow":
                    governed_result = governed_runner_fn(task, user_prompt=None)
                    final_output = governed_result.get("final_output", {}) or {}
                    worker_result_payload = governed_result.get("worker_result", {}) or {}
                    manifest_path = final_output.get("artifact_manifest_path") or worker_result_payload.get("artifact_paths", {}).get("run_manifest")
                    manifest = _load_manifest(manifest_path)
                    result_summary = {
                        "case_id": final_output.get("case_id") or worker_result_payload.get("case_id") or case_id,
                        "run_id": final_output.get("run_id") or worker_result_payload.get("run_id") or manifest.run_id,
                        "status": final_output.get("worker_status") or worker_result_payload.get("status"),
                        "reserve_summary": dict(final_output.get("cited_values", {})),
                        "artifact_manifest_path": manifest_path,
                        "artifact_root": manifest.artifact_root,
                        "review_reasons": list(final_output.get("review_reasons", []) or worker_result_payload.get("review_reasons", [])),
                        "input_source": _input_source(case),
                    }
                else:
                    raise ValueError(f"Unsupported benchmark mode: {mode!r}")
            except Exception as exc:
                result_summary = {
                    "case_id": case_id,
                    "run_id": None,
                    "status": "failed",
                    "reserve_summary": {},
                    "artifact_manifest_path": None,
                    "artifact_root": None,
                    "review_reasons": [],
                    "errors": [f"{type(exc).__name__}: {exc}"],
                    "mode": mode,
                    "input_source": _input_source(case),
                }
                manifest_path = None

            mode_results[mode].append(result_summary)
            if manifest_path:
                mode_artifact_manifests[mode].append(str(manifest_path))
                _record_registry_entry(
                    registry_path=resolved_registry_path,
                    mode=mode,
                    case=case,
                    result_summary=result_summary,
                    case_pack_id=case_pack_id,
                )

    scored = comparison_module.score_batch_mode_results(mode_results)
    report = {
        "case_count": len(resolved_cases),
        "case_pack_id": case_pack_id,
        "modes": list(modes),
        "mode_results": mode_results,
        "mode_artifact_manifests": mode_artifact_manifests,
        "registry_path": str(resolved_registry_path),
        "resolved_case_pack_path": str(case_pack_path),
        **scored,
    }
    comparison_report_path = root / "comparison_report.json"
    report["batch_manifest_path"] = str(batch_manifest_path)
    report["comparison_report_path"] = str(comparison_report_path)
    comparison_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    batch_manifest_path.write_text(
        json.dumps(
            {
                "case_count": report["case_count"],
                "case_pack_id": case_pack_id,
                "comparison_report_path": report["comparison_report_path"],
                "mode_artifact_manifests": mode_artifact_manifests,
                "registry_path": report["registry_path"],
                "resolved_case_pack_path": report["resolved_case_pack_path"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return report



def _load_repo_module(name: str, relative_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_manifest(manifest_path: str | Path | None) -> RunArtifactManifest:
    if manifest_path is None:
        raise ValueError("Missing run manifest path for benchmark result")
    payload = json.loads(Path(manifest_path).expanduser().resolve().read_text(encoding="utf-8"))
    return RunArtifactManifest.model_validate(payload)


def _input_source(case: dict[str, Any]) -> str:
    if case.get("simulation") is not None:
        return "simulation"
    if case.get("case_payload") is not None:
        return "case_payload"
    return "sample_name"


def _benchmark_run_id(*, mode: str, case_id: str) -> str:
    return f"benchmark-{mode}-{case_id}"


def _record_registry_entry(
    *,
    registry_path: Path,
    mode: str,
    case: dict[str, Any],
    result_summary: dict[str, Any],
    case_pack_id: str | None = None,
) -> None:
    artifact_root = result_summary.get("artifact_root")
    run_id = result_summary.get("run_id")
    if not artifact_root or not run_id:
        return
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=f"benchmark-{mode}-{case['case_id']}",
        case_id=str(case["case_id"]),
        run_id=str(run_id),
        status=str(result_summary.get("status") or "failed"),
        artifact_root=str(artifact_root),
        summary=f"Batch benchmark {mode} run for {case['case_id']}",
        operator_params={
            "case_id": case["case_id"],
            "artifact_dir": artifact_root,
            "sample_name": case.get("sample_name"),
            "case_payload": case.get("case_payload"),
            "review_threshold_origin_count": case.get("review_threshold_origin_count"),
            "case_pack_id": case_pack_id or case.get("case_pack_id"),
        },
        created_by="batch-benchmark",
        review_required=str(result_summary.get("status")) == "needs_review",
    )
