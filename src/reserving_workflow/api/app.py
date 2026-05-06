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
from reserving_workflow.artifacts.storage import read_json_artifact, resolve_artifact_path, write_json_artifact
from reserving_workflow.contracts.control_plane import (
    ArtifactRef,
    ChainladderToolInput,
    Review,
    ReviewDecision,
    Run,
    RunEvent,
    RerunSemantics,
    ToolInvocation,
    ValidatedToolInput,
    run_event_type_for_status,
)
from reserving_workflow.review import build_review_contract, ensure_review_record, write_run_review_decision_artifacts
from reserving_workflow.storage.local import LocalReviewStore
from reserving_workflow.runtime import run_registry
from reserving_workflow.tools import build_builtin_tool_registry
from reserving_workflow.workflows import build_builtin_workflow_catalog


class ApiSettings(BaseModel):
    """Runtime settings for the local FastAPI control plane."""

    registry_path: str | Path = Field(default="./tmp/run-registry.json")
    artifact_root: str | Path = Field(default="./tmp/api-artifacts")
    review_delivery_dir: str | Path | None = None
    review_store_dir: str | Path = Field(default="./tmp/reviews")


class RunCreateRequest(BaseModel):
    case_id: str
    artifact_dir: str | Path | None = None
    objective: str = "API-triggered governed workflow run"
    workflow_id: str | None = None
    tool_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    sample_name: str | None = None
    method: str | None = None
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


class ReviewDecisionRequest(BaseModel):
    decision: str
    comment: str | None = None
    decided_by: str | None = None
    follow_up_run_id: str | None = None


