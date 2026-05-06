"""Storage boundary protocols for local control-plane adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


ArtifactFormat = Literal["json", "text"]


@runtime_checkable
class RunStore(Protocol):
    def create_run(
        self,
        *,
        task_id: str,
        case_id: str | None,
        run_id: str,
        status: str,
        artifact_root: str | None = None,
        summary: str | None = None,
        operator_params: dict[str, Any] | None = None,
        review_required: bool | None = None,
        error_category: str | None = None,
        errors: list[str] | None = None,
        review_delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def update_run_status(
        self,
        *,
        run_id: str,
        task_id: str,
        case_id: str | None,
        status: str,
        artifact_root: str | None = None,
        summary: str | None = None,
        operator_params: dict[str, Any] | None = None,
        review_required: bool | None = None,
        error_category: str | None = None,
        errors: list[str] | None = None,
        review_delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def append_event(self, *, run_id: str, status: str, summary: str | None = None) -> dict[str, Any]: ...
    def get_run(self, run_id: str) -> dict[str, Any]: ...
    def list_runs(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class ArtifactStore(Protocol):
    def write_artifact(
        self,
        *,
        root: str | Path,
        relative_path: str | Path,
        payload: Any,
        format: ArtifactFormat = "json",
    ) -> Path: ...

    def read_artifact(self, path: str | Path, *, format: ArtifactFormat = "json") -> Any: ...
    def list_artifacts(self, root: str | Path) -> list[str]: ...


@runtime_checkable
class ReviewStore(Protocol):
    def create_review(
        self,
        *,
        review_id: str,
        run_id: str,
        case_id: str,
        status: str,
        reason_codes: list[str] | None = None,
        assigned_to: str | None = None,
        packet: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def submit_decision(
        self,
        *,
        review_id: str,
        decision: str,
        comment: str | None = None,
        decided_by: str | None = None,
        follow_up_run_id: str | None = None,
    ) -> dict[str, Any]: ...

    def get_review(self, review_id: str) -> dict[str, Any]: ...
