"""Runtime diagnostics helpers for the local control plane."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any


def build_preflight_report(
    *,
    service: str,
    version: str,
    registry_path: str | Path,
    artifact_root: str | Path,
    review_store_dir: str | Path,
    review_delivery_dir: str | Path | None,
    tool_ids: list[str],
    workflow_ids: list[str],
    default_operator_id: str = "local-actuary",
    default_workspace_id: str = "default-workspace",
) -> dict[str, Any]:
    """Build a machine-readable local runtime readiness report."""

    checks = [
        _check_registry_path(registry_path),
        _check_directory_root(
            artifact_root,
            check_id="artifact_root",
            label="Artifact root",
            missing_summary="Artifact root can be created on first run.",
        ),
        _check_directory_root(
            review_store_dir,
            check_id="review_store",
            label="Review store",
            missing_summary="Review store can be created on first review write.",
        ),
        _check_optional_directory_root(review_delivery_dir),
        _check_catalog(
            check_id="tool_catalog",
            label="Tool catalog",
            item_label="tool",
            ids=tool_ids,
        ),
        _check_catalog(
            check_id="workflow_catalog",
            label="Workflow catalog",
            item_label="workflow",
            ids=workflow_ids,
        ),
    ]
    warning_items = [item for item in checks if item["status"] == "warning"]
    error_items = [item for item in checks if item["status"] == "error"]
    if error_items:
        status = "error"
        readiness = "not_ready"
    elif warning_items:
        status = "degraded"
        readiness = "degraded"
    else:
        status = "ok"
        readiness = "ready"
    return {
        "ok": not error_items,
        "service": service,
        "status": status,
        "readiness": readiness,
        "warnings": [_check_message(item) for item in warning_items],
        "errors": [_check_message(item) for item in error_items],
        "summary": {
            "check_count": len(checks),
            "ok_count": len([item for item in checks if item["status"] == "ok"]),
            "warning_count": len(warning_items),
            "error_count": len(error_items),
        },
        "configuration": {
            "defaults": {
                "operator_id": default_operator_id,
                "workspace_id": default_workspace_id,
            },
            "paths": {
                "registry_path": str(Path(registry_path).expanduser().resolve()),
                "artifact_root": str(Path(artifact_root).expanduser().resolve()),
                "review_store_dir": str(Path(review_store_dir).expanduser().resolve()),
                "review_delivery_dir": (
                    str(Path(review_delivery_dir).expanduser().resolve()) if review_delivery_dir is not None else None
                ),
            },
            "catalog": {
                "tool_count": len(tool_ids),
                "tool_ids": sorted(tool_ids),
                "workflow_count": len(workflow_ids),
                "workflow_ids": sorted(workflow_ids),
            },
        },
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "cwd": str(Path.cwd().resolve()),
            "service_version": version,
        },
        "checks": checks,
    }


def _check_registry_path(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if target.exists():
        if target.is_dir():
            return _check_result(
                check_id="registry_path",
                status="error",
                summary="Registry path points to a directory.",
                details={"path": str(target), "exists": True, "kind": "directory"},
            )
        writable = os.access(target, os.W_OK)
        return _check_result(
            check_id="registry_path",
            status="ok" if writable else "error",
            summary="Registry file is writable." if writable else "Registry file is not writable.",
            details={"path": str(target), "exists": True, "kind": "file", "writable": writable},
        )

    parent = _nearest_existing_parent(target)
    writable = _path_is_creatable(target)
    return _check_result(
        check_id="registry_path",
        status="ok" if writable else "error",
        summary="Registry file can be created." if writable else "Registry file cannot be created.",
        details={
            "path": str(target),
            "exists": False,
            "parent": str(parent),
            "parent_writable": writable,
        },
    )


def _check_directory_root(
    path: str | Path,
    *,
    check_id: str,
    label: str,
    missing_summary: str,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if target.exists():
        if not target.is_dir():
            return _check_result(
                check_id=check_id,
                status="error",
                summary=f"{label} path points to a file.",
                details={"path": str(target), "exists": True, "kind": "file"},
            )
        writable = os.access(target, os.W_OK | os.X_OK)
        return _check_result(
            check_id=check_id,
            status="ok" if writable else "error",
            summary=f"{label} directory is writable." if writable else f"{label} directory is not writable.",
            details={"path": str(target), "exists": True, "kind": "directory", "writable": writable},
        )

    parent = _nearest_existing_parent(target)
    writable = _path_is_creatable(target)
    return _check_result(
        check_id=check_id,
        status="ok" if writable else "error",
        summary=missing_summary if writable else f"{label} cannot be created.",
        details={
            "path": str(target),
            "exists": False,
            "parent": str(parent),
            "parent_writable": writable,
        },
    )


def _check_optional_directory_root(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return _check_result(
            check_id="review_delivery",
            status="warning",
            summary="Review delivery directory is not configured.",
            details={"configured": False, "delivery_enabled": False},
        )
    result = _check_directory_root(
        path,
        check_id="review_delivery",
        label="Review delivery",
        missing_summary="Review delivery directory can be created on first delivery.",
    )
    result["details"]["configured"] = True
    result["details"]["delivery_enabled"] = result["status"] != "error"
    return result


def _check_catalog(*, check_id: str, label: str, item_label: str, ids: list[str]) -> dict[str, Any]:
    sorted_ids = sorted(str(item) for item in ids)
    if not sorted_ids:
        return _check_result(
            check_id=check_id,
            status="warning",
            summary=f"{label} is empty.",
            details={"count": 0, "ids": []},
        )
    return _check_result(
        check_id=check_id,
        status="ok",
        summary=f"{label} is loaded.",
        details={"count": len(sorted_ids), "ids": sorted_ids, "item_label": item_label},
    )


def _check_result(*, check_id: str, status: str, summary: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "summary": summary,
        "details": details,
    }


def _check_message(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_id": check["check_id"],
        "status": check["status"],
        "summary": check["summary"],
    }


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            return current
        current = current.parent
    return current


def _path_is_creatable(path: Path) -> bool:
    parent = _nearest_existing_parent(path)
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
