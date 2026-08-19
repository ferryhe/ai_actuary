"""FastAPI control plane for operator-facing AI Actuary runs.

This module intentionally wraps the existing operator/artifact/registry
boundaries instead of introducing a second runtime implementation.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from reserving_workflow import operator_entrypoint
from reserving_workflow.adapters.control_plane.projections import (
    ARTIFACT_PROJECTION_SPECS,
    ArtifactProjectionReadError,
    build_artifact_projection,
    provenance_for_artifact,
    read_bounded_json_object,
    validate_artifact_projection_schema,
)
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
from reserving_workflow.interfaces.operator_console import load_operator_console_html
from reserving_workflow.model_tools import (
    MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
    ExperienceStudyToolInput,
    run_minimax_experience_study,
)
from reserving_workflow.review import (
    build_review_contract,
    build_review_snapshot,
    ensure_review_record,
    write_run_review_decision_artifacts,
)
from reserving_workflow.reports import export_run_report
from reserving_workflow.storage.local import LocalReviewStore, ReviewDecisionConflictError
from reserving_workflow.runtime import build_preflight_report, run_registry
from reserving_workflow.tools import build_builtin_tool_registry
from reserving_workflow.validation import (
    ReservingValidationError,
    build_chainladder_case_input,
    build_chainladder_case_payload,
    build_chainladder_validation_summary,
    validate_chainladder_case,
)
from reserving_workflow.workflows import build_builtin_workflow_catalog

DEFAULT_OPERATOR_ID = "local-actuary"
DEFAULT_WORKSPACE_ID = "default-workspace"
MODEL_COMPARISON_TOOL_RUNNERS = {
    MINIMAX_EXPERIENCE_STUDY_TOOL_ID: run_minimax_experience_study,
}
MAX_RESULT_ARTIFACT_BYTES = 1_000_000
MAX_PROJECTED_RESULTS = 1_000
UNAVAILABLE = "unavailable"


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
    operator_id: str | None = None
    workspace_id: str | None = None
    created_by: str | None = None
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
    app = FastAPI(title="AI Actuary Control Plane", version="0.1.0")

    def _get_review_store() -> LocalReviewStore:
        try:
            return LocalReviewStore(resolved_settings.review_store_dir)
        except OSError as exc:  # pragma: no cover - exercised through API surface
            raise HTTPException(status_code=503, detail="Review store unavailable.") from exc

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-actuary-control-plane"}

    @app.get("/health/preflight")
    async def health_preflight() -> dict[str, Any]:
        return build_preflight_report(
            service="ai-actuary-control-plane",
            version=app.version,
            registry_path=resolved_settings.registry_path,
            artifact_root=resolved_settings.artifact_root,
            review_store_dir=resolved_settings.review_store_dir,
            review_delivery_dir=resolved_settings.review_delivery_dir,
            tool_ids=[entry.tool_id for entry in resolved_tool_registry.list_tools()],
            workflow_ids=[entry.workflow_id for entry in resolved_workflow_catalog.list_workflows()],
            default_operator_id=DEFAULT_OPERATOR_ID,
            default_workspace_id=DEFAULT_WORKSPACE_ID,
        )

    @app.get("/console", response_class=HTMLResponse)
    async def operator_console() -> str:
        return load_operator_console_html()

    @app.get("/console/state")
    async def operator_console_state(
        request: Request,
        run_id: str | None = None,
        operator_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        current_identity = _resolve_current_identity(
            operator_id=operator_id,
            workspace_id=workspace_id,
            request=request,
            fallback_to_defaults=True,
        )
        all_runs = run_registry.list_runs(resolved_settings.registry_path)
        runs = _filter_run_entries(
            all_runs,
            operator_id=current_identity["operator_id"],
            workspace_id=current_identity["workspace_id"],
        )
        selected_entry = _select_console_run(runs, run_id)
        return _console_state_payload(
            selected_entry,
            runs,
            all_runs=all_runs,
            tool_registry=resolved_tool_registry,
            review_store=_get_review_store(),
            review_store_root=resolved_settings.review_store_dir,
            filters=current_identity,
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
    def create_run(request: RunCreateRequest, background_tasks: BackgroundTasks, http_request: Request) -> Any:
        try:
            _safe_artifact_component(request.case_id, field_name="case_id")
            artifact_dir = request.artifact_dir or _default_artifact_dir(resolved_settings, request.case_id)
            workflow_entry = None
            validated_tool_input = None
            ownership = _resolve_request_ownership(request, http_request)
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
            try:
                operator_params = _workflow_operator_params_from_request(
                    request,
                    workflow_entry=workflow_entry,
                    artifact_dir=artifact_dir,
                    review_delivery_dir=review_delivery_dir,
                    registry_path=resolved_settings.registry_path,
                    ownership=ownership,
                    runner_module=runner_module,
                    task_contracts_module=task_contracts_module,
                    tool_registry=resolved_tool_registry,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=exc.errors()) from exc
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
                    ownership=ownership,
                )
                scheduler = background_task_runner or background_tasks.add_task
                scheduler(_run_workflow_background, operator_params)
                return JSONResponse(status_code=202, content=accepted_payload)
            return JSONResponse(
                content=_run_sequential_workflow(**operator_params)
            )
        if validated_tool_input.tool_id in MODEL_COMPARISON_TOOL_RUNNERS:
            model_tool_params = _model_tool_params_from_request(
                request,
                validated_tool_input=validated_tool_input,
                artifact_dir=artifact_dir,
                registry_path=resolved_settings.registry_path,
                ownership=ownership,
            )
            run_id = _generate_api_run_id(request.case_id)
            model_tool_params["run_id"] = run_id
            if request.background:
                accepted_payload = _record_background_acceptance(
                    request,
                    validated_tool_input=validated_tool_input,
                    artifact_dir=artifact_dir,
                    review_delivery_dir=review_delivery_dir,
                    registry_path=resolved_settings.registry_path,
                    run_id=run_id,
                    workflow_id=None,
                    ownership=ownership,
                )
                scheduler = background_task_runner or background_tasks.add_task
                scheduler(_run_model_tool_background, model_tool_params)
                return JSONResponse(status_code=202, content=accepted_payload)
            return JSONResponse(
                content=_run_registered_model_tool(
                    model_tool_params,
                    execution_mode="synchronous",
                )
            )
        operator_params = _operator_params_from_request(
            request,
            validated_tool_input=validated_tool_input,
            artifact_dir=artifact_dir,
            review_delivery_dir=review_delivery_dir,
            registry_path=resolved_settings.registry_path,
            ownership=ownership,
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
                ownership=ownership,
            )
            scheduler = background_task_runner or background_tasks.add_task
            scheduler(_run_operator_flow_background, operator_params)
            return JSONResponse(status_code=202, content=accepted_payload)
        return JSONResponse(content=operator_entrypoint.run_operator_flow(**operator_params))

    @app.get("/runs")
    async def list_runs(request: Request, operator_id: str | None = None, workspace_id: str | None = None) -> dict[str, Any]:
        runs = _filter_run_entries(
            run_registry.list_runs(resolved_settings.registry_path),
            operator_id=_normalize_identity_filter(operator_id, request=request, header_name="x-operator-id"),
            workspace_id=_normalize_identity_filter(workspace_id, request=request, header_name="x-workspace-id"),
        )
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
    def rerun(run_id: str, request: RerunRequest) -> dict[str, Any]:
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
        model_tool_id = operator_params.get("tool_id")
        if model_tool_id in MODEL_COMPARISON_TOOL_RUNNERS:
            rerun_id = _generate_api_run_id(str(entry.get("case_id") or "case"))
            result = _run_registered_model_tool(
                {
                    "case_id": str(entry.get("case_id") or "case"),
                    "inputs": dict(operator_params.get("inputs") or {}),
                    "artifact_dir": str(
                        request.artifact_dir
                        or _default_artifact_dir(
                            resolved_settings, str(entry.get("case_id") or "case")
                        )
                    ),
                    "registry_path": resolved_settings.registry_path,
                    "run_id": rerun_id,
                    "created_by": str(entry.get("created_by") or DEFAULT_OPERATOR_ID),
                    "operator_id": str(entry.get("operator_id") or DEFAULT_OPERATOR_ID),
                    "workspace_id": str(entry.get("workspace_id") or DEFAULT_WORKSPACE_ID),
                    "tool_id": model_tool_id,
                },
                execution_mode="synchronous",
            )
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

    @app.get("/runs/{run_id}/artifacts/{artifact_id}/projection")
    async def get_artifact_projection(run_id: str, artifact_id: str) -> dict[str, Any]:
        if artifact_id not in ARTIFACT_PROJECTION_SPECS:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "artifact_unsupported",
                    "message": "Artifact projection is not supported.",
                },
            )
        try:
            entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": "Run was not found."},
            ) from exc
        try:
            return _artifact_projection_for_run(entry, artifact_id=artifact_id)
        except ArtifactProjectionReadError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    @app.get("/runs/{run_id}/results")
    async def get_results(run_id: str) -> dict[str, Any]:
        entry = _get_registry_entry(resolved_settings.registry_path, run_id)
        return _result_panel_projection(entry)

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
                review_store=_get_review_store(),
                review_store_root=resolved_settings.review_store_dir,
            )
        }

    @app.post("/runs/{run_id}/report-export")
    async def create_run_report_export(run_id: str) -> dict[str, Any]:
        try:
            report = export_run_report(
                registry_path=resolved_settings.registry_path,
                run_id=run_id,
                review_store_root=resolved_settings.review_store_dir,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"report": report}

    @app.get("/reviews")
    async def list_reviews(request: Request, operator_id: str | None = None, workspace_id: str | None = None) -> dict[str, Any]:
        reviews = _list_review_payloads(
            registry_path=resolved_settings.registry_path,
            review_store=_get_review_store(),
            review_store_root=resolved_settings.review_store_dir,
            operator_id=_normalize_identity_filter(operator_id, request=request, header_name="x-operator-id"),
            workspace_id=_normalize_identity_filter(workspace_id, request=request, header_name="x-workspace-id"),
        )
        return {"review_count": len(reviews), "reviews": reviews}

    @app.get("/reviews/{review_id}")
    async def get_review(review_id: str) -> dict[str, Any]:
        run_id = _run_id_from_review_id(review_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="Review not found.")
        try:
            review_store = _get_review_store()
            record = review_store.get_review(review_id)
        except ValueError:
            run_entry = _get_registry_entry(resolved_settings.registry_path, run_id)
            review = _review_payload_for_run(
                run_entry,
                review_store=review_store,
                review_store_root=resolved_settings.review_store_dir,
            )
            if review.get("review_id") != review_id:
                raise HTTPException(status_code=404, detail="Review not found.")
            return {"review": review}
        run_entry = _get_registry_entry(resolved_settings.registry_path, str(record.get("run_id")))
        return {
            "review": _review_payload_for_run(
                run_entry,
                review_store=review_store,
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
            review_store = _get_review_store()
            review_record = review_store.get_review(review_id)
        except ValueError as exc:
            review_record = _materialize_review_record_from_id(
                review_id,
                registry_path=resolved_settings.registry_path,
                review_store=review_store,
            )
            if review_record is None:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            run_entry = _get_registry_entry(resolved_settings.registry_path, str(review_record.get("run_id")))
            decision_record = review_store.submit_decision(
                review_id=review_id,
                decision=decision_contract.decision,
                comment=request.comment,
                decided_by=request.decided_by,
                follow_up_run_id=request.follow_up_run_id,
            )
        except ReviewDecisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        decision_record["artifacts"] = write_run_review_decision_artifacts(
            run_entry=run_entry,
            decision_record=decision_record,
        )
        review_packet = _load_review_packet_for_entry(run_entry)
        review = build_review_contract(
            review_store.get_review(review_id),
            review_packet_result=review_packet,
            review_store_root=resolved_settings.review_store_dir,
            decision_artifacts=_decision_artifacts_for_run(run_entry),
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
    ownership: dict[str, str],
    runner_module=None,
    task_contracts_module=None,
) -> dict[str, Any]:
    tool_inputs = dict(validated_tool_input.inputs)
    case_payload = _case_payload_from_tool_input(request.case_id, validated_tool_input)
    params: dict[str, Any] = {
        "case_id": request.case_id,
        "artifact_dir": artifact_dir,
        "objective": request.objective,
        "sample_name": tool_inputs.get("sample_name", "RAA"),
        "method": tool_inputs.get("method_variant", "chainladder"),
        "review_threshold_origin_count": tool_inputs.get("review_threshold_origin_count"),
        "case_payload": case_payload,
        "user_prompt": request.user_prompt,
        "review_delivery_dir": review_delivery_dir,
        "registry_path": registry_path,
        "created_by": ownership["created_by"],
        "operator_id": ownership["operator_id"],
        "workspace_id": ownership["workspace_id"],
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


def _model_tool_params_from_request(
    request: RunCreateRequest,
    *,
    validated_tool_input: ValidatedToolInput,
    artifact_dir: str | Path,
    registry_path: str | Path,
    ownership: dict[str, str],
) -> dict[str, Any]:
    return {
        "case_id": request.case_id,
        "tool_id": validated_tool_input.tool_id,
        "inputs": dict(validated_tool_input.inputs),
        "artifact_dir": str(artifact_dir),
        "registry_path": str(registry_path),
        "created_by": ownership["created_by"],
        "operator_id": ownership["operator_id"],
        "workspace_id": ownership["workspace_id"],
    }


def _workflow_operator_params_from_request(
    request: RunCreateRequest,
    *,
    workflow_entry,
    artifact_dir: str | Path,
    review_delivery_dir: str | Path | None,
    registry_path: str | Path,
    ownership: dict[str, str],
    runner_module=None,
    task_contracts_module=None,
    tool_registry=None,
) -> dict[str, Any]:
    workflow_inputs = _workflow_inputs_from_request(request)
    if tool_registry is not None:
        for step in workflow_entry.steps:
            _normalize_tool_invocation(
                RunCreateRequest(
                    case_id=request.case_id,
                    objective=request.objective,
                    tool_id=step.tool_id,
                    inputs={**dict(step.inputs), **workflow_inputs},
                    user_prompt=request.user_prompt,
                ),
                tool_registry=tool_registry,
            )
    params: dict[str, Any] = {
        "case_id": request.case_id,
        "artifact_dir": artifact_dir,
        "objective": request.objective,
        "workflow_id": workflow_entry.workflow_id,
        "workflow_entry": workflow_entry,
        "workflow_inputs": workflow_inputs,
        "review_delivery_dir": review_delivery_dir,
        "registry_path": registry_path,
        "user_prompt": request.user_prompt,
        "created_by": ownership["created_by"],
        "operator_id": ownership["operator_id"],
        "workspace_id": ownership["workspace_id"],
    }
    if runner_module is not None:
        params["runner_module"] = runner_module
    if task_contracts_module is not None:
        params["task_contracts_module"] = task_contracts_module
    if tool_registry is not None:
        params["tool_registry"] = tool_registry
    return params


def _case_payload_from_tool_input(case_id: str, validated_tool_input: ValidatedToolInput) -> dict[str, Any]:
    if validated_tool_input.tool_id == "chainladder":
        return build_chainladder_case_payload(
            case_id=case_id,
            tool_inputs=validated_tool_input.inputs,
        )
    if validated_tool_input.tool_id in MODEL_COMPARISON_TOOL_RUNNERS:
        return {
            "case_id": case_id,
            "tool_id": validated_tool_input.tool_id,
            "inputs": dict(validated_tool_input.inputs),
        }
    raise ValueError(f"Unknown tool_id: {validated_tool_input.tool_id}")


def _workflow_inputs_from_request(request: RunCreateRequest) -> dict[str, Any]:
    workflow_inputs = dict(request.inputs or {})
    if request.sample_name is not None and "sample_name" not in workflow_inputs:
        workflow_inputs["sample_name"] = request.sample_name
    if request.review_threshold_origin_count is not None and "review_threshold_origin_count" not in workflow_inputs:
        workflow_inputs["review_threshold_origin_count"] = request.review_threshold_origin_count
    if request.method is not None and "method_variant" not in workflow_inputs and "method" not in workflow_inputs:
        workflow_inputs["method"] = request.method
    return workflow_inputs


def _record_background_acceptance(
    request: RunCreateRequest,
    *,
    validated_tool_input: ValidatedToolInput | None,
    artifact_dir: str | Path,
    review_delivery_dir: str | Path | None,
    registry_path: str | Path,
    run_id: str,
    workflow_id: str | None,
    ownership: dict[str, str],
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
        "created_by": ownership["created_by"],
        "operator_id": ownership["operator_id"],
        "workspace_id": ownership["workspace_id"],
    }
    if validated_tool_input is not None:
        operator_params["tool_id"] = validated_tool_input.tool_id
        operator_params["inputs"] = tool_inputs
        operator_params["case_payload"] = _case_payload_from_tool_input(request.case_id, validated_tool_input)
        operator_params["validated_input"] = {
            "case_id": request.case_id,
            "tool_id": validated_tool_input.tool_id,
            "inputs": tool_inputs,
        }
    if workflow_id is not None:
        operator_params["workflow_id"] = workflow_id
        operator_params["workflow_inputs"] = _workflow_inputs_from_request(request)
    entry = run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=request.case_id,
        run_id=run_id,
        status="accepted",
        artifact_root=str(Path(artifact_dir).expanduser().resolve()),
        summary=f"Accepted background operator run for {request.case_id}",
        operator_params=operator_params,
        created_by=ownership["created_by"],
        operator_id=ownership["operator_id"],
        workspace_id=ownership["workspace_id"],
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
        _record_run_failure(operator_params, exc, execution_mode="background")


def _run_registered_model_tool(
    operator_params: dict[str, Any],
    *,
    execution_mode: Literal["synchronous", "background"],
) -> dict[str, Any]:
    params = dict(operator_params)
    tool_id = str(params.pop("tool_id"))
    try:
        runner = MODEL_COMPARISON_TOOL_RUNNERS[tool_id]
    except KeyError as exc:
        raise ValueError(f"Unknown model comparison tool_id: {tool_id}") from exc
    try:
        return runner(**params)
    except Exception as exc:
        _record_run_failure(operator_params, exc, execution_mode=execution_mode)
        raise


def _run_model_tool_background(operator_params: dict[str, Any]) -> None:
    try:
        _run_registered_model_tool(operator_params, execution_mode="background")
    except Exception:
        return


def _run_workflow_background(operator_params: dict[str, Any]) -> None:
    try:
        _run_sequential_workflow(**operator_params)
    except Exception as exc:
        _record_run_failure(operator_params, exc, execution_mode="background")


def _record_run_failure(
    operator_params: dict[str, Any],
    exc: Exception,
    *,
    execution_mode: Literal["synchronous", "background"],
) -> None:
    registry_path = operator_params.get("registry_path")
    if registry_path is None:
        return
    case_id = operator_params.get("case_id")
    run_id = operator_params.get("run_id") or _generate_api_run_id(str(case_id or "case"))
    default_failure_dir = f"./tmp/api-artifacts/{execution_mode}-failed"
    artifact_dir = operator_params.get("artifact_dir") or default_failure_dir
    artifact_root = Path(artifact_dir).expanduser().resolve()
    if operator_params.get("tool_id") in MODEL_COMPARISON_TOOL_RUNNERS:
        artifact_root = (artifact_root / str(run_id)).resolve()
    execution_label = execution_mode.capitalize()
    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=f"operator-{case_id or 'unknown-case'}",
        case_id=str(case_id) if case_id is not None else None,
        run_id=str(run_id),
        status="failed",
        artifact_root=str(artifact_root),
        summary=f"{execution_label} operator run failed for {case_id or 'unknown-case'}",
        created_by=operator_params.get("created_by"),
        operator_id=operator_params.get("operator_id"),
        workspace_id=operator_params.get("workspace_id"),
        review_required=False,
        error_category=f"{execution_mode}_runtime",
        errors=[str(exc)],
    )


def _generate_api_run_id(case_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"operator-{case_id}-{timestamp}"


def _normalize_identity_value(value: Any) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    return candidate


def _normalize_identity_filter(value: str | None, *, request: Request, header_name: str) -> str | None:
    direct = _normalize_identity_value(value)
    if direct is not None:
        return direct
    return _normalize_identity_value(request.headers.get(header_name))


def _resolve_current_identity(
    *,
    operator_id: str | None,
    workspace_id: str | None,
    request: Request,
    fallback_to_defaults: bool,
) -> dict[str, str]:
    resolved_operator_id = _normalize_identity_filter(operator_id, request=request, header_name="x-operator-id")
    resolved_workspace_id = _normalize_identity_filter(workspace_id, request=request, header_name="x-workspace-id")
    if fallback_to_defaults:
        resolved_operator_id = resolved_operator_id or DEFAULT_OPERATOR_ID
        resolved_workspace_id = resolved_workspace_id or DEFAULT_WORKSPACE_ID
    return {
        "operator_id": resolved_operator_id or "",
        "workspace_id": resolved_workspace_id or "",
    }


def _resolve_request_ownership(request: RunCreateRequest, http_request: Request) -> dict[str, str]:
    operator_id = _normalize_identity_value(request.operator_id) or _normalize_identity_value(
        http_request.headers.get("x-operator-id")
    ) or DEFAULT_OPERATOR_ID
    workspace_id = _normalize_identity_value(request.workspace_id) or _normalize_identity_value(
        http_request.headers.get("x-workspace-id")
    ) or DEFAULT_WORKSPACE_ID
    created_by = _normalize_identity_value(request.created_by) or _normalize_identity_value(
        http_request.headers.get("x-created-by")
    ) or operator_id
    return {
        "created_by": created_by,
        "operator_id": operator_id,
        "workspace_id": workspace_id,
    }


def _entry_identity_value(entry: dict[str, Any], field_name: str) -> str | None:
    value = _normalize_identity_value(entry.get(field_name))
    if value is not None:
        return value
    if field_name in {"operator_id", "created_by"}:
        return DEFAULT_OPERATOR_ID
    if field_name == "workspace_id":
        return DEFAULT_WORKSPACE_ID
    return None


def _entry_matches_identity_filters(
    entry: dict[str, Any],
    *,
    operator_id: str | None,
    workspace_id: str | None,
) -> bool:
    if operator_id is not None and _entry_identity_value(entry, "operator_id") != operator_id:
        return False
    if workspace_id is not None and _entry_identity_value(entry, "workspace_id") != workspace_id:
        return False
    return True


def _filter_run_entries(
    runs: list[dict[str, Any]],
    *,
    operator_id: str | None,
    workspace_id: str | None,
) -> list[dict[str, Any]]:
    return [
        entry for entry in runs
        if _entry_matches_identity_filters(entry, operator_id=operator_id, workspace_id=workspace_id)
    ]


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
    if (
        tool_invocation.tool_id == "chainladder"
        and legacy_method is not None
        and "method_variant" not in merged_inputs
        and "method" not in merged_inputs
    ):
        merged_inputs["method_variant"] = legacy_method

    if tool_invocation.tool_id == "chainladder":
        validated_inputs = ChainladderToolInput.model_validate(merged_inputs)
        case_input = build_chainladder_case_input(
            case_id=request.case_id,
            tool_inputs=validated_inputs.model_dump(mode="json"),
        )
        validate_chainladder_case(case_input)
        return ValidatedToolInput(
            tool_id=tool_invocation.tool_id,
            inputs=validated_inputs.model_dump(mode="json"),
        )
    if tool_invocation.tool_id == MINIMAX_EXPERIENCE_STUDY_TOOL_ID:
        validated_inputs = ExperienceStudyToolInput.model_validate(merged_inputs)
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
    created_by: str | None = None,
    operator_id: str | None = None,
    workspace_id: str | None = None,
    run_id: str | None = None,
    runner_module=None,
    task_contracts_module=None,
    tool_registry=None,
    workflow_entry=None,
    workflow_inputs: dict[str, Any] | None = None,
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
    resolved_workflow_inputs = dict(workflow_inputs or {})

    run_registry.record_run_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=parent_run_id,
        status="queued",
        artifact_root=str(artifact_root),
        summary=f"Queued workflow run for {case_id}",
        workflow_id=workflow_id,
        operator_params={
            "case_id": case_id,
            "workflow_id": workflow_id,
            "workflow_inputs": resolved_workflow_inputs,
            "created_by": created_by,
            "operator_id": operator_id,
            "workspace_id": workspace_id,
        },
        created_by=created_by,
        operator_id=operator_id,
        workspace_id=workspace_id,
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
        operator_params={
            "case_id": case_id,
            "workflow_id": workflow_id,
            "workflow_inputs": resolved_workflow_inputs,
            "created_by": created_by,
            "operator_id": operator_id,
            "workspace_id": workspace_id,
        },
        created_by=created_by,
        operator_id=operator_id,
        workspace_id=workspace_id,
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
            inputs={**dict(step.inputs), **resolved_workflow_inputs},
            user_prompt=user_prompt,
        )
        step_inputs = _normalize_tool_invocation(step_request, tool_registry=tool_registry)
        step_artifact_dir = artifact_root / step.step_id
        case_payload = _case_payload_from_tool_input(case_id, step_inputs)
        step_payload = {
            "workflow_id": workflow_id,
            "step_id": step.step_id,
            "tool_id": step.tool_id,
            "step_kind": step.step_kind,
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
        if step.step_kind == "validate":
            step_result = _run_validation_step(
                case_id=case_id,
                artifact_dir=step_artifact_dir,
                tool_input=step_inputs,
                case_payload=case_payload,
            )
        else:
            step_result = operator_entrypoint.run_operator_flow(
                case_id=case_id,
                artifact_dir=step_artifact_dir,
                objective=objective,
                sample_name=step_inputs.inputs.get("sample_name", "RAA"),
                method=step_inputs.inputs.get("method_variant", "chainladder"),
                review_threshold_origin_count=step_inputs.inputs.get("review_threshold_origin_count"),
                case_payload=case_payload,
                user_prompt=user_prompt,
                review_delivery_dir=review_delivery_dir,
                created_by=created_by,
                operator_id=operator_id,
                workspace_id=workspace_id,
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
                "step_kind": step.step_kind,
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
            "workflow_inputs": resolved_workflow_inputs,
            "created_by": created_by,
            "operator_id": operator_id,
            "workspace_id": workspace_id,
        },
        created_by=created_by,
        operator_id=operator_id,
        workspace_id=workspace_id,
    )

    result = {
        "ok": final_status != "failed",
        "status": final_status,
        "case_id": case_id,
        "run_id": parent_run_id,
        "summary": final_summary,
        "created_by": created_by,
        "operator_id": operator_id,
        "workspace_id": workspace_id,
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


def _run_validation_step(
    *,
    case_id: str,
    artifact_dir: str | Path,
    tool_input: ValidatedToolInput,
    case_payload: dict[str, Any],
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    validated_input_payload = {
        "case_id": case_id,
        "tool_id": tool_input.tool_id,
        "inputs": dict(tool_input.inputs),
    }
    validated_input_path = write_json_artifact(
        resolve_artifact_path(artifact_root, "validated_input.json"),
        validated_input_payload,
    )
    case_input_path = write_json_artifact(
        resolve_artifact_path(artifact_root, "case_input.json"),
        case_payload,
    )
    try:
        case_input = build_chainladder_case_input(case_id=case_id, tool_inputs=tool_input.inputs)
        validated_source = validate_chainladder_case(case_input)
        validation_result = build_chainladder_validation_summary(case_input, validated_source)
        status = "completed"
        ok = True
        summary = f"Validated chainladder input for {case_id}"
        errors: list[str] = []
        error_category = None
    except (ValidationError, ReservingValidationError, ValueError) as exc:
        validation_result = {
            "case_id": case_id,
            "status": "failed",
            "tool_id": tool_input.tool_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        status = "failed"
        ok = False
        summary = f"Validation failed for {case_id}"
        errors = [str(exc)]
        error_category = "validation"
    validation_result_path = write_json_artifact(
        resolve_artifact_path(artifact_root, "validation_result.json"),
        validation_result,
    )
    manifest_payload = {
        "case_id": case_id,
        "run_id": None,
        "artifact_root": str(artifact_root),
        "artifact_paths": {
            "validated_input": str(validated_input_path),
            "case_input": str(case_input_path),
            "validation_result": str(validation_result_path),
        },
    }
    run_manifest_path = resolve_artifact_path(artifact_root, "run_manifest.json")
    manifest_payload["artifact_paths"]["run_manifest"] = str(run_manifest_path)
    write_json_artifact(run_manifest_path, manifest_payload)
    return {
        "ok": ok,
        "status": status,
        "case_id": case_id,
        "run_id": None,
        "summary": summary,
        "route": {},
        "trace": {},
        "worker_result": {
            "status": status,
            "case_id": case_id,
            "run_id": None,
            "summary": summary,
            "artifact_paths": manifest_payload["artifact_paths"],
        },
        "final_output": {
            "artifact_manifest_path": str(run_manifest_path),
        },
        "validation": validation_result,
        "errors": errors,
        "error_category": error_category,
    }


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
        created_by=entry.get("created_by"),
        operator_id=entry.get("operator_id"),
        workspace_id=entry.get("workspace_id"),
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
    all_runs: list[dict[str, Any]] | None = None,
    tool_registry,
    review_store,
    review_store_root: str | Path,
    filters: dict[str, str],
) -> dict[str, Any]:
    selected_run_id = str(selected_entry.get("run_id")) if selected_entry else None
    review_inbox = _review_inbox_payload(
        registry_path=None,
        runs=runs,
        review_store=review_store,
        review_store_root=review_store_root,
        selected_run_id=selected_run_id,
    )
    filter_option_runs = all_runs if all_runs is not None else runs
    return {
        "console": {
            "title": "AI Actuary Operator Console",
            "description": "Symphony-style shell over the existing governed run control plane.",
            "version": "pr13-workspace-ownership",
        },
        "filters": {
            "operator_id": filters["operator_id"],
            "workspace_id": filters["workspace_id"],
            "available_operator_ids": _identity_filter_options(filter_option_runs, field_name="operator_id"),
            "available_workspace_ids": _identity_filter_options(filter_option_runs, field_name="workspace_id"),
        },
        "tool_catalog": {"tool_count": len(tool_registry.list_tools()), "tools": tool_registry.list_tool_summaries()},
        "selected_run_id": selected_run_id,
        "selected_run": _console_selected_run(selected_entry),
        "run_cards": [_console_run_card(entry, selected_run_id=selected_run_id) for entry in runs],
        "timeline": _console_timeline(selected_entry),
        "result_panel": _result_panel_projection(selected_entry),
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
        created_by=entry.get("created_by"),
        operator_id=entry.get("operator_id"),
        workspace_id=entry.get("workspace_id"),
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
        "created_by": entry.get("created_by"),
        "operator_id": entry.get("operator_id"),
        "workspace_id": entry.get("workspace_id"),
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


def _empty_result_panel(
    *,
    status: str,
    tool_id: Any = UNAVAILABLE,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "tool_id": _projection_scalar(tool_id),
        "model": UNAVAILABLE,
        "method": UNAVAILABLE,
        "result_count": UNAVAILABLE,
        "population_id": UNAVAILABLE,
        "period": UNAVAILABLE,
        "results": [],
        "narrative_summary": UNAVAILABLE,
        "key_points": [],
        "errors": list(errors or []),
    }


def _artifact_projection_for_run(entry: dict[str, Any], *, artifact_id: str) -> dict[str, Any]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        raise ArtifactProjectionReadError(
            "manifest_missing",
            "Run manifest is unavailable.",
            status_code=404,
        )
    root = Path(str(artifact_root)).expanduser().absolute()
    manifest = read_bounded_json_object(root, "run_manifest.json", namespace="manifest")
    validate_artifact_projection_schema("run_manifest", manifest)
    run_id = str(entry.get("run_id"))
    if manifest.get("run_id") != run_id:
        raise ArtifactProjectionReadError(
            "manifest_run_mismatch",
            "Run manifest does not match the selected run.",
            status_code=409,
        )
    expected_case_id = entry.get("case_id")
    if expected_case_id is not None and manifest.get("case_id") != str(expected_case_id):
        raise ArtifactProjectionReadError(
            "manifest_case_mismatch",
            "Run manifest does not match the selected case.",
            status_code=409,
        )
    expected_tool_id = _registered_tool_id(entry)
    if (
        expected_tool_id is not None
        and "tool_id" in manifest
        and str(manifest.get("tool_id")) != expected_tool_id
    ):
        raise ArtifactProjectionReadError(
            "manifest_tool_mismatch",
            "Run manifest does not match the selected tool.",
            status_code=409,
        )
    artifact_paths = manifest.get("artifact_paths")
    if not isinstance(artifact_paths, dict) or artifact_id not in artifact_paths:
        raise ArtifactProjectionReadError(
            "artifact_not_registered",
            "Artifact is not registered in the run manifest.",
            status_code=404,
        )
    raw_ref = artifact_paths.get(artifact_id)
    if not isinstance(raw_ref, str) or not raw_ref:
        raise ArtifactProjectionReadError(
            "artifact_path_rejected",
            "Registered artifact path failed safety validation.",
        )
    spec = ARTIFACT_PROJECTION_SPECS[artifact_id]
    normalized_ref = raw_ref.replace("\\", "/")
    if normalized_ref.rsplit("/", 1)[-1] != spec.filename:
        raise ArtifactProjectionReadError(
            "artifact_schema_mismatch",
            "Registered artifact does not match the projection schema.",
            status_code=422,
        )
    payload = read_bounded_json_object(root, raw_ref, namespace="artifact")
    validate_artifact_projection_schema(artifact_id, payload)
    if expected_case_id is not None and payload.get("case_id") != str(expected_case_id):
        raise ArtifactProjectionReadError(
            "artifact_case_mismatch",
            "Registered artifact does not match the selected case.",
            status_code=409,
        )
    if "run_id" in payload and payload.get("run_id") != run_id:
        raise ArtifactProjectionReadError(
            "artifact_run_mismatch",
            "Registered artifact does not match the selected run.",
            status_code=409,
        )
    if (
        expected_tool_id is not None
        and "tool_id" in payload
        and str(payload.get("tool_id")) != expected_tool_id
    ):
        raise ArtifactProjectionReadError(
            "artifact_tool_mismatch",
            "Registered artifact does not match the selected tool.",
            status_code=409,
        )
    if (
        artifact_id == "deterministic_result"
        and expected_tool_id is not None
        and "tool_id" not in payload
        and str(payload.get("method")) != expected_tool_id
    ):
        raise ArtifactProjectionReadError(
            "artifact_method_mismatch",
            "Registered artifact does not match the selected method.",
            status_code=409,
        )
    return build_artifact_projection(
        run_id=run_id,
        artifact_id=artifact_id,
        payload=payload,
    ).model_dump()


def _result_panel_projection(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Project registered result artifacts into a path-free Console contract."""

    if entry is None:
        return _empty_result_panel(status="no_run_selected")

    registered_tool_id = _registered_tool_id(entry)
    if registered_tool_id not in {None, MINIMAX_EXPERIENCE_STUDY_TOOL_ID}:
        return _empty_result_panel(status="not_available", tool_id=registered_tool_id)

    root, manifest, manifest_error = _load_result_manifest(entry)
    if manifest_error is not None or root is None or manifest is None:
        return _empty_result_panel(
            status="error",
            tool_id=registered_tool_id or UNAVAILABLE,
            errors=[manifest_error] if manifest_error is not None else [],
        )

    manifest_tool_id = manifest.get("tool_id")
    if (
        registered_tool_id is not None
        and manifest_tool_id is not None
        and str(manifest_tool_id) != registered_tool_id
    ):
        return _empty_result_panel(
            status="error",
            tool_id=registered_tool_id,
            errors=[
                _result_projection_error(
                    "run_manifest",
                    "artifact_tool_mismatch",
                    "Run manifest tool identity does not match the selected run.",
                )
            ],
        )
    tool_id = registered_tool_id or manifest_tool_id
    if tool_id != MINIMAX_EXPERIENCE_STUDY_TOOL_ID:
        return _empty_result_panel(status="not_available", tool_id=tool_id or UNAVAILABLE)

    payloads: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for artifact_id in ("validated_input", "deterministic_result", "narrative_draft"):
        payload, error = _read_registered_result_artifact(
            artifact_root=root,
            manifest=manifest,
            artifact_id=artifact_id,
        )
        if error is not None:
            errors.append(error)
        elif payload is not None:
            identity_errors = _result_artifact_identity_errors(
                payload,
                artifact_id=artifact_id,
                expected_run_id=entry.get("run_id"),
                expected_tool_id=tool_id,
            )
            if identity_errors:
                errors.extend(identity_errors)
            else:
                payloads[artifact_id] = payload

    deterministic = payloads.get("deterministic_result", {})
    raw_results = deterministic.get("results")
    projected_results: list[dict[str, Any]] = []
    if "deterministic_result" in payloads and not isinstance(raw_results, list):
        errors.append(
            _result_projection_error(
                "deterministic_result",
                "artifact_invalid_shape",
                "Registered deterministic result does not contain a results array.",
            )
        )
    elif isinstance(raw_results, list) and len(raw_results) > MAX_PROJECTED_RESULTS:
        errors.append(
            _result_projection_error(
                "deterministic_result",
                "result_limit_exceeded",
                "Registered result count exceeds the Console projection limit.",
            )
        )
    elif isinstance(raw_results, list):
        if any(not isinstance(item, dict) for item in raw_results):
            errors.append(
                _result_projection_error(
                    "deterministic_result",
                    "artifact_invalid_shape",
                    "Registered results must contain JSON objects.",
                )
            )
        else:
            projected_results = [_project_experience_result(item) for item in raw_results]

    result_count = deterministic.get("result_count", UNAVAILABLE)
    if result_count != UNAVAILABLE and (
        isinstance(result_count, bool)
        or not isinstance(result_count, int)
        or result_count < 0
        or result_count > MAX_PROJECTED_RESULTS
    ):
        result_count = UNAVAILABLE
        errors.append(
            _result_projection_error(
                "deterministic_result",
                "invalid_result_count",
                "Registered result_count is invalid for Console projection.",
            )
        )
    elif isinstance(raw_results, list) and isinstance(result_count, int) and result_count != len(raw_results):
        errors.append(
            _result_projection_error(
                "deterministic_result",
                "result_count_mismatch",
                "Registered result_count does not match the results array.",
            )
        )

    validated_inputs = payloads.get("validated_input", {}).get("inputs")
    if not isinstance(validated_inputs, dict):
        validated_inputs = {}
    narrative = payloads.get("narrative_draft", {})
    raw_key_points = narrative.get("key_points")
    key_points = (
        [_projection_scalar(item) for item in raw_key_points[:MAX_PROJECTED_RESULTS]]
        if isinstance(raw_key_points, list)
        else []
    )
    status = "available" if not errors else ("partial" if projected_results else "error")
    return {
        "status": status,
        "tool_id": _projection_scalar(deterministic.get("tool_id", tool_id)),
        "model": _projection_scalar(deterministic.get("model", manifest.get("model", UNAVAILABLE))),
        "method": _projection_scalar(deterministic.get("method", UNAVAILABLE)),
        "result_count": result_count,
        "population_id": _projection_scalar(validated_inputs.get("population_id", UNAVAILABLE)),
        "period": _projection_scalar(validated_inputs.get("period", UNAVAILABLE)),
        "results": projected_results,
        "narrative_summary": _projection_scalar(narrative.get("summary", UNAVAILABLE)),
        "key_points": key_points,
        "errors": errors,
    }


