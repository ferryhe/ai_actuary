"""Safe agent projections and descriptor-pinned JSON artifact reads."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from reserving_workflow.runtime.redaction import (
    is_sensitive_key,
    looks_like_absolute_path,
    looks_sensitive,
    sanitize_for_runtime,
)
from reserving_workflow.contracts import Review, Run, RunEvent
from reserving_workflow.storage.safe_json import (
    MAX_ARTIFACT_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_FIELDS,
    MAX_JSON_LIST_LENGTH,
    MAX_JSON_NODES,
    MAX_JSON_STRING_LENGTH,
    PinnedJsonRoot as TrustedArtifactRoot,
    SafeJsonReadError as ArtifactProjectionReadError,
    read_bounded_json_object,
    stat_regular_artifact,
)

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


MAX_PROJECTED_OUTPUT_BYTES = 500_000

_PEM_PRIVATE_KEY_MARKER = re.compile(
    r"(?i)-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
_URL_USERINFO = re.compile(
    r"(?i)\b[A-Z][A-Z0-9+.-]{1,31}://[^\s/@]{1,256}@"
)


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
        "run_manifest": {"case_id": str, "run_id": str, "artifact_paths": dict},
        "validated_input": {"case_id": str, "tool_id": str, "inputs": dict},
        "deterministic_result": {"case_id": str, "method": str},
        "narrative_draft": {"case_id": str, "summary": str},
        "constitution_check": {"case_id": str, "status": str},
        "review_packet": {"case_id": str, "run_id": str, "status": str},
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


def validate_projected_artifact_payload_schema(
    artifact_id: str,
    payload: dict[str, Any],
) -> None:
    """Validate an already allowlisted, path-free server projection."""

    try:
        spec = ARTIFACT_PROJECTION_SPECS[artifact_id]
    except KeyError as exc:
        raise ArtifactProjectionReadError(
            "artifact_schema_mismatch",
            "Artifact projection does not match the expected safe schema.",
            status_code=422,
        ) from exc
    if set(payload) - set(spec.fields):
        raise ArtifactProjectionReadError(
            "artifact_schema_mismatch",
            "Artifact projection does not match the expected safe schema.",
            status_code=422,
        )
    try:
        validate_artifact_projection_schema(
            artifact_id,
            {
                **payload,
                **({"artifact_paths": {}} if artifact_id == "run_manifest" else {}),
            },
        )
    except ArtifactProjectionReadError as exc:
        raise ArtifactProjectionReadError(
            "artifact_schema_mismatch",
            "Artifact projection does not match the expected safe schema.",
            status_code=422,
        ) from exc


def build_artifact_projection(
    *,
    run_id: str,
    artifact_id: str,
    case_id: str | None,
    tool_id: str | None,
    payload: dict[str, Any],
) -> ArtifactProjection:
    spec = ARTIFACT_PROJECTION_SPECS[artifact_id]
    return ArtifactProjection(
        run_id=run_id,
        artifact_id=artifact_id,
        case_id=case_id,
        tool_id=tool_id,
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
    validate_projected_artifact_payload_schema(value.artifact_id, value.data)
    return {
        "run_id": _safe_json_value(value.run_id),
        "artifact_id": _safe_json_value(value.artifact_id),
        "status": value.status,
        "provenance": value.provenance,
        "data": _safe_json_value(value.data),
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


def _safe_json_value(value: Any) -> Any:
    return sanitize_for_runtime(value)


def _forbidden_key(key: str) -> bool:
    return is_sensitive_key(key)


def _legacy_forbidden_key(key: str) -> bool:
    candidate = key.strip()
    if (
        _looks_like_absolute_path(candidate)
        or _PEM_PRIVATE_KEY_MARKER.search(candidate)
        or _URL_USERINFO.search(candidate)
    ):
        return True
    tokens = _semantic_tokens(candidate)
    compact = "".join(tokens)
    if _has_sensitive_semantics(tokens, compact=compact):
        return True
    path_suffixes = (
        "path",
        "paths",
        "root",
        "roots",
        "filename",
        "filenames",
        "url",
        "urls",
    )
    if compact != "curl" and compact.endswith(path_suffixes):
        return True
    if compact in {"reviewdelivery", "operatorparams"}:
        return True
    if "apikey" in compact or "accesskey" in compact:
        return True
    if compact in {
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "session",
        "sessionid",
        "authheader",
        "authorization",
        "apikey",
        "accesskey",
        "privatekey",
        "clientsecret",
        "secretkey",
        "accesstoken",
        "refreshtoken",
        "sharedsecret",
        "tokenvalue",
        "authtoken",
        "registrypath",
        "filename",
        "basicvalue",
    }:
        return True
    sensitive_tokens = set(tokens) & {
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "passphrases",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "session",
        "sessions",
        "secret",
        "auth",
        "authentication",
        "authorization",
        "header",
        "headers",
        "private",
    }
    if sensitive_tokens:
        return True
    if any(
        token == "token"
        and (index + 1 >= len(tokens) or tokens[index + 1] != "count")
        for index, token in enumerate(tokens)
    ):
        return True
    normalized = "_".join(tokens)
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
    return bool(tokens and tokens[-1] in {"path", "paths", "root"})


def _semantic_tokens(value: str) -> tuple[str, ...]:
    """Split bounded key text consistently across separators, case, and acronyms."""

    tokens: list[str] = []
    current: list[str] = []
    for index, character in enumerate(value):
        if not character.isalnum():
            if current:
                tokens.append("".join(current).casefold())
                current = []
            continue
        previous = value[index - 1] if index else ""
        following = value[index + 1] if index + 1 < len(value) else ""
        if (
            character.isupper()
            and current
            and (
                previous.islower()
                or previous.isdigit()
                or (previous.isupper() and following.islower())
            )
        ):
            tokens.append("".join(current).casefold())
            current = []
        current.append(character)
    if current:
        tokens.append("".join(current).casefold())
    return tuple(tokens)


def _contains_sensitive_assignment(value: str) -> bool:
    """Inspect bounded assignment keys without a backtracking expression."""

    assignment_boundary = "\r\n;,&|()[]{}"
    segment_start = 0
    for index, character in enumerate(value):
        if character in assignment_boundary:
            segment_start = index + 1
            continue
        if character not in {":", "="}:
            continue
        start = max(segment_start, index - 128)
        candidate = value[start:index].strip()
        if candidate and _forbidden_key(candidate):
            return True
        segment_start = index + 1
    return False


def _has_sensitive_semantics(
    tokens: tuple[str, ...],
    *,
    compact: str | None = None,
) -> bool:
    """Classify credential language consistently in keys and free-form values."""

    joined = compact if compact is not None else "".join(tokens)
    if any(
        marker in joined
        for marker in (
            "apikey",
            "accesskey",
            "privatekey",
            "secretkey",
            "authheader",
        )
    ):
        return True
    if set(tokens) & {
        "auth",
        "authentication",
        "authorization",
        "header",
        "headers",
    }:
        return True
    return any(
        token == "token"
        and (index + 1 >= len(tokens) or tokens[index + 1] != "count")
        for index, token in enumerate(tokens)
    )


def _looks_like_absolute_path(value: str) -> bool:
    return looks_like_absolute_path(value)


def _looks_sensitive(value: str) -> bool:
    return looks_sensitive(value)


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
    "TrustedArtifactRoot",
    "build_artifact_projection",
    "project_artifact_projection",
    "project_artifact_payload",
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
    "stat_regular_artifact",
    "validate_artifact_projection_schema",
    "validate_projected_artifact_payload_schema",
]
