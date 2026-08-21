"""Compatibility wrapper over the local run-store adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import secrets
from pathlib import Path
from typing import Any

from reserving_workflow.storage.local import (
    LocalRunStore,
    RunNotFoundError,
    _read_registry_payload,
    _utc_now,
    _write_registry_payload,
    resolve_registry_path,
)
try:
    from reserving_workflow.storage.registry_transactions import locked_registry_transaction
except ModuleNotFoundError:  # Direct-file compatibility for the legacy CLI loaders.
    transaction_path = Path(__file__).resolve().parents[1] / "storage" / "registry_transactions.py"
    transaction_spec = importlib.util.spec_from_file_location(
        "run_registry_transactions", transaction_path
    )
    if transaction_spec is None or transaction_spec.loader is None:
        raise
    transaction_module = importlib.util.module_from_spec(transaction_spec)
    transaction_spec.loader.exec_module(transaction_module)
    locked_registry_transaction = transaction_module.locked_registry_transaction


class IdempotencyConflictError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RegistryIntegrityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


RUN_BOUND_ADK_ACTIONS = frozenset({"start_workflow_run", "rerun_run"})
DEBUG_ADK_ACTIONS = frozenset({"run_bounded_benchmark", "export_run_report"})


def _unique_value(
    seen: dict[str, str], value: Any, *, owner: str, code: str
) -> None:
    candidate = str(value or "")
    if not candidate or (candidate in seen and seen[candidate] != owner):
        raise RegistryIntegrityError(code)
    seen[candidate] = owner


def _audit_adk_payload(payload: dict[str, Any]) -> set[str]:
    runs = payload.get("runs", [])
    operations = payload.get("adk_operations", [])
    debug_operations = payload.get("adk_debug_operations", [])
    if (
        not isinstance(runs, list)
        or not isinstance(operations, list)
        or not isinstance(debug_operations, list)
    ):
        raise RegistryIntegrityError("adk_registry_binding_invalid")
    runs_by_id: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict):
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        run_id = str(run.get("run_id") or "")
        if not run_id or run_id in runs_by_id:
            raise RegistryIntegrityError("adk_registry_cardinality_conflict")
        runs_by_id[run_id] = run

    operation_run_ids = {
        str(operation.get("run_id"))
        for operation in operations
        if isinstance(operation, dict)
        and operation.get("principal") == "adk-developer"
        and operation.get("action") in RUN_BOUND_ADK_ACTIONS
    }
    authoritative_run_ids = set(operation_run_ids)
    for run_id, run in runs_by_id.items():
        provenance = run.get("provenance")
        if run.get("source") == "adk-developer" or (
            isinstance(provenance, dict) and provenance.get("source") == "adk-developer"
        ):
            authoritative_run_ids.add(run_id)

    operations_by_run: dict[str, list[dict[str, Any]]] = {}
    operation_ids: dict[str, str] = {}
    idempotency_digests: dict[str, str] = {}
    operation_correlations: dict[str, str] = {}
    for operation in operations:
        if not isinstance(operation, dict):
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        run_id = str(operation.get("run_id") or "")
        if run_id not in authoritative_run_ids:
            continue
        if (
            operation.get("principal") != "adk-developer"
            or operation.get("action") not in RUN_BOUND_ADK_ACTIONS
        ):
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        action = str(operation.get("action"))
        _unique_value(
            operation_ids,
            operation.get("operation_id"),
            owner=run_id,
            code="adk_registry_cardinality_conflict",
        )
        _unique_value(
            idempotency_digests,
            f"{action}:{operation.get('idempotency_key_digest')}",
            owner=run_id,
            code="adk_registry_cardinality_conflict",
        )
        _unique_value(
            operation_correlations,
            operation.get("correlation_id"),
            owner=run_id,
            code="adk_registry_cardinality_conflict",
        )
        for digest_field in (
            "idempotency_key_digest",
            "request_fingerprint",
            "confirmation_grant_digest",
        ):
            digest = operation.get(digest_field)
            if not isinstance(digest, str) or len(digest) != 64:
                raise RegistryIntegrityError("adk_registry_binding_invalid")
        operations_by_run.setdefault(run_id, []).append(operation)

    debug_operation_ids: dict[str, str] = {}
    debug_idempotency_digests: dict[str, str] = {}
    for operation in debug_operations:
        if not isinstance(operation, dict):
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        if (
            operation.get("principal") != "adk-developer"
            or operation.get("action") not in DEBUG_ADK_ACTIONS
            or not isinstance(operation.get("result"), dict)
        ):
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        action = str(operation.get("action"))
        owner = f"{action}:{operation.get('object_id')}"
        _unique_value(
            debug_operation_ids,
            operation.get("operation_id"),
            owner=owner,
            code="adk_registry_cardinality_conflict",
        )
        _unique_value(
            debug_idempotency_digests,
            f"{action}:{operation.get('idempotency_key_digest')}",
            owner=owner,
            code="adk_registry_cardinality_conflict",
        )
        for digest_field in (
            "idempotency_key_digest",
            "request_fingerprint",
            "confirmation_grant_digest",
        ):
            digest = operation.get(digest_field)
            if not isinstance(digest, str) or len(digest) != 64:
                raise RegistryIntegrityError("adk_registry_binding_invalid")

    correlations: dict[str, str] = {}
    invocations: dict[str, str] = {}
    traces: dict[str, str] = {}
    if authoritative_run_ids:
        from reserving_workflow.runtime.adk_execution import validate_adk_provenance

    for run_id in authoritative_run_ids:
        run = runs_by_id.get(run_id)
        if run is None:
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        provenance = run.get("provenance")
        try:
            validated = validate_adk_provenance(provenance)
        except ValueError as exc:
            raise RegistryIntegrityError("adk_provenance_invalid") from exc
        if (
            run.get("source") != "adk-developer"
            or run.get("workspace_id") != "adk-development"
            or validated.get("source") != run.get("source")
        ):
            raise RegistryIntegrityError("adk_provenance_invalid")
        _unique_value(
            correlations,
            validated.get("correlation_id"),
            owner=run_id,
            code="adk_registry_cardinality_conflict",
        )
        if validated.get("run_id") is not None and validated.get("run_id") != run_id:
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        invocation_key = (
            f"{validated.get('adk_app')}:{validated.get('adk_session_id')}:"
            f"{validated.get('adk_invocation_id')}"
        )
        _unique_value(
            invocations,
            invocation_key,
            owner=run_id,
            code="adk_registry_cardinality_conflict",
        )
        for trace_field in ("trace_id", "adk_trace_id"):
            if validated.get(trace_field) is not None:
                _unique_value(
                    traces,
                    validated[trace_field],
                    owner=run_id,
                    code="adk_registry_cardinality_conflict",
                )
        parent_run_id = validated.get("parent_run_id")
        source_run_id = validated.get("source_run_id")
        if parent_run_id is not None and parent_run_id not in runs_by_id:
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        if source_run_id is not None and source_run_id not in runs_by_id:
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        lineage = validated.get("lineage")
        if lineage is not None:
            if (
                not isinstance(lineage, dict)
                or lineage.get("parent_run_id") != parent_run_id
                or lineage.get("source_run_id") != (source_run_id or parent_run_id)
            ):
                raise RegistryIntegrityError("adk_registry_binding_invalid")
        bound_operations = operations_by_run.get(run_id, [])
        if len(bound_operations) != 1:
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        operation = bound_operations[0]
        if (
            operation.get("operation_id") != run.get("operation_id")
            or operation.get("correlation_id") != validated.get("correlation_id")
        ):
            raise RegistryIntegrityError("adk_registry_binding_invalid")
    return authoritative_run_ids


def audit_adk_registry(registry_path: str | Path) -> set[str]:
    path = resolve_registry_path(registry_path)
    if not path.exists():
        return set()
    with locked_registry_transaction(path):
        return _audit_adk_payload(_read_registry_payload(path))


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
    created_by: str | None = None,
    operator_id: str | None = None,
    workspace_id: str | None = None,
    review_required: bool | None = None,
    error_category: str | None = None,
    errors: list[str] | None = None,
    review_delivery: dict[str, Any] | None = None,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    source: str | None = None,
    provenance: dict[str, Any] | None = None,
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
            created_by=created_by,
            operator_id=operator_id,
            workspace_id=workspace_id,
            review_required=review_required,
            error_category=error_category,
            errors=errors,
            review_delivery=review_delivery,
            event_type=event_type,
            event_payload=event_payload,
            workflow_id=workflow_id,
            source=source,
            provenance=provenance,
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
            created_by=created_by,
            operator_id=operator_id,
            workspace_id=workspace_id,
            review_required=review_required,
            error_category=error_category,
            errors=errors,
            review_delivery=review_delivery,
            event_type=event_type,
            event_payload=event_payload,
            workflow_id=workflow_id,
            source=source,
            provenance=provenance,
        )


def list_runs(registry_path: str | Path) -> list[dict[str, Any]]:
    return _read_audited_runs(registry_path)


def get_run(registry_path: str | Path, run_id: str) -> dict[str, Any]:
    for entry in _read_audited_runs(registry_path):
        if entry.get("run_id") == run_id:
            return entry
    raise RunNotFoundError(f"Run id not found in registry: {run_id}")


def _read_audited_runs(registry_path: str | Path) -> list[dict[str, Any]]:
    path = resolve_registry_path(registry_path)
    if not path.exists():
        return []
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        return sorted(
            list(payload.get("runs", [])),
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )


def get_run_scope_record(
    registry_path: str | Path, run_id: str
) -> tuple[dict[str, Any], bool]:
    """Read only enough trusted state to decide an object's capability scope."""
    path = resolve_registry_path(registry_path)
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        runs = payload.get("runs", [])
        if not isinstance(runs, list):
            raise RunNotFoundError(f"Run id not found in registry: {run_id}")
        matches = [
            entry
            for entry in runs
            if isinstance(entry, dict) and entry.get("run_id") == run_id
        ]
        if not matches:
            raise RunNotFoundError(f"Run id not found in registry: {run_id}")
        operations = payload.get("adk_operations", [])
        operation_bound = isinstance(operations, list) and any(
            isinstance(operation, dict)
            and operation.get("run_id") == run_id
            and operation.get("principal") == "adk-developer"
            and operation.get("action") in RUN_BOUND_ADK_ACTIONS
            for operation in operations
        )
        source_marked = any(
            entry.get("source") == "adk-developer"
            or (
                isinstance(entry.get("provenance"), dict)
                and entry["provenance"].get("source") == "adk-developer"
            )
            for entry in matches
        )
        return matches[0], operation_bound or source_marked


