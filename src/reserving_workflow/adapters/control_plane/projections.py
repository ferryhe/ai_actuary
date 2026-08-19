"""Safe agent projections and descriptor-pinned JSON artifact reads."""

from __future__ import annotations

import errno
import json
import math
import ntpath
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reserving_workflow.contracts import Review, Run, RunEvent

from .contracts import (
    ArtifactMetadata,
    ArtifactProjection,
    ArtifactProvenance,
    HealthStatus,
    PreflightStatus,
    ToolDetail,
    ToolSummary,
    Workflow,
    WorkflowSummary,
)


MAX_ARTIFACT_BYTES = 1_000_000
MAX_JSON_DEPTH = 20
MAX_JSON_FIELDS = 5_000
MAX_JSON_NODES = 20_000
MAX_JSON_LIST_LENGTH = 2_000
MAX_JSON_STRING_LENGTH = 100_000
MAX_PROJECTED_OUTPUT_BYTES = 500_000


@dataclass(frozen=True)
class ArtifactProjectionSpec:
    filename: str
    provenance: ArtifactProvenance
    fields: tuple[str, ...]


ARTIFACT_PROJECTION_SPECS: dict[str, ArtifactProjectionSpec] = {
    "run_manifest": ArtifactProjectionSpec(
        filename="run_manifest.json",
        provenance="system_manifest",
        fields=("case_id", "run_id", "created_by", "status", "version", "tool_id", "workflow_id"),
    ),
    "validated_input": ArtifactProjectionSpec(
        filename="validated_input.json",
        provenance="deterministic",
        fields=("case_id", "run_id", "tool_id", "inputs", "validation_status"),
    ),
    "deterministic_result": ArtifactProjectionSpec(
        filename="deterministic_result.json",
        provenance="deterministic",
        fields=(
            "case_id",
            "run_id",
            "tool_id",
            "method",
            "reserve_summary",
            "diagnostics",
            "metrics",
            "model",
            "result_count",
            "results",
        ),
    ),
    "narrative_draft": ArtifactProjectionSpec(
        filename="narrative_draft.json",
        provenance="model_generated",
        fields=("case_id", "run_id", "summary", "key_points", "cited_values", "model"),
    ),
    "constitution_check": ArtifactProjectionSpec(
        filename="constitution_check.json",
        provenance="deterministic",
        fields=(
            "case_id",
            "run_id",
            "status",
            "hard_constraints",
            "soft_guidance",
            "review_triggers",
            "failed_checks",
        ),
    ),
    "review_packet": ArtifactProjectionSpec(
        filename="review_packet.json",
        provenance="review",
        fields=(
            "case_id",
            "run_id",
            "status",
            "summary",
            "failed_checks",
            "review_reasons",
            "assigned_to",
        ),
    ),
}


