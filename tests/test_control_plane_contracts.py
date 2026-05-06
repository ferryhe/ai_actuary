from __future__ import annotations

import pytest
from pydantic import ValidationError

from reserving_workflow.contracts.control_plane import (
    AgentExecutionPlan,
    AgentPlanningRequest,
    AgentRunHandle,
    AgentRunSummary,
    ArtifactRef,
    ChainladderToolInput,
    Review,
    ReviewDecision,
    RerunSemantics,
    Run,
    RunEvent,
    ToolInvocation,
    ValidatedToolInput,
    is_terminal_run_status,
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
    assert validated.inputs == {
        "sample_name": "RAA",
        "triangle_rows": None,
        "origin_column": "origin",
        "development_column": "development",
        "value_column": "value",
        "cumulative": True,
        "index_column": None,
        "method_variant": "chainladder",
        "review_threshold_origin_count": None,
    }


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


def test_agent_planning_and_summary_contracts_stay_json_serializable():
    request = AgentPlanningRequest(
        case_id="case-14",
        objective="Run governed workflow",
        inputs={"sample_name": "RAA"},
        available_tool_ids=["chainladder"],
        available_workflow_ids=["chainladder-basic"],
    )
    plan = AgentExecutionPlan(
        case_id="case-14",
        objective="Run governed workflow",
        workflow_id="chainladder-basic",
        inputs={"sample_name": "RAA"},
        background=True,
    )
    handle = AgentRunHandle(
        run_id="run-14",
        case_id="case-14",
        status="accepted",
        execution_mode="background",
    )
    summary = AgentRunSummary(
        run_id="run-14",
        case_id="case-14",
        status="needs_review",
        terminal=True,
        event_count=3,
        last_event_type="run.needs_review",
        artifact_ids=["run_manifest", "review_packet"],
        review_status="review_required",
        review_required=True,
    )

    assert request.model_dump(mode="json")["available_tool_ids"] == ["chainladder"]
    assert plan.to_run_create_payload()["workflow_id"] == "chainladder-basic"
    assert handle.execution_mode == "background"
    assert summary.model_dump(mode="json")["artifact_ids"] == ["run_manifest", "review_packet"]
    assert is_terminal_run_status(summary.status) is True


def test_agent_execution_plan_requires_exactly_one_target():
    with pytest.raises(ValidationError, match="Exactly one of tool_id or workflow_id"):
        AgentExecutionPlan(
            case_id="case-14",
            objective="Invalid plan",
            inputs={},
        )

    with pytest.raises(ValidationError, match="Exactly one of tool_id or workflow_id"):
        AgentExecutionPlan(
            case_id="case-14",
            objective="Invalid plan",
            tool_id="chainladder",
            workflow_id="chainladder-basic",
            inputs={},
        )
