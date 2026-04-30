"""FastAPI control plane for operator-facing AI Actuary runs.

This module intentionally wraps the existing operator/artifact/registry
boundaries instead of introducing a second runtime implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from reserving_workflow import operator_entrypoint
from reserving_workflow.artifacts import replay as replay_helpers
from reserving_workflow.artifacts.storage import read_json_artifact, resolve_artifact_path
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
    background: bool = False


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
    background_task_runner=None,
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

    @app.get("/console", response_class=HTMLResponse)
    def operator_console() -> str:
        return _operator_console_html()

    @app.get("/console/state")
    def operator_console_state(run_id: str | None = None) -> dict[str, Any]:
        runs = run_registry.list_runs(resolved_settings.registry_path)
        selected_entry = _select_console_run(runs, run_id)
        return _console_state_payload(selected_entry, runs)

    @app.post("/runs")
    def create_run(request: RunCreateRequest, background_tasks: BackgroundTasks) -> Any:
        try:
            _safe_artifact_component(request.case_id, field_name="case_id")
            artifact_dir = request.artifact_dir or _default_artifact_dir(resolved_settings, request.case_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        review_delivery_dir = request.review_delivery_dir
        if review_delivery_dir is None:
            review_delivery_dir = resolved_settings.review_delivery_dir
        operator_params = _operator_params_from_request(
            request,
            artifact_dir=artifact_dir,
            review_delivery_dir=review_delivery_dir,
            registry_path=resolved_settings.registry_path,
            runner_module=runner_module,
            task_contracts_module=task_contracts_module,
        )
        if request.background:
            run_id = _generate_api_run_id(request.case_id)
            operator_params["run_id"] = run_id
            accepted_payload = _record_background_acceptance(
                request,
                artifact_dir=artifact_dir,
                review_delivery_dir=review_delivery_dir,
                registry_path=resolved_settings.registry_path,
                run_id=run_id,
            )
            scheduler = background_task_runner or background_tasks.add_task
            scheduler(_run_operator_flow_background, operator_params)
            return JSONResponse(status_code=202, content=accepted_payload)
        return operator_entrypoint.run_operator_flow(**operator_params)

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

    @app.get("/runs/{run_id}/events")
    def get_run_events(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        events = [_event_from_history(run_id, item) for item in entry.get("status_history", [])]
        return {"run_id": run_id, "event_count": len(events), "events": events}

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
        except (FileNotFoundError, ValueError, KeyError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/repeatability")
    def compare_repeatability(request: RepeatabilityRequest) -> dict[str, Any]:
        try:
            return resolved_replay_module.compare_repeatability(request.manifest_paths)
        except (FileNotFoundError, ValueError, KeyError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/benchmarks/batch")
    def run_batch_benchmark(request: BatchBenchmarkRequest) -> dict[str, Any]:
        nonlocal resolved_batch_runner_module
        if resolved_batch_runner_module is None:
            resolved_batch_runner_module = _load_batch_runner_module()
        artifact_root = request.artifact_root or (Path(resolved_settings.artifact_root).expanduser().resolve() / "batch")
        return resolved_batch_runner_module.run_batch_benchmark(cases=request.cases, artifact_root=artifact_root)

    return app


def _default_artifact_dir(settings: ApiSettings, case_id: str) -> Path:
    return resolve_artifact_path(settings.artifact_root, _safe_artifact_component(case_id, field_name="case_id"))


def _operator_params_from_request(
    request: RunCreateRequest,
    *,
    artifact_dir: str | Path,
    review_delivery_dir: str | Path | None,
    registry_path: str | Path,
    runner_module=None,
    task_contracts_module=None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "case_id": request.case_id,
        "artifact_dir": artifact_dir,
        "objective": request.objective,
        "sample_name": request.sample_name,
        "method": request.method,
        "review_threshold_origin_count": request.review_threshold_origin_count,
        "user_prompt": request.user_prompt,
        "review_delivery_dir": review_delivery_dir,
        "registry_path": registry_path,
    }
    if runner_module is not None:
        params["runner_module"] = runner_module
    if task_contracts_module is not None:
        params["task_contracts_module"] = task_contracts_module
    return params


def _record_background_acceptance(
    request: RunCreateRequest,
    *,
    artifact_dir: str | Path,
    review_delivery_dir: str | Path | None,
    registry_path: str | Path,
    run_id: str,
) -> dict[str, Any]:
    task_id = f"operator-{request.case_id}"
    operator_params = {
        "case_id": request.case_id,
        "artifact_dir": str(artifact_dir),
        "objective": request.objective,
        "sample_name": request.sample_name,
        "method": request.method,
        "review_threshold_origin_count": request.review_threshold_origin_count,
        "user_prompt": request.user_prompt,
        "review_delivery_dir": str(review_delivery_dir) if review_delivery_dir is not None else None,
    }
    entry = run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=request.case_id,
        run_id=run_id,
        status="accepted",
        artifact_root=str(Path(artifact_dir).expanduser().resolve()),
        summary=f"Accepted background operator run for {request.case_id}",
        operator_params=operator_params,
        review_required=False,
    )
    events = [_event_from_history(run_id, item) for item in entry.get("status_history", [])]
    return {
        "ok": True,
        "status": "accepted",
        "execution_mode": "background",
        "case_id": request.case_id,
        "run_id": run_id,
        "summary": f"Accepted background operator run for {request.case_id}",
        "events": events,
    }


def _run_operator_flow_background(operator_params: dict[str, Any]) -> None:
    try:
        operator_entrypoint.run_operator_flow(**operator_params)
    except Exception as exc:
        _record_background_failure(operator_params, exc)


def _record_background_failure(operator_params: dict[str, Any], exc: Exception) -> None:
    registry_path = operator_params.get("registry_path")
    if registry_path is None:
        return
    case_id = operator_params.get("case_id")
    run_id = operator_params.get("run_id") or _generate_api_run_id(str(case_id or "case"))
    artifact_dir = operator_params.get("artifact_dir") or "./tmp/api-artifacts/background-failed"
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=f"operator-{case_id or 'unknown-case'}",
        case_id=str(case_id) if case_id is not None else None,
        run_id=str(run_id),
        status="failed",
        artifact_root=str(Path(artifact_dir).expanduser().resolve()),
        summary=f"Background operator run failed for {case_id or 'unknown-case'}",
        review_required=False,
        error_category="background_runtime",
        errors=[str(exc)],
    )


def _generate_api_run_id(case_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"operator-{case_id}-{timestamp}"


def _safe_artifact_component(value: Any, *, field_name: str) -> str:
    component = str(value)
    candidate = Path(component)
    if component in {"", ".", ".."}:
        raise ValueError(f"Invalid {field_name}: {component!r}")
    if "/" in component or "\\" in component:
        raise ValueError(f"Invalid {field_name}: {component!r}")
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise ValueError(f"Invalid {field_name}: {component!r}")
    return component


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


def _select_console_run(runs: list[dict[str, Any]], run_id: str | None) -> dict[str, Any] | None:
    if run_id is None:
        return runs[0] if runs else None
    for entry in runs:
        if entry.get("run_id") == run_id:
            return entry
    raise HTTPException(status_code=404, detail=f"Run id not found in registry: {run_id}")


def _console_state_payload(selected_entry: dict[str, Any] | None, runs: list[dict[str, Any]]) -> dict[str, Any]:
    selected_run_id = str(selected_entry.get("run_id")) if selected_entry else None
    return {
        "console": {
            "title": "AI Actuary Operator Console",
            "description": "Symphony-style shell over the existing governed run control plane.",
            "version": "pr5-shell",
        },
        "selected_run_id": selected_run_id,
        "selected_run": _console_selected_run(selected_entry),
        "run_cards": [_console_run_card(entry, selected_run_id=selected_run_id) for entry in runs],
        "timeline": _console_timeline(selected_entry),
        "artifact_panel": _console_artifact_panel(selected_entry),
        "review_panel": _console_review_panel(selected_entry),
        "action_panel": _console_action_panel(selected_entry),
    }


def _console_selected_run(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "run_id": entry.get("run_id"),
        "case_id": entry.get("case_id"),
        "status": entry.get("status"),
        "summary": entry.get("summary"),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "artifact_root": entry.get("artifact_root"),
        "review_required": bool(entry.get("review_required")) or entry.get("status") == "needs_review",
    }


def _console_run_card(entry: dict[str, Any], *, selected_run_id: str | None) -> dict[str, Any]:
    status = entry.get("status")
    return {
        "run_id": entry.get("run_id"),
        "case_id": entry.get("case_id"),
        "status": status,
        "summary": entry.get("summary"),
        "updated_at": entry.get("updated_at"),
        "needs_review": bool(entry.get("review_required")) or status == "needs_review",
        "selected": entry.get("run_id") == selected_run_id,
    }


def _console_timeline(entry: dict[str, Any] | None) -> list[dict[str, Any]]:
    if entry is None:
        return []
    run_id = str(entry.get("run_id"))
    return [_event_from_history(run_id, item) for item in entry.get("status_history", [])]


def _console_artifact_panel(entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {"present": False, "artifact_root": None, "artifact_manifest": None, "artifact_paths": {}}
    manifest = _load_manifest_for_entry(entry)
    return {
        "present": manifest is not None,
        "artifact_root": entry.get("artifact_root"),
        "artifact_manifest": manifest,
        "artifact_paths": manifest.get("artifact_paths", {}) if manifest else {},
    }


def _console_review_panel(entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {"present": False, "status": "not_available", "review_required": False, "packet": None}
    packet = _load_review_packet_for_entry(entry)
    packet_payload = packet.get("packet") if packet.get("present") else None
    return {
        "present": bool(packet.get("present")),
        "status": (packet_payload or {}).get("status", "not_required"),
        "review_required": bool(entry.get("review_required")) or entry.get("status") == "needs_review",
        "packet": packet_payload,
        "json_path": packet.get("json_path"),
        "markdown_path": packet.get("markdown_path"),
        "review_delivery": entry.get("review_delivery"),
    }


def _console_action_panel(entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {"actions": []}
    run_id = entry.get("run_id")
    return {
        "actions": [
            {
                "action_id": "rerun",
                "label": "Rerun",
                "method": "POST",
                "path": f"/runs/{run_id}/rerun",
                "enabled": bool(run_id),
            }
        ]
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
        "accepted": "run.accepted",
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


def _operator_console_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Actuary Operator Console</title>
  <style>
    :root { color-scheme: light; --border: #d9e2ec; --muted: #52606d; --accent: #2457c5; --danger: #b42318; --ok: #027a48; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f7fb; color: #102a43; }
    header { padding: 24px 32px; background: #102a43; color: white; }
    header p { margin: 8px 0 0; color: #bcccdc; }
    main { display: grid; grid-template-columns: 340px 1fr; gap: 16px; padding: 16px; }
    section { background: white; border: 1px solid var(--border); border-radius: 12px; padding: 16px; box-shadow: 0 1px 2px rgba(16, 42, 67, 0.08); }
    .workspace { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    h1, h2 { margin: 0 0 12px; }
    h2 { font-size: 16px; }
    label { display: block; margin: 10px 0; font-size: 13px; color: var(--muted); }
    input { box-sizing: border-box; width: 100%; margin-top: 4px; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; color: #102a43; background: white; }
    input[type="checkbox"] { width: auto; margin-right: 6px; }
    button, .pill { border: 1px solid var(--border); border-radius: 999px; padding: 6px 10px; background: #f8fafc; cursor: pointer; }
    button.primary { border-color: var(--accent); background: var(--accent); color: white; }
    button:disabled { cursor: not-allowed; opacity: 0.55; }
    .run-card { display: block; width: 100%; margin: 0 0 8px; text-align: left; border-radius: 10px; }
    .run-card[selected] { border-color: var(--accent); color: var(--accent); }
    .status { min-height: 18px; margin: 8px 0 16px; color: var(--muted); font-size: 13px; }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--ok); }
    pre, .panel-body { white-space: pre-wrap; word-break: break-word; background: #f8fafc; border-radius: 8px; padding: 12px; max-height: 360px; overflow: auto; }
    .timeline-list { margin: 0; padding: 0; list-style: none; }
    .timeline-list li { padding: 8px 0; border-bottom: 1px solid var(--border); }
    .timeline-list li:last-child { border-bottom: 0; }
    .event-type { color: var(--accent); font-weight: 700; }
    .event-time { display: block; color: var(--muted); font-size: 12px; }
    .empty { color: var(--muted); }
    @media (max-width: 900px) { main, .workspace { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>AI Actuary Operator Console</h1>
    <p>Symphony-style shell over runs, timeline events, artifacts, review packets, and actions. Data source: <code>/console/state</code>.</p>
  </header>
  <main>
    <section aria-label="Run Queue">
      <h2>Create Governed Run</h2>
      <form id="create-run-form" onsubmit="createRun(event)">
        <label>case_id<input name="case_id" required placeholder="demo-case"></label>
        <label>sample_name<input name="sample_name" value="RAA"></label>
        <label>method<input name="method" value="chainladder"></label>
        <label>review_threshold_origin_count<input name="review_threshold_origin_count" type="number" min="0" step="1" placeholder="optional"></label>
        <label><input name="background" type="checkbox" checked> background</label>
        <button id="create-run-button" class="primary" type="submit">Create run</button>
      </form>
      <div id="operation-status" class="status" aria-live="polite"></div>
      <h2>Run Queue</h2>
      <div id="run-queue" class="empty">Loading runs…</div>
    </section>
    <div class="workspace">
      <section aria-label="Timeline"><h2>Timeline</h2><div id="timeline" class="panel-body">Loading…</div></section>
      <section aria-label="Artifact Panel"><h2>Artifact Panel</h2><pre id="artifact-panel">Loading…</pre></section>
      <section aria-label="Review Panel"><h2>Review Panel</h2><pre id="review-panel">Loading…</pre></section>
      <section aria-label="Action Panel"><h2>Action Panel</h2><div id="action-panel" class="panel-body">Loading…</div></section>
    </div>
  </main>
  <script>
    let selectedRunId = null;
    let pollTimer = null;
    const activeEventTypes = ["run.accepted", "run.queued", "run.running"];
    const terminalEventTypes = ["run.completed", "run.failed", "run.needs_review"];

    function setOperationStatus(message, statusClass = "") {
      const status = document.getElementById("operation-status");
      status.textContent = message || "";
      status.className = statusClass ? `status ${statusClass}` : "status";
    }

    function renderConsoleError(message) {
      const queue = document.getElementById("run-queue");
      queue.textContent = message;
      queue.className = "empty";
      renderTimeline([{event_type: "run.failed", summary: message}]);
      document.getElementById("artifact-panel").textContent = message;
      document.getElementById("review-panel").textContent = message;
      document.getElementById("action-panel").textContent = message;
    }

    async function parseJsonOrThrow(response, context) {
      const body = await response.text();
      let payload;
      try {
        payload = body ? JSON.parse(body) : {};
      } catch (error) {
        throw new Error(`${context} response was not valid JSON.`);
      }
      if (!response.ok) {
        const detail = payload.detail ? `: ${payload.detail}` : "";
        throw new Error(`${context} failed (${response.status} ${response.statusText})${detail}`);
      }
      return payload;
    }

    function renderTimeline(events) {
      const timeline = document.getElementById("timeline");
      timeline.innerHTML = "";
      if (!events || events.length === 0) {
        timeline.textContent = "No events recorded yet.";
        timeline.className = "panel-body empty";
        return;
      }
      timeline.className = "panel-body";
      const list = document.createElement("ul");
      list.className = "timeline-list";
      for (const event of events) {
        const item = document.createElement("li");
        const type = document.createElement("span");
        type.className = "event-type";
        type.textContent = event.event_type || "run.unknown";
        const time = document.createElement("span");
        time.className = "event-time";
        time.textContent = event.timestamp || "pending timestamp";
        const summary = document.createElement("span");
        summary.textContent = event.summary ? ` — ${event.summary}` : "";
        item.append(type, summary, time);
        list.appendChild(item);
      }
      timeline.appendChild(list);
    }

    function shouldPollEvents(events) {
      if (!events || events.length === 0) return false;
      if (events.some((event) => terminalEventTypes.includes(event.event_type))) return false;
      return events.some((event) => activeEventTypes.includes(event.event_type));
    }

    function stopPolling() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function startPolling(runId) {
      stopPolling();
      if (!runId) return;
      selectedRunId = runId;
      pollRunEvents(runId);
      pollTimer = setInterval(() => pollRunEvents(runId), 2000);
    }

    async function pollRunEvents(runId) {
      try {
        const response = await fetch(`/runs/${encodeURIComponent(runId)}/events`);
        const payload = await parseJsonOrThrow(response, "Run events");
        renderTimeline(payload.events || []);
        if (!shouldPollEvents(payload.events || [])) {
          stopPolling();
          await loadConsole(runId, { preservePolling: true });
        }
      } catch (error) {
        stopPolling();
        const message = error instanceof Error ? error.message : "Failed to poll run events.";
        setOperationStatus(message, "error");
      }
    }

    function renderRunQueue(state) {
      const queue = document.getElementById("run-queue");
      queue.innerHTML = "";
      if (!state.run_cards.length) {
        queue.textContent = "No runs recorded yet.";
        queue.className = "empty";
        return;
      }
      queue.className = "";
      for (const card of state.run_cards) {
        const button = document.createElement("button");
        button.className = "run-card";
        if (card.selected) button.setAttribute("selected", "selected");
        button.textContent = `${card.case_id || "unknown case"} · ${card.status || "unknown"}`;
        button.onclick = () => loadConsole(card.run_id);
        queue.appendChild(button);
      }
    }

    function renderActionPanel(actionPanel) {
      const container = document.getElementById("action-panel");
      container.innerHTML = "";
      const actions = (actionPanel && actionPanel.actions) || [];
      if (actions.length === 0) {
        container.textContent = "No actions available.";
        container.className = "panel-body empty";
        return;
      }
      container.className = "panel-body";
      for (const action of actions) {
        const button = document.createElement("button");
        button.textContent = action.label || action.action_id || "Run action";
        button.disabled = !action.enabled;
        button.onclick = () => runConsoleAction(action);
        container.appendChild(button);
      }
    }

    async function loadConsole(runId, options = {}) {
      const suffix = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
      try {
        const response = await fetch(`/console/state${suffix}`);
        const state = await parseJsonOrThrow(response, "Console state");
        selectedRunId = state.selected_run_id;
        renderRunQueue(state);
        renderTimeline(state.timeline || []);
        document.getElementById("artifact-panel").textContent = JSON.stringify(state.artifact_panel, null, 2);
        document.getElementById("review-panel").textContent = JSON.stringify(state.review_panel, null, 2);
        renderActionPanel(state.action_panel);
        if (!options.preservePolling && shouldPollEvents(state.timeline || [])) {
          startPolling(state.selected_run_id);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to load console state.";
        renderConsoleError(message);
      }
    }

    async function createRun(event) {
      event.preventDefault();
      stopPolling();
      const form = event.currentTarget;
      const formData = new FormData(form);
      const thresholdValue = formData.get("review_threshold_origin_count");
      const payload = {
        case_id: String(formData.get("case_id") || "").trim(),
        sample_name: String(formData.get("sample_name") || "RAA").trim() || "RAA",
        method: String(formData.get("method") || "chainladder").trim() || "chainladder",
        background: formData.get("background") === "on",
      };
      if (thresholdValue !== null && String(thresholdValue).trim() !== "") {
        payload.review_threshold_origin_count = Number(thresholdValue);
      }
      setOperationStatus("Creating governed run…");
      try {
        const response = await fetch("/runs", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await parseJsonOrThrow(response, "Create run");
        setOperationStatus(`${result.status || "created"}: ${result.run_id || result.case_id || "run"}`, "ok");
        await loadConsole(result.run_id);
        if (result.run_id && (result.execution_mode === "background" || activeEventTypes.includes(`run.${result.status}`))) {
          startPolling(result.run_id);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to create run.";
        setOperationStatus(message, "error");
      }
    }

    async function runConsoleAction(action) {
      if (!action || !action.enabled) return;
      stopPolling();
      setOperationStatus(`Running action: ${action.label || action.action_id || "action"}…`);
      try {
        const response = await fetch(action.path, {
          method: action.method || "POST",
          headers: { "content-type": "application/json" },
          body: "{}",
        });
        const result = await parseJsonOrThrow(response, action.label || "Console action");
        setOperationStatus(`${result.status || "completed"}: ${result.run_id || "action complete"}`, "ok");
        if (result.run_id) {
          await loadConsole(result.run_id);
          if (activeEventTypes.includes(`run.${result.status}`)) {
            startPolling(result.run_id);
          }
        } else {
          await loadConsole(selectedRunId);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to run console action.";
        setOperationStatus(message, "error");
      }
    }

    loadConsole();
  </script>
</body>
</html>"""