class ArtifactProjectionReadError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def read_bounded_json_object(
    artifact_root: str | Path,
    relative_path: str,
    *,
    namespace: str = "artifact",
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> dict[str, Any]:
    """Open each component without following links, pin the final fd, then parse."""

    parts = _safe_relative_parts(relative_path, namespace=namespace)
    root = Path(os.path.abspath(os.path.expanduser(str(artifact_root))))
    try:
        descriptor = _open_descriptor_no_follow(root, parts, namespace=namespace)
    except ArtifactProjectionReadError:
        raise
    except FileNotFoundError as exc:
        raise _read_error(namespace, "missing", "Registered JSON artifact is missing.", status_code=404) from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise _read_error(namespace, "path_rejected", "Registered artifact path failed safety validation.") from exc
        raise _read_error(namespace, "unreadable", "Registered JSON artifact could not be read safely.") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _read_error(namespace, "not_regular", "Registered artifact must be a regular file.")
        if metadata.st_size > max_bytes:
            raise _read_error(
                namespace,
                "size_exceeded",
                "Registered JSON artifact exceeds the size limit.",
                status_code=413,
            )
        content = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise _read_error(
                    namespace,
                    "size_exceeded",
                    "Registered JSON artifact exceeds the size limit.",
                    status_code=413,
                )
    except ArtifactProjectionReadError:
        raise
    except OSError as exc:
        raise _read_error(namespace, "unreadable", "Registered JSON artifact could not be read safely.") from exc
    finally:
        os.close(descriptor)

    try:
        text = bytes(content).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _read_error(namespace, "invalid_encoding", "Registered JSON artifact is not valid UTF-8.") from exc
    try:
        payload = json.loads(text, parse_constant=_reject_json_constant)
    except RecursionError as exc:
        raise _read_error(
            namespace,
            "depth_exceeded",
            "Registered JSON artifact is nested too deeply.",
            status_code=422,
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise _read_error(namespace, "invalid_json", "Registered artifact is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise _read_error(namespace, "invalid_shape", "Registered artifact must be a JSON object.", status_code=422)
    _validate_json_complexity(payload, namespace=namespace)
    return payload


def project_artifact_payload(artifact_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    validate_artifact_projection_schema(artifact_id, payload)
    spec = ARTIFACT_PROJECTION_SPECS[artifact_id]
    projected = {
        key: _safe_json_value(payload[key])
        for key in spec.fields
        if key in payload and not _forbidden_key(key)
    }
    serialized = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(serialized) > MAX_PROJECTED_OUTPUT_BYTES:
        raise ArtifactProjectionReadError(
            "artifact_output_limit_exceeded",
            "Artifact projection exceeds the output limit.",
            status_code=413,
        )
    return projected


def validate_artifact_projection_schema(artifact_id: str, payload: dict[str, Any]) -> None:
    """Validate the container shapes consumed by each fixed projection."""

    required_shapes: dict[str, dict[str, type | tuple[type, ...]]] = {
        "run_manifest": {"run_id": str, "artifact_paths": dict},
        "validated_input": {"inputs": dict},
    }
    optional_shapes: dict[str, dict[str, type | tuple[type, ...]]] = {
        "run_manifest": {
            "case_id": str,
            "created_by": str,
            "status": str,
            "version": (str, int),
            "tool_id": str,
            "workflow_id": str,
        },
        "validated_input": {
            "case_id": str,
            "run_id": str,
            "tool_id": str,
            "validation_status": str,
        },
        "deterministic_result": {
            "case_id": str,
            "run_id": str,
            "tool_id": str,
            "method": str,
            "reserve_summary": dict,
            "diagnostics": dict,
            "metrics": dict,
            "model": str,
            "result_count": int,
            "results": list,
        },
        "narrative_draft": {
            "case_id": str,
            "run_id": str,
            "summary": str,
            "key_points": list,
            "cited_values": dict,
            "model": str,
        },
        "constitution_check": {
            "case_id": str,
            "run_id": str,
            "status": str,
            "hard_constraints": list,
            "soft_guidance": list,
            "review_triggers": list,
            "failed_checks": list,
        },
        "review_packet": {
            "case_id": str,
            "run_id": str,
            "status": str,
            "summary": str,
            "failed_checks": list,
            "review_reasons": list,
            "assigned_to": str,
        },
    }
    for field, expected_type in required_shapes.get(artifact_id, {}).items():
        if field not in payload or not isinstance(payload[field], expected_type):
            raise ArtifactProjectionReadError(
                "artifact_schema_mismatch",
                "Registered artifact does not match the projection schema.",
                status_code=422,
            )
    for field, expected_type in optional_shapes.get(artifact_id, {}).items():
        if field in payload and not isinstance(payload[field], expected_type):
            raise ArtifactProjectionReadError(
                "artifact_schema_mismatch",
                "Registered artifact does not match the projection schema.",
                status_code=422,
            )


def build_artifact_projection(
    *,
    run_id: str,
    artifact_id: str,
    payload: dict[str, Any],
) -> ArtifactProjection:
    spec = ARTIFACT_PROJECTION_SPECS[artifact_id]
    return ArtifactProjection(
        run_id=run_id,
        artifact_id=artifact_id,
        status="available",
        provenance=spec.provenance,
        data=project_artifact_payload(artifact_id, payload),
        errors=[],
    )


def project_health(value: HealthStatus) -> dict[str, Any]:
    return _safe_json_value(value.model_dump(exclude_none=True))


def project_preflight(value: PreflightStatus) -> dict[str, Any]:
    catalog = value.configuration.get("catalog")
    check_id_aliases = {
        "registry_path": "run_registry",
        "artifact_root": "artifact_storage",
        "review_store": "review_storage",
        "review_delivery": "review_delivery",
        "tool_catalog": "tool_catalog",
        "workflow_catalog": "workflow_catalog",
    }

    def project_check(check: Any) -> dict[str, Any]:
        return {
            "check_id": check_id_aliases.get(str(check.check_id), "runtime_check"),
            "status": _safe_json_value(check.status),
            "summary": _safe_json_value(check.summary),
        }

    def project_message(message: dict[str, Any]) -> dict[str, Any]:
        return {
            "check_id": check_id_aliases.get(str(message.get("check_id")), "runtime_check"),
            "status": _safe_json_value(str(message.get("status", "unknown"))),
            "summary": _safe_json_value(
                str(message.get("summary", "Runtime check reported an issue."))
            ),
        }

    return {
        "ok": value.ok,
        "service": _safe_json_value(value.service),
        "status": _safe_json_value(value.status),
        "readiness": _safe_json_value(value.readiness),
        "summary": _safe_json_value(value.summary),
        "catalog": _safe_json_value(catalog) if isinstance(catalog, dict) else {},
        "checks": [project_check(check) for check in value.checks],
        "warnings": [project_message(item) for item in value.warnings],
        "errors": [project_message(item) for item in value.errors],
    }


def project_tool(value: ToolSummary | ToolDetail) -> dict[str, Any]:
    result = {
        "tool_id": _safe_json_value(value.tool_id),
        "method": _safe_json_value(value.method),
        "title": _safe_json_value(value.title),
        "description": _safe_json_value(value.description),
        "builtin": value.builtin,
        "tags": _safe_json_value(value.tags),
        "console_defaults": _safe_json_value(value.console_defaults),
    }
    if isinstance(value, ToolDetail):
        result["input_schema"] = _safe_json_value(value.input_schema)
    return result


def project_workflow(value: WorkflowSummary | Workflow) -> dict[str, Any]:
    result = {
        "workflow_id": _safe_json_value(value.workflow_id),
        "title": _safe_json_value(value.title),
        "description": _safe_json_value(value.description),
        "builtin": value.builtin,
        "step_count": value.step_count,
    }
    if isinstance(value, Workflow):
        result["steps"] = [
            {
                "step_id": _safe_json_value(step.step_id),
                "tool_id": _safe_json_value(step.tool_id),
                "title": _safe_json_value(step.title),
                "description": _safe_json_value(step.description),
                "step_kind": step.step_kind,
                "order": step.order,
                "inputs": _safe_json_value(step.inputs),
                "status": step.status,
            }
            for step in value.steps
        ]
    return result


def project_run(value: Run) -> dict[str, Any]:
    return {
        key: _safe_json_value(getattr(value, key))
        for key in (
            "run_id",
            "case_id",
            "status",
            "created_by",
            "operator_id",
            "workspace_id",
            "summary",
            "created_at",
            "updated_at",
            "review_required",
            "workflow_id",
        )
        if getattr(value, key) is not None
    }


def project_event(value: RunEvent) -> dict[str, Any]:
    return {
        key: _safe_json_value(getattr(value, key))
        for key in ("type", "run_id", "timestamp", "status", "summary")
        if getattr(value, key) is not None
    }


def project_artifact_metadata(value: ArtifactMetadata) -> dict[str, Any]:
    provenance = value.provenance or provenance_for_artifact(value.artifact_id)
    return {
        "artifact_id": _safe_json_value(value.artifact_id),
        "label": _safe_json_value(value.label or value.artifact_id.replace("_", " ")),
        "category": _safe_json_value(value.category or _category_for_artifact(value.artifact_id)),
        "present": value.present,
        **({"provenance": _safe_json_value(provenance)} if provenance is not None else {}),
    }


def project_artifact_projection(value: ArtifactProjection) -> dict[str, Any]:
    return {
        "run_id": _safe_json_value(value.run_id),
        "artifact_id": _safe_json_value(value.artifact_id),
        "status": value.status,
        "provenance": value.provenance,
        "data": project_artifact_payload(value.artifact_id, value.data),
        "errors": [
            {
                "code": _safe_json_value(error.code),
                "message": _safe_json_value(error.message),
            }
            for error in value.errors
        ],
    }


def project_review(value: Review) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": value.status,
        "review_required": value.review_required,
        "reason_codes": _safe_json_value(value.reason_codes),
    }
    for field in (
        "review_id",
        "run_id",
        "case_id",
        "workspace_id",
        "assigned_to",
        "created_at",
        "updated_at",
    ):
        if (field_value := getattr(value, field)) is not None:
            result[field] = _safe_json_value(field_value)
    if value.packet is not None:
        result["packet"] = {
            key: _safe_json_value(value.packet[key])
            for key in ARTIFACT_PROJECTION_SPECS["review_packet"].fields
            if key in value.packet and not _forbidden_key(key)
        }
    if value.decision is not None:
        decision = value.decision
        result["decision"] = {
            key: _safe_json_value(getattr(decision, key))
            for key in (
                "review_id",
                "run_id",
                "decision",
                "comment",
                "decided_by",
                "decided_at",
                "follow_up_run_id",
            )
            if getattr(decision, key) is not None
        }
        result["decision"]["artifacts"] = [
            {
                "artifact_id": _safe_json_value(item.artifact_id),
                "label": _safe_json_value(item.label),
                "present": item.present,
            }
            for item in decision.artifacts
        ]
    return result


def provenance_for_artifact(artifact_id: str) -> ArtifactProvenance | None:
    spec = ARTIFACT_PROJECTION_SPECS.get(artifact_id)
    return spec.provenance if spec is not None else None


def _category_for_artifact(artifact_id: str) -> str:
    provenance = provenance_for_artifact(artifact_id)
    return {
        "deterministic": "result",
        "model_generated": "narrative",
        "review": "review",
        "system_manifest": "system",
    }.get(provenance, "other")


def _safe_relative_parts(relative_path: str, *, namespace: str) -> tuple[str, ...]:
    raw = str(relative_path)
    parts = raw.replace("\\", "/").split("/")
    if (
        not raw
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or any(
            part.casefold() in {"?", "??", "device", "globalroot", "unc"}
            for part in parts
        )
    ):
        raise _read_error(namespace, "path_rejected", "Registered artifact path failed safety validation.")
    return tuple(parts)


def _open_descriptor_no_follow(root: Path, parts: tuple[str, ...], *, namespace: str) -> int:
    if os.name == "posix" and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd:
        return _open_descriptor_posix(root, parts, namespace=namespace)
    return _open_descriptor_fallback(root, parts, namespace=namespace)


def _open_descriptor_posix(root: Path, parts: tuple[str, ...], *, namespace: str) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    current_fd = os.open(root.anchor or os.sep, directory_flags)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        final_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        return os.open(parts[-1], final_flags, dir_fd=current_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}:
            raise _read_error(namespace, "path_rejected", "Registered artifact path failed safety validation.") from exc
        raise
    finally:
        os.close(current_fd)


def _open_descriptor_fallback(root: Path, parts: tuple[str, ...], *, namespace: str) -> int:
    if os.name != "nt":
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )

    import msvcrt

    if not root.anchor or not root.is_absolute():
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )

    parent_handles: list[int] = []
    final_handle: int | None = None
    try:
        current = Path(root.anchor)
        parent_handles.append(
            _windows_open_handle(current, expect_directory=True, namespace=namespace)
        )
        for component in root.parts[1:]:
            current /= component
            parent_handles.append(
                _windows_open_handle(current, expect_directory=True, namespace=namespace)
            )
        trusted_root_handle = parent_handles[-1]
        for component in parts[:-1]:
            current /= component
            parent_handles.append(
                _windows_open_handle(current, expect_directory=True, namespace=namespace)
            )

        final_path = current / parts[-1]
        final_handle = _windows_open_handle(
            final_path,
            expect_directory=False,
            namespace=namespace,
        )
        _windows_verify_handle_within_root(
            final_handle,
            trusted_root_handle,
            namespace=namespace,
        )
        descriptor = msvcrt.open_osfhandle(
            final_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        final_handle = None
        return descriptor
    finally:
        if final_handle is not None:
            _windows_close_handle(final_handle)
        for handle in reversed(parent_handles):
            _windows_close_handle(handle)


def _windows_open_handle(
    path: Path,
    *,
    expect_directory: bool,
    namespace: str,
) -> int:
    """Open one Windows path component without traversing its final reparse point."""

    import ctypes
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    file_attribute_directory = 0x00000010
    file_attribute_device = 0x00000040
    file_attribute_reparse_point = 0x00000400
    file_attribute_tag_info_class = 9

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        str(path),
        file_read_attributes if expect_directory else generic_read,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_backup_semantics,
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, "Windows path component is missing")
        raise OSError(error, "Windows path component could not be opened safely")

    raw_handle = int(handle)
    info = FileAttributeTagInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        file_attribute_tag_info_class,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        _windows_close_handle(raw_handle)
        raise OSError(error, "Windows path component metadata could not be read safely")

    attributes = int(info.file_attributes)
    if attributes & file_attribute_reparse_point:
        _windows_close_handle(raw_handle)
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    if expect_directory and not attributes & file_attribute_directory:
        _windows_close_handle(raw_handle)
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    if not expect_directory and attributes & (file_attribute_directory | file_attribute_device):
        _windows_close_handle(raw_handle)
        raise _read_error(
            namespace,
            "not_regular",
            "Registered artifact must be a regular file.",
        )
    return raw_handle


def _windows_verify_handle_within_root(
    final_handle: int,
    trusted_root_handle: int,
    *,
    namespace: str,
) -> None:
    """Check the fixed final object against the fixed trusted root object."""

    root_path = _windows_final_path_for_handle(trusted_root_handle)
    final_path = _windows_final_path_for_handle(final_handle)
    try:
        common = ntpath.commonpath((root_path, final_path))
    except ValueError as exc:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        ) from exc
    if ntpath.normcase(common) != ntpath.normcase(root_path):
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )


