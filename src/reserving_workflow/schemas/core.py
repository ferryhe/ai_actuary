"""Minimal shared schemas for the AI Actuary skeleton.

These models define the contract between CAS Core, the OpenAI planner,
and Hermes workers. They intentionally avoid business logic.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ReservingCaseInput(BaseModel):
    case_id: str
    triangles: dict[str, list[float]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_config: dict[str, Any] = Field(default_factory=dict)


class DeterministicReserveResult(BaseModel):
    case_id: str
    method: str
    reserve_summary: dict[str, float] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NarrativeDraft(BaseModel):
    case_id: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    cited_values: dict[str, float] = Field(default_factory=dict)


class ConstitutionCheckResult(BaseModel):
    case_id: str
    status: Literal["pass", "fail", "review_required"]
    hard_constraints: list[str] = Field(default_factory=list)
    soft_guidance: list[str] = Field(default_factory=list)
    review_triggers: list[str] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    case_id: str
    status: Literal["not_required", "pending", "approved", "rejected"]
    reviewer: str | None = None
    notes: str | None = None


class RunArtifactManifest(BaseModel):
    case_id: str
    run_id: str
    artifact_root: str | None = None
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    created_by: str = "skeleton"
    metadata: dict[str, Any] = Field(default_factory=dict)
