"""Execution and artifact adapter for model-specific experience-study tools."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from reserving_workflow.artifacts.storage import write_json_artifact
from reserving_workflow.constitution.engine import evaluate_case_constitution
from reserving_workflow.review.ai_reviewer import (
    build_ai_suggestion_payload,
    generate_ai_review,
)
from reserving_workflow.review.generator import (
    build_review_packet_from_artifacts,
    write_review_packet_files,
)
from reserving_workflow.ai_narrative import draft_narrative_dict, narrative_writer_from_env
from reserving_workflow.runtime import run_registry
from reserving_workflow.schemas import (
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    RunArtifactManifest,
)
from reserving_workflow.storage.local import resolve_artifact_root

from .contracts import ExperienceStudyToolInput, MINIMAX_EXPERIENCE_STUDY_TOOL_ID
from .minimax_experience_study import (
    ExperienceInput,
    GroupingRequest,
    compute_grouped_actual_to_expected,
)


MINIMAX_MODEL_ID = "MiniMax-M3"
SOURCE_EVALUATION_ID = "minimax-m3-c4-thinking-64k-docker-eval-20260807-02"
SOURCE_EVALUATION_SHA256 = "cdf19803908b92a9305c18e2d7d8331b1f4aafd6e5629f7d76a4a9f84accdbfd"

# Default experience-study review rules. The constitution engine triggers
# review when `diagnostics[metric] > threshold`, so a zero threshold means
# "any occurrence triggers human review".
EXPERIENCE_STUDY_DEFAULT_REVIEW_THRESHOLDS: dict[str, float] = {
    "zero_denominator_count": 0.0,
    "low_credibility_count": 0.0,
    "max_ae_ratio": 5.0,
}
EXPERIENCE_STUDY_STATUS_BY_CONSTITUTION = {
    "pass": "completed",
    "review_required": "needs_review",
    "fail": "failed",
}

AE_SMALL_ROWS: tuple[dict[str, str], ...] = (
    {
        "Death_Claim_Amount": "200000",
        "Death_Count": "2",
        "ExpDth_VBT2015_Amt": "160000",
        "ExpDth_VBT2015_Cnt": "1.2",
        "ExpDth_VBT2015wMI_Amt": "150000",
        "ExpDth_VBT2015wMI_Cnt": "1.0",
        "product": "Term",
        "record_id": "SYN-001",
    },
    {
        "Death_Claim_Amount": "100000",
        "Death_Count": "1",
        "ExpDth_VBT2015_Amt": "80000",
        "ExpDth_VBT2015_Cnt": "0.8",
        "ExpDth_VBT2015wMI_Amt": "100000",
        "ExpDth_VBT2015wMI_Cnt": "1.0",
        "product": "Term",
        "record_id": "SYN-002",
    },
    {
        "Death_Claim_Amount": "0",
        "Death_Count": "0",
        "ExpDth_VBT2015_Amt": "0",
        "ExpDth_VBT2015_Cnt": "0",
        "ExpDth_VBT2015wMI_Amt": "0",
        "ExpDth_VBT2015wMI_Cnt": "0",
        "product": "Whole",
        "record_id": "SYN-003",
    },
)


def execute_minimax_experience_study(
    tool_input: ExperienceStudyToolInput | dict[str, Any],
) -> list[dict[str, Any]]:
    """Execute the promoted MiniMax implementation against the shared C4 boundary."""

    validated = (
        tool_input
        if isinstance(tool_input, ExperienceStudyToolInput)
        else ExperienceStudyToolInput.model_validate(tool_input)
    )
    rows = [dict(row) for row in (validated.rows or AE_SMALL_ROWS)]
    experience_input = ExperienceInput(
        population_id=validated.population_id,
        period=validated.period,
        rows=pl.DataFrame(rows).lazy(),
    )
    grouping = GroupingRequest(tuple(validated.dimensions))
    return [asdict(result) for result in compute_grouped_actual_to_expected(experience_input, grouping)]


def build_experience_study_diagnostics(
    results: list[dict[str, Any]],
    *,
    row_count: int,
) -> dict[str, float]:
    """Derive numeric review diagnostics from grouped A/E results."""

    group_keys = {
        tuple(tuple(pair) for pair in result.get("group_values", ())) for result in results
    }
    ratios = [float(result["ratio"]) for result in results if result.get("ratio") is not None]
    count_base = [
        result
        for result in results
        if result.get("metric_kind") == "count" and not result.get("mortality_improvement")
    ]
    actual_total = sum((Decimal(str(result["actual_total"])) for result in count_base), Decimal("0"))
    expected_total = sum(
        (Decimal(str(result["expected_total"])) for result in count_base), Decimal("0")
    )
    return {
        "row_count": float(row_count),
        "group_count": float(len(group_keys)),
        "result_count": float(len(results)),
        "zero_denominator_count": float(sum(1 for result in results if result.get("zero_denominator"))),
        "low_credibility_count": float(
            sum(1 for result in results if result.get("credibility_flag") != "credible")
        ),
        "max_ae_ratio": float(max(ratios)) if ratios else 0.0,
        "min_ae_ratio": float(min(ratios)) if ratios else 0.0,
        "actual_claim_count_total": float(actual_total),
        "expected_claim_count_total": float(expected_total),
    }


def evaluate_experience_study_constitution(
    *,
    case_id: str,
    run_id: str,
    population_id: str,
    period: str,
    diagnostics: dict[str, float],
    review_thresholds: dict[str, Any] | None,
    artifact_paths: dict[str, str],
) -> dict[str, Any]:
    """Run the shared constitution engine over experience-study diagnostics."""

    thresholds = {**EXPERIENCE_STUDY_DEFAULT_REVIEW_THRESHOLDS}
    for key, value in (review_thresholds or {}).items():
        try:
            thresholds[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    case_input = ReservingCaseInput(
        case_id=case_id,
        metadata={
            "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
            "population_id": population_id,
            "period": period,
        },
        run_config={"review_thresholds": thresholds},
    )
    deterministic_result = DeterministicReserveResult(
        case_id=case_id,
        method="grouped_actual_to_expected",
        reserve_summary={key: float(value) for key, value in diagnostics.items()},
        diagnostics=dict(diagnostics),
    )
    narrative_draft = NarrativeDraft(
        case_id=case_id,
        summary=f"Grouped actual-to-expected study for {population_id} ({period}).",
    )
    manifest = RunArtifactManifest(
        case_id=case_id,
        run_id=run_id,
        artifact_paths=dict(artifact_paths),
    )
    return evaluate_case_constitution(case_input, deterministic_result, narrative_draft, manifest).model_dump(
        mode="json"
    )


def build_experience_study_review_packet(
    *,
    case_id: str,
    run_id: str,
    constitution_check: dict[str, Any],
    deterministic_result: dict[str, Any],
    narrative_draft: dict[str, Any],
    run_manifest: dict[str, Any],
    artifact_root: Path,
    summary: str,
) -> dict[str, Any]:
    """Build the review packet and attach AI review guidance to it."""

    packet_manifest = dict(run_manifest)
    packet_manifest["artifact_paths"] = {
        key: str(Path(artifact_root) / value)
        for key, value in (run_manifest.get("artifact_paths") or {}).items()
    }
    packet = build_review_packet_from_artifacts(
        constitution_check=constitution_check,
        deterministic_result={
            **deterministic_result,
            "reserve_summary": dict(deterministic_result.get("diagnostics") or {}),
        },
        narrative_draft=narrative_draft,
        run_manifest=packet_manifest,
        output_dir=artifact_root,
        case_summary=summary,
    )
    try:
        ai_review = generate_ai_review(packet, output_dir=artifact_root, case_id=case_id, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        # Advisory only: a malformed or failing review reply must never fail the run.
        ai_review = {"status": "failed", "error": f"ai_review_failed: {exc}"}
    packet["ai_review"] = ai_review
    packet["ai_suggestion"] = build_ai_suggestion_payload(ai_review)
    return write_review_packet_files(packet, output_dir=artifact_root)


def resolve_run_artifact_root(artifact_dir: str | Path, run_id: str) -> Path:
    """Resolve the per-run artifact directory; idempotent in `run_id`.

    Callers may pass either a case-level directory (`<root>/<case_id>`) or a
    per-run one (`<root>/<case_id>/<run_id>`). Appending the run id twice would
    register a directory the runner never writes to, which surfaces as
    `manifest_missing` on every operator entry point.
    """

    base = resolve_artifact_root(artifact_dir)
    run_id = str(run_id)
    root = base if base.name == run_id else base / run_id
    root = root.resolve()
    root.relative_to(base)
    return root


def run_minimax_experience_study(
    *,
    case_id: str,
    inputs: dict[str, Any],
    artifact_dir: str | Path,
    registry_path: str | Path,
    run_id: str,
    created_by: str,
    operator_id: str,
    workspace_id: str,
    review_thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the MiniMax tool and persist standard comparison-ready artifacts."""

    validated = ExperienceStudyToolInput.model_validate(inputs)
    artifact_base = resolve_artifact_root(artifact_dir)
    artifact_root = resolve_run_artifact_root(artifact_dir, run_id)
    artifact_root.mkdir(parents=True, exist_ok=True)
    task_id = f"operator-{case_id}"
    operator_params: dict[str, Any] = {
        "case_id": case_id,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "inputs": validated.model_dump(mode="json"),
        "artifact_dir": str(artifact_base),
        "registry_path": str(Path(registry_path).expanduser().resolve()),
        "created_by": created_by,
        "operator_id": operator_id,
        "workspace_id": workspace_id,
    }
    if review_thresholds:
        operator_params["review_thresholds"] = dict(review_thresholds)
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=run_id,
        status="running",
        artifact_root=str(artifact_root),
        summary=f"Running {MINIMAX_EXPERIENCE_STUDY_TOOL_ID} for {case_id}",
        operator_params=operator_params,
        created_by=created_by,
        operator_id=operator_id,
        workspace_id=workspace_id,
        review_required=False,
        event_payload={"tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID},
    )

    results = execute_minimax_experience_study(validated)
    resolved_row_count = len(validated.rows or AE_SMALL_ROWS)
    diagnostics = build_experience_study_diagnostics(results, row_count=resolved_row_count)
    validated_input = {
        "case_id": case_id,
        "run_id": run_id,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "model": MINIMAX_MODEL_ID,
        "inputs": validated.model_dump(mode="json"),
        "resolved_row_count": resolved_row_count,
    }
    deterministic_result = {
        "case_id": case_id,
        "run_id": run_id,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "method": "grouped_actual_to_expected",
        "model": MINIMAX_MODEL_ID,
        "result_count": len(results),
        "results": results,
        "diagnostics": diagnostics,
        "source_evaluation_id": SOURCE_EVALUATION_ID,
        "source_evaluation_sha256": SOURCE_EVALUATION_SHA256,
    }
    narrative_draft = {
        "case_id": case_id,
        "run_id": run_id,
        "status": "completed",
        "summary": (
            f"{MINIMAX_MODEL_ID} produced {len(results)} grouped actual-to-expected results "
            f"for {validated.population_id}."
        ),
        "key_points": [
            "Outputs preserve the promoted model implementation for cross-model comparison.",
            "Count and amount bases are returned with and without mortality improvement.",
        ],
    }
    narrative_draft = draft_narrative_dict(
        narrative_draft,
        case_id=case_id,
        method="grouped_actual_to_expected",
        evidence={"diagnostics": diagnostics},
        writer=narrative_writer_from_env(),
    )
    artifact_paths: dict[str, str] = {
        artifact_id: f"{artifact_id}.json"
        for artifact_id in ("validated_input", "deterministic_result", "narrative_draft", "constitution_check")
    }
    constitution_check = evaluate_experience_study_constitution(
        case_id=case_id,
        run_id=run_id,
        population_id=validated.population_id,
        period=validated.period,
        diagnostics=diagnostics,
        review_thresholds=review_thresholds,
        artifact_paths=artifact_paths,
    )
    run_status = EXPERIENCE_STUDY_STATUS_BY_CONSTITUTION.get(
        str(constitution_check.get("status")), "completed"
    )

    artifact_payloads = {
        "validated_input": validated_input,
        "deterministic_result": deterministic_result,
        "narrative_draft": {**narrative_draft, "status": run_status},
        "constitution_check": constitution_check,
    }
    for artifact_id, payload in artifact_payloads.items():
        write_json_artifact(artifact_root / artifact_paths[artifact_id], payload)
    artifact_paths["run_manifest"] = "run_manifest.json"
    run_manifest = {
        "schema_version": "1.0.0",
        "case_id": case_id,
        "run_id": run_id,
        "task_id": task_id,
        "status": run_status,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "model": MINIMAX_MODEL_ID,
        "artifact_root": str(artifact_root),
        "artifact_paths": artifact_paths,
        "source_evaluation_id": SOURCE_EVALUATION_ID,
        "source_evaluation_sha256": SOURCE_EVALUATION_SHA256,
    }
    run_manifest_path = write_json_artifact(artifact_root / "run_manifest.json", run_manifest)
    summary = f"{MINIMAX_EXPERIENCE_STUDY_TOOL_ID} for {case_id} finished with status {run_status}"
    review_packet: dict[str, Any] | None = None
    if run_status == "needs_review":
        review_packet = build_experience_study_review_packet(
            case_id=case_id,
            run_id=run_id,
            constitution_check=constitution_check,
            deterministic_result=deterministic_result,
            narrative_draft=narrative_draft,
            run_manifest=run_manifest,
            artifact_root=artifact_root,
            summary=summary,
        )
        packet_paths = review_packet.get("packet_paths") or {}
        artifact_paths.update(
            {
                "review_packet": str(packet_paths.get("json") or (artifact_root / "review_packet.json")),
                "review_packet_markdown": str(
                    packet_paths.get("markdown") or (artifact_root / "review_packet.md")
                ),
                "ai_review": str(artifact_root / "ai_review.json"),
                "ai_review_markdown": str(artifact_root / "ai_review.md"),
            }
        )
        run_manifest["artifact_paths"] = artifact_paths
        write_json_artifact(artifact_root / "run_manifest.json", run_manifest)
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=run_id,
        status=run_status,
        artifact_root=str(artifact_root),
        summary=summary,
        operator_params=operator_params,
        created_by=created_by,
        operator_id=operator_id,
        workspace_id=workspace_id,
        review_required=run_status == "needs_review",
        event_payload={
            "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
            "result_count": len(results),
            "diagnostics": diagnostics,
        },
    )
    response = {
        "ok": run_status != "failed",
        "status": run_status,
        "case_id": case_id,
        "run_id": run_id,
        "summary": summary,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "result_count": len(results),
        "results": results,
        "diagnostics": diagnostics,
        "review_required": run_status == "needs_review",
        "final_output": {"artifact_manifest_path": str(run_manifest_path)},
        "errors": list(constitution_check.get("hard_constraints") or []),
    }
    if review_packet is not None:
        response["review_packet"] = review_packet
    return response
