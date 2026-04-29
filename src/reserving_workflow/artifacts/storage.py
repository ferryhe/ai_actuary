"""Shared local artifact storage helpers for AI Actuary artifact-backed workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def resolve_artifact_root(root: str | Path) -> Path:
    path = Path(root).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_artifact_path(root: str | Path, relative_path: str | Path) -> Path:
    base = resolve_artifact_root(root)
    path = (base / Path(relative_path)).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_json_artifact(path: str | Path, payload: Any) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_to_serializable(payload), indent=2, sort_keys=True), encoding="utf-8")
    return target


def write_text_artifact(path: str | Path, content: str) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def read_json_artifact(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))


def _to_serializable(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, dict):
        return {str(key): _to_serializable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_to_serializable(item) for item in payload]
    return payload