def _registered_tool_id(entry: dict[str, Any]) -> str | None:
    operator_params = entry.get("operator_params")
    if not isinstance(operator_params, dict):
        operator_params = {}
    value = entry.get("tool_id") or operator_params.get("tool_id") or operator_params.get("method")
    return str(value) if value else None


def _load_result_manifest(
    entry: dict[str, Any],
) -> tuple[Path | None, dict[str, Any] | None, dict[str, str] | None]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        return None, None, _result_projection_error(
            "run_manifest", "artifact_root_missing", "Run artifact root is unavailable."
        )
    try:
        root = Path(str(artifact_root)).expanduser().absolute()
        manifest = read_bounded_json_object(
            root,
            "run_manifest.json",
            namespace="artifact",
            max_bytes=MAX_RESULT_ARTIFACT_BYTES,
        )
    except ArtifactProjectionReadError as exc:
        code = "artifact_unreadable" if exc.code in {"artifact_invalid_encoding", "artifact_invalid_json"} else exc.code
        return None, None, _result_projection_error(
            "run_manifest", code, "Run manifest could not be read safely."
        )
    if manifest.get("run_id") not in {None, entry.get("run_id")}:
        return root, None, _result_projection_error(
            "run_manifest", "manifest_run_mismatch", "Run manifest does not match the selected run."
        )
    return root, manifest, None