def create_app(
    *,
    settings: ApiSettings | None = None,
    runner_module=None,
    task_contracts_module=None,
    replay_module=None,
    batch_runner_module=None,
    background_task_runner=None,
    tool_registry=None,
    workflow_catalog=None,
) -> FastAPI:
    """Create the FastAPI control plane app.

    Test and future runtime callers can inject runner/task-contract modules so
    the API layer remains a transport wrapper over the existing operator core.
    """

    resolved_settings = settings or ApiSettings()
    resolved_replay_module = replay_module or replay_helpers
    resolved_batch_runner_module = batch_runner_module
    resolved_tool_registry = tool_registry or build_builtin_tool_registry()
    resolved_workflow_catalog = workflow_catalog or build_builtin_workflow_catalog()
    resolved_review_store = LocalReviewStore(resolved_settings.review_store_dir)
    app = FastAPI(title="AI Actuary Control Plane", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-actuary-control-plane"}

    @app.get("/console", response_class=HTMLResponse)
    async def operator_console() -> str:
        return _operator_console_html()

    @app.get("/console/state")
    async def operator_console_state(run_id: str | None = None) -> dict[str, Any]:
        runs = run_registry.list_runs(resolved_settings.registry_path)
        selected_entry = _select_console_run(runs, run_id)
        return _console_state_payload(
            selected_entry,
            runs,
            tool_registry=resolved_tool_registry,
            review_store=resolved_review_store,
            review_store_root=resolved_settings.review_store_dir,
        )

    @app.get("/tools")
    async def list_tools() -> dict[str, Any]:
        tools = resolved_tool_registry.list_tool_summaries()
        return {"tool_count": len(tools), "tools": tools}

    @app.get("/tools/{tool_id}")
    async def get_tool(tool_id: str) -> dict[str, Any]:
        try:
            return resolved_tool_registry.get_tool(tool_id).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/workflows")
    async def list_workflows() -> dict[str, Any]:
        workflows = resolved_workflow_catalog.list_workflow_summaries()
        return {"workflow_count": len(workflows), "workflows": workflows}

    @app.get("/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str) -> dict[str, Any]:
        try:
            return resolved_workflow_catalog.get_workflow(workflow_id).to_contract().model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/runs")
    async def create_run(request: RunCreateRequest, background_tasks: BackgroundTasks) -> Any:
        try:
            _safe_artifact_component(request.case_id, field_name="case_id")
            artifact_dir = request.artifact_dir or _default_artifact_dir(resolved_settings, request.case_id)
            workflow_entry = None
            validated_tool_input = None
            if request.workflow_id is not None:
                workflow_entry = resolved_workflow_catalog.get_workflow(request.workflow_id)
            else:
                validated_tool_input = _normalize_tool_invocation(request, tool_registry=resolved_tool_registry)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.errors()) from exc
        review_delivery_dir = request.review_delivery_dir
        if review_delivery_dir is None:
            review_delivery_dir = resolved_settings.review_delivery_dir
        if workflow_entry is not None:
            operator_params = _workflow_operator_params_from_request(
                request,
                workflow_entry=workflow_entry,
                artifact_dir=artifact_dir,
                review_delivery_dir=review_delivery_dir,
                registry_path=resolved_settings.registry_path,
                runner_module=runner_module,
                task_contracts_module=task_contracts_module,
                tool_registry=resolved_tool_registry,
            )
            if request.background:
                run_id = _generate_api_run_id(request.case_id)
                operator_params["run_id"] = run_id
                accepted_payload = _record_background_acceptance(
                    request,
                    validated_tool_input=None,
                    artifact_dir=artifact_dir,
                    review_delivery_dir=review_delivery_dir,
                    registry_path=resolved_settings.registry_path,
                    run_id=run_id,
                    workflow_id=request.workflow_id,
                )
                scheduler = background_task_runner or background_tasks.add_task
                scheduler(_run_workflow_background, operator_params)
                return JSONResponse(status_code=202, content=accepted_payload)
            return JSONResponse(
                content=_run_sequential_workflow(**operator_params)
            )
        operator_params = _operator_params_from_request(
            request,
            validated_tool_input=validated_tool_input,
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
                validated_tool_input=validated_tool_input,
                artifact_dir=artifact_dir,
                review_delivery_dir=review_delivery_dir,
                registry_path=resolved_settings.registry_path,
                run_id=run_id,
                workflow_id=None,
            )
            scheduler = background_task_runner or background_tasks.add_task
            scheduler(_run_operator_flow_background, operator_params)
            return JSONResponse(status_code=202, content=accepted_payload)
        return JSONResponse(content=operator_entrypoint.run_operator_flow(**operator_params))

    @app.get("/runs")
    async def list_runs() -> dict[str, Any]:
        runs = run_registry.list_runs(resolved_settings.registry_path)
        return {
            "registry_path": str(Path(resolved_settings.registry_path)),
            "run_count": len(runs),
            "runs": [_run_summary(entry) for entry in runs],
        }

    @app.get("/runs/{run_id}")
    async def get_run_detail(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        artifact_manifest = _load_manifest_for_entry(entry)
        review_packet = _load_review_packet_for_entry(entry)
        run_payload = dict(entry)
        run_payload.update(_console_selected_run(entry) or {})
        return {
            "run": run_payload,
            "events": [_event_from_history(run_id, item) for item in entry.get("status_history", [])],
            "artifact_manifest": artifact_manifest,
            "artifacts": _artifact_refs_from_manifest(artifact_manifest),
            "review_packet": review_packet.get("packet") if review_packet.get("present") else None,
            "review_delivery": entry.get("review_delivery"),
        }

    @app.get("/runs/{run_id}/events")
    async def get_run_events(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        events = [_event_from_history(run_id, item) for item in entry.get("status_history", [])]
        return {"run_id": run_id, "event_count": len(events), "events": events}

    @app.post("/runs/{run_id}/rerun")
    async def rerun(run_id: str, request: RerunRequest) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        operator_params = dict(entry.get("operator_params", {}) or {})
        if operator_params.get("workflow_id"):
            operator_params["artifact_dir"] = str(request.artifact_dir or entry.get("artifact_root") or _default_artifact_dir(resolved_settings, str(entry.get("case_id") or "case")))
            operator_params["review_delivery_dir"] = request.review_delivery_dir or resolved_settings.review_delivery_dir
            operator_params["registry_path"] = resolved_settings.registry_path
            operator_params["run_id"] = _generate_api_run_id(str(entry.get("case_id") or "case"))
            if runner_module is not None:
                operator_params["runner_module"] = runner_module
            if task_contracts_module is not None:
                operator_params["task_contracts_module"] = task_contracts_module
            operator_params["tool_registry"] = resolved_tool_registry
            result = _run_sequential_workflow(**operator_params)
            result["rerun"] = RerunSemantics(source_run_id=run_id).model_dump()
            return JSONResponse(content=result)
        try:
            return JSONResponse(
                content=operator_entrypoint.rerun_from_registry(
                    run_id,
                    registry_path=resolved_settings.registry_path,
                    artifact_dir=request.artifact_dir,
                    review_delivery_dir=request.review_delivery_dir or resolved_settings.review_delivery_dir,
                    runner_module=runner_module,
                    task_contracts_module=task_contracts_module,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/artifacts")
    async def get_artifacts(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        manifest = _load_manifest_for_entry(entry)
        artifact_root = entry.get("artifact_root")
        return {
            "run_id": run_id,
            "artifact_root": artifact_root,
            "artifact_manifest": manifest,
            "artifact_paths": manifest.get("artifact_paths", {}) if manifest else {},
            "artifacts": _artifact_refs_from_manifest(manifest),
        }

    @app.get("/runs/{run_id}/review-packet")
    async def get_review_packet(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        return _load_review_packet_for_entry(entry)

    @app.get("/runs/{run_id}/review")
    async def get_run_review(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        return {
            "review": _review_payload_for_run(
                entry,
                review_store=resolved_review_store,
                review_store_root=resolved_settings.review_store_dir,
            )
        }

    @app.get("/reviews")
    async def list_reviews() -> dict[str, Any]:
        reviews = _list_review_payloads(
            registry_path=resolved_settings.registry_path,
            review_store=resolved_review_store,
            review_store_root=resolved_settings.review_store_dir,
        )
        return {"review_count": len(reviews), "reviews": reviews}

    @app.get("/reviews/{review_id}")
    async def get_review(review_id: str) -> dict[str, Any]:
        try:
            record = resolved_review_store.get_review(review_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        run_entry = _get_registry_entry(resolved_settings.registry_path, str(record.get("run_id")))
        review_packet = _load_review_packet_for_entry(run_entry)
        return {
            "review": build_review_contract(
                record,
                review_packet_result=review_packet,
                review_store_root=resolved_settings.review_store_dir,
            )
        }

    @app.post("/reviews/{review_id}/decision")
    async def submit_review_decision(review_id: str, request: ReviewDecisionRequest) -> dict[str, Any]:
        try:
            decision_contract = ReviewDecision(
                review_id=review_id,
                run_id="pending-run-id",
                decision=request.decision,
                comment=request.comment,
                decided_by=request.decided_by,
                follow_up_run_id=request.follow_up_run_id,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.errors()) from exc

        try:
            review_record = resolved_review_store.get_review(review_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        run_entry = _get_registry_entry(resolved_settings.registry_path, str(review_record.get("run_id")))
        decision_record = resolved_review_store.submit_decision(
            review_id=review_id,
            decision=decision_contract.decision,
            comment=request.comment,
            decided_by=request.decided_by,
            follow_up_run_id=request.follow_up_run_id,
        )
        decision_record["artifacts"] = write_run_review_decision_artifacts(
            run_entry=run_entry,
            decision_record=decision_record,
        )
        review_packet = _load_review_packet_for_entry(run_entry)
        review = build_review_contract(
            resolved_review_store.get_review(review_id),
            review_packet_result=review_packet,
            review_store_root=resolved_settings.review_store_dir,
        )
        return {"review": review, "decision": decision_record, "run_status": run_entry.get("status")}

    @app.post("/replay")
    async def replay_case(request: ReplayRequest) -> dict[str, Any]:
        try:
            return resolved_replay_module.replay_case_from_manifest(request.manifest_path)
        except (FileNotFoundError, ValueError, KeyError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/repeatability")
    async def compare_repeatability(request: RepeatabilityRequest) -> dict[str, Any]:
        try:
            return resolved_replay_module.compare_repeatability(request.manifest_paths)
        except (FileNotFoundError, ValueError, KeyError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/benchmarks/batch")
    async def run_batch_benchmark(request: BatchBenchmarkRequest) -> dict[str, Any]:
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
    validated_tool_input: ValidatedToolInput,
    artifact_dir: str | Path,
    review_delivery_dir: str | Path | None,
    registry_path: str | Path,
    runner_module=None,
    task_contracts_module=None,
) -> dict[str, Any]:
    tool_inputs = dict(validated_tool_input.inputs)
    params: dict[str, Any] = {
        "case_id": request.case_id,
        "artifact_dir": artifact_dir,
        "objective": request.objective,
        "sample_name": tool_inputs.get("sample_name", "RAA"),
        "method": tool_inputs.get("method_variant", "chainladder"),
        "review_threshold_origin_count": tool_inputs.get("review_threshold_origin_count"),
        "user_prompt": request.user_prompt,
        "review_delivery_dir": review_delivery_dir,
        "registry_path": registry_path,
        "validated_input": {
            "case_id": request.case_id,
            "tool_id": validated_tool_input.tool_id,
            "inputs": tool_inputs,
        },
    }
    if runner_module is not None:
        params["runner_module"] = runner_module
    if task_contracts_module is not None:
        params["task_contracts_module"] = task_contracts_module
    return params


def _workflow_operator_params_from_request(
    request: RunCreateRequest,
    *,
    workflow_entry,
    artifact_dir: str | Path,
    review_delivery_dir: str | Path | None,
    registry_path: str | Path,
    runner_module=None,
    task_contracts_module=None,
    tool_registry=None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "case_id": request.case_id,
        "artifact_dir": artifact_dir,
        "objective": request.objective,
        "workflow_id": workflow_entry.workflow_id,
        "workflow_entry": workflow_entry,
        "review_delivery_dir": review_delivery_dir,
        "registry_path": registry_path,
        "user_prompt": request.user_prompt,
    }
    if runner_module is not None:
        params["runner_module"] = runner_module
    if task_contracts_module is not None:
        params["task_contracts_module"] = task_contracts_module
    if tool_registry is not None:
        params["tool_registry"] = tool_registry
    return params


def _record_background_acceptance(
    request: RunCreateRequest,
    *,
    validated_tool_input: ValidatedToolInput | None,
    artifact_dir: str | Path,
    review_delivery_dir: str | Path | None,
    registry_path: str | Path,
    run_id: str,
    workflow_id: str | None,
) -> dict[str, Any]:
    tool_inputs = dict(validated_tool_input.inputs) if validated_tool_input is not None else {}
    task_id = f"operator-{request.case_id}"
    operator_params = {
        "case_id": request.case_id,
        "artifact_dir": str(artifact_dir),
        "objective": request.objective,
        "sample_name": tool_inputs.get("sample_name", "RAA"),
        "method": tool_inputs.get("method_variant", "chainladder"),
        "review_threshold_origin_count": tool_inputs.get("review_threshold_origin_count"),
        "user_prompt": request.user_prompt,
        "review_delivery_dir": str(review_delivery_dir) if review_delivery_dir is not None else None,
    }
    if validated_tool_input is not None:
        operator_params["validated_input"] = {
            "case_id": request.case_id,
            "tool_id": validated_tool_input.tool_id,
            "inputs": tool_inputs,
        }
    if workflow_id is not None:
        operator_params["workflow_id"] = workflow_id
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
        workflow_id=workflow_id,
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


def _run_workflow_background(operator_params: dict[str, Any]) -> None:
    try:
        _run_sequential_workflow(**operator_params)
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


def _normalize_tool_invocation(request: RunCreateRequest, *, tool_registry) -> ValidatedToolInput:
    tool_invocation = ToolInvocation(tool_id=request.tool_id or "chainladder", inputs=dict(request.inputs or {}))
    legacy_method = (request.method or "").strip() or None
    if request.tool_id is None and legacy_method is not None:
        tool_invocation.tool_id = legacy_method
    elif request.tool_id is not None and legacy_method is not None and request.tool_id != legacy_method:
        raise ValueError(
            f"Conflicting tool selectors: tool_id={request.tool_id!r} does not match legacy method={legacy_method!r}"
        )

    try:
        tool_registry.get_tool(tool_invocation.tool_id)
    except ValueError as exc:
        raise ValueError(f"Unknown tool_id: {tool_invocation.tool_id}") from exc

    merged_inputs = dict(tool_invocation.inputs)
    if request.sample_name is not None and "sample_name" not in merged_inputs:
        merged_inputs["sample_name"] = request.sample_name
    if request.review_threshold_origin_count is not None and "review_threshold_origin_count" not in merged_inputs:
        merged_inputs["review_threshold_origin_count"] = request.review_threshold_origin_count
    if legacy_method is not None and "method_variant" not in merged_inputs and "method" not in merged_inputs:
        merged_inputs["method_variant"] = legacy_method

    if tool_invocation.tool_id == "chainladder":
        validated_inputs = ChainladderToolInput.model_validate(merged_inputs)
        return ValidatedToolInput(
            tool_id=tool_invocation.tool_id,
            inputs=validated_inputs.model_dump(mode="json"),
        )

    raise ValueError(f"Unknown tool_id: {tool_invocation.tool_id}")


def _run_sequential_workflow(
    *,
    case_id: str,
    artifact_dir: str | Path,
    workflow_id: str,
    registry_path: str | Path,
    objective: str = "API-triggered governed workflow run",
    review_delivery_dir: str | Path | None = None,
    user_prompt: str | None = None,
    run_id: str | None = None,
    runner_module=None,
    task_contracts_module=None,
    tool_registry=None,
    workflow_entry=None,
) -> dict[str, Any]:
    if workflow_entry is None:
        workflow_catalog = build_builtin_workflow_catalog()
        workflow_entry = workflow_catalog.get_workflow(workflow_id)
    if tool_registry is None:
        tool_registry = build_builtin_tool_registry()
    artifact_root = Path(artifact_dir).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    parent_run_id = run_id or _generate_api_run_id(case_id)
    task_id = f"operator-{case_id}"
    workflow_steps: list[dict[str, Any]] = []
    step_artifact_paths: dict[str, str] = {}
    last_result: dict[str, Any] | None = None
    final_status = "completed"
    final_summary = f"Workflow {workflow_id} completed for {case_id}"
    workflow_event_payload = {"workflow_id": workflow_id, "step_count": len(workflow_entry.steps)}

    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=parent_run_id,
        status="queued",
        artifact_root=str(artifact_root),
        summary=f"Queued workflow run for {case_id}",
        workflow_id=workflow_id,
        operator_params={"case_id": case_id, "workflow_id": workflow_id},
        review_required=False,
    )
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=parent_run_id,
        status="running",
        artifact_root=str(artifact_root),
        summary=f"Running workflow run for {case_id}",
        workflow_id=workflow_id,
        operator_params={"case_id": case_id, "workflow_id": workflow_id},
        review_required=False,
    )
    _record_workflow_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=parent_run_id,
        artifact_root=artifact_root,
        status="running",
        summary=f"Workflow {workflow_id} started for {case_id}",
        workflow_id=workflow_id,
        event_type="workflow.started",
        event_payload=workflow_event_payload,
    )

    for step_index, step in enumerate(workflow_entry.steps, start=1):
        step_request = RunCreateRequest(
            case_id=case_id,
            objective=objective,
            tool_id=step.tool_id,
            inputs=dict(step.inputs),
            user_prompt=user_prompt,
        )
        step_inputs = _normalize_tool_invocation(step_request, tool_registry=tool_registry)
        step_artifact_dir = artifact_root / step.step_id
        step_payload = {
            "workflow_id": workflow_id,
            "step_id": step.step_id,
            "tool_id": step.tool_id,
            "order": step_index,
        }
        _record_workflow_event(
            registry_path=registry_path,
            task_id=task_id,
            case_id=case_id,
            run_id=parent_run_id,
            artifact_root=artifact_root,
            status="running",
            summary=f"Workflow step {step.step_id} started for {case_id}",
            workflow_id=workflow_id,
            event_type="workflow.step.started",
            event_payload=step_payload,
        )
        step_result = operator_entrypoint.run_operator_flow(
            case_id=case_id,
            artifact_dir=step_artifact_dir,
            objective=objective,
            sample_name=step_inputs.inputs.get("sample_name", "RAA"),
            method=step_inputs.inputs.get("method_variant", "chainladder"),
            review_threshold_origin_count=step_inputs.inputs.get("review_threshold_origin_count"),
            user_prompt=user_prompt,
            review_delivery_dir=review_delivery_dir,
            validated_input={
                "case_id": case_id,
                "tool_id": step_inputs.tool_id,
                "inputs": dict(step_inputs.inputs),
            },
            runner_module=runner_module,
            task_contracts_module=task_contracts_module,
        )
        last_result = step_result
        step_status = step_result.get("status", "failed")
        step_manifest_path = Path(step_result.get("final_output", {}).get("artifact_manifest_path") or step_artifact_dir / "run_manifest.json").expanduser().resolve()
        if step_manifest_path.exists():
            step_artifact_paths[f"step_{step.step_id}_run_manifest"] = str(step_manifest_path)
        workflow_steps.append(
            {
                "step_id": step.step_id,
                "tool_id": step.tool_id,
                "title": step.title,
                "status": step_status,
                "artifact_dir": str(step_artifact_dir),
                "run_id": step_result.get("run_id"),
            }
        )
        step_finished_event_type = _workflow_step_finished_event_type(step_status)
        _record_workflow_event(
            registry_path=registry_path,
            task_id=task_id,
            case_id=case_id,
            run_id=parent_run_id,
            artifact_root=artifact_root,
            status="running" if step_status == "completed" else step_status,
            summary=f"Workflow step {step.step_id} finished with status {step_status}",
            workflow_id=workflow_id,
            event_type=step_finished_event_type,
            event_payload={**step_payload, "status": step_status},
        )
        if step_status != "completed":
            final_status = step_status
            final_summary = f"Workflow {workflow_id} ended with status {step_status} for {case_id}"
            break

    workflow_summary_path = write_json_artifact(
        resolve_artifact_path(artifact_root, "workflow_summary.json"),
        {
            "workflow_id": workflow_id,
            "case_id": case_id,
            "run_id": parent_run_id,
            "status": final_status,
            "step_count": len(workflow_steps),
            "steps": workflow_steps,
        },
    )
    manifest_payload = {
        "workflow_id": workflow_id,
        "case_id": case_id,
        "run_id": parent_run_id,
        "artifact_root": str(artifact_root),
        "artifact_paths": {
            "workflow_summary": str(workflow_summary_path),
            **step_artifact_paths,
        },
    }
    run_manifest_path = write_json_artifact(resolve_artifact_path(artifact_root, "run_manifest.json"), manifest_payload)

    _record_workflow_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=parent_run_id,
        artifact_root=artifact_root,
        status=final_status if final_status != "completed" else "running",
        summary=final_summary,
        workflow_id=workflow_id,
        event_type=_workflow_finished_event_type(final_status),
        event_payload={"workflow_id": workflow_id, "status": final_status, "step_count": len(workflow_steps)},
    )
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=parent_run_id,
        status=final_status,
        artifact_root=str(artifact_root),
        summary=final_summary,
        review_required=final_status == "needs_review",
        workflow_id=workflow_id,
        operator_params={
            "case_id": case_id,
            "artifact_dir": str(artifact_root),
            "objective": objective,
            "workflow_id": workflow_id,
            "user_prompt": user_prompt,
            "review_delivery_dir": str(review_delivery_dir) if review_delivery_dir is not None else None,
        },
    )

    result = {
        "ok": final_status != "failed",
        "status": final_status,
        "case_id": case_id,
        "run_id": parent_run_id,
        "summary": final_summary,
        "workflow": {
            "workflow_id": workflow_id,
            "title": workflow_entry.title,
            "description": workflow_entry.description,
            "step_count": len(workflow_steps),
            "steps": workflow_steps,
        },
        "final_output": {
            "artifact_manifest_path": str(run_manifest_path),
        },
        "worker_result": {
            "status": final_status,
            "case_id": case_id,
            "run_id": parent_run_id,
            "summary": final_summary,
            "artifact_paths": manifest_payload["artifact_paths"],
        },
        "errors": list((last_result or {}).get("errors", []) or []),
        "error_category": (last_result or {}).get("error_category"),
    }
    if last_result is not None:
        result["route"] = last_result.get("route", {})
        result["trace"] = last_result.get("trace", {})
        if last_result.get("review_packet") is not None:
            result["review_packet"] = last_result["review_packet"]
        if last_result.get("review_delivery") is not None:
            result["review_delivery"] = last_result["review_delivery"]
    else:
        result["route"] = {}
        result["trace"] = {}
    return result


def _workflow_step_finished_event_type(status: str) -> str:
    if status == "completed":
        return "workflow.step.completed"
    if status == "needs_review":
        return "workflow.step.needs_review"
    return "workflow.step.failed"


def _workflow_finished_event_type(status: str) -> str:
    if status == "completed":
        return "workflow.completed"
    if status == "needs_review":
        return "workflow.needs_review"
    return "workflow.failed"


def _record_workflow_event(
    *,
    registry_path: str | Path,
    task_id: str,
    case_id: str,
    run_id: str,
    artifact_root: str | Path,
    status: str,
    summary: str,
    workflow_id: str,
    event_type: str,
    event_payload: dict[str, Any],
) -> dict[str, Any]:
    return run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=run_id,
        status=status,
        artifact_root=str(Path(artifact_root).expanduser().resolve()),
        summary=summary,
        workflow_id=workflow_id,
        operator_params={"case_id": case_id, "workflow_id": workflow_id},
        event_type=event_type,
        event_payload=event_payload,
        review_required=False,
    )


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
    return Run(
        run_id=str(entry.get("run_id")),
        case_id=entry.get("case_id"),
        status=entry.get("status"),
        summary=entry.get("summary"),
        created_at=entry.get("created_at"),
        updated_at=entry.get("updated_at"),
        artifact_root=entry.get("artifact_root"),
        review_required=bool(entry.get("review_required")) or entry.get("status") == "needs_review",
        workflow_id=entry.get("workflow_id") or (entry.get("operator_params", {}) or {}).get("workflow_id"),
    ).model_dump(exclude_none=True)


def _select_console_run(runs: list[dict[str, Any]], run_id: str | None) -> dict[str, Any] | None:
    if run_id is None:
        return runs[0] if runs else None
    for entry in runs:
        if entry.get("run_id") == run_id:
            return entry
    raise HTTPException(status_code=404, detail=f"Run id not found in registry: {run_id}")


def _console_state_payload(
    selected_entry: dict[str, Any] | None,
    runs: list[dict[str, Any]],
    *,
    tool_registry,
    review_store,
    review_store_root: str | Path,
) -> dict[str, Any]:
    selected_run_id = str(selected_entry.get("run_id")) if selected_entry else None
    review_inbox = _review_inbox_payload(
        registry_path=None,
        runs=runs,
        review_store=review_store,
        review_store_root=review_store_root,
        selected_run_id=selected_run_id,
    )
    return {
        "console": {
            "title": "AI Actuary Operator Console",
            "description": "Symphony-style shell over the existing governed run control plane.",
            "version": "pr12-review-inbox",
        },
        "tool_catalog": {"tool_count": len(tool_registry.list_tools()), "tools": tool_registry.list_tool_summaries()},
        "selected_run_id": selected_run_id,
        "selected_run": _console_selected_run(selected_entry),
        "run_cards": [_console_run_card(entry, selected_run_id=selected_run_id) for entry in runs],
        "timeline": _console_timeline(selected_entry),
        "artifact_panel": _console_artifact_panel(selected_entry),
        "review_inbox": review_inbox,
        "review_panel": _console_review_panel(
            selected_entry,
            review_store=review_store,
            review_store_root=review_store_root,
        ),
        "action_panel": _console_action_panel(selected_entry),
    }


def _console_selected_run(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return Run(
        run_id=str(entry.get("run_id")),
        case_id=entry.get("case_id"),
        status=entry.get("status"),
        summary=entry.get("summary"),
        created_at=entry.get("created_at"),
        updated_at=entry.get("updated_at"),
        artifact_root=entry.get("artifact_root"),
        review_required=bool(entry.get("review_required")) or entry.get("status") == "needs_review",
        workflow_id=entry.get("workflow_id") or (entry.get("operator_params", {}) or {}).get("workflow_id"),
    ).model_dump(exclude_none=True)


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
        return {"present": False, "artifact_root": None, "artifact_manifest": None, "artifact_paths": {}, "artifacts": []}
    manifest = _load_manifest_for_entry(entry)
    return {
        "present": manifest is not None,
        "artifact_root": entry.get("artifact_root"),
        "artifact_manifest": manifest,
        "artifact_paths": manifest.get("artifact_paths", {}) if manifest else {},
        "artifacts": _artifact_refs_from_manifest(manifest),
    }


def _console_review_panel(
    entry: dict[str, Any] | None,
    *,
    review_store,
    review_store_root: str | Path,
) -> dict[str, Any]:
    if entry is None:
        review = Review(status="not_available", review_required=False)
    else:
        review = Review.model_validate(
            _review_payload_for_run(
                entry,
                review_store=review_store,
                review_store_root=review_store_root,
            )
        )
    payload = review.model_dump()
    payload["present"] = bool(payload.get("review_id")) or payload.get("packet") is not None
    return payload


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
                "semantics": RerunSemantics(source_run_id=str(run_id)).model_dump(),
            }
        ]
    }


def _event_from_history(run_id: str, history_item: dict[str, Any]) -> dict[str, Any]:
    status = history_item.get("status")
    event_type = history_item.get("event_type") or run_event_type_for_status(status)
    event = RunEvent(
        type=event_type,
        run_id=run_id,
        timestamp=history_item.get("timestamp"),
        status=status,
        summary=history_item.get("summary"),
        payload=dict(history_item.get("payload", history_item)),
    ).model_dump()
    event["event_type"] = event["type"]
    return event


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


def _review_payload_for_run(
    entry: dict[str, Any],
    *,
    review_store,
    review_store_root: str | Path,
) -> dict[str, Any]:
    review_packet = _load_review_packet_for_entry(entry)
    record = ensure_review_record(
        review_store=review_store,
        run_entry=entry,
        review_packet=review_packet.get("packet") if review_packet.get("present") else None,
    )
    if record is None:
        status = "review_required" if bool(entry.get("review_required")) or entry.get("status") == "needs_review" else "not_required"
        return Review(
            status=status,
            review_required=status == "review_required",
            run_id=str(entry.get("run_id")),
            case_id=entry.get("case_id"),
            packet=review_packet.get("packet") if review_packet.get("present") else None,
            json_path=review_packet.get("json_path"),
            markdown_path=review_packet.get("markdown_path"),
            review_delivery=entry.get("review_delivery"),
        ).model_dump(exclude_none=True)
    return build_review_contract(
        record,
        review_packet_result=review_packet,
        review_store_root=review_store_root,
    )


def _list_review_payloads(
    *,
    registry_path: str | Path,
    review_store,
    review_store_root: str | Path,
) -> list[dict[str, Any]]:
    runs = run_registry.list_runs(registry_path)
    return _review_inbox_payload(
        registry_path=registry_path,
        runs=runs,
        review_store=review_store,
        review_store_root=review_store_root,
        selected_run_id=None,
    )


def _review_inbox_payload(
    *,
    registry_path: str | Path | None,
    runs: list[dict[str, Any]],
    review_store,
    review_store_root: str | Path,
    selected_run_id: str | None,
) -> list[dict[str, Any]]:
    del registry_path
    reviews: list[dict[str, Any]] = []
    seen_review_ids: set[str] = set()
    for entry in runs:
        review_payload = _review_payload_for_run(
            entry,
            review_store=review_store,
            review_store_root=review_store_root,
        )
        review_id = review_payload.get("review_id")
        if not review_id:
            continue
        if review_id in seen_review_ids:
            continue
        seen_review_ids.add(str(review_id))
        reviews.append(
            {
                "review_id": review_id,
                "run_id": review_payload.get("run_id"),
                "case_id": review_payload.get("case_id"),
                "status": review_payload.get("status"),
                "decision": (review_payload.get("decision") or {}).get("decision"),
                "updated_at": review_payload.get("updated_at"),
                "selected": review_payload.get("run_id") == selected_run_id,
            }
        )
    return sorted(reviews, key=lambda item: item.get("updated_at") or "", reverse=True)


def _artifact_refs_from_manifest(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    if manifest is None:
        return []
    artifact_paths = manifest.get("artifact_paths", {}) or {}
    artifact_root = manifest.get("artifact_root")
    root = Path(artifact_root).expanduser() if artifact_root else None
    artifacts = []
    for artifact_id, path in artifact_paths.items():
        artifact_path = Path(str(path)).expanduser()
        if not artifact_path.is_absolute() and root is not None:
            artifact_path = root / artifact_path
        artifacts.append(
            ArtifactRef(
                artifact_id=str(artifact_id),
                label=str(artifact_id).replace("_", " "),
                path=str(artifact_path),
                present=artifact_path.exists(),
            ).model_dump()
        )
    return artifacts


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
    input, select { box-sizing: border-box; width: 100%; margin-top: 4px; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; color: #102a43; background: white; }
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
        <label>tool
          <select id="tool-selector" name="tool_id" data-default-tool-id="chainladder">
            <option value="chainladder">Loading tool catalog…</option>
          </select>
        </label>
        <label>review_threshold_origin_count<input name="review_threshold_origin_count" type="number" min="0" step="1" placeholder="optional"></label>
        <label><input name="background" type="checkbox" checked> background</label>
        <button id="create-run-button" class="primary" type="submit">Create run</button>
      </form>
      <div id="operation-status" class="status" aria-live="polite"></div>
      <h2>Run Queue</h2>
      <div id="run-queue" class="empty">Loading runs…</div>
      <h2>Review Inbox</h2>
      <div id="review-inbox" class="empty">Loading reviews…</div>
    </section>
    <div class="workspace">
      <section aria-label="Timeline"><h2>Timeline</h2><div id="timeline" class="panel-body">Loading…</div></section>
      <section aria-label="Artifact Panel"><h2>Artifact Panel</h2><pre id="artifact-panel">Loading…</pre></section>
      <section aria-label="Review Panel">
        <h2>Review Panel</h2>
        <pre id="review-panel">Loading…</pre>
        <form id="review-decision-form" onsubmit="submitReviewDecision(event)">
          <input id="review-id-input" name="review_id" type="hidden">
          <label>decision
            <select name="decision">
              <option value="approved">approved</option>
              <option value="changes_requested">changes_requested</option>
              <option value="rejected">rejected</option>
            </select>
          </label>
          <label>decided_by<input name="decided_by" placeholder="actuary-001"></label>
          <label>comment<input name="comment" placeholder="Optional decision note"></label>
          <button id="submit-review-decision" type="submit">Submit review decision</button>
        </form>
      </section>
      <section aria-label="Action Panel"><h2>Action Panel</h2><div id="action-panel" class="panel-body">Loading…</div></section>
    </div>
  </main>
  <script>
    let selectedRunId = null;
    let pollTimer = null;
    let pollGeneration = 0;
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
      document.getElementById("review-inbox").textContent = message;
      document.getElementById("action-panel").textContent = message;
    }

    function formatResponseDetail(detail) {
      if (!detail) return "";
      if (typeof detail === "string") return detail;
      try {
        return JSON.stringify(detail);
      } catch (error) {
        return String(detail);
      }
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
        const detailMessage = formatResponseDetail(payload.detail);
        const detail = detailMessage ? `: ${detailMessage}` : "";
        throw new Error(`${context} failed (${response.status} ${response.statusText})${detail}`);
      }
      return payload;
    }

    async function loadToolCatalog() {
      const selector = document.getElementById("tool-selector");
      try {
        const response = await fetch("/tools");
        const payload = await parseJsonOrThrow(response, "Tool catalog");
        const tools = payload.tools || [];
        if (!tools.length) return;
        selector.innerHTML = "";
        for (const tool of tools) {
          const option = document.createElement("option");
          option.value = tool.tool_id || tool.method;
          option.textContent = tool.title ? `${tool.title} (${tool.tool_id})` : (tool.tool_id || tool.method || "tool");
          if ((tool.tool_id || tool.method) === selector.dataset.defaultToolId) {
            option.selected = true;
          }
          selector.appendChild(option);
        }
        if (!selector.value && tools.length) {
          selector.value = tools[0].tool_id || tools[0].method;
        }
      } catch (error) {
        const fallback = selector.querySelector("option[value='chainladder']") || selector.options[0];
        if (fallback) {
          fallback.value = fallback.value || "chainladder";
          fallback.textContent = "Chainladder (chainladder)";
          fallback.selected = true;
        }
        const message = error instanceof Error ? error.message : "Tool catalog unavailable; using default tool.";
        setOperationStatus(message, "error");
      }
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
      pollGeneration += 1;
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function startPolling(runId) {
      stopPolling();
      if (!runId) return;
      selectedRunId = runId;
      const generation = pollGeneration;
      pollRunEvents(runId, generation);
      pollTimer = setInterval(() => pollRunEvents(runId, generation), 2000);
    }

    async function pollRunEvents(runId, generation = pollGeneration) {
      const isCurrentPoll = () => generation === pollGeneration && runId === selectedRunId;
      try {
        const response = await fetch(`/runs/${encodeURIComponent(runId)}/events`);
        const payload = await parseJsonOrThrow(response, "Run events");
        if (!isCurrentPoll()) return;
        renderTimeline(payload.events || []);
        if (!shouldPollEvents(payload.events || [])) {
          stopPolling();
          await loadConsole(runId, { preservePolling: true });
        }
      } catch (error) {
        if (!isCurrentPoll()) return;
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

    function renderReviewInbox(reviews) {
      const inbox = document.getElementById("review-inbox");
      inbox.innerHTML = "";
      if (!reviews || reviews.length === 0) {
        inbox.textContent = "No reviews recorded yet.";
        inbox.className = "empty";
        return;
      }
      inbox.className = "";
      for (const review of reviews) {
        const button = document.createElement("button");
        button.className = "run-card";
        if (review.selected) button.setAttribute("selected", "selected");
        button.textContent = `${review.case_id || "unknown case"} · ${review.status || "review"}`;
        button.onclick = () => loadConsole(review.run_id);
        inbox.appendChild(button);
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
      if (!options.preservePolling) stopPolling();
      const suffix = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
      try {
        const response = await fetch(`/console/state${suffix}`);
        const state = await parseJsonOrThrow(response, "Console state");
        selectedRunId = state.selected_run_id;
        renderRunQueue(state);
        renderReviewInbox(state.review_inbox || []);
        renderTimeline(state.timeline || []);
        document.getElementById("artifact-panel").textContent = JSON.stringify(state.artifact_panel, null, 2);
        document.getElementById("review-panel").textContent = JSON.stringify(state.review_panel, null, 2);
        document.getElementById("review-id-input").value = state.review_panel && state.review_panel.review_id ? state.review_panel.review_id : "";
        document.getElementById("submit-review-decision").disabled = !(state.review_panel && state.review_panel.review_id);
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
        tool_id: String(formData.get("tool_id") || "chainladder").trim() || "chainladder",
        method: String(formData.get("tool_id") || "chainladder").trim() || "chainladder",
        inputs: {
          sample_name: String(formData.get("sample_name") || "RAA").trim() || "RAA",
        },
        background: formData.get("background") === "on",
      };
      if (thresholdValue !== null && String(thresholdValue).trim() !== "") {
        const thresholdNumber = Number(thresholdValue);
        if (!Number.isInteger(thresholdNumber) || thresholdNumber < 0) {
          setOperationStatus("review_threshold_origin_count must be a non-negative integer.", "error");
          return;
        }
        payload.inputs.review_threshold_origin_count = thresholdNumber;
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

    async function submitReviewDecision(event) {
      event.preventDefault();
      const formData = new FormData(event.currentTarget);
      const reviewId = String(formData.get("review_id") || "").trim();
      if (!reviewId) {
        setOperationStatus("No review selected for decision submission.", "error");
        return;
      }
      const payload = {
        decision: String(formData.get("decision") || "").trim(),
        decided_by: String(formData.get("decided_by") || "").trim() || null,
        comment: String(formData.get("comment") || "").trim() || null,
      };
      setOperationStatus(`Submitting review decision for ${reviewId}…`);
      try {
        const response = await fetch(`/reviews/${encodeURIComponent(reviewId)}/decision`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await parseJsonOrThrow(response, "Review decision");
        setOperationStatus(`${result.decision.decision}: ${reviewId}`, "ok");
        await loadConsole(result.review.run_id || selectedRunId);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to submit review decision.";
        setOperationStatus(message, "error");
      }
    }

    Promise.all([loadToolCatalog(), loadConsole()]).catch((error) => {
      const message = error instanceof Error ? error.message : "Failed to initialize console.";
      renderConsoleError(message);
      setOperationStatus(message, "error");
    });
  </script>
</body>
</html>"""
