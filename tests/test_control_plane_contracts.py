from __future__ import annotations

import pytest
from pydantic import ValidationError

from reserving_workflow.contracts.control_plane import (
    ArtifactRef,
    ChainladderToolInput,
    Review,
    ReviewDecision,
    RerunSemantics,
    Run,
    RunEvent,
    ToolInvocation,
    ValidatedToolInput,
    Workflow,
    WorkflowStep,
    run_event_type_for_status,
    validate_run_status,
)


def test_control_plane_run_contract_freezes_status_values():
    run = Run(run_id="run-1", case_id="case-1", status="completed")

    assert run.status == "completed"
    assert validate_run_status("needs_review") == "needs_review"
    with pytest.raises(ValueError, match="Unsupported run status"):
        validate_run_status("accepted_pending")
    with pytest.raises(ValueError, match="Unsupported run status"):
        validate_run_status("approved")


def test_control_plane_run_event_contract_freezes_event_types():
    event = RunEvent(type="run.running", run_id="run-1", status="running")

    assert event.type == "run.running"
    assert run_event_type_for_status("failed") == "run.failed"
    with pytest.raises(ValidationError):
        RunEvent(type="run.unknown", run_id="run-1", status="running")


def test_control_plane_artifact_review_and_rerun_contracts_are_stable():
    artifact = ArtifactRef(artifact_id="run_manifest", path="/tmp/run_manifest.json", present=True)
    review = Review(
        status="review_required",
        review_required=True,
        review_id="review-run-1",
        run_id="run-1",
        assigned_to="actuary-002",
        workspace_id="workspace-1",
    )
    decision = ReviewDecision(review_id="review-run-1", run_id="run-1", decision="changes_requested")
    rerun = RerunSemantics(source_run_id="run-1")
    run = Run(
        run_id="run-1",
        case_id="case-1",
        status="completed",
        created_by="actuary-001",
        operator_id="actuary-001",
        workspace_id="workspace-1",
    )

    assert artifact.artifact_id == "run_manifest"
    assert review.status == "review_required"
    assert review.assigned_to == "actuary-002"
    assert review.workspace_id == "workspace-1"
    assert decision.decision == "changes_requested"
    assert run.created_by == "actuary-001"
    assert run.operator_id == "actuary-001"
    assert run.workspace_id == "workspace-1"
    assert rerun.creates_distinct_run is True
    assert rerun.overrideable_fields == ("artifact_dir", "review_delivery_dir")


def test_tool_invocation_contract_normalizes_chainladder_legacy_method_alias():
    invocation = ToolInvocation(tool_id="chainladder", inputs={"sample_name": "RAA"})
    normalized = ChainladderToolInput.model_validate({"sample_name": "RAA", "method": "chainladder"})
    validated = ValidatedToolInput(tool_id=invocation.tool_id, inputs=normalized.model_dump(mode="json"))

    assert invocation.tool_id == "chainladder"
    assert validated.inputs == {"sample_name": "RAA", "method_variant": "chainladder", "review_threshold_origin_count": None}


def test_workflow_contracts_freeze_builtin_schema_shape():
    workflow = Workflow(
        workflow_id="chainladder-basic",
        title="Chainladder Basic",
        description="Sequential deterministic workflow.",
        builtin=True,
        step_count=1,
        steps=[WorkflowStep(step_id="chainladder", tool_id="chainladder", title="Run chainladder")],
    )

    assert workflow.workflow_id == "chainladder-basic"
    assert workflow.step_count == 1
    assert workflow.steps[0].tool_id == "chainladder"