def _windows_final_path_for_handle(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            error = ctypes.get_last_error()
            raise OSError(error, "Windows handle path could not be verified safely")
        if length < size:
            value = buffer.value
            break
        size = int(length) + 1

    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\") or value.startswith("\\??\\"):
        value = value[4:]
    return ntpath.normpath(value)


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _validate_json_complexity(payload: dict[str, Any], *, namespace: str) -> None:
    field_count = 0
    node_count = 0
    stack: list[tuple[Any, int]] = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > MAX_JSON_NODES:
            raise _read_error(namespace, "node_limit_exceeded", "Registered JSON artifact is too complex.", status_code=422)
        if depth > MAX_JSON_DEPTH:
            raise _read_error(namespace, "depth_exceeded", "Registered JSON artifact is nested too deeply.", status_code=422)
        if isinstance(value, dict):
            field_count += len(value)
            if field_count > MAX_JSON_FIELDS:
                raise _read_error(namespace, "field_limit_exceeded", "Registered JSON artifact has too many fields.", status_code=422)
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _read_error(namespace, "invalid_shape", "Registered JSON artifact contains an invalid key.", status_code=422)
                if len(key) > MAX_JSON_STRING_LENGTH:
                    raise _read_error(namespace, "string_limit_exceeded", "Registered JSON artifact contains an oversized string.", status_code=422)
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            if len(value) > MAX_JSON_LIST_LENGTH:
                raise _read_error(namespace, "list_limit_exceeded", "Registered JSON artifact contains an oversized list.", status_code=422)
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and len(value) > MAX_JSON_STRING_LENGTH:
            raise _read_error(namespace, "string_limit_exceeded", "Registered JSON artifact contains an oversized string.", status_code=422)
        elif isinstance(value, float) and not math.isfinite(value):
            raise _read_error(
                namespace,
                "invalid_json",
                "Registered artifact is not valid JSON.",
            )


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_json_value(item)
            for key, item in value.items()
            if not _forbidden_key(str(key))
        }
    if isinstance(value, list):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, str):
        if _looks_like_absolute_path(value) or _looks_sensitive(value):
            return "[redacted]"
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return "[unsupported]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported]"


