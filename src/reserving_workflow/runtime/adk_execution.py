"""Trusted ADK execution profile, canonical provenance, and start validation."""

from __future__ import annotations

import hashlib
import json
import math
import stat
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ADK_WORKSPACE_ID = "adk-development"
ADK_SOURCE = "adk-developer"
ADK_CAPABILITY = "adk-developer"
PROVENANCE_SCHEMA_VERSION = "1.0"
MAX_ADK_INPUT_BYTES = 65_536
MAX_ADK_INPUT_DEPTH = 8
MAX_ADK_INPUT_NODES = 2_048
MAX_ADK_INPUT_KEYS = 1_024
MAX_ADK_LIST_ITEMS = 512
MAX_ADK_STRING_LENGTH = 4_096
MAX_ADK_KEY_LENGTH = 128
MAX_ADK_TRIANGLE_ROWS = 256
MAX_ADK_SUMMARY_BYTES = 2_048
MAX_ADK_SUMMARY_KEYS = 8
ALLOWED_ADK_WORKFLOWS = frozenset({"chainladder-basic", "chainladder-validated"})
EXPECTED_ARTIFACT_TYPES: dict[str, tuple[str, ...]] = {
    "chainladder-basic": (
        "run_manifest",
        "workflow_summary",
        "deterministic_result",
        "review_packet",
    ),
    "chainladder-validated": (
        "run_manifest",
        "workflow_summary",
        "validation_result",
        "deterministic_result",
        "review_packet",
    ),
}


class AdkStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    case_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    adk_app: str
    adk_session_id: str
    adk_invocation_id: str
    parent_run_id: str | None = None
    draft_workflow_digest: None = None

    @field_validator(
        "workflow_id", "case_id", "adk_app", "adk_session_id", "adk_invocation_id", "parent_run_id"
    )
    @classmethod
    def _bounded_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = str(value)
        if not candidate or len(candidate) > 128 or not candidate[0].isalnum():
            raise ValueError("Identifiers must be bounded safe logical values")
        if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in candidate):
            raise ValueError("Identifiers must be bounded safe logical values")
        return candidate

    @model_validator(mode="after")
    def _validate_scope_and_inputs(self) -> "AdkStartRequest":
        if self.workflow_id not in ALLOWED_ADK_WORKFLOWS:
            raise ValueError("Workflow is not published for ADK execution")
        _reject_storage_like_values(self.inputs)
        validate_adk_inputs(self.inputs)
        return self


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def request_fingerprint(request: AdkStartRequest) -> str:
    return hashlib.sha256(
        canonical_json(request.model_dump(mode="json", exclude_none=True)).encode("utf-8")
    ).hexdigest()


def validate_adk_inputs(inputs: Any) -> dict[str, int]:
    if not isinstance(inputs, dict):
        raise ValueError("adk_inputs_invalid")
    node_count = 0
    key_count = 0
    max_depth = 0
    stack: list[tuple[Any, int, str | None]] = [(inputs, 1, None)]
    while stack:
        value, depth, parent_key = stack.pop()
        node_count += 1
        max_depth = max(max_depth, depth)
        if node_count > MAX_ADK_INPUT_NODES or depth > MAX_ADK_INPUT_DEPTH:
            raise ValueError("adk_inputs_too_complex")
        if isinstance(value, dict):
            key_count += len(value)
            if key_count > MAX_ADK_INPUT_KEYS:
                raise ValueError("adk_inputs_too_complex")
            for key, nested in value.items():
                if not isinstance(key, str) or not key or len(key) > MAX_ADK_KEY_LENGTH:
                    raise ValueError("adk_input_key_invalid")
                stack.append((nested, depth + 1, key))
        elif isinstance(value, list):
            if len(value) > MAX_ADK_LIST_ITEMS:
                raise ValueError("adk_inputs_too_large")
            if parent_key is not None and "triangle" in parent_key.casefold():
                if len(value) > MAX_ADK_TRIANGLE_ROWS:
                    raise ValueError("adk_triangle_too_large")
            stack.extend((nested, depth + 1, parent_key) for nested in value)
        elif isinstance(value, str):
            if len(value) > MAX_ADK_STRING_LENGTH:
                raise ValueError("adk_input_string_too_long")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("adk_input_nonfinite")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("adk_input_type_invalid")
    try:
        serialized = canonical_json(inputs).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("adk_inputs_invalid") from exc
    if len(serialized) > MAX_ADK_INPUT_BYTES:
        raise ValueError("adk_inputs_too_large")
    return {
        "serialized_bytes": len(serialized),
        "node_count": node_count,
        "key_count": key_count,
        "max_depth": max_depth,
    }