def _read_registered_result_artifact(
    *,
    artifact_root: Path,
    manifest: dict[str, Any],
    artifact_id: str,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    artifact_paths = manifest.get("artifact_paths")
    if not isinstance(artifact_paths, dict) or artifact_id not in artifact_paths:
        return None, _result_projection_error(
            artifact_id,
            "artifact_not_registered",
            "Required result artifact is not registered in the run manifest.",
        )
    raw_ref = artifact_paths.get(artifact_id)
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        return None, _result_projection_error(
            artifact_id, "artifact_path_rejected", "Registered artifact path failed safety validation."
        )
    try:
        return read_bounded_json_object(
            artifact_root,
            raw_ref,
            namespace="artifact",
            max_bytes=MAX_RESULT_ARTIFACT_BYTES,
        ), None
    except ArtifactProjectionReadError as exc:
        code = "artifact_unreadable" if exc.code in {"artifact_invalid_encoding", "artifact_invalid_json"} else exc.code
        return None, _result_projection_error(
            artifact_id, code, "Registered result artifact could not be read safely."
        )


def _result_artifact_identity_errors(
    payload: dict[str, Any],
    *,
    artifact_id: str,
    expected_run_id: Any,
    expected_tool_id: Any,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if "run_id" in payload and payload.get("run_id") != expected_run_id:
        errors.append(
            _result_projection_error(
                artifact_id,
                "artifact_run_mismatch",
                "Registered result artifact does not match the selected run.",
            )
        )
    if (
        artifact_id in {"validated_input", "deterministic_result"}
        and "tool_id" in payload
        and payload.get("tool_id") != expected_tool_id
    ):
        errors.append(
            _result_projection_error(
                artifact_id,
                "artifact_tool_mismatch",
                "Registered result artifact does not match the selected tool.",
            )
        )
    return errors


def _project_experience_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_values": _project_group_values(result.get("group_values", UNAVAILABLE)),
        "metric_kind": _projection_scalar(result.get("metric_kind", UNAVAILABLE)),
        "mortality_improvement": _projection_scalar(
            result.get("mortality_improvement", UNAVAILABLE)
        ),
        "actual": _projection_scalar(result.get("actual_total", UNAVAILABLE)),
        "expected": _projection_scalar(result.get("expected_total", UNAVAILABLE)),
        "ratio": _projection_scalar(result.get("ratio", UNAVAILABLE)),
        "reason_code": _projection_scalar(result.get("reason_code", UNAVAILABLE)),
        "uncertainty": {
            "lower": _projection_scalar(result.get("lower_ci", UNAVAILABLE)),
            "upper": _projection_scalar(result.get("upper_ci", UNAVAILABLE)),
            "confidence_level": _projection_scalar(result.get("confidence_level", UNAVAILABLE)),
            "basis": _projection_scalar(result.get("uncertainty_basis", UNAVAILABLE)),
        },
    }


def _project_group_values(value: Any) -> list[list[Any]] | str:
    if not isinstance(value, (list, tuple)):
        return UNAVAILABLE
    projected: list[list[Any]] = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return UNAVAILABLE
        projected.append([_projection_scalar(pair[0]), _projection_scalar(pair[1])])
    return projected


def _projection_scalar(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return UNAVAILABLE
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return UNAVAILABLE


def _result_projection_error(artifact_id: str, code: str, message: str) -> dict[str, str]:
    return {"artifact_id": artifact_id, "code": code, "message": message}


def _console_artifact_panel(entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {
            "present": False,
            "status": "no_run_selected",
            "error": None,
            "artifact_root": None,
            "artifact_manifest": None,
            "artifact_paths": {},
            "artifacts": [],
            "primary_artifact_refs": [],
            "review_artifact_refs": [],
            "decision_artifact_refs": [],
            "evidence_items": [],
            "missing_expected_artifacts": [],
            "freshness": None,
        }
    manifest_error = None
    try:
        manifest = _load_manifest_for_entry(entry)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        manifest = None
        manifest_error = {
            "code": "manifest_unreadable",
            "message": "Run manifest could not be read safely.",
        }
    artifact_root = entry.get("artifact_root")
    root = Path(artifact_root).expanduser().resolve() if artifact_root else None
    primary_refs = _console_expected_artifact_refs(root, manifest, category="primary")
    review_refs = _console_expected_artifact_refs(root, manifest, category="review")
    decision_refs = _console_expected_artifact_refs(root, manifest, category="decision")
    evidence_items = [*primary_refs, *review_refs, *decision_refs]
    return {
        "present": manifest is not None,
        "status": (
            "ok"
            if manifest is not None
            else "manifest_unreadable"
            if manifest_error is not None
            else "manifest_missing"
        ),
        "error": manifest_error,
        "artifact_root": artifact_root,
        "artifact_manifest": manifest,
        "artifact_paths": manifest.get("artifact_paths", {}) if manifest else {},
        "artifacts": _artifact_refs_from_manifest(manifest),
        "primary_artifact_refs": primary_refs,
        "review_artifact_refs": review_refs,
        "decision_artifact_refs": decision_refs,
        "evidence_items": evidence_items,
        "missing_expected_artifacts": [item["artifact_id"] for item in evidence_items if not item["present"]],
        "freshness": _artifact_panel_freshness(evidence_items),
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
            },
            {
                "action_id": "report_export",
                "label": "Export handoff report",
                "method": "POST",
                "path": f"/runs/{run_id}/report-export",
                "enabled": bool(run_id),
            },
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
    return build_review_snapshot(
        review_store=review_store,
        run_entry=entry,
        review_packet_result=review_packet,
        review_store_root=review_store_root,
        decision_artifacts=_decision_artifacts_for_run(entry),
    )


def _list_review_payloads(
    *,
    registry_path: str | Path,
    review_store,
    review_store_root: str | Path,
    operator_id: str | None,
    workspace_id: str | None,
) -> list[dict[str, Any]]:
    runs = _filter_run_entries(
        run_registry.list_runs(registry_path),
        operator_id=operator_id,
        workspace_id=workspace_id,
    )
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
                "workspace_id": review_payload.get("workspace_id"),
                "status": review_payload.get("status"),
                "decision": (review_payload.get("decision") or {}).get("decision"),
                "decision_artifacts": (review_payload.get("decision") or {}).get("artifacts", []),
                "review_required": review_payload.get("review_required", False),
                "reason_codes": list(review_payload.get("reason_codes", []) or []),
                "assigned_to": review_payload.get("assigned_to"),
                "created_at": review_payload.get("created_at"),
                "updated_at": review_payload.get("updated_at"),
                "selected": review_payload.get("run_id") == selected_run_id,
            }
        )
    return sorted(reviews, key=lambda item: item.get("updated_at") or "", reverse=True)


def _materialize_review_record_from_id(
    review_id: str,
    *,
    registry_path: str | Path,
    review_store,
) -> dict[str, Any] | None:
    run_id = _run_id_from_review_id(review_id)
    if run_id is None:
        return None
    try:
        run_entry = _get_registry_entry(registry_path, run_id)
    except HTTPException:
        return None
    review_packet = _load_review_packet_for_entry(run_entry)
    return ensure_review_record(
        review_store=review_store,
        run_entry=run_entry,
        review_packet=review_packet.get("packet") if review_packet.get("present") else None,
    )


def _run_id_from_review_id(review_id: str) -> str | None:
    prefix = "review-"
    if not review_id.startswith(prefix):
        return None
    run_id = review_id[len(prefix):].strip()
    return run_id or None


def _decision_artifacts_for_run(entry: dict[str, Any]) -> list[dict[str, Any]]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        return []
    root = Path(artifact_root).expanduser().resolve()
    return [
        ArtifactRef(
            artifact_id="review_decision",
            path=str(root / "review_decision.json"),
            label="review decision",
            present=(root / "review_decision.json").exists(),
        ).model_dump(),
        ArtifactRef(
            artifact_id="review_decision_markdown",
            path=str(root / "review_decision.md"),
            label="review decision markdown",
            present=(root / "review_decision.md").exists(),
        ).model_dump(),
    ]


def _identity_filter_options(runs: list[dict[str, Any]], *, field_name: str) -> list[str]:
    values = {
        value
        for entry in runs
        if (value := _entry_identity_value(entry, field_name)) is not None
    }
    return sorted(values)


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
        artifact = ArtifactRef(
                artifact_id=str(artifact_id),
                label=str(artifact_id).replace("_", " "),
                path=str(artifact_path),
                present=artifact_path.exists(),
            ).model_dump()
        provenance = provenance_for_artifact(str(artifact_id))
        if provenance is not None:
            artifact["provenance"] = provenance
            artifact["category"] = {
                "system_manifest": "system",
                "deterministic": "result",
                "model_generated": "narrative",
                "review": "review",
            }[provenance]
        artifacts.append(artifact)
    return artifacts


_CONSOLE_ARTIFACT_SPECS: tuple[dict[str, str], ...] = (
    {"artifact_id": "run_manifest", "label": "Run manifest", "filename": "run_manifest.json", "category": "primary"},
    {"artifact_id": "validated_input", "label": "Validated input", "filename": "validated_input.json", "category": "primary"},
    {"artifact_id": "deterministic_result", "label": "Deterministic result", "filename": "deterministic_result.json", "category": "primary"},
    {"artifact_id": "narrative_draft", "label": "Narrative draft", "filename": "narrative_draft.json", "category": "primary"},
    {"artifact_id": "constitution_check", "label": "Constitution check", "filename": "constitution_check.json", "category": "primary"},
    {"artifact_id": "review_packet", "label": "Review packet", "filename": "review_packet.json", "category": "review"},
    {"artifact_id": "review_packet_markdown", "label": "Review packet markdown", "filename": "review_packet.md", "category": "review"},
    {"artifact_id": "review_decision", "label": "Review decision", "filename": "review_decision.json", "category": "decision"},
    {"artifact_id": "review_decision_markdown", "label": "Review decision markdown", "filename": "review_decision.md", "category": "decision"},
    {"artifact_id": "operator_handoff", "label": "Operator handoff", "filename": "operator_handoff.md", "category": "decision"},
    {"artifact_id": "reserve_summary_json", "label": "Reserve summary", "filename": "reserve_summary.json", "category": "decision"},
    {"artifact_id": "reserve_summary_markdown", "label": "Reserve summary markdown", "filename": "reserve_summary.md", "category": "decision"},
)


def _console_expected_artifact_refs(
    artifact_root: Path | None,
    manifest: dict[str, Any] | None,
    *,
    category: str,
) -> list[dict[str, Any]]:
    manifest_paths = manifest.get("artifact_paths", {}) if manifest else {}
    refs: list[dict[str, Any]] = []
    for spec in _CONSOLE_ARTIFACT_SPECS:
        if spec["category"] != category:
            continue
        if (
            manifest is not None
            and category != "primary"
            and spec["artifact_id"] not in manifest_paths
        ):
            continue
        artifact_path = _console_artifact_path(
            artifact_root,
            manifest_paths.get(spec["artifact_id"]),
            fallback_filename=spec["filename"],
        )
        refs.append(
            {
                "artifact_id": spec["artifact_id"],
                "label": spec["label"],
                "category": category,
                "present": artifact_path.exists() if artifact_path is not None else False,
                "ref": _safe_artifact_ref(artifact_path, artifact_root),
                "mtime": _artifact_mtime(artifact_path),
            }
        )
    return refs


def _console_artifact_path(
    artifact_root: Path | None,
    manifest_path: Any,
    *,
    fallback_filename: str,
) -> Path | None:
    if manifest_path is not None:
        candidate = Path(str(manifest_path)).expanduser()
        if not candidate.is_absolute() and artifact_root is not None:
            candidate = artifact_root / candidate
        resolved = candidate.resolve()
        if artifact_root is not None and not resolved.is_relative_to(artifact_root):
            return None
        return resolved
    if artifact_root is None:
        return None
    return (artifact_root / fallback_filename).resolve()


def _safe_artifact_ref(path: Path | None, artifact_root: Path | None) -> str | None:
    if path is None:
        return None
    if artifact_root is not None:
        try:
            return str(path.relative_to(artifact_root))
        except ValueError:
            pass
    return path.name


def _artifact_mtime(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _artifact_panel_freshness(evidence_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    present_items = [item for item in evidence_items if item.get("present")]
    if not present_items:
        return None
    mtimes = [item["mtime"] for item in present_items if item.get("mtime")]
    latest_mtime = max(mtimes) if mtimes else None
    manifest_mtime = next((item.get("mtime") for item in present_items if item.get("artifact_id") == "run_manifest"), None)
    return {
        "present_artifact_count": len(present_items),
        "latest_mtime": latest_mtime,
        "run_manifest_mtime": manifest_mtime,
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
