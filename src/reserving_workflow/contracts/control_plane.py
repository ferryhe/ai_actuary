"""Frozen control-plane contracts for operator-facing run surfaces."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunStatus = Literal["accepted", "queued", "running", "completed", "needs_review", "failed"]
RunEventType = Literal[
    "run.accepted",
    "run.queued",
    "run.running",
    "run.completed",
    "run.needs_review",
    "run.failed",
    "workflow.started",
    "workflow.completed",
    "workflow.needs_review",
    "workflow.failed",
    "workflow.step.started",
    "workflow.step.running",
    "workflow.step.completed",
    "workflow.step.needs_review",
    "workflow.step.failed",
]
ReviewStatus = Literal["not_available", "not_required", "review_required", "review_decided"]
ReviewDecisionValue = Literal["approved", "rejected", "changes_requested"]
AgentExecutionMode = Literal["background", "inline"]

_RUN_EVENT_TYPE_BY_STATUS: dict[str, RunEventType] = {
    "accepted": "run.accepted",
    "queued": "run.queued",
    "running": "run.running",
    "completed": "run.completed",
    "needs_review": "run.needs_review",
    "failed": "run.failed",
}


class ArtifactRef(BaseModel):
    artifact_id: str
    path: str | None = None
    label: str | None = None
    present: bool = False


class ToolInvocation(BaseModel):
    tool_id: str = "chainladder"
    inputs: dict[str, Any] = Field(default_factory=dict)


class ChainladderToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_name: str | None = None
    triangle_rows: list[dict[str, Any]] | None = None
    origin_column: str = "origin"
    development_column: str = "development"
    value_column: str = "value"
    cumulative: bool = True
    index_column: str | None = None
    method_variant: Literal["chainladder"] = "chainladder"
    review_threshold_origin_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_method_alias(cls, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        normalized = dict(payload)
        legacy_method = normalized.pop("method", None)
        if "method_variant" not in normalized and legacy_method is not None:
            normalized["method_variant"] = legacy_method
        return normalized

    @model_validator(mode="after")
    def _validate_triangle_source(self) -> "ChainladderToolInput":
        sample_name = self.sample_name.strip() if isinstance(self.sample_name, str) else None
        if sample_name == "":
            sample_name = None
        triangle_rows_present = self.triangle_rows is not None

        if sample_name is None and not triangle_rows_present:
            sample_name = "RAA"
        if sample_name is not None and triangle_rows_present:
            raise ValueError("Provide exactly one of sample_name or triangle_rows.")
        if sample_name is None and not triangle_rows_present:
            raise ValueError("Provide sample_name or triangle_rows.")

        self.sample_name = sample_name
        return self


class ValidatedToolInput(BaseModel):
    tool_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    step_id: str
    tool_id: str
    title: str
    description: str | None = None
    step_kind: Literal["validate", "execute"] = "execute"
    order: int | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus | None = None


class Workflow(BaseModel):
    workflow_id: str
    title: str
    description: str
    builtin: bool = True
    step_count: int
    steps: list[WorkflowStep] = Field(default_factory=list)


class Run(BaseModel):
    run_id: str
    case_id: str | None = None
    status: RunStatus
    created_by: str | None = None
    operator_id: str | None = None
    workspace_id: str | None = None
    summary: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    artifact_root: str | None = None
    review_required: bool = False
    workflow_id: str | None = None


class RunEvent(BaseModel):
    type: RunEventType
    run_id: str
    timestamp: str | None = None
    status: RunStatus
    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewDecisionArtifact(BaseModel):
    artifact_id: str
    path: str | None = None
    label: str | None = None
    present: bool = False


class ReviewDecision(BaseModel):
    review_id: str
    run_id: str
    decision: ReviewDecisionValue
    comment: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    follow_up_run_id: str | None = None
    artifacts: list[ReviewDecisionArtifact] = Field(default_factory=list)


class Review(BaseModel):
    status: ReviewStatus
    review_id: str | None = None
    run_id: str | None = None
    case_id: str | None = None
    workspace_id: str | None = None
    review_required: bool = False
    decision: ReviewDecision | None = None
    reason_codes: list[str] = Field(default_factory=list)
    assigned_to: str | None = None
    packet: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    record_path: str | None = None
    json_path: str | None = None
    markdown_path: str | None = None
    review_delivery: dict[str, Any] | None = None


class RerunSemantics(BaseModel):
    source_run_id: str
    creates_distinct_run: bool = True
    preserves_source_run: bool = True
    reuses_recorded_operator_params: bool = True
    overrideable_fields: tuple[str, ...] = ("artifact_dir", "review_delivery_dir")


class AgentPlanningRequest(BaseModel):
    case_id: str
    objective: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    available_tool_ids: list[str] = Field(default_factory=list)
    available_workflow_ids: list[str] = Field(default_factory=list)
    user_prompt: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class AgentExecutionPlan(BaseModel):
    case_id: str
    objective: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    tool_id: str | None = None
    workflow_id: str | None = None
    user_prompt: str | None = None
    operator_id: str | None = None
    workspace_id: str | None = None
    created_by: str | None = None
    background: bool = True

    @model_validator(mode="after")
    def _validate_single_target(self) -> "AgentExecutionPlan":
        targets = [self.tool_id is not None, self.workflow_id is not None]
        if sum(targets) != 1:
            raise ValueError("Exactly one of tool_id or workflow_id must be provided.")
        return self

    def to_run_create_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "objective": self.objective,
            "inputs": dict(self.inputs),
            "background": self.background,
        }
        if self.tool_id is not None:
            payload["tool_id"] = self.tool_id
        if self.workflow_id is not None:
            payload["workflow_id"] = self.workflow_id
        if self.user_prompt is not None:
            payload["user_prompt"] = self.user_prompt
        if self.operator_id is not None:
            payload["operator_id"] = self.operator_id
        if self.workspace_id is not None:
            payload["workspace_id"] = self.workspace_id
        if self.created_by is not None:
            payload["created_by"] = self.created_by
        return payload


class AgentRunHandle(BaseModel):
    run_id: str
    case_id: str
    status: RunStatus
    summary: str | None = None
    execution_mode: AgentExecutionMode | None = None


class AgentRunSummary(BaseModel):
    run_id: str
    case_id: str | None = None
    status: RunStatus
    summary: str | None = None
    terminal: bool = False
    event_count: int = 0
    last_event_type: RunEventType | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = "not_available"
    review_required: bool = False


def validate_run_status(status: Any) -> RunStatus:
    candidate = str(status)
    if candidate not in _RUN_EVENT_TYPE_BY_STATUS:
        raise ValueError(f"Unsupported run status: {candidate!r}")
    return candidate  # type: ignore[return-value]


def run_event_type_for_status(status: Any) -> RunEventType:
    return _RUN_EVENT_TYPE_BY_STATUS[validate_run_status(status)]


def is_terminal_run_status(status: Any) -> bool:
    return validate_run_status(status) in {"completed", "needs_review", "failed"}