def summarize_adk_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    metrics = validate_adk_inputs(inputs)
    shapes: dict[str, str] = {}
    selected_keys = sorted(inputs)[:MAX_ADK_SUMMARY_KEYS]
    for key in selected_keys:
        value = inputs[key]
        if isinstance(value, dict):
            shapes[key] = f"object:{len(value)}"
        elif isinstance(value, list):
            shapes[key] = f"list:{len(value)}"
        elif value is None:
            shapes[key] = "null"
        elif isinstance(value, bool):
            shapes[key] = "boolean"
        elif isinstance(value, (int, float)):
            shapes[key] = "number"
        else:
            shapes[key] = "string"
    summary = {
        **metrics,
        "top_level_shapes": shapes,
        "omitted_top_level_key_count": len(inputs) - len(selected_keys),
        "input_digest": hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest(),
    }
    while (
        len(canonical_json(summary).encode("utf-8")) > MAX_ADK_SUMMARY_BYTES
        and shapes
    ):
        shapes.pop(next(reversed(shapes)))
        summary["omitted_top_level_key_count"] = len(inputs) - len(shapes)
    if len(canonical_json(summary).encode("utf-8")) > MAX_ADK_SUMMARY_BYTES:
        raise ValueError("adk_input_summary_too_large")
    return summary


def workflow_digest(workflow_entry: Any) -> str:
    payload = workflow_entry.model_dump(mode="json") if hasattr(workflow_entry, "model_dump") else workflow_entry
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_adk_provenance(request: AdkStartRequest, *, workflow_entry: Any) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "source": ADK_SOURCE,
        "workflow_origin": "published",
        "workflow_id": request.workflow_id,
        "workflow_digest": workflow_digest(workflow_entry),
        "input_digest": hashlib.sha256(canonical_json(request.inputs).encode("utf-8")).hexdigest(),
        "adk_app": request.adk_app,
        "adk_session_id": request.adk_session_id,
        "adk_invocation_id": request.adk_invocation_id,
        "correlation_id": f"corr_{uuid.uuid4().hex}",
        "capability_class": ADK_CAPABILITY,
    }
    if request.parent_run_id is not None:
        provenance["parent_run_id"] = request.parent_run_id
    validate_adk_provenance(provenance)
    return provenance


def validate_adk_provenance(provenance: Any) -> dict[str, Any]:
    if not isinstance(provenance, dict):
        raise ValueError("adk_provenance_missing")
    required = {
        "provenance_schema_version",
        "source",
        "workflow_origin",
        "workflow_id",
        "workflow_digest",
        "input_digest",
        "adk_app",
        "adk_session_id",
        "adk_invocation_id",
        "correlation_id",
        "capability_class",
    }
    if required - provenance.keys():
        raise ValueError("adk_provenance_missing")
    if provenance.get("provenance_schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ValueError("adk_provenance_invalid")
    if provenance.get("source") != ADK_SOURCE or provenance.get("workflow_origin") != "published":
        raise ValueError("adk_provenance_invalid")
    if provenance.get("capability_class") != ADK_CAPABILITY:
        raise ValueError("adk_provenance_invalid")
    if provenance.get("workflow_id") not in ALLOWED_ADK_WORKFLOWS:
        raise ValueError("adk_provenance_invalid")
    if provenance.get("draft_workflow_digest") not in (None, ""):
        raise ValueError("draft_workflow_forbidden")
    for digest_name in ("workflow_digest", "input_digest"):
        digest = provenance.get(digest_name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("adk_provenance_invalid")
    return dict(provenance)


def prepare_isolated_run_root(root: str | Path, run_id: str) -> Path:
    base = Path(root).expanduser().absolute()
    _ensure_safe_directory_chain(base)
    target = base / run_id
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    _ensure_safe_directory(target)
    if target.parent != base:
        raise ValueError("adk_artifact_boundary_violation")
    return target


def _ensure_safe_directory_chain(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    while True:
        _ensure_safe_directory(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        _ensure_safe_directory(directory.parent)
        directory.mkdir(mode=0o700, exist_ok=False)
        _ensure_safe_directory(directory)


def _ensure_safe_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("adk_artifact_boundary_violation")
    file_attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if file_attributes & reparse_flag:
        raise ValueError("adk_artifact_boundary_violation")


def _reject_storage_like_values(payload: Any) -> None:
    stack: list[Any] = [payload]
    forbidden_keys = {
        "artifact_dir", "artifact_root", "output_dir", "review_dir", "review_store_dir",
        "registry_path", "workspace_id", "source", "created_by", "operator_id", "correlation_id",
    }
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in forbidden_keys:
                    raise ValueError("caller_selected_scope_forbidden")
                stack.append(nested)
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str):
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                raise ValueError("external_source_forbidden")
            if value.startswith(("/", "\\")) or (len(value) >= 3 and value[1:3] in {":\\", ":/"}):
                raise ValueError("absolute_source_forbidden")
            normalized = value.replace("\\", "/")
            if any(part == ".." for part in normalized.split("/")) or any(char in value for char in "*?[]{}"):
                raise ValueError("unsafe_source_forbidden")


__all__ = [
    "ADK_CAPABILITY",
    "ADK_SOURCE",
    "ADK_WORKSPACE_ID",
    "ALLOWED_ADK_WORKFLOWS",
    "AdkStartRequest",
    "EXPECTED_ARTIFACT_TYPES",
    "PROVENANCE_SCHEMA_VERSION",
    "MAX_ADK_INPUT_BYTES",
    "build_adk_provenance",
    "canonical_json",
    "prepare_isolated_run_root",
    "request_fingerprint",
    "summarize_adk_inputs",
    "validate_adk_inputs",
    "validate_adk_provenance",
    "workflow_digest",
]
