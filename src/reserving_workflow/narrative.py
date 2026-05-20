"""Shared narrative draft helpers for artifact-backed reserving runs."""

from __future__ import annotations

from reserving_workflow.schemas import DeterministicReserveResult, NarrativeDraft, ReservingCaseInput


def build_narrative_draft(
    case_input: ReservingCaseInput,
    deterministic_result: DeterministicReserveResult,
) -> NarrativeDraft:
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
