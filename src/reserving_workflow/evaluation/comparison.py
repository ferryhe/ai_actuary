"""Minimal scoring and comparison utilities for benchmark batch runs."""

from __future__ import annotations

from collections import Counter
from typing import Any


def score_batch_mode_results(mode_results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    mode_summaries: dict[str, dict[str, Any]] = {}
    cases_index: dict[str, dict[str, dict[str, Any]]] = {}

    for mode, results in mode_results.items():
        status_counts = Counter(result.get("status", "unknown") for result in results)
        ibnr_values = [
            float(result.get("reserve_summary", {}).get("ibnr"))
            for result in results
            if result.get("reserve_summary", {}).get("ibnr") is not None
        ]
        mode_summaries[mode] = {
            "case_count": len(results),
            "status_counts": dict(status_counts),
            "average_ibnr": (sum(ibnr_values) / len(ibnr_values)) if ibnr_values else None,
        }
        for result in results:
            case_id = str(result.get("case_id"))
            cases_index.setdefault(case_id, {})[mode] = result

    case_comparisons: dict[str, dict[str, Any]] = {}
    for case_id, by_mode in cases_index.items():
        baseline = by_mode.get("baseline_prompt", {})
        governed = by_mode.get("governed_workflow", {})
        baseline_ibnr = _extract_metric(baseline, "ibnr")
        governed_ibnr = _extract_metric(governed, "ibnr")
        case_comparisons[case_id] = {
            "baseline_status": baseline.get("status"),
            "governed_status": governed.get("status"),
            "baseline_ibnr": baseline_ibnr,
            "governed_ibnr": governed_ibnr,
            "ibnr_delta": None if baseline_ibnr is None or governed_ibnr is None else governed_ibnr - baseline_ibnr,
        }

    return {
        "mode_summaries": mode_summaries,
        "case_comparisons": case_comparisons,
    }



def _extract_metric(result: dict[str, Any], metric_name: str) -> float | None:
    reserve_summary = result.get("reserve_summary", {}) or {}
    value = reserve_summary.get(metric_name)
    if value is None:
        return None
    return float(value)
