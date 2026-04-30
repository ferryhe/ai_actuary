"""Local JSON-backed run registry for operator-facing task state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REGISTRY_PAYLOAD = {"runs": []}


def resolve_registry_path(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


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
    review_required: bool | None = None,
    error_category: str | None = None,
    errors: list[str] | None = None,
    review_delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = resolve_registry_path(registry_path)
    payload = _read_registry_payload(target)
    now = _utc_now()
    runs = payload.setdefault("runs", [])
    entry = next((item for item in runs if item.get("run_id") == run_id), None)

    history_item = {"status": status, "timestamp": now}
    if summary is not None:
        history_item["summary"] = summary

    if entry is None:
        entry = {
            "task_id": task_id,
            "case_id": case_id,
            "run_id": run_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "artifact_root": artifact_root,
            "summary": summary,
            "review_required": bool(review_required) if review_required is not None else status == "needs_review",
            "error_category": error_category,
            "errors": list(errors or []),
            "review_delivery": review_delivery,
            "operator_params": _to_serializable(operator_params or {}),
            "status_history": [history_item],
        }
        runs.append(entry)
    else:
        entry["task_id"] = task_id
        entry["case_id"] = case_id
        entry["status"] = status
        entry["updated_at"] = now
        if artifact_root is not None:
            entry["artifact_root"] = artifact_root
        if summary is not None:
            entry["summary"] = summary
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
        entry.setdefault("status_history", []).append(history_item)

    _write_registry_payload(target, payload)
    return entry


def list_runs(registry_path: str | Path) -> list[dict[str, Any]]:
    payload = _read_registry_payload(resolve_registry_path(registry_path))
    runs = list(payload.get("runs", []))
    return sorted(runs, key=lambda item: item.get("updated_at", ""), reverse=True)


def get_run(registry_path: str | Path, run_id: str) -> dict[str, Any]:
    for entry in list_runs(registry_path):
        if entry.get("run_id") == run_id:
            return entry
    raise ValueError(f"Run id not found in registry: {run_id}")


def _read_registry_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"runs": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("runs", []), list):
        raise ValueError(f"Invalid run registry payload at {path}")
    return payload


def _write_registry_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_serializable(payload: Any) -> Any:
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, dict):
        return {str(key): _to_serializable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_to_serializable(item) for item in payload]
    return payload
