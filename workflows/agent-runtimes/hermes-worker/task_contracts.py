"""Hermes worker contract schemas for the local callable adapter."""

from datetime import datetime, timezone
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
    run_id: str | None = None
    artifact_root: str | None = None
    planner_context: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_by: str = "openai-planner"


class WorkerResult(BaseModel):
    task_id: str
    task_kind: str = "run_case"
    case_id: str | None = None
    run_id: str | None = None
    status: Literal["completed", "failed", "needs_review"]
    summary: str
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    review_reasons: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    deterministic_result: dict[str, Any] = Field(default_factory=dict)
    narrative_draft: dict[str, Any] = Field(default_factory=dict)
    constitution_check: dict[str, Any] = Field(default_factory=dict)
    artifact_manifest: dict[str, Any] = Field(default_factory=dict)
    worker_metadata: dict[str, Any] = Field(default_factory=dict)
    completed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
