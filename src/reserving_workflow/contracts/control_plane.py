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
ReviewStatus = Literal["not_available", "not_required", "review_required"]
ReviewDecision = Literal["not_required", "pending", "approved", "rejected"]

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

    sample_name: str = "RAA"
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


class ValidatedToolInput(BaseModel):
    tool_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    step_id: str
    tool_id: str
    title: str
    description: str | None = None
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


class Review(BaseModel):
    status: ReviewStatus
    review_required: bool = False
    decision: ReviewDecision | None = None
    packet: dict[str, Any] | None = None
    json_path: str | None = None
    markdown_path: str | None = None
    review_delivery: dict[str, Any] | None = None


class RerunSemantics(BaseModel):
    source_run_id: str
    creates_distinct_run: bool = True
    preserves_source_run: bool = True
    reuses_recorded_operator_params: bool = True
    overrideable_fields: tuple[str, ...] = ("artifact_dir", "review_delivery_dir")


def validate_run_status(status: Any) -> RunStatus:
    candidate = str(status)
    if candidate not in _RUN_EVENT_TYPE_BY_STATUS:
        raise ValueError(f"Unsupported run status: {candidate!r}")
    return candidate  # type: ignore[return-value]


def run_event_type_for_status(status: Any) -> RunEventType:
    return _RUN_EVENT_TYPE_BY_STATUS[validate_run_status(status)]
