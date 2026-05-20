from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PipelineStepSpec(BaseModel):
    id: str
    toolId: str
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    when: str | None = None


class ToolPipelineSpec(BaseModel):
    pipelineId: str
    version: str
    artifactRoot: str
    steps: list[PipelineStepSpec] = Field(default_factory=list)


class CommandExecutionResult(BaseModel):
    command: list[str]
    cwd: str
    exit_code: int
    stdout_log_path: str
    stderr_log_path: str


class StepRunResult(BaseModel):
    step_id: str
    tool_id: str
    status: Literal["pending", "running", "completed", "skipped", "failed"]
    command: list[str] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    output_values: dict[str, Any] = Field(default_factory=dict)
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None
    exit_code: int | None = None
    when: str | None = None
    skip_reason: str | None = None
    error: dict[str, Any] | None = None


class PipelineRunResult(BaseModel):
    ok: bool
    status: Literal["ok", "error"]
    pipeline_id: str
    version: str
    run_id: str
    artifact_root: str
    command_log_root: str
    run_manifest_path: str
    registry_path: str
    review_store_dir: str
    run_status: Literal["completed", "needs_review", "failed"]
    steps: list[StepRunResult] = Field(default_factory=list)
    failed_step_id: str | None = None
    error: dict[str, Any] | None = None
