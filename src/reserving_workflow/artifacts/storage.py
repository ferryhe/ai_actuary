"""Compatibility wrapper over the local artifact-store adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reserving_workflow.storage.local import (
    LocalArtifactStore,
    resolve_artifact_path as _resolve_artifact_path,
    resolve_artifact_root as _resolve_artifact_root,
)


def resolve_artifact_root(root: str | Path) -> Path:
    return _resolve_artifact_root(root)


def resolve_artifact_path(root: str | Path, relative_path: str | Path) -> Path:
    return _resolve_artifact_path(root, relative_path)


def write_json_artifact(path: str | Path, payload: Any) -> Path:
    target = Path(path).expanduser().resolve()
    return LocalArtifactStore().write_artifact(
        root=target.parent,
        relative_path=target.name,
        payload=payload,
        format="json",
    )


def write_text_artifact(path: str | Path, content: str) -> Path:
    target = Path(path).expanduser().resolve()
    return LocalArtifactStore().write_artifact(
        root=target.parent,
        relative_path=target.name,
        payload=content,
        format="text",
    )


def read_json_artifact(path: str | Path) -> dict[str, Any]:
    return LocalArtifactStore().read_artifact(path, format="json")