def _forbidden_key(key: str) -> bool:
    candidate = key.strip()
    if _looks_like_absolute_path(candidate) or _looks_sensitive(candidate):
        return True
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", candidate)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")
    compact = normalized.replace("_", "")
    if any(
        marker in compact
        for marker in (
            "password",
            "passwd",
            "passphrase",
            "credential",
            "cookie",
            "session",
            "authheader",
            "authorization",
            "apikey",
            "accesskey",
            "privatekey",
            "clientsecret",
        )
    ):
        return True
    if normalized in {
        "path",
        "paths",
        "filename",
        "url",
        "registry_path",
        "artifact_root",
        "artifact_paths",
        "manifest_path",
        "record_path",
        "json_path",
        "markdown_path",
        "review_delivery",
        "operator_params",
        "secret",
        "token",
        "api_key",
        "access_key",
        "private_key",
        "client_secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "session",
        "session_id",
        "sessionid",
        "auth",
        "authentication",
        "authorization",
        "header",
        "headers",
        "private",
    }:
        return True
    if re.search(
        r"(?:^|_)(?:passwords?|passwd|passphrases?|credentials?|cookies?|sessions?|"
        r"auth(?:entication|orization)?|headers?)(?:_|$)",
        normalized,
    ):
        return True
    return normalized.endswith(
        (
            "_path",
            "_paths",
            "_root",
            "_token",
            "_secret",
            "_api_key",
            "_access_key",
            "_private_key",
        )
    )


