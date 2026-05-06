from __future__ import annotations

import pytest
from pydantic import ValidationError

from reserving_workflow.contracts.control_plane import (
    ArtifactRef,
    ChainladderToolInput,
    Review,
    RerunSemantics,
    Run,
    RunEvent,
    ToolInvocation,
    ValidatedToolInput,
    run_event_type_for_status,
    validate_run_status,
)


def test_control_plane_run_contract_freezes_status_values():
    run = Run(run_id="run-1", case_id="case-1", status="completed")

    assert run.status == "completed"
    assert validate_run_status("needs_review") == "needs_review"
    with pytest.raises(ValueError, match="Unsupported run status"):
        validate_run_status("accepted_pending")


def test_control_plane_run_event_contract_freezes_event_types():
    event = RunEvent(type="run.running", run_id="run-1", status="running")

    assert event.type == "run.running"
    assert run_event_type_for_status("failed") == "run.failed"
    with pytest.raises(ValidationError):
        RunEvent(type="run.unknown", run_id="run-1", status="running")


def test_control_plane_artifact_review_and_rerun_contracts_are_stable():
    artifact = ArtifactRef(artifact_id="run_manifest", path="/tmp/run_manifest.json", present=True)
    review = Review(status="review_required", review_required=True, decision="pending")
    rerun = RerunSemantics(source_run_id="run-1")

    assert artifact.artifact_id == "run_manifest"
    assert review.status == "review_required"
    assert review.decision == "pending"
    assert rerun.creates_distinct_run is True
    assert rerun.overrideable_fields == ("artifact_dir", "review_delivery_dir")


def test_tool_invocation_contract_normalizes_chainladder_legacy_method_alias():
    invocation = ToolInvocation(tool_id="chainladder", inputs={"sample_name": "RAA"})
    normalized = ChainladderToolInput.model_validate({"sample_name": "RAA", "method": "chainladder"})
    validated = ValidatedToolInput(tool_id=invocation.tool_id, inputs=normalized.model_dump(mode="json"))

    assert invocation.tool_id == "chainladder"
    assert validated.inputs == {"sample_name": "RAA", "method_variant": "chainladder", "review_threshold_origin_count": None}
