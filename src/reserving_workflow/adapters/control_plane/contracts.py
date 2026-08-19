"""Typed response contracts for public control-plane reads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from reserving_workflow.contracts import ArtifactRef, Review, Run, RunEvent
from reserving_workflow.contracts.control_plane import Workflow, WorkflowStep


class ResponseContract(BaseModel):
    """Require declared field types while tolerating legacy envelope additions."""

    model_config = ConfigDict(extra="ignore")


class HealthStatus(ResponseContract):
    ok: bool
    service: str | None = None


class PreflightCheck(ResponseContract):
    check_id: str
    status: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class PreflightStatus(ResponseContract):
    ok: bool
    service: str
    status: str
    readiness: str
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, int]
    configuration: dict[str, Any]
    runtime: dict[str, Any]
    checks: list[PreflightCheck]


class ToolSummary(ResponseContract):
    tool_id: str
    method: str
    title: str
    description: str
    builtin: bool = True
    tags: list[str] = Field(default_factory=list)
    console_defaults: dict[str, Any] = Field(default_factory=dict)


class ToolDetail(ToolSummary):
    input_schema: dict[str, Any]


class WorkflowSummary(ResponseContract):
    workflow_id: str
    title: str
    description: str
    builtin: bool = True
    step_count: int


class ToolListEnvelope(ResponseContract):
    tool_count: int
    tools: list[ToolSummary]


class WorkflowListEnvelope(ResponseContract):
    workflow_count: int
    workflows: list[WorkflowSummary]


class RunListEnvelope(ResponseContract):
    run_count: int
    runs: list[Run]


class RunEnvelope(ResponseContract):
    run: Run


class RunEventListEnvelope(ResponseContract):
    run_id: str
    event_count: int
    events: list[RunEvent]


ArtifactProvenance = Literal[
    "deterministic",
    "model_generated",
    "review",
    "system_manifest",
]


class ArtifactMetadata(ArtifactRef):
    provenance: ArtifactProvenance | None = None
    category: str | None = None


class ArtifactListEnvelope(ResponseContract):
    run_id: str
    artifacts: list[ArtifactMetadata]


class ReviewEnvelope(ResponseContract):
    review: Review


class ProjectionError(ResponseContract):
    code: str
    message: str


class ArtifactProjection(ResponseContract):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    artifact_id: str
    status: Literal["available"]
    provenance: ArtifactProvenance
    data: dict[str, Any]
    errors: list[ProjectionError] = Field(default_factory=list)


__all__ = [
    "ArtifactListEnvelope",
    "ArtifactMetadata",
    "ArtifactProjection",
    "ArtifactProvenance",
    "HealthStatus",
    "PreflightStatus",
    "ProjectionError",
    "ReviewEnvelope",
    "RunEnvelope",
    "RunEventListEnvelope",
    "RunListEnvelope",
    "ToolDetail",
    "ToolListEnvelope",
    "ToolSummary",
    "Workflow",
    "WorkflowListEnvelope",
    "WorkflowStep",
    "WorkflowSummary",
]
