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
    if not isinstance(runs, list) or not isinstance(operations, list):
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
        and (
            operation.get("principal") == "adk-developer"
            or operation.get("action") == "start_workflow_run"
        )
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
            or operation.get("action") != "start_workflow_run"
        ):
            raise RegistryIntegrityError("adk_registry_binding_invalid")
        _unique_value(
            operation_ids,
            operation.get("operation_id"),
            owner=run_id,
            code="adk_registry_cardinality_conflict",
        )
        _unique_value(
            idempotency_digests,
            operation.get("idempotency_key_digest"),
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
        invocation_key = (
            f"{validated.get('adk_app')}:{validated.get('adk_invocation_id')}"
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
            and (
                operation.get("principal") == "adk-developer"
                or operation.get("action") == "start_workflow_run"
            )
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
) -> tuple[dict[str, Any], bool]:
    """Atomically bind one confirmed ADK invocation to one accepted run."""

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
                and item.get("action") == "start_workflow_run"
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

        invocation_id = str(provenance.get("adk_invocation_id", ""))
        correlation_id = str(provenance.get("correlation_id", ""))
        if any(
            isinstance(item.get("provenance"), dict)
            and item["provenance"].get("adk_invocation_id") == invocation_id
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
            "operator_params": {
                "case_id": case_id,
                "workflow_id": workflow_id,
                "created_by": "adk-developer",
                "operator_id": "adk-developer",
                "workspace_id": "adk-development",
            },
            "workflow_id": workflow_id,
            "provenance": provenance,
            "operation_id": operation_id,
            "status_history": [history],
        }
        operation = {
            "principal": "adk-developer",
            "action": "start_workflow_run",
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
