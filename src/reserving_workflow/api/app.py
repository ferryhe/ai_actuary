"""FastAPI control plane for operator-facing AI Actuary runs.

This module intentionally wraps the existing operator/artifact/registry
boundaries instead of introducing a second runtime implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from reserving_workflow import operator_entrypoint
from reserving_workflow.artifacts import replay as replay_helpers
from reserving_workflow.artifacts.storage import read_json_artifact
from reserving_workflow.runtime import run_registry


class ApiSettings(BaseModel):
    """Runtime settings for the local FastAPI control plane."""

    registry_path: str | Path = Field(default="./tmp/run-registry.json")
    artifact_root: str | Path = Field(default="./tmp/api-artifacts")
    review_delivery_dir: str | Path | None = None


class RunCreateRequest(BaseModel):
    case_id: str
    artifact_dir: str | Path | None = None
    objective: str = "API-triggered governed workflow run"
    sample_name: str = "RAA"
    method: str = "chainladder"
    review_threshold_origin_count: int | None = None
    user_prompt: str | None = None
    review_delivery_dir: str | Path | None = None


class RerunRequest(BaseModel):
    artifact_dir: str | Path | None = None
    review_delivery_dir: str | Path | None = None


class ReplayRequest(BaseModel):
    manifest_path: str | Path


class RepeatabilityRequest(BaseModel):
    manifest_paths: list[str | Path]


class BatchBenchmarkRequest(BaseModel):
    cases: list[dict[str, Any]]
    artifact_root: str | Path | None = None


def create_app(
    *,
    settings: ApiSettings | None = None,
    runner_module=None,
    task_contracts_module=None,
    replay_module=None,
    batch_runner_module=None,
) -> FastAPI:
    """Create the FastAPI control plane app.

    Test and future runtime callers can inject runner/task-contract modules so
    the API layer remains a transport wrapper over the existing operator core.
    """

    resolved_settings = settings or ApiSettings()
    resolved_replay_module = replay_module or replay_helpers
    resolved_batch_runner_module = batch_runner_module
    app = FastAPI(title="AI Actuary Control Plane", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-actuary-control-plane"}

    @app.post("/runs")
    def create_run(request: RunCreateRequest) -> dict[str, Any]:
        artifact_dir = request.artifact_dir or _default_artifact_dir(resolved_settings, request.case_id)
        review_delivery_dir = request.review_delivery_dir
        if review_delivery_dir is None:
            review_delivery_dir = resolved_settings.review_delivery_dir
        return operator_entrypoint.run_operator_flow(
            case_id=request.case_id,
            artifact_dir=artifact_dir,
            objective=request.objective,
            sample_name=request.sample_name,
            method=request.method,
            review_threshold_origin_count=request.review_threshold_origin_count,
            user_prompt=request.user_prompt,
            review_delivery_dir=review_delivery_dir,
            registry_path=resolved_settings.registry_path,
            runner_module=runner_module,
            task_contracts_module=task_contracts_module,
        )

    @app.get("/runs")
    def list_runs() -> dict[str, Any]:
        runs = run_registry.list_runs(resolved_settings.registry_path)
        return {
            "registry_path": str(Path(resolved_settings.registry_path)),
            "run_count": len(runs),
            "runs": [_run_summary(entry) for entry in runs],
        }

    @app.get("/runs/{run_id}")
    def get_run_detail(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        artifact_manifest = _load_manifest_for_entry(entry)
        review_packet = _load_review_packet_for_entry(entry)
        return {
            "run": entry,
            "events": [_event_from_history(run_id, item) for item in entry.get("status_history", [])],
            "artifact_manifest": artifact_manifest,
            "review_packet": review_packet.get("packet") if review_packet.get("present") else None,
            "review_delivery": entry.get("review_delivery"),
        }

    @app.post("/runs/{run_id}/rerun")
    def rerun(run_id: str, request: RerunRequest) -> dict[str, Any]:
        try:
            return operator_entrypoint.rerun_from_registry(
                run_id,
                registry_path=resolved_settings.registry_path,
                artifact_dir=request.artifact_dir,
                review_delivery_dir=request.review_delivery_dir or resolved_settings.review_delivery_dir,
                runner_module=runner_module,
                task_contracts_module=task_contracts_module,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/artifacts")
    def get_artifacts(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        manifest = _load_manifest_for_entry(entry)
        artifact_root = entry.get("artifact_root")
        return {
            "run_id": run_id,
            "artifact_root": artifact_root,
            "artifact_manifest": manifest,
            "artifact_paths": manifest.get("artifact_paths", {}) if manifest else {},
        }

    @app.get("/runs/{run_id}/review-packet")
    def get_review_packet(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        return _load_review_packet_for_entry(entry)

    @app.post("/replay")
    def replay_case(request: ReplayRequest) -> dict[str, Any]:
        try:
            return resolved_replay_module.replay_case_from_manifest(request.manifest_path)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/repeatability")
    def compare_repeatability(request: RepeatabilityRequest) -> dict[str, Any]:
        try:
            return resolved_replay_module.compare_repeatability(request.manifest_paths)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/benchmarks/batch")
    def run_batch_benchmark(request: BatchBenchmarkRequest) -> dict[str, Any]:
        batch_runner = resolved_batch_runner_module or _load_batch_runner_module()
        artifact_root = request.artifact_root or (Path(resolved_settings.artifact_root).expanduser().resolve() / "batch")
        return batch_runner.run_batch_benchmark(cases=request.cases, artifact_root=artifact_root)

    return app


def _default_artifact_dir(settings: ApiSettings, case_id: str) -> Path:
    return Path(settings.artifact_root).expanduser().resolve() / case_id


def _get_registry_entry(registry_path: str | Path, run_id: str) -> dict[str, Any]:
    try:
        return run_registry.get_run(registry_path, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _run_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": entry.get("run_id"),
        "case_id": entry.get("case_id"),
        "status": entry.get("status"),
        "summary": entry.get("summary"),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


def _event_from_history(run_id: str, history_item: dict[str, Any]) -> dict[str, Any]:
    status = history_item.get("status")
    return {
        "event_type": _event_type_for_status(status),
        "run_id": run_id,
        "timestamp": history_item.get("timestamp"),
        "status": status,
        "summary": history_item.get("summary"),
        "payload": dict(history_item),
    }


def _event_type_for_status(status: Any) -> str:
    mapping = {
        "queued": "run.queued",
        "running": "run.running",
        "completed": "run.completed",
        "needs_review": "run.needs_review",
        "failed": "run.failed",
    }
    return mapping.get(str(status), f"run.{status}")


def _load_manifest_for_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        return None
    manifest_path = Path(artifact_root).expanduser().resolve() / "run_manifest.json"
    if not manifest_path.exists():
        return None
    return read_json_artifact(manifest_path)


def _load_review_packet_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
    packet_paths = _review_packet_paths(entry)
    packet_json = packet_paths.get("json")
    markdown_path = packet_paths.get("markdown")
    if packet_json is None or not Path(packet_json).exists():
        return {"present": False, "run_id": entry.get("run_id"), "packet": None, "markdown_path": markdown_path}
    return {
        "present": True,
        "run_id": entry.get("run_id"),
        "packet": read_json_artifact(packet_json),
        "json_path": str(packet_json),
        "markdown_path": str(markdown_path) if markdown_path is not None else None,
    }


def _review_packet_paths(entry: dict[str, Any]) -> dict[str, str | None]:
    delivery_paths = (entry.get("review_delivery") or {}).get("delivered_paths")
    if isinstance(delivery_paths, dict):
        return {
            "json": delivery_paths.get("json"),
            "markdown": delivery_paths.get("markdown"),
        }
    artifact_root = entry.get("artifact_root")
    if artifact_root is None:
        return {"json": None, "markdown": None}
    root = Path(artifact_root).expanduser().resolve()
    return {
        "json": str(root / "review_packet.json"),
        "markdown": str(root / "review_packet.md"),
    }


def _load_batch_runner_module():
    import importlib.util

    module_path = Path(__file__).resolve().parents[3] / "benchmarks" / "runners" / "batch_runner.py"
    spec = importlib.util.spec_from_file_location("api_batch_runner", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load batch runner module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
