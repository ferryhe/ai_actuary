"""Shared narrative draft helpers for artifact-backed reserving runs."""

from __future__ import annotations

from typing import Any

from reserving_workflow.schemas import DeterministicReserveResult, NarrativeDraft, ReservingCaseInput


def build_narrative_draft(
    case_input: ReservingCaseInput,
    deterministic_result: DeterministicReserveResult,
    *,
    narrative_writer: Any = None,
) -> NarrativeDraft:
    """Build `narrative_draft.json`, optionally using the narrative model slot.

    `narrative_writer` is an optional :class:`~reserving_workflow.ai_narrative.NarrativeDraftWriter`.
    Without it the draft is fully deterministic. With it, the model may only
    rewrite the wording; `cited_values` always come from the deterministic
    result and any unsupported number falls back to the template.
    """

    draft, _meta = build_narrative_draft_with_meta(
        case_input, deterministic_result, narrative_writer=narrative_writer
    )
    return draft


def build_narrative_draft_with_meta(
    case_input: ReservingCaseInput,
    deterministic_result: DeterministicReserveResult,
    *,
    narrative_writer: Any = None,
) -> tuple[NarrativeDraft, dict[str, Any]]:
    """Same as :func:`build_narrative_draft` but also returns drafting metadata."""

    draft = _template_draft(case_input, deterministic_result)
    if narrative_writer is None:
        return draft, {"source": "template", "reason": "no_writer"}
    try:
        drafted = narrative_writer.draft_text(
            case_id=case_input.case_id,
            method=deterministic_result.method,
            evidence={
                "reserve_summary": deterministic_result.reserve_summary or {},
                "diagnostics": deterministic_result.diagnostics or {},
            },
            fallback_summary=draft.summary,
            fallback_key_points=list(draft.key_points),
        )
    except Exception as exc:  # noqa: BLE001 - drafting must never break a run
        return draft, {
            "source": "template",
            "reason": "writer_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    meta = dict(drafted.get("meta") or {})
    summary = str(drafted.get("summary") or draft.summary)
    key_points = [str(item) for item in drafted.get("key_points") or draft.key_points]
    return (
        NarrativeDraft(
            case_id=case_input.case_id,
            summary=summary,
            key_points=key_points,
            cited_values=dict(draft.cited_values),
        ),
        meta,
    )


def _template_draft(
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
