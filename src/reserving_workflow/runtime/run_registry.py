"""Compatibility wrapper over the local run-store adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reserving_workflow.storage.local import DEFAULT_REGISTRY_PAYLOAD, LocalRunStore, RunNotFoundError, resolve_registry_path


def record_run_event(
    *,
    registry_path: str | Path,
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
) -> dict[str, Any]:
    store = LocalRunStore(registry_path)
    try:
        return store.update_run_status(
            run_id=run_id,
            task_id=task_id,
            case_id=case_id,
            status=status,
            artifact_root=artifact_root,
            summary=summary,
            operator_params=operator_params,
            review_required=review_required,
            error_category=error_category,
            errors=errors,
            review_delivery=review_delivery,
        )
    except RunNotFoundError:
        return store.create_run(
            task_id=task_id,
            case_id=case_id,
            run_id=run_id,
            status=status,
            artifact_root=artifact_root,
            summary=summary,
            operator_params=operator_params,
            review_required=review_required,
            error_category=error_category,
            errors=errors,
            review_delivery=review_delivery,
        )


def list_runs(registry_path: str | Path) -> list[dict[str, Any]]:
    return LocalRunStore(registry_path).list_runs()


def get_run(registry_path: str | Path, run_id: str) -> dict[str, Any]:
    return LocalRunStore(registry_path).get_run(run_id)