def _looks_like_absolute_path(value: str) -> bool:
    candidate = value.strip()
    return (
        bool(re.search(r"(?i)file:(?://|\\\\)", candidate))
        or bool(re.search(r"[A-Za-z]:[\\/]", candidate))
        or bool(re.search(r"(?:\\\\|(?<!:)//)[^\\/\s]+[\\/][^\\/\s]+", candidate))
        or bool(
            re.search(
                r"(?:^|[^A-Za-z0-9._~\\/])\\(?![\\\s])[^\\/\s]+(?:[\\/][^\\/\s]+)*",
                candidate,
            )
        )
        or bool(re.search(r"(?:^|[^A-Za-z0-9._~/])/(?![/\s])", candidate))
    )


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    if re.search(
        r"\b(?:secret|token|api[-_ ]?key|password|passphrase|credentials?|cookies?|sessionid)\b",
        lowered,
    ):
        return True
    return bool(
        re.search(
            r"(?i)(?:"
            r"\bauthorization\s*:?\s*basic\s+\S+|"
            r"\bbearer\s+\S+|"
            r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])|"
            r"\bsessionid\s*[:=]\s*\S+|"
            r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}|"
            r"(?<![A-Za-z0-9])gh[po]_[A-Za-z0-9]{8,}|"
            r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{8,}|"
            r"(?<![A-Za-z0-9])xox[a-z]-[A-Za-z0-9-]{8,}|"
            r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}|"
            r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{20,}"
            r")",
            value,
        )
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _read_error(namespace: str, suffix: str, message: str, *, status_code: int = 400) -> ArtifactProjectionReadError:
    return ArtifactProjectionReadError(f"{namespace}_{suffix}", message, status_code=status_code)


__all__ = [
    "ARTIFACT_PROJECTION_SPECS",
    "MAX_ARTIFACT_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_JSON_FIELDS",
    "MAX_JSON_LIST_LENGTH",
    "MAX_JSON_NODES",
    "MAX_JSON_STRING_LENGTH",
    "MAX_PROJECTED_OUTPUT_BYTES",
    "ArtifactProjectionReadError",
    "build_artifact_projection",
    "project_artifact_projection",
    "project_artifact_metadata",
    "project_event",
    "project_health",
    "project_preflight",
    "project_review",
    "project_run",
    "project_tool",
    "project_workflow",
    "provenance_for_artifact",
    "read_bounded_json_object",
    "validate_artifact_projection_schema",
]
