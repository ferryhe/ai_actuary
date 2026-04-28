"""Minimal constitution rule engine for governed reserving workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reserving_workflow.schemas import (
    ConstitutionCheckResult,
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    RunArtifactManifest,
)


@dataclass(frozen=True)
class ConstitutionEvaluator:
    """Evaluate hard constraints and review triggers for a single case."""

    default_materiality: float = 1e-6

    def evaluate(
        self,
        case_input: ReservingCaseInput,
        deterministic_result: DeterministicReserveResult,
        narrative_draft: NarrativeDraft,
        artifact_manifest: RunArtifactManifest | None = None,
    ) -> ConstitutionCheckResult:
        hard_constraints: list[str] = []
        soft_guidance: list[str] = []
        review_triggers: list[str] = []

        if not case_input.triangles and not case_input.metadata.get("chainladder_sample") and not case_input.metadata.get("triangle_rows"):
            hard_constraints.append("required_input_missing: no triangle input or approved chainladder source provided")

        materiality = float(case_input.run_config.get("numeric_materiality", self.default_materiality))
        reserve_summary = deterministic_result.reserve_summary or {}
        for name, cited_value in (narrative_draft.cited_values or {}).items():
            if name not in reserve_summary:
                hard_constraints.append(f"unsupported_numeric_claim:{name}")
                continue
            expected = float(reserve_summary[name])
            if abs(float(cited_value) - expected) > materiality:
                hard_constraints.append(
                    f"numeric_mismatch:{name}:expected={expected}:actual={float(cited_value)}"
                )

        diagnostics = deterministic_result.diagnostics or {}
        review_thresholds = case_input.run_config.get("review_thresholds", {})
        for metric_name, threshold in review_thresholds.items():
            if metric_name in diagnostics:
                try:
                    metric_value = float(diagnostics[metric_name])
                    threshold_value = float(threshold)
                except (TypeError, ValueError):
                    continue
                if metric_value > threshold_value:
                    review_triggers.append(
                        f"diagnostic_threshold:{metric_name}:value={metric_value}:threshold={threshold_value}"
                    )

        if artifact_manifest is not None:
            required_artifacts = case_input.run_config.get("required_artifacts", [])
            missing = [name for name in required_artifacts if name not in artifact_manifest.artifact_paths]
            if missing:
                review_triggers.append("artifact_incomplete:" + ",".join(sorted(missing)))

        if narrative_draft.key_points:
            soft_guidance.extend(narrative_draft.key_points)

        if hard_constraints:
            status = "fail"
        elif review_triggers:
            status = "review_required"
        else:
            status = "pass"

        return ConstitutionCheckResult(
            case_id=case_input.case_id,
            status=status,
            hard_constraints=hard_constraints,
            soft_guidance=soft_guidance,
            review_triggers=review_triggers,
        )


def evaluate_case_constitution(
    case_input: ReservingCaseInput,
    deterministic_result: DeterministicReserveResult,
    narrative_draft: NarrativeDraft,
    artifact_manifest: RunArtifactManifest | None = None,
    **kwargs: Any,
) -> ConstitutionCheckResult:
    """Convenience wrapper around :class:`ConstitutionEvaluator`."""
    evaluator = ConstitutionEvaluator(**kwargs)
    return evaluator.evaluate(case_input, deterministic_result, narrative_draft, artifact_manifest)