def accept_adk_run(
    *,
    registry_path: str | Path,
    action: str = "start_workflow_run",
    idempotency_key: str,
    request_fingerprint: str,
    confirmation_grant_digest: str,
    run_id: str,
    operation_id: str,
    task_id: str,
    case_id: str,
    artifact_root: str,
    workflow_id: str,
    provenance: dict[str, Any],
    operator_params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Atomically bind one confirmed ADK invocation to one accepted run."""

    if action not in RUN_BOUND_ADK_ACTIONS:
        raise ValueError("adk_action_invalid")
    if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 256:
        raise ValueError("idempotency_key_invalid")
    key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    path = resolve_registry_path(registry_path)
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        operations = payload.setdefault("adk_operations", [])
        runs = payload.setdefault("runs", [])
        existing_operation = next(
            (
                item
                for item in operations
                if item.get("idempotency_key_digest") == key_digest
                and item.get("principal") == "adk-developer"
                and item.get("action") == action
            ),
            None,
        )
        if existing_operation is not None:
            if not secrets.compare_digest(
                str(existing_operation.get("request_fingerprint", "")), request_fingerprint
            ):
                raise IdempotencyConflictError("idempotency_conflict")
            existing_run_id = str(existing_operation.get("run_id"))
            existing_run = next((item for item in runs if item.get("run_id") == existing_run_id), None)
            if existing_run is None:
                raise IdempotencyConflictError("idempotency_binding_invalid")
            return existing_run, False

        invocation_key = (
            f"{provenance.get('adk_app')}:{provenance.get('adk_session_id')}:"
            f"{provenance.get('adk_invocation_id')}"
        )
        correlation_id = str(provenance.get("correlation_id", ""))
        if any(
            isinstance(item.get("provenance"), dict)
            and (
                f"{item['provenance'].get('adk_app')}:"
                f"{item['provenance'].get('adk_session_id')}:"
                f"{item['provenance'].get('adk_invocation_id')}"
            )
            == invocation_key
            for item in runs
        ):
            raise IdempotencyConflictError("invocation_conflict")
        if any(
            isinstance(item.get("provenance"), dict)
            and item["provenance"].get("correlation_id") == correlation_id
            for item in runs
        ):
            raise IdempotencyConflictError("correlation_conflict")
        if any(item.get("run_id") == run_id for item in runs):
            raise IdempotencyConflictError("run_id_conflict")

        resolved_operator_params = {
            "case_id": case_id,
            "workflow_id": workflow_id,
            "created_by": "adk-developer",
            "operator_id": "adk-developer",
            "workspace_id": "adk-development",
        }
        if operator_params is not None:
            resolved_operator_params.update(operator_params)
        now = _utc_now()
        history = {
            "status": "accepted",
            "timestamp": now,
            "summary": f"Accepted ADK workflow run for {case_id}",
            "event_type": "run.accepted",
            "payload": {"operation_id": operation_id, "correlation_id": correlation_id},
            "provenance": provenance,
        }
        entry = {
            "task_id": task_id,
            "case_id": case_id,
            "run_id": run_id,
            "status": "accepted",
            "created_at": now,
            "updated_at": now,
            "artifact_root": artifact_root,
            "summary": f"Accepted ADK workflow run for {case_id}",
            "created_by": "adk-developer",
            "operator_id": "adk-developer",
            "workspace_id": "adk-development",
            "source": "adk-developer",
            "review_required": False,
            "error_category": None,
            "errors": [],
            "review_delivery": None,
            "operator_params": resolved_operator_params,
            "workflow_id": workflow_id,
            "provenance": provenance,
            "operation_id": operation_id,
            "status_history": [history],
        }
        operation = {
            "principal": "adk-developer",
            "action": action,
            "idempotency_key_digest": key_digest,
            "request_fingerprint": request_fingerprint,
            "confirmation_grant_digest": confirmation_grant_digest,
            "run_id": run_id,
            "operation_id": operation_id,
            "correlation_id": correlation_id,
            "created_at": now,
        }
        runs.append(entry)
        operations.append(operation)
        _audit_adk_payload(payload)
        _write_registry_payload(path, payload)
        return entry, True


def get_adk_debug_operation(
    *,
    operation_store_path: str | Path,
    action: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    """Return an existing action-scoped ADK debug operation, if bound."""

    if action not in DEBUG_ADK_ACTIONS:
        raise ValueError("adk_action_invalid")
    if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 256:
        raise ValueError("idempotency_key_invalid")
    key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    path = resolve_registry_path(operation_store_path)
    if not path.exists():
        return None
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        operations = payload.get("adk_debug_operations", [])
        existing_operation = next(
            (
                item
                for item in operations
                if item.get("idempotency_key_digest") == key_digest
                and item.get("principal") == "adk-developer"
                and item.get("action") == action
            ),
            None,
        )
        if existing_operation is None:
            return None
        if not secrets.compare_digest(
            str(existing_operation.get("request_fingerprint", "")),
            request_fingerprint,
        ):
            raise IdempotencyConflictError("idempotency_conflict")
        return dict(existing_operation)


def get_adk_debug_operation_by_id(
    *,
    operation_store_path: str | Path,
    operation_id: str,
) -> dict[str, Any] | None:
    """Return one ADK debug operation by logical operation ID."""

    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        raise ValueError("operation_id_invalid")
    path = resolve_registry_path(operation_store_path)
    if not path.exists():
        return None
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        operation = next(
            (
                item
                for item in payload.get("adk_debug_operations", [])
                if item.get("principal") == "adk-developer"
                and item.get("operation_id") == operation_id
            ),
            None,
        )
        return dict(operation) if isinstance(operation, dict) else None


def get_adk_run_operation_by_id(
    *,
    registry_path: str | Path,
    operation_id: str,
) -> dict[str, Any] | None:
    """Return one run-bound ADK operation by logical operation ID."""

    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        raise ValueError("operation_id_invalid")
    path = resolve_registry_path(registry_path)
    if not path.exists():
        return None
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        operation = next(
            (
                item
                for item in payload.get("adk_operations", [])
                if item.get("principal") == "adk-developer"
                and item.get("operation_id") == operation_id
                and item.get("action") in RUN_BOUND_ADK_ACTIONS
            ),
            None,
        )
        if not isinstance(operation, dict):
            return None
        run_id = str(operation.get("run_id") or "")
        run = next(
            (
                item
                for item in payload.get("runs", [])
                if isinstance(item, dict) and item.get("run_id") == run_id
            ),
            None,
        )
        if not isinstance(run, dict):
            raise RegistryIntegrityError("adk_operation_run_missing")
        provenance = run.get("provenance") if isinstance(run.get("provenance"), dict) else {}
        result = {
            "ok": run.get("status") not in {"failed", "stale"},
            "operation_id": str(operation.get("operation_id")),
            "status": str(run.get("status") or "unknown"),
            "run": {
                "run_id": run_id,
                "case_id": run.get("case_id"),
                "workflow_id": run.get("workflow_id"),
                "status": str(run.get("status") or "unknown"),
                "correlation_id": provenance.get("correlation_id"),
                "parent_run_id": provenance.get("parent_run_id"),
                "source_run_id": provenance.get("source_run_id"),
            },
        }
        return {**dict(operation), "result": result}


def record_adk_debug_operation(
    *,
    operation_store_path: str | Path,
    action: str,
    object_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    confirmation_grant_digest: str,
    operation_id: str,
    result: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Atomically persist one action-scoped ADK debug operation result."""

    if action not in DEBUG_ADK_ACTIONS:
        raise ValueError("adk_action_invalid")
    if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 256:
        raise ValueError("idempotency_key_invalid")
    key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    path = resolve_registry_path(operation_store_path)
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        operations = payload.setdefault("adk_debug_operations", [])
        existing_operation = next(
            (
                item
                for item in operations
                if item.get("idempotency_key_digest") == key_digest
                and item.get("principal") == "adk-developer"
                and item.get("action") == action
            ),
            None,
        )
        if existing_operation is not None:
            if not secrets.compare_digest(
                str(existing_operation.get("request_fingerprint", "")),
                request_fingerprint,
            ):
                raise IdempotencyConflictError("idempotency_conflict")
            return dict(existing_operation), False

        operation = {
            "principal": "adk-developer",
            "action": action,
            "object_id": str(object_id),
            "idempotency_key_digest": key_digest,
            "request_fingerprint": request_fingerprint,
            "confirmation_grant_digest": confirmation_grant_digest,
            "operation_id": operation_id,
            "result": dict(result),
            "created_at": _utc_now(),
        }
        operations.append(operation)
        _audit_adk_payload(payload)
        _write_registry_payload(path, payload)
        return operation, True


def complete_adk_debug_operation(
    *,
    operation_store_path: str | Path,
    operation_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Update a previously accepted ADK debug operation with its final result."""

    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        raise ValueError("operation_id_invalid")
    path = resolve_registry_path(operation_store_path)
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        operations = payload.get("adk_debug_operations", [])
        operation = next(
            (
                item
                for item in operations
                if item.get("principal") == "adk-developer"
                and item.get("operation_id") == operation_id
            ),
            None,
        )
        if not isinstance(operation, dict):
            raise IdempotencyConflictError("operation_binding_invalid")
        operation["result"] = dict(result)
        operation["updated_at"] = _utc_now()
        _audit_adk_payload(payload)
        _write_registry_payload(path, payload)
        return dict(operation)


def mark_incomplete_adk_runs_stale(registry_path: str | Path) -> list[str]:
    path = resolve_registry_path(registry_path)
    if not path.exists():
        return []
    changed: list[str] = []
    with locked_registry_transaction(path):
        payload = _read_registry_payload(path)
        _audit_adk_payload(payload)
        now = _utc_now()
        for entry in payload.get("runs", []):
            if entry.get("source") != "adk-developer" or entry.get("status") not in {"accepted", "queued", "running"}:
                continue
            entry["status"] = "failed"
            entry["recovery_state"] = "stale"
            entry["updated_at"] = now
            entry["summary"] = "ADK run was incomplete when the control plane restarted."
            entry["error_category"] = "stale_incomplete"
            entry["errors"] = ["Persisted run state is incomplete; no terminal result was inferred."]
            entry.setdefault("status_history", []).append(
                {
                    "status": "failed",
                    "timestamp": now,
                    "summary": entry["summary"],
                    "event_type": "run.failed",
                    "payload": {"code": "stale_incomplete", "recovery_state": "stale"},
                    "provenance": entry.get("provenance"),
                }
            )
            changed.append(str(entry.get("run_id")))
        if changed:
            _write_registry_payload(path, payload)
    return changed
