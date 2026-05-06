"""Frozen control-plane contracts for operator-facing run surfaces."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RunStatus = Literal["accepted", "queued", "running", "completed", "needs_review", "failed"]
RunEventType = Literal[
    "run.accepted",
    "run.queued",
    "run.running",
    "run.completed",
    "run.needs_review",
    "run.failed",
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


class Run(BaseModel):
    run_id: str
    case_id: str | None = None
    status: RunStatus
    summary: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    artifact_root: str | None = None
    review_required: bool = False


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
