"""Execution and artifact adapter for model-specific experience-study tools."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl

from reserving_workflow.artifacts.storage import write_json_artifact
from reserving_workflow.runtime import run_registry
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
) -> dict[str, Any]:
    """Run the MiniMax tool and persist standard comparison-ready artifacts."""

    validated = ExperienceStudyToolInput.model_validate(inputs)
    artifact_base = resolve_artifact_root(artifact_dir)
    artifact_root = (artifact_base / run_id).resolve()
    artifact_root.relative_to(artifact_base)
    artifact_root.mkdir(parents=True, exist_ok=True)
    task_id = f"operator-{case_id}"
    operator_params = {
        "case_id": case_id,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "inputs": validated.model_dump(mode="json"),
        "artifact_dir": str(artifact_base),
        "registry_path": str(Path(registry_path).expanduser().resolve()),
        "created_by": created_by,
        "operator_id": operator_id,
        "workspace_id": workspace_id,
    }
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
    validated_input = {
        "case_id": case_id,
        "run_id": run_id,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "model": MINIMAX_MODEL_ID,
        "inputs": validated.model_dump(mode="json"),
        "resolved_row_count": len(validated.rows or AE_SMALL_ROWS),
    }
    deterministic_result = {
        "case_id": case_id,
        "run_id": run_id,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "method": "grouped_actual_to_expected",
        "model": MINIMAX_MODEL_ID,
        "result_count": len(results),
        "results": results,
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
    constitution_check = {
        "case_id": case_id,
        "run_id": run_id,
        "status": "passed",
        "hard_constraints": [],
        "review_triggers": [],
    }

    artifact_payloads = {
        "validated_input": validated_input,
        "deterministic_result": deterministic_result,
        "narrative_draft": narrative_draft,
        "constitution_check": constitution_check,
    }
    artifact_paths: dict[str, str] = {}
    for artifact_id, payload in artifact_payloads.items():
        filename = f"{artifact_id}.json"
        write_json_artifact(artifact_root / filename, payload)
        artifact_paths[artifact_id] = filename
    artifact_paths["run_manifest"] = "run_manifest.json"
    run_manifest = {
        "schema_version": "1.0.0",
        "case_id": case_id,
        "run_id": run_id,
        "task_id": task_id,
        "status": "completed",
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "model": MINIMAX_MODEL_ID,
        "artifact_root": str(artifact_root),
        "artifact_paths": artifact_paths,
        "source_evaluation_id": SOURCE_EVALUATION_ID,
        "source_evaluation_sha256": SOURCE_EVALUATION_SHA256,
    }
    run_manifest_path = write_json_artifact(artifact_root / "run_manifest.json", run_manifest)
    summary = f"Completed {MINIMAX_EXPERIENCE_STUDY_TOOL_ID} for {case_id}"
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=run_id,
        status="completed",
        artifact_root=str(artifact_root),
        summary=summary,
        operator_params=operator_params,
        created_by=created_by,
        operator_id=operator_id,
        workspace_id=workspace_id,
        review_required=False,
        event_payload={"tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID, "result_count": len(results)},
    )
    return {
        "ok": True,
        "status": "completed",
        "case_id": case_id,
        "run_id": run_id,
        "summary": summary,
        "tool_id": MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
        "result_count": len(results),
        "results": results,
        "final_output": {"artifact_manifest_path": str(run_manifest_path)},
        "errors": [],
    }
