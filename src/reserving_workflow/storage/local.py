"""Local JSON/filesystem-backed storage adapters."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterator

from reserving_workflow.contracts.control_plane import validate_run_status
from reserving_workflow.storage.safe_json import (
    PinnedJsonRoot,
    SafeJsonReadError,
)
from reserving_workflow.storage.registry_transactions import locked_registry_transaction


DEFAULT_REGISTRY_PAYLOAD = {"runs": []}


class RunNotFoundError(ValueError):
    """Raised when a run id is absent from the local registry."""


class ReviewNotFoundError(ValueError):
    """Raised when a review id is absent from the local review store."""


class ReviewDecisionConflictError(ValueError):
    """Raised when a submitted review decision conflicts with a stored decision."""


class ReviewRecordReadError(RuntimeError):
    """Raised when a persisted review record cannot be read safely."""

    code = "review_record_unsafe"
    message = "Stored review record could not be read safely."

    def __init__(self) -> None:
        super().__init__(self.message)


class LocalRunStore:
    """Adapter over the existing local JSON run registry."""

    def __init__(self, registry_path: str | Path):
        self.registry_path = resolve_registry_path(registry_path)

    def create_run(
        self,
        *,
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
        return self._upsert_run(
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
            create_if_missing=True,
            reject_existing=True,
        )

    def update_run_status(
        self,
        *,
        run_id: str,
        task_id: str,
        case_id: str | None,
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
        return self._upsert_run(
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
            create_if_missing=False,
            reject_existing=False,
        )

    def append_event(self, *, run_id: str, status: str, summary: str | None = None) -> dict[str, Any]:
        entry = self.get_run(run_id)
        return self.update_run_status(
            run_id=run_id,
            task_id=str(entry.get("task_id") or ""),
            case_id=entry.get("case_id"),
            status=status,
            artifact_root=entry.get("artifact_root"),
            summary=summary,
            operator_params=dict(entry.get("operator_params", {}) or {}),
            created_by=entry.get("created_by"),
            operator_id=entry.get("operator_id"),
            workspace_id=entry.get("workspace_id"),
            review_required=entry.get("review_required"),
            error_category=entry.get("error_category"),
            errors=list(entry.get("errors", []) or []),
            review_delivery=entry.get("review_delivery"),
            workflow_id=entry.get("workflow_id"),
            source=entry.get("source"),
            provenance=entry.get("provenance"),
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        for entry in self.list_runs():
            if entry.get("run_id") == run_id:
                return entry
        raise RunNotFoundError(f"Run id not found in registry: {run_id}")

    def list_runs(self) -> list[dict[str, Any]]:
        payload = _read_registry_payload(self.registry_path)
        runs = list(payload.get("runs", []))
        return sorted(runs, key=lambda item: item.get("updated_at", ""), reverse=True)

    def _upsert_run(self, **kwargs: Any) -> dict[str, Any]:
        with locked_registry_transaction(self.registry_path):
            return self._upsert_run_unlocked(**kwargs)

    def _upsert_run_unlocked(
        self,
        *,
        task_id: str,
        case_id: str | None,
        run_id: str,
        status: str,
        artifact_root: str | None,
        summary: str | None,
        operator_params: dict[str, Any] | None,
        created_by: str | None,
        operator_id: str | None,
        workspace_id: str | None,
        review_required: bool | None,
        error_category: str | None,
        errors: list[str] | None,
        review_delivery: dict[str, Any] | None,
        event_type: str | None,
        event_payload: dict[str, Any] | None,
        workflow_id: str | None,
        source: str | None,
        provenance: dict[str, Any] | None,
        create_if_missing: bool,
        reject_existing: bool,
    ) -> dict[str, Any]:
        status = validate_run_status(status)
        payload = _read_registry_payload(self.registry_path)
        runs = payload.setdefault("runs", [])
        entry = next((item for item in runs if item.get("run_id") == run_id), None)
        now = _utc_now()
        history_item = {"status": status, "timestamp": now}
        if summary is not None:
            history_item["summary"] = summary
        if event_type is not None:
            history_item["event_type"] = event_type
        if event_payload is not None:
            history_item["payload"] = _to_serializable(event_payload)

        resolved_workflow_id = workflow_id
        if resolved_workflow_id is None and operator_params is not None:
            candidate_workflow_id = operator_params.get("workflow_id")
            if candidate_workflow_id is not None:
                resolved_workflow_id = str(candidate_workflow_id)
        resolved_created_by = created_by
        resolved_operator_id = operator_id
        resolved_workspace_id = workspace_id
        if operator_params is not None:
            if resolved_created_by is None and operator_params.get("created_by") is not None:
                resolved_created_by = str(operator_params["created_by"])
            if resolved_operator_id is None and operator_params.get("operator_id") is not None:
                resolved_operator_id = str(operator_params["operator_id"])
            if resolved_workspace_id is None and operator_params.get("workspace_id") is not None:
                resolved_workspace_id = str(operator_params["workspace_id"])

        if entry is not None and reject_existing:
            raise ValueError(f"Run id already exists in registry: {run_id}")

        existing_provenance = entry.get("provenance") if entry is not None else None
        if existing_provenance is not None and provenance is not None:
            if _to_serializable(existing_provenance) != _to_serializable(provenance):
                raise ValueError("Run provenance is immutable")
        resolved_provenance = provenance if provenance is not None else existing_provenance
        if resolved_provenance is not None:
            history_item["provenance"] = _to_serializable(resolved_provenance)
        resolved_source = source
        if resolved_source is None and isinstance(resolved_provenance, dict):
            candidate_source = resolved_provenance.get("source")
            if candidate_source is not None:
                resolved_source = str(candidate_source)
        if entry is not None and entry.get("source") is not None:
            if resolved_source is not None and str(entry["source"]) != resolved_source:
                raise ValueError("Run source is immutable")
            resolved_source = str(entry["source"])

        if entry is None:
            if not create_if_missing:
                raise RunNotFoundError(f"Run id not found in registry: {run_id}")
            entry = {
                "task_id": task_id,
                "case_id": case_id,
                "run_id": run_id,
                "status": status,
                "created_at": now,
                "updated_at": now,
                "artifact_root": artifact_root,
                "summary": summary,
                "created_by": resolved_created_by,
                "operator_id": resolved_operator_id,
                "workspace_id": resolved_workspace_id,
                "review_required": bool(review_required) if review_required is not None else status == "needs_review",
                "error_category": error_category,
                "errors": list(errors or []),
                "review_delivery": _to_serializable(review_delivery),
                "operator_params": _to_serializable(operator_params or {}),
                "workflow_id": resolved_workflow_id,
                "source": resolved_source,
                "provenance": _to_serializable(resolved_provenance),
                "status_history": [history_item],
            }
            runs.append(entry)
        else:
            entry["task_id"] = task_id
            if case_id is not None:
                entry["case_id"] = case_id
            entry["status"] = status
            entry["updated_at"] = now
            if artifact_root is not None:
                entry["artifact_root"] = artifact_root
            if summary is not None:
                entry["summary"] = summary
            if resolved_created_by is not None:
                entry["created_by"] = resolved_created_by
            if resolved_operator_id is not None:
                entry["operator_id"] = resolved_operator_id
            if resolved_workspace_id is not None:
                entry["workspace_id"] = resolved_workspace_id
            if review_required is not None:
                entry["review_required"] = bool(review_required)
            else:
                entry["review_required"] = status == "needs_review"
            entry["error_category"] = error_category
            entry["errors"] = list(errors or [])
            if review_delivery is not None:
                entry["review_delivery"] = _to_serializable(review_delivery)
            if operator_params is not None:
                entry["operator_params"] = _to_serializable(operator_params)
            if resolved_workflow_id is not None:
                entry["workflow_id"] = resolved_workflow_id
            if resolved_source is not None:
                entry["source"] = resolved_source
            if resolved_provenance is not None:
                entry["provenance"] = _to_serializable(resolved_provenance)
            entry.setdefault("status_history", []).append(history_item)

        _write_registry_payload(self.registry_path, payload)
        return entry


class LocalArtifactStore:
    """Adapter over the current filesystem artifact helpers."""

    def write_artifact(
        self,
        *,
        root: str | Path,
        relative_path: str | Path,
        payload: Any,
        format: str = "json",
    ) -> Path:
        target = resolve_artifact_path(root, relative_path)
        if format == "json":
            target.write_text(
                json.dumps(_to_serializable(payload), indent=2, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
            )
            return target
        if format == "text":
            target.write_text(str(payload), encoding="utf-8")
            return target
        raise ValueError(f"Unsupported artifact format: {format!r}")

    def read_artifact(self, path: str | Path, *, format: str = "json") -> Any:
        target = Path(path).expanduser().resolve()
        if format == "json":
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object in artifact '{target}', got {type(payload).__name__}")
            return payload
        if format == "text":
            return target.read_text(encoding="utf-8")
        raise ValueError(f"Unsupported artifact format: {format!r}")

    def list_artifacts(self, root: str | Path) -> list[str]:
        base = Path(root).expanduser().resolve()
        if not base.exists():
            return []
        return sorted(
            str(path.relative_to(base)).replace("\\", "/")
            for path in base.rglob("*")
            if path.is_file()
        )


class LocalReviewStore:
    """Artifact-backed local placeholder for persistent review records."""

    def __init__(self, root: str | Path):
        self.root = Path(os.path.abspath(os.path.expanduser(str(root))))
        self.artifact_store = LocalArtifactStore()
        self._trusted_read_root: PinnedJsonRoot | None = None
        self._trusted_read_root_missing = False

    @contextmanager
    def pinned_reads(self) -> Iterator["LocalReviewStore"]:
        if self._trusted_read_root is not None or self._trusted_read_root_missing:
            yield self
            return
        trusted_root = PinnedJsonRoot(
            self.root,
            namespace="review_record",
            allow_nested=True,
        )
        try:
            trusted_root.__enter__()
        except SafeJsonReadError as exc:
            if exc.code != "review_record_missing":
                raise ReviewRecordReadError() from exc
            self._trusted_read_root_missing = True
            try:
                yield self
            finally:
                self._trusted_read_root_missing = False
            return
        self._trusted_read_root = trusted_root
        try:
            yield self
        finally:
            self._trusted_read_root = None
            trusted_root.close()

    def create_review(
        self,
        *,
        review_id: str,
        run_id: str,
        case_id: str,
        status: str,
        reason_codes: list[str] | None = None,
        assigned_to: str | None = None,
        workspace_id: str | None = None,
        packet: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        review_path = self._review_path(review_id, create_parent=True)
        if review_path.exists():
            raise ValueError(f"Review id already exists: {review_id}")
        now = _utc_now()
        record = {
            "review_id": review_id,
            "run_id": run_id,
            "case_id": case_id,
            "status": status,
            "reason_codes": list(reason_codes or []),
            "assigned_to": assigned_to,
            "workspace_id": workspace_id,
            "packet": _to_serializable(packet),
            "created_at": now,
            "updated_at": now,
            "decision": None,
        }
        self.artifact_store.write_artifact(
            root=self.root,
            relative_path=Path(review_id) / "review_record.json",
            payload=record,
        )
        return record

    def submit_decision(
        self,
        *,
        review_id: str,
        decision: str,
        comment: str | None = None,
        decided_by: str | None = None,
        follow_up_run_id: str | None = None,
        bound_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        review = dict(bound_review) if bound_review is not None else self.get_review(review_id)
        if review.get("review_id") != review_id:
            raise ValueError("Review identity does not match the requested review.")
        existing_decision = review.get("decision")
        candidate_decision = {
            "review_id": review_id,
            "run_id": review["run_id"],
            "decision": decision,
            "comment": comment,
            "decided_by": decided_by,
            "follow_up_run_id": follow_up_run_id,
        }
        if isinstance(existing_decision, dict):
            if _decision_payload_matches(existing_decision, candidate_decision):
                if bound_review is not None and self.get_review(review_id) != review:
                    self.artifact_store.write_artifact(
                        root=self.root,
                        relative_path=Path(review_id) / "review_record.json",
                        payload=review,
                    )
                return existing_decision
            raise ReviewDecisionConflictError("Review decision already recorded with different content.")
        now = _utc_now()
        decision_record = {
            **candidate_decision,
            "decided_at": now,
        }
        review["decision"] = decision_record
        review["status"] = "review_decided"
        review["updated_at"] = now
        self.artifact_store.write_artifact(
            root=self.root,
            relative_path=Path(review_id) / "review_record.json",
            payload=review,
        )
        self.artifact_store.write_artifact(
            root=self.root,
            relative_path=Path(review_id) / "review_decision.json",
            payload=decision_record,
        )
        self.artifact_store.write_artifact(
            root=self.root,
            relative_path=Path(review_id) / "review_decision.md",
            payload=_render_review_decision_markdown(review, decision_record),
            format="text",
        )
        return decision_record

    def get_review(self, review_id: str) -> dict[str, Any]:
        _validate_artifact_component(review_id, field_name="review_id")
        if self._trusted_read_root_missing:
            raise ReviewNotFoundError(f"Review id not found: {review_id}")
        if self._trusted_read_root is not None:
            return self._read_review_from_trusted_root(
                review_id,
                trusted_root=self._trusted_read_root,
            )
        try:
            with PinnedJsonRoot(
                self.root,
                namespace="review_record",
                allow_nested=True,
            ) as trusted_root:
                return self._read_review_from_trusted_root(
                    review_id,
                    trusted_root=trusted_root,
                )
        except SafeJsonReadError as exc:
            if exc.code == "review_record_missing":
                raise ReviewNotFoundError(f"Review id not found: {review_id}") from None
            raise ReviewRecordReadError() from exc

    def get_review_for_run(self, run_id: str) -> dict[str, Any] | None:
        for review in self.list_reviews():
            if review.get("run_id") == run_id:
                return review
        return None

    def list_reviews(self) -> list[dict[str, Any]]:
        if self._trusted_read_root_missing:
            return []
        if self._trusted_read_root is not None:
            return self._list_reviews_from_trusted_root(self._trusted_read_root)
        try:
            with PinnedJsonRoot(
                self.root,
                namespace="review_record",
                allow_nested=True,
            ) as trusted_root:
                return self._list_reviews_from_trusted_root(trusted_root)
        except SafeJsonReadError as exc:
            if exc.code == "review_record_missing":
                return []
            raise ReviewRecordReadError() from exc

    def _list_reviews_from_trusted_root(
        self,
        trusted_root: PinnedJsonRoot,
    ) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        try:
            review_ids = trusted_root.list_directories()
            for review_id in review_ids:
                try:
                    _validate_artifact_component(
                        review_id,
                        field_name="review_id",
                    )
                    reviews.append(
                        self._read_review_from_trusted_root(
                            review_id,
                            trusted_root=trusted_root,
                        )
                    )
                except ReviewNotFoundError:
                    continue
        except SafeJsonReadError as exc:
            raise ReviewRecordReadError() from exc
        return sorted(
            reviews,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

    def _read_review_from_trusted_root(
        self,
        review_id: str,
        *,
        trusted_root: PinnedJsonRoot,
    ) -> dict[str, Any]:
        try:
            return trusted_root.read_bounded_json_object(
                f"{review_id}/review_record.json",
                namespace="review_record",
            )
        except SafeJsonReadError as exc:
            if exc.code == "review_record_missing":
                raise ReviewNotFoundError(f"Review id not found: {review_id}") from None
            raise ReviewRecordReadError() from exc

    def _review_path(self, review_id: str, *, create_parent: bool) -> Path:
        _validate_artifact_component(review_id, field_name="review_id")
        path = (self.root / review_id / "review_record.json").resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Review path escapes review root: {review_id}") from exc
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path


def resolve_registry_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def resolve_artifact_root(root: str | Path) -> Path:
    path = Path(root).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_artifact_path(root: str | Path, relative_path: str | Path) -> Path:
    base = resolve_artifact_root(root)
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(f"Artifact path must be relative: {relative_path}")
    path = (base / candidate).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Artifact path escapes artifact root: {relative_path}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_artifact_component(value: str, *, field_name: str) -> None:
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"{field_name} must be a single safe path component: {value!r}")


def _read_registry_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"runs": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("runs", []), list):
        raise ValueError(f"Invalid run registry payload at {path}")
    return payload


def _write_registry_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(serialized)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _render_review_decision_markdown(review: dict[str, Any], decision_record: dict[str, Any]) -> str:
    lines = [
        "# Review Decision",
        "",
        f"- review_id: {review.get('review_id')}",
        f"- run_id: {review.get('run_id')}",
        f"- case_id: {review.get('case_id')}",
        f"- decision: {decision_record.get('decision')}",
        f"- decided_by: {decision_record.get('decided_by') or 'unknown'}",
        f"- decided_at: {decision_record.get('decided_at')}",
    ]
    if decision_record.get("follow_up_run_id"):
        lines.append(f"- follow_up_run_id: {decision_record['follow_up_run_id']}")
    if decision_record.get("comment"):
        lines.extend(["", "## Comment", "", str(decision_record["comment"])])
    return "\n".join(lines) + "\n"


def _decision_payload_matches(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    keys = ("review_id", "run_id", "decision", "comment", "decided_by", "follow_up_run_id")
    return all(existing.get(key) == candidate.get(key) for key in keys)
