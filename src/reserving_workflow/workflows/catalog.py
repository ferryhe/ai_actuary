"""Builtin workflow catalog and sequential workflow metadata."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from reserving_workflow.contracts.control_plane import Workflow, WorkflowStep


class WorkflowStepEntry(BaseModel):
    step_id: str
    tool_id: str
    title: str
    description: str | None = None
    step_kind: Literal["validate", "execute"] = "execute"
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("step_id")
    @classmethod
    def _step_id_must_be_safe_component(cls, value: str) -> str:
        return _safe_catalog_id(value, field_name="step_id")

    def to_contract(self, *, order: int, status: str | None = None) -> WorkflowStep:
        return WorkflowStep(
            step_id=self.step_id,
            tool_id=self.tool_id,
            title=self.title,
            description=self.description,
            step_kind=self.step_kind,
            order=order,
            inputs=dict(self.inputs),
            status=status,
        )


class WorkflowCatalogEntry(BaseModel):
    workflow_id: str
    title: str
    description: str
    builtin: bool = True
    steps: list[WorkflowStepEntry] = Field(default_factory=list)

    @field_validator("workflow_id")
    @classmethod
    def _workflow_id_must_be_safe_component(cls, value: str) -> str:
        return _safe_catalog_id(value, field_name="workflow_id")

    def summary(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "title": self.title,
            "description": self.description,
            "builtin": self.builtin,
            "step_count": len(self.steps),
        }

    def to_contract(self) -> Workflow:
        return Workflow(
            workflow_id=self.workflow_id,
            title=self.title,
            description=self.description,
            builtin=self.builtin,
            step_count=len(self.steps),
            steps=[step.to_contract(order=index + 1) for index, step in enumerate(self.steps)],
        )


class WorkflowCatalog:
    def __init__(self, entries: list[WorkflowCatalogEntry] | None = None):
        sorted_entries = sorted(entries or [], key=lambda item: item.workflow_id)
        self._entries: dict[str, WorkflowCatalogEntry] = {}
        for entry in sorted_entries:
            if entry.workflow_id in self._entries:
                raise ValueError(f"Duplicate workflow id in catalog: {entry.workflow_id}")
            self._entries[entry.workflow_id] = entry

    def list_workflows(self) -> list[WorkflowCatalogEntry]:
        return list(self._entries.values())

    def list_workflow_summaries(self) -> list[dict[str, Any]]:
        return [entry.summary() for entry in self.list_workflows()]

    def get_workflow(self, workflow_id: str) -> WorkflowCatalogEntry:
        try:
            return self._entries[workflow_id]
        except KeyError as exc:
            raise ValueError(f"Workflow id not found in catalog: {workflow_id}") from exc


def build_builtin_workflow_catalog() -> WorkflowCatalog:
    return WorkflowCatalog(
        entries=[
            _builtin_chainladder_basic_workflow(),
            _builtin_chainladder_validation_workflow(),
        ]
    )


def _safe_catalog_id(value: str, *, field_name: str) -> str:
    candidate = str(value).strip()
    if not candidate:
        raise ValueError(f"{field_name} must not be empty")
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise ValueError(f"{field_name} must be a single safe path component")
    return candidate


def _builtin_chainladder_basic_workflow() -> WorkflowCatalogEntry:
    return WorkflowCatalogEntry(
        workflow_id="chainladder-basic",
        title="Chainladder Basic",
        description="Sequential builtin workflow that executes the existing chainladder governed run as a single ordered step.",
        steps=[
            WorkflowStepEntry(
                step_id="chainladder",
                tool_id="chainladder",
                title="Run chainladder",
                description="Execute the legacy chainladder governed run path.",
            )
        ],
    )


def _builtin_chainladder_validation_workflow() -> WorkflowCatalogEntry:
    return WorkflowCatalogEntry(
        workflow_id="chainladder-validated",
        title="Chainladder Validated",
        description="Two-step builtin workflow that validates chainladder inputs before executing the deterministic governed run.",
        steps=[
            WorkflowStepEntry(
                step_id="validate",
                tool_id="chainladder",
                step_kind="validate",
                title="Validate chainladder input",
                description="Validate triangle/sample inputs and write a deterministic validation artifact.",
            ),
            WorkflowStepEntry(
                step_id="execute",
                tool_id="chainladder",
                step_kind="execute",
                title="Run chainladder",
                description="Execute the deterministic chainladder run after validation passes.",
            ),
        ],
    )
