from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from reserving_workflow.contracts.control_plane import Review, Run, RunEvent, ToolInvocation, Workflow
from reserving_workflow.schemas import (
    ConstitutionCheckResult,
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    RunArtifactManifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "export_contract_schemas.py"
SCHEMA_DIR = REPO_ROOT / "schemas" / "actuarial-reserving" / "v1"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "tool_contracts" / "golden_run"

EXPECTED_SCHEMA_FILES = {
    "ReservingCaseInput.schema.json": ReservingCaseInput,
    "DeterministicReserveResult.schema.json": DeterministicReserveResult,
    "NarrativeDraft.schema.json": NarrativeDraft,
    "ConstitutionCheckResult.schema.json": ConstitutionCheckResult,
    "RunArtifactManifest.schema.json": RunArtifactManifest,
    "ToolInvocation.schema.json": ToolInvocation,
    "Workflow.schema.json": Workflow,
    "Run.schema.json": Run,
    "RunEvent.schema.json": RunEvent,
    "Review.schema.json": Review,
}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_export_contract_schemas_script_writes_expected_files_and_shapes():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["output_dir"] == str(SCHEMA_DIR)
    assert payload["count"] == len(EXPECTED_SCHEMA_FILES)

    missing = [name for name in EXPECTED_SCHEMA_FILES if not (SCHEMA_DIR / name).exists()]
    assert missing == []

    case_schema = _load_json(SCHEMA_DIR / "ReservingCaseInput.schema.json")
    assert case_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert case_schema["title"] == "ReservingCaseInput"
    assert set(case_schema["required"]) == {"case_id"}
    assert "triangles" in case_schema["properties"]
    assert "metadata" in case_schema["properties"]
    assert "run_config" in case_schema["properties"]

    result_schema = _load_json(SCHEMA_DIR / "DeterministicReserveResult.schema.json")
    assert set(result_schema["required"]) == {"case_id", "method"}
    assert "reserve_summary" in result_schema["properties"]
    assert "diagnostics" in result_schema["properties"]

    draft_schema = _load_json(SCHEMA_DIR / "NarrativeDraft.schema.json")
    assert set(draft_schema["required"]) == {"case_id", "summary"}
    assert "key_points" in draft_schema["properties"]
    assert "cited_values" in draft_schema["properties"]

    constitution_schema = _load_json(SCHEMA_DIR / "ConstitutionCheckResult.schema.json")
    assert "status" in constitution_schema["properties"]
    assert constitution_schema["properties"]["status"]["enum"] == ["pass", "fail", "review_required"]

    manifest_schema = _load_json(SCHEMA_DIR / "RunArtifactManifest.schema.json")
    assert set(manifest_schema["required"]) == {"case_id", "run_id"}
    assert "artifact_paths" in manifest_schema["properties"]

    tool_invocation_schema = _load_json(SCHEMA_DIR / "ToolInvocation.schema.json")
    assert "tool_id" in tool_invocation_schema["properties"]
    assert "inputs" in tool_invocation_schema["properties"]

    workflow_schema = _load_json(SCHEMA_DIR / "Workflow.schema.json")
    assert {"workflow_id", "title", "description", "step_count", "steps"}.issubset(workflow_schema["properties"])
    assert "$defs" in workflow_schema

    run_schema = _load_json(SCHEMA_DIR / "Run.schema.json")
    assert {"run_id", "status", "artifact_root", "workflow_id"}.issubset(run_schema["properties"])

    event_schema = _load_json(SCHEMA_DIR / "RunEvent.schema.json")
    assert {"type", "run_id", "status", "payload"}.issubset(event_schema["properties"])

    review_schema = _load_json(SCHEMA_DIR / "Review.schema.json")
    assert {"status", "review_id", "run_id", "packet", "decision"}.issubset(review_schema["properties"])
    assert "$defs" in review_schema


def test_golden_run_json_fixtures_validate_against_existing_models():
    case_input = ReservingCaseInput.model_validate(_load_json(FIXTURE_DIR / "case_input.json"))
    deterministic_result = DeterministicReserveResult.model_validate(_load_json(FIXTURE_DIR / "deterministic_result.json"))
    narrative_draft = NarrativeDraft.model_validate(_load_json(FIXTURE_DIR / "narrative_draft.json"))
    constitution_check = ConstitutionCheckResult.model_validate(_load_json(FIXTURE_DIR / "constitution_check.json"))
    run_manifest = RunArtifactManifest.model_validate(_load_json(FIXTURE_DIR / "run_manifest.json"))
    review_packet = _load_json(FIXTURE_DIR / "review_packet.json")
    reserve_summary = _load_json(FIXTURE_DIR / "reserve_summary.json")
    review_packet_markdown = (FIXTURE_DIR / "review_packet.md").read_text(encoding="utf-8")
    operator_handoff = (FIXTURE_DIR / "operator_handoff.md").read_text(encoding="utf-8")
    reserve_summary_markdown = (FIXTURE_DIR / "reserve_summary.md").read_text(encoding="utf-8")

    artifact_root = (FIXTURE_DIR / run_manifest.artifact_root).resolve()
    missing_artifacts = [
        artifact_id
        for artifact_id, relative_path in run_manifest.artifact_paths.items()
        if not (artifact_root / relative_path).exists()
    ]

    assert case_input.case_id == "golden-raa"
    assert case_input.metadata["chainladder_sample"] == "RAA"

    assert deterministic_result.reserve_summary == {
        "latest_diagonal": 1600.0,
        "ultimate": 1950.0,
        "ibnr": 350.0,
    }
    assert deterministic_result.diagnostics["origin_count"] == 7

    assert narrative_draft.cited_values == deterministic_result.reserve_summary
    assert "ultimate=1950.0" in narrative_draft.summary

    assert constitution_check.status == "review_required"
    assert constitution_check.review_triggers == ["origin_count_below_review_threshold"]

    assert run_manifest.artifact_root == "."
    assert run_manifest.artifact_paths["narrative_draft"] == "narrative_draft.json"
    assert run_manifest.artifact_paths["review_packet_markdown"] == "review_packet.md"
    assert run_manifest.artifact_paths["reserve_summary_json"] == "reserve_summary.json"
    assert run_manifest.artifact_paths["reserve_summary_markdown"] == "reserve_summary.md"
    assert run_manifest.artifact_paths["run_manifest"] == "run_manifest.json"
    assert missing_artifacts == []

    assert review_packet["status"] == "review_required"
    assert review_packet["deterministic_outputs"]["reserve_summary"]["ibnr"] == 350.0
    assert review_packet["artifact_links"]["operator_handoff"] == "operator_handoff.md"
    assert "Review Packet — golden-raa" in review_packet_markdown

    assert reserve_summary["values"] == {
        "ibnr": 350.0,
        "ultimate": 1950.0,
        "latest_diagonal": 1600.0,
    }
    assert reserve_summary["missing_metrics"] == []
    assert reserve_summary["deterministic_method"] == "chainladder"
    assert reserve_summary["source"] == "deterministic_result.reserve_summary"

    assert "Operator Handoff — golden-raa" in operator_handoff
    assert "review_required" in operator_handoff
    assert "Reserve Summary" in reserve_summary_markdown
    assert "deterministic_method: chainladder" in reserve_summary_markdown
