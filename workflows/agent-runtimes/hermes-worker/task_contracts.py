"""Hermes worker contract schemas for the project skeleton."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkerTask(BaseModel):
    task_id: str
    task_kind: Literal["run_case", "run_batch", "build_review_packet", "replay_case"]
    case_ref: str | None = None
    objective: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    escalation_policy: dict[str, Any] = Field(default_factory=dict)
    success_criteria: dict[str, Any] = Field(default_factory=dict)


class WorkerResult(BaseModel):
    task_id: str
    status: Literal["completed", "failed", "needs_review"]
    summary: str
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    review_reason: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
