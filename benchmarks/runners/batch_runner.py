"""Benchmark batch runner for baseline and governed workflow comparisons."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

MODES = ("baseline_prompt", "governed_workflow")


def run_batch_benchmark(
    *,
    cases: list[dict[str, Any]],
    artifact_root: str | Path,
    modes: tuple[str, ...] = MODES,
    governed_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(artifact_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

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

    for case in cases:
        case_id = str(case["case_id"])
        sample_name = str(case.get("sample_name", "RAA"))
        review_threshold_origin_count = case.get("review_threshold_origin_count")
        for mode in modes:
            artifact_dir = root / mode / case_id
            task = operator_entrypoint.build_operator_task(
                case_id=case_id,
                artifact_dir=artifact_dir,
                objective=f"Batch benchmark run via {mode}",
                sample_name=sample_name,
                review_threshold_origin_count=review_threshold_origin_count,
            )
            if mode == "baseline_prompt":
                worker_result = case_worker_module.run_case_worker(task)
                result_summary = {
                    "case_id": worker_result.case_id,
                    "status": worker_result.status,
                    "reserve_summary": dict(worker_result.deterministic_result.get("reserve_summary", {})),
                    "artifact_manifest_path": worker_result.artifact_paths.get("run_manifest"),
                    "review_reasons": list(worker_result.review_reasons),
                }
                manifest_path = worker_result.artifact_paths.get("run_manifest")
            elif mode == "governed_workflow":
                governed_result = governed_runner_fn(task, user_prompt=None)
                final_output = governed_result.get("final_output", {}) or {}
                worker_result_payload = governed_result.get("worker_result", {}) or {}
                result_summary = {
                    "case_id": final_output.get("case_id") or worker_result_payload.get("case_id") or case_id,
                    "status": final_output.get("worker_status") or worker_result_payload.get("status"),
                    "reserve_summary": dict(final_output.get("cited_values", {})),
                    "artifact_manifest_path": final_output.get("artifact_manifest_path")
                    or worker_result_payload.get("artifact_paths", {}).get("run_manifest"),
                    "review_reasons": list(final_output.get("review_reasons", []) or worker_result_payload.get("review_reasons", [])),
                }
                manifest_path = result_summary["artifact_manifest_path"]
            else:
                raise ValueError(f"Unsupported benchmark mode: {mode!r}")

            mode_results[mode].append(result_summary)
            if manifest_path:
                mode_artifact_manifests[mode].append(str(manifest_path))

    scored = comparison_module.score_batch_mode_results(mode_results)
    report = {
        "case_count": len(cases),
        "modes": list(modes),
        "mode_results": mode_results,
        "mode_artifact_manifests": mode_artifact_manifests,
        **scored,
    }
    comparison_report_path = root / "comparison_report.json"
    report["comparison_report_path"] = str(comparison_report_path)
    comparison_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
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
