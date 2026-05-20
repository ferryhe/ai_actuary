"""Shared CLI helpers for reserving workflow tool entrypoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from reserving_workflow.artifacts.storage import read_json_artifact, write_json_artifact


class ToolCliError(Exception):
    def __init__(self, message: str, *, category: str = "execution_error", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.category = category
        self.details = details or {}


ModelT = TypeVar("ModelT", bound=BaseModel)


class ToolArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that routes parse errors through the tool JSON contract."""

    def error(self, message: str) -> None:
        raise ToolCliError(message, category="validation_error", details={"usage": self.format_usage().strip()})


def parse_args(tool_id: str, parser: argparse.ArgumentParser, argv: list[str] | None = None) -> argparse.Namespace:
    try:
        return parser.parse_args(argv)
    except ToolCliError as exc:
        _print_error(tool_id, str(exc), category=exc.category, details=exc.details)
        raise SystemExit(1) from exc


def run_tool(tool_id: str, action) -> int:
    try:
        outputs = action()
        payload = {
            "ok": True,
            "status": "ok",
            "tool_id": tool_id,
            "outputs": _stringify_paths(outputs),
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except ValidationError as exc:
        return _print_error(tool_id, str(exc), category="validation_error", details={"errors": exc.errors()})
    except ToolCliError as exc:
        return _print_error(tool_id, str(exc), category=exc.category, details=exc.details)
    except FileNotFoundError as exc:
        return _print_error(tool_id, str(exc), category="io_error")
    except ValueError as exc:
        return _print_error(tool_id, str(exc), category="validation_error")
    except Exception as exc:  # pragma: no cover - defensive fallback
        return _print_error(tool_id, str(exc), category="execution_error")



def load_model(path: str | Path, model: type[ModelT]) -> ModelT:
    return model.model_validate(load_json(path))



def load_json(path: str | Path) -> dict[str, Any]:
    return read_json_artifact(Path(path).expanduser().resolve())



def write_model(path: str | Path, model: BaseModel) -> Path:
    return write_json(path, model.model_dump(mode="json"))



def write_json(path: str | Path, payload: Any) -> Path:
    return write_json_artifact(Path(path).expanduser().resolve(), payload)



def resolve_output_path(path: str | None, *, default_dir: str | Path, filename: str) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return (Path(default_dir).expanduser().resolve() / filename).resolve()



def manifest_artifact_dir(manifest_path: str | Path, manifest_payload: dict[str, Any] | None = None) -> Path:
    manifest_file = Path(manifest_path).expanduser().resolve()
    artifact_root = (manifest_payload or {}).get("artifact_root")
    if artifact_root:
        root = Path(str(artifact_root)).expanduser()
        if root.is_absolute():
            return root.resolve()
        return (manifest_file.parent / root).resolve()
    return manifest_file.parent



def _print_error(tool_id: str, message: str, *, category: str, details: dict[str, Any] | None = None) -> int:
    payload = {
        "ok": False,
        "status": "error",
        "tool_id": tool_id,
        "error_category": category,
        "message": message,
    }
    if details:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False))
    return 1



def _stringify_paths(payload: Any) -> Any:
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, dict):
        return {str(key): _stringify_paths(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_stringify_paths(item) for item in payload]
    return payload
