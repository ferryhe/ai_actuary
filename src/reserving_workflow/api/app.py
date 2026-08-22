"""FastAPI control plane for operator-facing AI Actuary runs.

This module intentionally wraps the existing operator/artifact/registry
boundaries instead of introducing a second runtime implementation.
"""

from __future__ import annotations

import math
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from reserving_workflow import operator_entrypoint
from reserving_workflow.api.capabilities import (
    CapabilityAuthority,
    Principal,
    assert_route_matrix_complete,
    object_in_scope,
    route_policy_for_scope,
)
from reserving_workflow.adapters.control_plane.projections import (
    ARTIFACT_PROJECTION_SPECS,
    ArtifactProjectionReadError,
    TrustedArtifactRoot,
    build_artifact_projection,
    project_artifact_payload,
    project_review,
    provenance_for_artifact,
    read_bounded_json_object,
    stat_regular_artifact,
    validate_artifact_projection_schema,
)
from reserving_workflow.artifacts import replay as replay_helpers
from reserving_workflow.artifacts.storage import resolve_artifact_path, write_json_artifact
from reserving_workflow.calculators import ChainladderAdapter
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
    is_terminal_run_status,
    run_event_type_for_status,
)
from reserving_workflow.evaluation import (
    run_offline_evaluation_lane,
    run_real_model_evaluation_lane,
)
from reserving_workflow.evaluation.case_packs import load_case_pack
from reserving_workflow.interfaces.operator_console import (
    DEFAULT_ADK_DEVELOPER_WEB_URL,
    render_operator_console_html,
)
from reserving_workflow.model_tools import (
    MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
    ExperienceStudyToolInput,
    run_minimax_experience_study,
)
from reserving_workflow.review import (
    ReviewIdentityMismatchError,
    bind_review_record_identity,
    build_review_contract,
    build_review_snapshot,
    ensure_review_record,
    validate_review_packet_identity,
    write_run_review_decision_artifacts,
)
from reserving_workflow.reports import export_run_report
from reserving_workflow.schemas import ReservingCaseInput
from reserving_workflow.storage.local import (
    LocalReviewStore,
    ReviewDecisionConflictError,
    ReviewRecordReadError,
)
from reserving_workflow.runtime import browser_smoke_runner, build_preflight_report, run_registry
from reserving_workflow.runtime.adk_execution import (
    ADK_SOURCE,
    ADK_WORKSPACE_ID,
    AdkBenchmarkRequest,
    AdkDebugContext,
    AdkEmptyDebugRequest,
    AdkRepeatabilityRequest,
    AdkStartRequest,
    MAX_ADK_BENCHMARK_CASE_LIMIT,
    adk_debug_request_fingerprint,
    build_adk_provenance,
    canonical_json,
    prepare_isolated_run_root,
    request_fingerprint,
    validate_adk_inputs,
    validate_adk_provenance,
    workflow_digest,
)
from reserving_workflow.storage.safe_json import (
    PinnedJsonRoot,
    SafeJsonReadError,
    write_json_object_exclusive,
)
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
ADK_STEP_JSON_ARTIFACTS = (
    "validated_input.json",
    "case_input.json",
    "deterministic_result.json",
    "narrative_draft.json",
    "constitution_check.json",
    "review_packet.json",
    "run_manifest.json",
)


class ApiSettings(BaseModel):
    """Runtime settings for the local FastAPI control plane."""

    registry_path: str | Path = Field(default="./tmp/run-registry.json")
    artifact_root: str | Path = Field(default="./tmp/api-artifacts")
    review_delivery_dir: str | Path | None = None
    review_store_dir: str | Path = Field(default="./tmp/reviews")
    adk_artifact_root: Path | None = None
    evaluation_state_root: Path | None = None
    operator_credential: str | None = None
    adk_credential: str | None = None
    operator_bootstrap_token: str | None = None
    operator_origin: str = "http://127.0.0.1:8000"
    adk_url: str = DEFAULT_ADK_DEVELOPER_WEB_URL
    capability_enforcement: bool | None = None
    operator_session_ttl_seconds: float = Field(default=900.0, gt=0, le=3600.0)
    operator_bootstrap_ttl_seconds: float = Field(default=60.0, gt=0, le=300.0)
    adk_benchmark_input_byte_limit: int = Field(default=65_536, gt=0)
    adk_benchmark_total_byte_limit: int = Field(default=1_000_000, gt=0)
    adk_benchmark_output_byte_limit: int = Field(default=100_000, gt=0)
    adk_benchmark_wall_time_seconds: float = Field(default=30.0, gt=0)
    adk_benchmark_temp_storage_bytes: int = Field(default=1_000_000, gt=0)
    adk_benchmark_retention_days: int = Field(default=7, gt=0)
    adk_benchmark_concurrency: int = Field(default=1, gt=0, le=1)


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


class OperatorBootstrapRequest(BaseModel):
    bootstrap_token: str = Field(min_length=8, max_length=256)


class OperatorHandoffCreateRequest(BaseModel):
    claim_token: str = Field(min_length=32, max_length=256)


class OperatorHandoffApproveRequest(BaseModel):
    handoff_id: str = Field(min_length=16, max_length=128)
    bootstrap_token: str = Field(min_length=8, max_length=256)


class OperatorHandoffClaimRequest(BaseModel):
    handoff_id: str = Field(min_length=16, max_length=128)
    claim_token: str = Field(min_length=32, max_length=256)


class BrowserSmokeCredentialRotationRequest(BaseModel):
    new_credential: str = Field(min_length=8, max_length=256)


class AdkOperationWaitRequest(BaseModel):
    model_config = {"extra": "forbid"}

    timeout_seconds: float = Field(default=1.0, ge=0, le=30)


def _create_app(
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
    resolved_runner_module = runner_module
    if (
        resolved_runner_module is None
        and os.environ.get("AI_ACTUARY_BROWSER_SMOKE_RUNNER") == "1"
    ):
        resolved_runner_module = browser_smoke_runner
    operator_credential = resolved_settings.operator_credential or os.environ.get(
        "AI_ACTUARY_OPERATOR_CREDENTIAL"
    )
    adk_credential = resolved_settings.adk_credential or os.environ.get(
        "AI_ACTUARY_ADK_CREDENTIAL"
    )
    bootstrap_token = resolved_settings.operator_bootstrap_token or os.environ.get(
        "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN"
    )
    operator_origin = (
        resolved_settings.operator_origin
        if settings is not None
        else os.environ.get("AI_ACTUARY_OPERATOR_ORIGIN", resolved_settings.operator_origin)
    )
    adk_developer_url = _loopback_http_origin(
        (
            resolved_settings.adk_url
            if settings is not None
            else os.environ.get("AI_ACTUARY_ADK_URL", resolved_settings.adk_url)
        ),
        purpose="ADK Developer Web URL",
    )
    supplied_secret_count = sum(
        value is not None for value in (operator_credential, adk_credential, bootstrap_token)
    )
    if resolved_settings.capability_enforcement is False:
        raise ValueError("Runtime capability enforcement cannot be disabled")
    if supplied_secret_count != 3:
        raise ValueError("All runtime capability credentials must be configured together")
    enforcement_enabled = True
    authority = CapabilityAuthority(
        operator_credential=str(operator_credential),
        adk_credential=str(adk_credential),
        operator_bootstrap_token=str(bootstrap_token),
        session_ttl_seconds=resolved_settings.operator_session_ttl_seconds,
        bootstrap_ttl_seconds=resolved_settings.operator_bootstrap_ttl_seconds,
    )
    app = FastAPI(
        title="AI Actuary Control Plane",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.capability_authority = authority
    app.state.capability_enforcement = enforcement_enabled
    app.state.adk_artifact_root = (
        resolved_settings.adk_artifact_root
        or Path(resolved_settings.artifact_root).expanduser().absolute() / ADK_WORKSPACE_ID
    )
    app.state.evaluation_state_root = (
        resolved_settings.evaluation_state_root
        or Path(resolved_settings.artifact_root).expanduser().absolute().parent / "adk-evaluations"
    )
    if enforcement_enabled:
        run_registry.mark_incomplete_adk_runs_stale(resolved_settings.registry_path)

    @app.middleware("http")
    async def _capability_middleware(request: Request, call_next):
        request.state.principal = None
        policy = route_policy_for_scope(
            app,
            method=request.method,
            path=request.url.path,
        )
        if policy is None:
            return await call_next(request)
        if not enforcement_enabled:
            return await call_next(request)

        expected_origin = operator_origin.rstrip("/")
        expected_host = urlsplit(expected_origin).netloc
        is_mutation = request.method.upper() not in {"GET", "HEAD"}
        if (is_mutation or request.url.path == "/console") and request.headers.get("host", "") != expected_host:
            return _safe_auth_error(403, "request_context_forbidden", "Request context is not allowed.")
        if policy.anonymous:
            if is_mutation and request.headers.get("origin") != expected_origin:
                return _safe_auth_error(403, "request_context_forbidden", "Request context is not allowed.")
            return await call_next(request)

        assert authority is not None
        principal = authority.authenticate_bearer(request.headers.get("authorization"))
        if principal is None:
            principal = authority.authenticate_session(
                request.cookies.get(authority.session_cookie_name)
            )
        if principal is None:
            return _safe_auth_error(401, "authentication_required", "Authentication is required.")
        if principal.capability_class not in policy.capabilities:
            return _safe_auth_error(403, "capability_forbidden", "Capability is not allowed.")
        if (
            is_mutation
            and principal.capability_class == "operator-console"
            and request.headers.get("origin") != expected_origin
        ):
            return _safe_auth_error(403, "request_context_forbidden", "Request context is not allowed.")
        if is_mutation and principal.transport == "session":
            if not authority.session_csrf_matches(
                principal.session_id, request.headers.get("x-csrf-token")
            ):
                return _safe_auth_error(403, "csrf_forbidden", "CSRF validation failed.")
        request.state.principal = principal
        return await call_next(request)

    @app.exception_handler(ReviewIdentityMismatchError)
    async def _review_identity_mismatch_handler(
        _request: Request,
        exc: ReviewIdentityMismatchError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(ArtifactProjectionReadError)
    async def _artifact_read_error_handler(
        _request: Request,
        exc: ArtifactProjectionReadError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(ReviewRecordReadError)
    async def _review_record_read_error_handler(
        _request: Request,
        exc: ReviewRecordReadError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    def _get_review_store() -> LocalReviewStore:
        try:
            return LocalReviewStore(resolved_settings.review_store_dir)
        except OSError as exc:  # pragma: no cover - exercised through API surface
            raise HTTPException(status_code=503, detail="Review store unavailable.") from exc

    def _get_scoped_entry(request: Request, run_id: str) -> dict[str, Any]:
        try:
            entry, authoritative_adk = run_registry.get_run_scope_record(
                resolved_settings.registry_path, run_id
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "object_not_found", "message": "Object was not found."},
            ) from exc
        principal = getattr(request.state, "principal", None)
        scope_entry = entry
        if authoritative_adk:
            scope_entry = {
                **entry,
                "workspace_id": "adk-development",
                "source": "adk-developer",
            }
        if isinstance(principal, Principal) and not object_in_scope(
            principal, scope_entry
        ):
            raise HTTPException(
                status_code=404,
                detail={"code": "object_not_found", "message": "Object was not found."},
            )
        authoritative_adk_runs = _audit_adk_registry()
        try:
            _validate_adk_entry_provenance(
                entry, authoritative_adk=run_id in authoritative_adk_runs
            )
        except HTTPException as exc:
            if (
                isinstance(principal, Principal)
                and principal.capability_class == "operator-console"
                and run_id in authoritative_adk_runs
                and exc.status_code == 409
            ):
                raise HTTPException(
                    status_code=404,
                    detail={"code": "object_not_found", "message": "Object was not found."},
                ) from exc
            raise
        return entry

    def _audit_adk_registry() -> set[str]:
        try:
            return run_registry.audit_adk_registry(resolved_settings.registry_path)
        except run_registry.RegistryIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "ADK registry integrity is invalid."},
            ) from exc

    def _validate_adk_entry_provenance(
        entry: dict[str, Any], *, authoritative_adk: bool
    ) -> None:
        if authoritative_adk:
            try:
                if entry.get("source") != ADK_SOURCE:
                    raise ValueError("adk_source_invalid")
                provenance = validate_adk_provenance(entry.get("provenance"))
                if provenance.get("source") != entry.get("source"):
                    raise ValueError("adk_source_invalid")
                manifest = read_bounded_json_object(
                    entry.get("artifact_root"),
                    "run_manifest.json",
                    namespace="manifest",
                )
                persisted_manifest_provenance = {
                    key: manifest.get(key)
                    for key in provenance
                }
                validate_adk_provenance(persisted_manifest_provenance)
                if persisted_manifest_provenance != provenance:
                    raise ValueError("adk_provenance_mismatch")
            except (ArtifactProjectionReadError, ValueError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "adk_provenance_invalid", "message": "Run provenance is invalid."},
                ) from exc

    def _trusted_list_scope(request: Request) -> tuple[str | None, str | None, str | None]:
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            return None, None, None
        return principal.operator_id, principal.workspace_id, principal.source

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
    async def operator_console() -> HTMLResponse:
        console_html = render_operator_console_html(adk_url=adk_developer_url)
        if authority is not None:
            console_html = _inject_console_csrf_transport(console_html)
        return HTMLResponse(console_html)

    @app.post("/auth/operator/bootstrap")
    async def operator_bootstrap(request: OperatorBootstrapRequest) -> JSONResponse:
        if authority is None:
            raise HTTPException(status_code=404, detail="Bootstrap is not configured.")
        try:
            session_id, csrf_token, max_age = authority.exchange_bootstrap(
                request.bootstrap_token
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "bootstrap_invalid", "message": "Bootstrap was not accepted."},
            ) from exc
        response = JSONResponse(
            content={"ok": True, "csrf_token": csrf_token, "expires_in": max_age}
        )
        _set_operator_session_cookies(
            response,
            authority=authority,
            session_id=session_id,
            csrf_token=csrf_token,
            max_age=max_age,
        )
        return response

    @app.post("/auth/operator/handoff/request")
    async def operator_handoff_request(
        request: OperatorHandoffCreateRequest,
    ) -> dict[str, Any]:
        assert authority is not None
        try:
            handoff_id, expires_in = authority.create_bootstrap_handoff(
                request.claim_token
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "bootstrap_invalid", "message": "Bootstrap was not accepted."},
            ) from exc
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "expires_in": expires_in,
        }

    @app.post("/auth/operator/handoff/approve")
    async def operator_handoff_approve(
        request: OperatorHandoffApproveRequest,
    ) -> dict[str, bool]:
        assert authority is not None
        try:
            authority.approve_bootstrap_handoff(
                request.bootstrap_token,
                request.handoff_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "bootstrap_invalid", "message": "Bootstrap was not accepted."},
            ) from exc
        return {"ok": True}

    @app.post("/auth/operator/handoff/claim")
    async def operator_handoff_claim(
        request: OperatorHandoffClaimRequest,
    ) -> JSONResponse:
        assert authority is not None
        try:
            session_id, csrf_token, max_age = authority.claim_bootstrap_handoff(
                request.handoff_id,
                request.claim_token,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "bootstrap_invalid", "message": "Bootstrap was not accepted."},
            ) from exc
        response = JSONResponse(
            content={"ok": True, "csrf_token": csrf_token, "expires_in": max_age}
        )
        _set_operator_session_cookies(
            response,
            authority=authority,
            session_id=session_id,
            csrf_token=csrf_token,
            max_age=max_age,
        )
        return response

    @app.post("/adk/browser-smoke/rotate-credential")
    async def browser_smoke_rotate_adk_credential(
        request: BrowserSmokeCredentialRotationRequest,
    ) -> dict[str, Any]:
        if os.environ.get("AI_ACTUARY_BROWSER_SMOKE_RUNNER") != "1":
            raise HTTPException(status_code=404, detail="Not found.")
        authority.rotate("adk-developer", request.new_credential)
        return {"ok": True, "rotated": "adk-developer"}

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if enforcement_enabled:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "request_invalid",
                        "message": "Request failed validation.",
                    }
                },
            )
        return await request_validation_exception_handler(request, exc)
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
        principal = getattr(request.state, "principal", None)
        if isinstance(principal, Principal):
            all_runs = [entry for entry in all_runs if object_in_scope(principal, entry)]
        runs = _filter_run_entries(
            all_runs,
            operator_id=current_identity["operator_id"],
            workspace_id=current_identity["workspace_id"],
        )
        if run_id is not None:
            selected_for_run_id = [
                entry for entry in all_runs if str(entry.get("run_id")) == str(run_id)
            ]
            for entry in selected_for_run_id:
                if entry not in runs:
                    runs.insert(0, entry)
        selected_entry = _select_console_run(runs, run_id)
        review_store = _get_review_store()
        with review_store.pinned_reads():
            return _console_state_payload(
                selected_entry,
                runs,
                all_runs=all_runs,
                tool_registry=resolved_tool_registry,
                review_store=review_store,
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
                    runner_module=resolved_runner_module,
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
            runner_module=resolved_runner_module,
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
        authoritative_adk_runs = _audit_adk_registry()
        trusted_operator, trusted_workspace, trusted_source = _trusted_list_scope(request)
        requested_operator = _normalize_identity_filter(
            operator_id, request=request, header_name="x-operator-id"
        )
        requested_workspace = _normalize_identity_filter(
            workspace_id, request=request, header_name="x-workspace-id"
        )
        filters_are_in_scope = (
            trusted_operator is None
            or requested_operator is None
            or requested_operator == trusted_operator
        ) and (
            trusted_workspace is None
            or requested_workspace is None
            or requested_workspace == trusted_workspace
        )
        runs = (
            _filter_run_entries(
                run_registry.list_runs(resolved_settings.registry_path),
                operator_id=trusted_operator or requested_operator,
                workspace_id=trusted_workspace or requested_workspace,
            )
            if filters_are_in_scope
            else []
        )
        if trusted_source is not None:
            runs = [entry for entry in runs if object_in_scope(request.state.principal, entry)]
        if trusted_source == ADK_SOURCE:
            for entry in runs:
                _validate_adk_entry_provenance(
                    entry,
                    authoritative_adk=str(entry.get("run_id")) in authoritative_adk_runs,
                )
        summaries = [_run_summary(entry) for entry in runs]
        if trusted_source == ADK_SOURCE:
            summaries = [_path_free_run_payload(item) for item in summaries]
        return {
            "run_count": len(runs),
            "runs": summaries,
        }

    @app.get("/runs/{run_id}")
    async def get_run_detail(run_id: str, request: Request) -> dict[str, Any]:
        entry = _get_scoped_entry(request, run_id)
        if getattr(request.state, "principal", None) is not None and request.state.principal.source == ADK_SOURCE:
            return {"run": _path_free_run_payload(_run_summary(entry))}
        artifact_root = entry.get("artifact_root")
        if artifact_root:
            with TrustedArtifactRoot(artifact_root, namespace="manifest") as trusted_root:
                return _run_detail_payload(entry, trusted_root=trusted_root)
        return _run_detail_payload(entry)

    @app.get("/runs/{run_id}/events")
    async def get_run_events(run_id: str, request: Request) -> dict[str, Any]:
        entry = _get_scoped_entry(request, run_id)
        events = [_event_from_history(run_id, item) for item in entry.get("status_history", [])]
        return {"run_id": run_id, "event_count": len(events), "events": events}

    @app.post("/runs/{run_id}/rerun")
    def rerun(run_id: str, request: RerunRequest, http_request: Request) -> dict[str, Any]:
        entry = _get_scoped_entry(http_request, run_id)
        operator_params = dict(entry.get("operator_params", {}) or {})
        if operator_params.get("workflow_id"):
            operator_params["artifact_dir"] = str(request.artifact_dir or entry.get("artifact_root") or _default_artifact_dir(resolved_settings, str(entry.get("case_id") or "case")))
            operator_params["review_delivery_dir"] = request.review_delivery_dir or resolved_settings.review_delivery_dir
            operator_params["registry_path"] = resolved_settings.registry_path
            operator_params["run_id"] = _generate_api_run_id(str(entry.get("case_id") or "case"))
            if resolved_runner_module is not None:
                operator_params["runner_module"] = resolved_runner_module
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
                    runner_module=resolved_runner_module,
                    task_contracts_module=task_contracts_module,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/artifacts")
    async def get_artifacts(run_id: str, request: Request) -> dict[str, Any]:
        entry = _get_scoped_entry(request, run_id)
        artifact_root = entry.get("artifact_root")
        if artifact_root:
            with TrustedArtifactRoot(artifact_root, namespace="manifest") as trusted_root:
                payload = _run_artifact_metadata_payload(entry, trusted_root=trusted_root)
                principal = getattr(request.state, "principal", None)
                return _path_free_artifact_payload(payload) if isinstance(principal, Principal) and principal.source == ADK_SOURCE else payload
        payload = _run_artifact_metadata_payload(entry)
        return _path_free_artifact_payload(payload) if getattr(request.state, "principal", None) is not None and request.state.principal.source == ADK_SOURCE else payload

    @app.get("/runs/{run_id}/artifacts/{artifact_id}/projection")
    async def get_artifact_projection(run_id: str, artifact_id: str, request: Request) -> dict[str, Any]:
        if artifact_id not in ARTIFACT_PROJECTION_SPECS:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "artifact_unsupported",
                    "message": "Artifact projection is not supported.",
                },
            )
        try:
            entry = _get_scoped_entry(request, run_id)
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
    async def get_results(run_id: str, request: Request) -> dict[str, Any]:
        entry = _get_scoped_entry(request, run_id)
        return _result_panel_projection(entry)

    @app.get("/runs/{run_id}/review-packet")
    async def get_review_packet(run_id: str, request: Request) -> dict[str, Any]:
        entry = _get_scoped_entry(request, run_id)
        payload = _load_review_packet_for_entry(entry)
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal) or principal.source != ADK_SOURCE:
            return payload
        packet = payload.get("packet")
        return {
            "present": bool(payload.get("present")),
            "run_id": payload.get("run_id"),
            "packet": (
                project_artifact_payload("review_packet", packet)
                if isinstance(packet, dict)
                else None
            ),
        }

    @app.get("/runs/{run_id}/review")
    async def get_run_review(run_id: str, request: Request) -> dict[str, Any]:
        entry = _get_scoped_entry(request, run_id)
        artifact_root = entry.get("artifact_root")
        review_store = _get_review_store()
        with review_store.pinned_reads():
            if artifact_root:
                with TrustedArtifactRoot(
                    artifact_root,
                    namespace="review_packet",
                ) as trusted_root:
                    review = _review_payload_for_run(
                        entry,
                        review_store=review_store,
                        review_store_root=resolved_settings.review_store_dir,
                        trusted_root=trusted_root,
                    )
            else:
                review = _review_payload_for_run(
                    entry,
                    review_store=review_store,
                    review_store_root=resolved_settings.review_store_dir,
                )
        principal = getattr(request.state, "principal", None)
        if isinstance(principal, Principal) and principal.source == ADK_SOURCE:
            review = project_review(Review.model_validate(review))
        return {"review": review}

    @app.post("/runs/{run_id}/report-export")
    async def create_run_report_export(run_id: str, request: Request) -> dict[str, Any]:
        _get_scoped_entry(request, run_id)
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
        authoritative_adk_runs = _audit_adk_registry()
        trusted_operator, trusted_workspace, trusted_source = _trusted_list_scope(request)
        requested_operator = _normalize_identity_filter(
            operator_id, request=request, header_name="x-operator-id"
        )
        requested_workspace = _normalize_identity_filter(
            workspace_id, request=request, header_name="x-workspace-id"
        )
        filters_are_in_scope = (
            trusted_operator is None
            or requested_operator is None
            or requested_operator == trusted_operator
        ) and (
            trusted_workspace is None
            or requested_workspace is None
            or requested_workspace == trusted_workspace
        )
        if trusted_source == ADK_SOURCE:
            for entry in run_registry.list_runs(resolved_settings.registry_path):
                if object_in_scope(request.state.principal, entry):
                    _validate_adk_entry_provenance(
                        entry,
                        authoritative_adk=(
                            str(entry.get("run_id")) in authoritative_adk_runs
                        ),
                    )
        review_store = _get_review_store()
        with review_store.pinned_reads():
            reviews = (
                _list_review_payloads(
                    registry_path=resolved_settings.registry_path,
                    review_store=review_store,
                    review_store_root=resolved_settings.review_store_dir,
                    principal=request.state.principal,
                    operator_id=trusted_operator or requested_operator,
                    workspace_id=trusted_workspace or requested_workspace,
                )
                if filters_are_in_scope
                else []
            )
        return {"review_count": len(reviews), "reviews": reviews}

    @app.get("/reviews/{review_id}")
    async def get_review(review_id: str, request: Request) -> dict[str, Any]:
        run_id = _run_id_from_review_id(review_id)
        if run_id is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "object_not_found", "message": "Object was not found."},
            )
        review_store = _get_review_store()
        run_entry = _get_scoped_entry(request, run_id)
        artifact_root = run_entry.get("artifact_root")
        with review_store.pinned_reads():
            if artifact_root:
                with TrustedArtifactRoot(
                    artifact_root,
                    namespace="review_packet",
                ) as trusted_root:
                    review = _review_payload_for_run(
                        run_entry,
                        review_store=review_store,
                        review_store_root=resolved_settings.review_store_dir,
                        trusted_root=trusted_root,
                    )
            else:
                review = _review_payload_for_run(
                    run_entry,
                    review_store=review_store,
                    review_store_root=resolved_settings.review_store_dir,
                )
        if review.get("review_id") != review_id:
            raise HTTPException(status_code=404, detail="Review not found.")
        principal = getattr(request.state, "principal", None)
        if isinstance(principal, Principal) and principal.source == ADK_SOURCE:
            review = project_review(Review.model_validate(review))
        return {"review": review}

    @app.post("/reviews/{review_id}/decision")
    async def submit_review_decision(review_id: str, request: ReviewDecisionRequest, http_request: Request) -> dict[str, Any]:
        run_id = _run_id_from_review_id(review_id)
        if run_id is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "object_not_found", "message": "Object was not found."},
            )
        run_entry = _get_scoped_entry(http_request, run_id)
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
            bound_review_record = bind_review_record_identity(review_record, run_entry=run_entry)
            decision_record = review_store.submit_decision(
                review_id=review_id,
                decision=decision_contract.decision,
                comment=request.comment,
                decided_by=request.decided_by,
                follow_up_run_id=request.follow_up_run_id,
                bound_review=bound_review_record,
            )
        except ReviewDecisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewIdentityMismatchError:
            raise
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

    @app.post("/adk/runs")
    def start_adk_workflow_run(
        request: AdkStartRequest,
        http_request: Request,
        background_tasks: BackgroundTasks,
    ) -> JSONResponse:
        if authority is None:
            raise HTTPException(status_code=503, detail="ADK execution is not configured.")
        idempotency_key = http_request.headers.get("idempotency-key")
        confirmation = http_request.headers.get("x-adk-confirmation")
        if not idempotency_key:
            raise HTTPException(
                status_code=400,
                detail={"code": "idempotency_key_required", "message": "Idempotency key is required."},
            )
        fingerprint = request_fingerprint(request)
        if not authority.verify_adk_confirmation(
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            candidate=confirmation,
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "confirmation_invalid", "message": "Confirmation binding is invalid."},
            )
        try:
            workflow_entry = resolved_workflow_catalog.get_workflow(request.workflow_id)
            if not workflow_entry.builtin:
                raise ValueError("Workflow is not published for ADK execution")
            if request.parent_run_id is not None:
                _get_scoped_entry(http_request, request.parent_run_id)
            adk_root = Path(app.state.adk_artifact_root).expanduser().absolute()
            run_id = f"adk-{uuid4_hex()}"
            operation_id = f"op_{uuid4_hex()}"
            artifact_dir = adk_root / run_id
            run_request = RunCreateRequest(
                case_id=request.case_id,
                workflow_id=request.workflow_id,
                inputs=dict(request.inputs),
                background=True,
            )
            operator_params = _workflow_operator_params_from_request(
                run_request,
                workflow_entry=workflow_entry,
                artifact_dir=artifact_dir,
                review_delivery_dir=None,
                registry_path=resolved_settings.registry_path,
                ownership={
                    "created_by": "adk-developer",
                    "operator_id": "adk-developer",
                    "workspace_id": ADK_WORKSPACE_ID,
                },
                    runner_module=resolved_runner_module,
                task_contracts_module=task_contracts_module,
                tool_registry=resolved_tool_registry,
            )
            provenance = build_adk_provenance(
                request,
                workflow_entry=workflow_entry,
                run_id=run_id,
            )
            entry, created = run_registry.accept_adk_run(
                registry_path=resolved_settings.registry_path,
                action="start_workflow_run",
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                confirmation_grant_digest=hashlib.sha256(str(confirmation).encode("utf-8")).hexdigest(),
                run_id=run_id,
                operation_id=operation_id,
                task_id=f"adk-{request.case_id}",
                case_id=request.case_id,
                artifact_root=str(artifact_dir),
                workflow_id=request.workflow_id,
                provenance=provenance,
                operator_params={
                    "case_id": request.case_id,
                    "workflow_id": request.workflow_id,
                    "workflow_inputs": dict(request.inputs),
                    "created_by": "adk-developer",
                    "operator_id": "adk-developer",
                    "workspace_id": ADK_WORKSPACE_ID,
                },
            )
        except run_registry.IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "Start request conflicts with a persisted binding."},
            ) from exc
        except run_registry.RegistryIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "ADK registry integrity is invalid."},
            ) from exc
        except (ValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "adk_start_invalid", "message": "ADK start request is invalid."},
            ) from exc

        if created:
            operator_params["run_id"] = str(entry["run_id"])
            operator_params["provenance"] = provenance
            try:
                isolated_root = prepare_isolated_run_root(adk_root, str(entry["run_id"]))
                _write_initial_adk_manifest(
                    isolated_root,
                    entry=entry,
                    provenance=provenance,
                )
                scheduler = background_task_runner or background_tasks.add_task
                scheduler(_run_workflow_background, operator_params)
            except Exception as exc:
                _record_run_failure(operator_params, exc, execution_mode="background")
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "status": str(entry.get("status")),
                "run_id": str(entry.get("run_id")),
                "operation_id": str(entry.get("operation_id")),
                "case_id": str(entry.get("case_id")),
                "correlation_id": str((entry.get("provenance") or {}).get("correlation_id")),
                "idempotent_replay": not created,
            },
        )

    def _verify_adk_debug_confirmation(
        *,
        action: str,
        object_id: str,
        request: AdkDebugContext | AdkBenchmarkRequest,
        http_request: Request,
    ) -> tuple[str, str, str]:
        if authority is None:
            raise HTTPException(status_code=503, detail="ADK execution is not configured.")
        idempotency_key = http_request.headers.get("idempotency-key")
        confirmation = http_request.headers.get("x-adk-confirmation")
        if not idempotency_key:
            raise HTTPException(
                status_code=400,
                detail={"code": "idempotency_key_required", "message": "Idempotency key is required."},
            )
        if not 16 <= len(idempotency_key) <= 256:
            raise HTTPException(
                status_code=400,
                detail={"code": "idempotency_key_invalid", "message": "Idempotency key is invalid."},
            )
        fingerprint = adk_debug_request_fingerprint(
            action=action,
            object_id=object_id,
            request=request,
        )
        if not authority.verify_adk_confirmation(
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            candidate=confirmation,
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "confirmation_invalid", "message": "Confirmation binding is invalid."},
            )
        return idempotency_key, fingerprint, str(confirmation)

    @app.post("/adk/runs/{run_id}/rerun")
    def rerun_adk_run(
        run_id: str,
        request: AdkDebugContext,
        http_request: Request,
        background_tasks: BackgroundTasks,
    ) -> JSONResponse:
        idempotency_key, fingerprint, confirmation = _verify_adk_debug_confirmation(
            action="rerun_run",
            object_id=run_id,
            request=request,
            http_request=http_request,
        )
        try:
            source_entry = _get_scoped_entry(http_request, run_id)
            source_provenance = validate_adk_provenance(source_entry.get("provenance"))
            workflow_id = str(source_provenance["workflow_id"])
            workflow_entry = resolved_workflow_catalog.get_workflow(workflow_id)
            if not workflow_entry.builtin:
                raise ValueError("Workflow is not published for ADK execution")
            source_workflow_digest = str(source_provenance["workflow_digest"])
            if workflow_digest(workflow_entry) != source_workflow_digest:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "workflow_version_unavailable",
                        "message": "The source run's workflow version is unavailable.",
                    },
                )
            workflow_inputs_unavailable_detail = {
                "code": "workflow_inputs_unavailable",
                "message": "The source run's frozen workflow inputs are unavailable.",
            }
            source_operator_params = source_entry.get("operator_params")
            if (
                not isinstance(source_operator_params, dict)
                or "workflow_inputs" not in source_operator_params
                or not isinstance(source_operator_params["workflow_inputs"], dict)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=workflow_inputs_unavailable_detail,
                )
            workflow_inputs = dict(source_operator_params["workflow_inputs"])
            try:
                validate_adk_inputs(workflow_inputs)
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=workflow_inputs_unavailable_detail,
                ) from exc
            child_request = AdkStartRequest(
                workflow_id=workflow_id,
                case_id=str(source_entry.get("case_id") or ""),
                inputs=workflow_inputs,
                adk_app=request.adk_app,
                adk_session_id=request.adk_session_id,
                adk_invocation_id=request.adk_invocation_id,
                parent_run_id=run_id,
            )
            adk_root = Path(app.state.adk_artifact_root).expanduser().absolute()
            child_run_id = f"adk-{uuid4_hex()}"
            operation_id = f"op_{uuid4_hex()}"
            artifact_dir = adk_root / child_run_id
            lineage = source_provenance.get("lineage")
            root_run_id = (
                lineage.get("root_run_id")
                if isinstance(lineage, dict) and lineage.get("root_run_id")
                else run_id
            )
            provenance = build_adk_provenance(
                child_request,
                workflow_entry=workflow_entry,
                run_id=child_run_id,
                workflow_digest_override=str(source_provenance["workflow_digest"]),
                source_run_id=run_id,
                root_run_id=str(root_run_id),
            )
            run_request = RunCreateRequest(
                case_id=child_request.case_id,
                workflow_id=workflow_id,
                inputs=workflow_inputs,
                background=True,
            )
            operator_params = _workflow_operator_params_from_request(
                run_request,
                workflow_entry=workflow_entry,
                artifact_dir=artifact_dir,
                review_delivery_dir=None,
                registry_path=resolved_settings.registry_path,
                ownership={
                    "created_by": "adk-developer",
                    "operator_id": "adk-developer",
                    "workspace_id": ADK_WORKSPACE_ID,
                },
                runner_module=resolved_runner_module,
                task_contracts_module=task_contracts_module,
                tool_registry=resolved_tool_registry,
            )
            entry, created = run_registry.accept_adk_run(
                registry_path=resolved_settings.registry_path,
                action="rerun_run",
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                confirmation_grant_digest=hashlib.sha256(confirmation.encode("utf-8")).hexdigest(),
                run_id=child_run_id,
                operation_id=operation_id,
                task_id=f"adk-{child_request.case_id}",
                case_id=child_request.case_id,
                artifact_root=str(artifact_dir),
                workflow_id=workflow_id,
                provenance=provenance,
                operator_params={
                    "case_id": child_request.case_id,
                    "workflow_id": workflow_id,
                    "workflow_inputs": workflow_inputs,
                    "parent_run_id": run_id,
                    "source_run_id": run_id,
                    "created_by": "adk-developer",
                    "operator_id": "adk-developer",
                    "workspace_id": ADK_WORKSPACE_ID,
                },
            )
        except run_registry.IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "Rerun request conflicts with a persisted binding."},
            ) from exc
        except run_registry.RegistryIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "ADK registry integrity is invalid."},
            ) from exc
        except (ValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "adk_rerun_invalid", "message": "ADK rerun request is invalid."},
            ) from exc

        if created:
            operator_params["run_id"] = str(entry["run_id"])
            operator_params["provenance"] = provenance
            try:
                isolated_root = prepare_isolated_run_root(adk_root, str(entry["run_id"]))
                _write_initial_adk_manifest(
                    isolated_root,
                    entry=entry,
                    provenance=provenance,
                )
                scheduler = background_task_runner or background_tasks.add_task
                scheduler(_run_workflow_background, operator_params)
            except Exception as exc:
                _record_run_failure(operator_params, exc, execution_mode="background")

        entry_provenance = entry.get("provenance") or {}
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "status": str(entry.get("status")),
                "run_id": str(entry.get("run_id")),
                "operation_id": str(entry.get("operation_id")),
                "case_id": str(entry.get("case_id")),
                "correlation_id": str(entry_provenance.get("correlation_id")),
                "parent_run_id": str(entry_provenance.get("parent_run_id")),
                "source_run_id": str(entry_provenance.get("source_run_id")),
                "idempotent_replay": not created,
            },
        )

    @app.post("/adk/runs/{run_id}/replay")
    async def replay_adk_run(
        run_id: str,
        request: AdkEmptyDebugRequest,
        http_request: Request,
    ) -> dict[str, Any]:
        del request
        entry = _get_scoped_entry(http_request, run_id)
        return {"replay": _adk_replay_payload(entry)}

    @app.post("/adk/repeatability")
    async def compare_adk_repeatability(
        request: AdkRepeatabilityRequest,
        http_request: Request,
    ) -> dict[str, Any]:
        entries = [_get_scoped_entry(http_request, run_id) for run_id in request.run_ids]
        return {"repeatability": _adk_repeatability_payload(entries)}

    @app.post("/adk/runs/{run_id}/report-export")
    async def export_adk_run_report(
        run_id: str,
        request: AdkDebugContext,
        http_request: Request,
    ) -> JSONResponse:
        idempotency_key, fingerprint, confirmation = _verify_adk_debug_confirmation(
            action="export_run_report",
            object_id=run_id,
            request=request,
            http_request=http_request,
        )
        try:
            entry = _get_scoped_entry(http_request, run_id)
            operation_id = f"op_{uuid4_hex()}"
            pending_result = {
                "ok": True,
                "operation_id": operation_id,
                "status": "running",
                "report": {
                    "run": {"run_id": run_id, "status": str(entry.get("status"))},
                    "artifacts": [],
                },
            }
            operation, created = run_registry.record_adk_debug_operation(
                operation_store_path=resolved_settings.registry_path,
                action="export_run_report",
                object_id=run_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                confirmation_grant_digest=hashlib.sha256(confirmation.encode("utf-8")).hexdigest(),
                operation_id=operation_id,
                result=pending_result,
            )
            if not created:
                result = dict(operation.get("result") or {})
                return JSONResponse(
                    status_code=202,
                    content={**result, "idempotent_replay": True},
                )
            operation_id = str(operation["operation_id"])
            try:
                report = _export_adk_report_artifacts(
                    settings=resolved_settings,
                    entry=entry,
                )
            except Exception:
                result = {
                    "ok": False,
                    "operation_id": operation_id,
                    "status": "failed",
                    "report": {
                        "run": {"run_id": run_id, "status": str(entry.get("status"))},
                        "failure_class": "report_export_failed",
                        "artifacts": [],
                    },
                }
                operation = run_registry.complete_adk_debug_operation(
                    operation_store_path=resolved_settings.registry_path,
                    operation_id=operation_id,
                    result=result,
                )
                stored_result = dict(operation.get("result") or result)
                return JSONResponse(
                    status_code=202,
                    content={**stored_result, "idempotent_replay": False},
                )
            result = {
                "ok": True,
                "operation_id": operation_id,
                "status": "completed",
                "report": report,
            }
            operation = run_registry.complete_adk_debug_operation(
                operation_store_path=resolved_settings.registry_path,
                operation_id=operation_id,
                result=result,
            )
        except run_registry.IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "Report request conflicts with a persisted binding."},
            ) from exc
        except run_registry.RegistryIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "ADK registry integrity is invalid."},
            ) from exc
        except ArtifactProjectionReadError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "Report artifact could not be written safely."},
            ) from exc
        except SafeJsonReadError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "Report artifact could not be written safely."},
            ) from exc
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "report_export_unavailable", "message": "Report artifacts could not be exported."},
            ) from exc
        stored_result = dict(operation.get("result") or result)
        return JSONResponse(
            status_code=202,
            content={**stored_result, "idempotent_replay": False},
        )

    @app.get("/adk/operations/{operation_id}")
    async def get_adk_debug_operation_status(
        operation_id: str,
        http_request: Request,
    ) -> dict[str, Any]:
        del http_request
        operation = _get_adk_debug_operation_by_id(operation_id)
        return {"operation": _path_free_adk_operation(operation)}

    @app.post("/adk/operations/{operation_id}/wait")
    async def wait_adk_debug_operation(
        operation_id: str,
        request: AdkOperationWaitRequest,
        http_request: Request,
    ) -> dict[str, Any]:
        del request, http_request
        operation = _get_adk_debug_operation_by_id(operation_id)
        return {"operation": _path_free_adk_operation(operation)}

    def _get_adk_debug_operation_by_id(operation_id: str) -> dict[str, Any]:
        for store_path in (
            Path(app.state.evaluation_state_root) / "operations.json",
            Path(resolved_settings.registry_path),
        ):
            try:
                operation = run_registry.get_adk_debug_operation_by_id(
                    operation_store_path=store_path,
                    operation_id=operation_id,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "operation_id_invalid", "message": "Operation ID is invalid."},
                ) from exc
            except run_registry.RegistryIntegrityError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": exc.code, "message": "ADK operation integrity is invalid."},
                ) from exc
            if operation is not None:
                return operation
        try:
            operation = run_registry.get_adk_run_operation_by_id(
                registry_path=resolved_settings.registry_path,
                operation_id=operation_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "operation_id_invalid", "message": "Operation ID is invalid."},
            ) from exc
        except run_registry.RegistryIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "ADK operation integrity is invalid."},
            ) from exc
        if operation is not None:
            return operation
        raise HTTPException(
            status_code=404,
            detail={"code": "object_not_found", "message": "Object was not found."},
        )

    @app.post("/adk/benchmarks/bounded")
    async def run_adk_bounded_benchmark(
        request: AdkBenchmarkRequest,
        http_request: Request,
    ) -> JSONResponse:
        budget = _validate_adk_benchmark_budget(request, resolved_settings)
        case_pack = _load_adk_case_pack(request.case_pack_id)
        idempotency_key, fingerprint, confirmation = _verify_adk_debug_confirmation(
            action="run_bounded_benchmark",
            object_id=request.case_pack_id,
            request=request,
            http_request=http_request,
        )
        operation_store_path = Path(app.state.evaluation_state_root) / "operations.json"
        operation_id = f"op_{uuid4_hex()}"
        pending_result = {
            "ok": True,
            "operation_id": operation_id,
            "status": "running",
            "benchmark": {
                "case_pack_id": request.case_pack_id,
                "lane": request.lane,
                "status": "running",
            },
        }
        try:
            operation, created = run_registry.record_adk_debug_operation(
                operation_store_path=operation_store_path,
                action="run_bounded_benchmark",
                object_id=request.case_pack_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                confirmation_grant_digest=hashlib.sha256(confirmation.encode("utf-8")).hexdigest(),
                operation_id=operation_id,
                result=pending_result,
            )
            if not created:
                result = dict(operation.get("result") or {})
                return JSONResponse(
                    status_code=202,
                    content={**result, "idempotent_replay": True},
                )
            operation_id = str(operation["operation_id"])
        except run_registry.IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "Benchmark request conflicts with a persisted binding."},
            ) from exc
        except run_registry.RegistryIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "ADK benchmark operation integrity is invalid."},
            ) from exc
        business_roots = [
            Path(resolved_settings.registry_path).expanduser().resolve().parent,
            Path(resolved_settings.artifact_root).expanduser().resolve(),
            Path(resolved_settings.review_store_dir).expanduser().resolve(),
        ]
        try:
            if request.lane == "offline":
                benchmark = run_offline_evaluation_lane(
                    case_pack_id=request.case_pack_id,
                    case_pack=case_pack,
                    state_root=app.state.evaluation_state_root,
                    business_roots=business_roots,
                    case_limit=budget.get("case_limit"),
                    evidence_id=operation_id,
                    budget=budget,
                )
            else:
                benchmark = run_real_model_evaluation_lane(
                    case_pack_id=request.case_pack_id,
                    case_pack=case_pack,
                    state_root=app.state.evaluation_state_root,
                    business_roots=business_roots,
                    case_limit=budget.get("case_limit"),
                    evidence_id=operation_id,
                    budget=budget,
                )
        except Exception:
            benchmark = {
                "ok": False,
                "lane": request.lane,
                "status": "failed",
                "case_pack_id": request.case_pack_id,
                "case_count": 0,
                "failure_class": "benchmark_execution_failed",
                "checks": [
                    {
                        "check_id": "benchmark_execution",
                        "status": "failed",
                        "summary": "Benchmark execution failed after operation acceptance.",
                    }
                ],
            }
        result = {
            "ok": bool(benchmark.get("ok", False)),
            "operation_id": operation_id,
            "status": str(benchmark.get("status")),
            "benchmark": benchmark,
        }
        try:
            operation = run_registry.complete_adk_debug_operation(
                operation_store_path=operation_store_path,
                operation_id=operation_id,
                result=result,
            )
        except run_registry.IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "Benchmark request conflicts with a persisted binding."},
            ) from exc
        except run_registry.RegistryIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": "ADK benchmark operation integrity is invalid."},
            ) from exc
        stored_result = dict(operation.get("result") or result)
        return JSONResponse(
            status_code=202,
            content={**stored_result, "idempotent_replay": False},
        )

    @app.exception_handler(run_registry.RegistryIntegrityError)
    async def _registry_integrity_handler(
        _request: Request,
        exc: run_registry.RegistryIntegrityError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": exc.code,
                    "message": "ADK registry integrity is invalid.",
                }
            },
        )

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

    assert_route_matrix_complete(app)
    return app


def create_app(**kwargs: Any) -> FastAPI:
    """Create a deployable fail-closed control-plane application."""

    return _create_app(**kwargs)


def _safe_auth_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


def _set_operator_session_cookies(
    response: JSONResponse | HTMLResponse,
    *,
    authority: CapabilityAuthority,
    session_id: str,
    csrf_token: str,
    max_age: int,
) -> None:
    response.set_cookie(
        key=authority.session_cookie_name,
        value=session_id,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        key="ai_actuary_csrf",
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        samesite="strict",
        path="/",
    )


def _loopback_http_origin(raw_url: str, *, purpose: str) -> str:
    """Return a normalized loopback-only HTTP origin with an explicit port."""

    try:
        parsed = urlsplit(raw_url.strip())
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{purpose} must be a loopback HTTP origin with an explicit port.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed_port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{purpose} must be a loopback HTTP origin with an explicit port.")
    return f"http://{parsed.hostname}:{parsed_port}"


def _inject_console_csrf_transport(html: str) -> str:
    script = """<script>
    (() => {
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (url, options = {}) => {
        const request = { ...options, headers: { ...(options.headers || {}) } };
        const method = String(request.method || "GET").toUpperCase();
        if (!(method === "GET" || method === "HEAD")) {
          const prefix = "ai_actuary_csrf=";
          const cookie = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(prefix));
          request.headers["X-CSRF-Token"] = cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
        }
        return nativeFetch(url, request);
      };
      window.addEventListener("DOMContentLoaded", () => {
        if (document.cookie.includes("ai_actuary_csrf=")) return;
        const panel = document.createElement("div");
        panel.setAttribute("role", "status");
        panel.style.cssText = "position:fixed;right:1rem;bottom:1rem;z-index:9999;max-width:28rem;padding:1rem;background:#102235;color:#fff;border-radius:.5rem";
        const message = document.createElement("div");
        message.textContent = "Operator session is locked. Request a one-time launcher handoff.";
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Request launcher handoff";
        button.style.cssText = "margin-top:.75rem;padding:.5rem";
        panel.append(message, button);
        document.body.append(panel);
        button.addEventListener("click", async () => {
          button.disabled = true;
          const bytes = new Uint8Array(32);
          crypto.getRandomValues(bytes);
          const claimToken = btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
          const requested = await nativeFetch("/auth/operator/handoff/request", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ claim_token: claimToken }),
          });
          if (!requested.ok) {
            message.textContent = "A launcher handoff is unavailable.";
            return;
          }
          const handoff = await requested.json();
          message.textContent = `Enter this handoff ID in the launcher: ${handoff.handoff_id}`;
          const deadline = Date.now() + (handoff.expires_in * 1000);
          while (Date.now() < deadline) {
            await new Promise((resolve) => setTimeout(resolve, 1000));
            const claimed = await nativeFetch("/auth/operator/handoff/claim", {
              method: "POST",
              credentials: "same-origin",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ handoff_id: handoff.handoff_id, claim_token: claimToken }),
            });
            if (claimed.ok) {
              window.location.reload();
              return;
            }
          }
          message.textContent = "The launcher handoff expired. Request a new workbench session.";
        });
      });
    })();
  </script>
  """
    marker = "<script>"
    if marker not in html:
        raise RuntimeError("Operator Console script marker is missing")
    return html.replace(marker, script + marker, 1)


def uuid4_hex() -> str:
    return uuid.uuid4().hex


def _write_initial_adk_manifest(
    artifact_root: Path,
    *,
    entry: dict[str, Any],
    provenance: dict[str, Any],
) -> Path:
    payload = {
        "case_id": entry.get("case_id"),
        "run_id": entry.get("run_id"),
        "workflow_id": entry.get("workflow_id"),
        "artifact_paths": {"run_manifest": "run_manifest.json"},
        **dict(provenance),
    }
    write_json_object_exclusive(
        artifact_root,
        "run_manifest.json",
        payload,
        namespace="manifest",
    )
    return artifact_root / "run_manifest.json"


def _export_adk_report_artifacts(
    *,
    settings: ApiSettings,
    entry: dict[str, Any],
) -> dict[str, Any]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        raise ValueError("artifact_root_missing")
    raw_report = export_run_report(
        registry_path=settings.registry_path,
        run_id=str(entry.get("run_id")),
        review_store_root=settings.review_store_dir,
        output_dir=artifact_root,
    )
    manifest_path = Path(str(artifact_root)).expanduser().resolve() / "run_manifest.json"
    manifest = read_bounded_json_object(
        artifact_root,
        "run_manifest.json",
        namespace="manifest",
    )
    artifact_paths = dict(manifest.get("artifact_paths", {}) or {})
    logical_exports = {
        "operator_handoff": "operator_handoff.md",
        "reserve_summary_json": "reserve_summary.json",
        "reserve_summary_markdown": "reserve_summary.md",
    }
    artifact_paths.update(logical_exports)
    manifest["artifact_paths"] = artifact_paths
    write_json_artifact(manifest_path, manifest)
    status = str(entry.get("status"))
    return {
        "run": _path_free_run_payload(_run_summary(entry)),
        "truthful_status": status,
        "terminal": is_terminal_run_status(status),
        "event_count": len(entry.get("status_history", []) or []),
        "reserve_summary": dict(raw_report.get("reserve_summary") or {}),
        "review": {
            "review_id": (raw_report.get("review") or {}).get("review_id"),
            "status": (raw_report.get("review") or {}).get("status"),
            "review_required": bool((raw_report.get("review") or {}).get("review_required")),
        },
        "artifacts": [
            {
                "artifact_id": artifact_id,
                "category": "report",
                "present": (Path(str(artifact_root)).expanduser().resolve() / filename).is_file(),
            }
            for artifact_id, filename in logical_exports.items()
        ],
    }


def _path_free_adk_operation(operation: dict[str, Any]) -> dict[str, Any]:
    result = dict(operation.get("result") or {})
    payload = {
        "operation_id": str(operation.get("operation_id")),
        "action": str(operation.get("action")),
        "status": str(result.get("status") or "unknown"),
    }
    for key in ("benchmark", "report", "run"):
        if isinstance(result.get(key), dict):
            payload[key] = result[key]
    return payload


def _validate_adk_benchmark_budget(
    request: AdkBenchmarkRequest,
    settings: ApiSettings,
) -> dict[str, Any]:
    policy = {
        "case_limit": MAX_ADK_BENCHMARK_CASE_LIMIT,
        "input_byte_limit": settings.adk_benchmark_input_byte_limit,
        "total_byte_limit": settings.adk_benchmark_total_byte_limit,
        "output_byte_limit": settings.adk_benchmark_output_byte_limit,
        "wall_time_seconds": settings.adk_benchmark_wall_time_seconds,
        "temp_storage_bytes": settings.adk_benchmark_temp_storage_bytes,
        "retention_days": settings.adk_benchmark_retention_days,
        "concurrency": 1,
    }
    requested = request.model_dump(mode="json", exclude_none=True)
    for field in (
        "input_byte_limit",
        "total_byte_limit",
        "output_byte_limit",
        "wall_time_seconds",
        "temp_storage_bytes",
        "retention_days",
        "concurrency",
    ):
        if field in requested and requested[field] > policy[field]:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "benchmark_quota_exceeded",
                    "message": "Benchmark request exceeds server quota.",
                    "field": field,
                    "ceiling": policy[field],
                },
            )
    return {
        field: min(requested.get(field, ceiling), ceiling)
        for field, ceiling in policy.items()
    }


def _load_adk_case_pack(case_pack_id: str) -> dict[str, Any]:
    try:
        return load_case_pack(case_pack_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "case_pack_invalid",
                "message": "Benchmark case pack is unavailable.",
            },
        ) from exc


def _canonical_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _path_free_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    safe.pop("artifact_root", None)
    return safe


def _path_free_artifact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = {"run_id": payload.get("run_id")}
    artifacts: list[dict[str, Any]] = []
    for artifact in payload.get("artifacts", []) or []:
        if not isinstance(artifact, dict):
            continue
        artifacts.append(
            {
                key: value
                for key, value in artifact.items()
                if key not in {"path", "absolute_path", "artifact_root"}
            }
        )
    safe["artifacts"] = artifacts
    return safe


def _adk_replay_payload(entry: dict[str, Any]) -> dict[str, Any]:
    status = str(entry.get("status"))
    if not is_terminal_run_status(status) or status != "completed":
        _raise_adk_not_replayable()
    try:
        replay = _adk_replay_evidence(entry)
    except (ArtifactProjectionReadError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "not_replayable", "message": "Run is not replayable."},
        ) from exc
    return {
        "run_id": str(entry.get("run_id")),
        "case_id": entry.get("case_id"),
        "workflow_id": entry.get("workflow_id"),
        "workflow_digest": replay["provenance"]["workflow_digest"],
        "input_digest": replay["provenance"]["input_digest"],
        "manifest_digest": replay["root_manifest_digest"],
        "step_manifest_digest": replay["step_manifest_digest"],
        "run_status": status,
        "terminal": True,
        "replay_status": "available",
        "replay_match": replay["saved_result_digest"] == replay["replayed_result_digest"],
        "deterministic_result": replay["saved_result"],
        "saved_result_digest": replay["saved_result_digest"],
        "replayed_result_digest": replay["replayed_result_digest"],
        "deterministic_result_digest": replay["saved_result_digest"],
        "evidence": {
            "manifest": True,
            "validated_input": True,
            "case_input": True,
            "deterministic_result": True,
        },
    }


def _adk_replay_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        raise ValueError("artifact_root_missing")
    root = Path(str(artifact_root)).expanduser().absolute()
    provenance = validate_adk_provenance(entry.get("provenance"))
    selected_case_id = str(entry.get("case_id") or "")
    selected_workflow_id = str(entry.get("workflow_id") or provenance.get("workflow_id") or "")
    with TrustedArtifactRoot(root, namespace="manifest", allow_nested=True) as trusted_root:
        root_manifest = trusted_root.read_bounded_json_object(
            "run_manifest.json",
            namespace="manifest",
        )
        validate_artifact_projection_schema("run_manifest", root_manifest)
        for key in ("workflow_id", "workflow_digest", "input_digest"):
            if root_manifest.get(key) != provenance.get(key):
                raise ValueError("manifest_provenance_mismatch")
        if str(root_manifest.get("run_id") or "") != str(entry.get("run_id") or ""):
            raise ValueError("manifest_run_mismatch")
        if str(root_manifest.get("case_id") or "") != selected_case_id:
            raise ValueError("manifest_case_mismatch")
        root_digests = _artifact_digest_ledger(root_manifest)
        candidates: list[tuple[str, dict[str, Any]]] = []
        root_paths = root_manifest.get("artifact_paths")
        if isinstance(root_paths, dict) and "deterministic_result" in root_paths:
            candidates.append(("", root_manifest))
        for artifact_id, raw_ref in sorted(
            (root_paths if isinstance(root_paths, dict) else {}).items()
        ):
            if not (
                isinstance(artifact_id, str)
                and artifact_id.startswith("step_")
                and artifact_id.endswith("_run_manifest")
                and isinstance(raw_ref, str)
            ):
                continue
            step_manifest = trusted_root.read_bounded_json_object(
                raw_ref,
                namespace="manifest",
            )
            validate_artifact_projection_schema("run_manifest", step_manifest)
            _require_digest_match(
                root_digests,
                artifact_id=artifact_id,
                payload=step_manifest,
            )
            base = str(Path(raw_ref.replace("\\", "/")).parent).replace("\\", "/")
            if base == ".":
                base = ""
            candidates.append((base, step_manifest))
        for base, manifest in candidates:
            if str(manifest.get("case_id") or selected_case_id) != selected_case_id:
                raise ValueError("step_manifest_case_mismatch")
            if str(manifest.get("workflow_id") or selected_workflow_id) != selected_workflow_id:
                raise ValueError("step_manifest_workflow_mismatch")
            artifact_paths = manifest.get("artifact_paths")
            if not isinstance(artifact_paths, dict):
                continue
            if not {"validated_input", "case_input", "deterministic_result"} <= set(artifact_paths):
                continue
            artifact_digests = _artifact_digest_ledger(manifest)
            validated_input = _read_verified_replay_artifact(
                trusted_root,
                artifact_digests,
                artifact_id="validated_input",
                relative_path=_join_adk_manifest_ref(
                    base,
                    artifact_paths["validated_input"],
                ),
            )
            case_input_payload = _read_verified_replay_artifact(
                trusted_root,
                artifact_digests,
                artifact_id="case_input",
                relative_path=_join_adk_manifest_ref(
                    base,
                    artifact_paths["case_input"],
                ),
            )
            saved_result = _read_verified_replay_artifact(
                trusted_root,
                artifact_digests,
                artifact_id="deterministic_result",
                relative_path=_join_adk_manifest_ref(
                    base,
                    artifact_paths["deterministic_result"],
                ),
            )
            validate_artifact_projection_schema("validated_input", validated_input)
            validate_artifact_projection_schema("deterministic_result", saved_result)
            _require_replay_payload_identity(
                validated_input,
                case_input_payload,
                saved_result,
                case_id=selected_case_id,
                workflow_id=selected_workflow_id,
            )
            case_input = ReservingCaseInput.model_validate(case_input_payload)
            replayed_result = ChainladderAdapter().calculate(case_input).model_dump(
                mode="json"
            )
            saved_result_digest = _canonical_digest(saved_result)
            replayed_result_digest = _canonical_digest(replayed_result)
            if saved_result_digest != replayed_result_digest:
                raise ValueError("deterministic_replay_mismatch")
            return {
                "provenance": provenance,
                "root_manifest_digest": _canonical_digest(root_manifest),
                "step_manifest_digest": _canonical_digest(manifest),
                "validated_input": validated_input,
                "case_input": case_input_payload,
                "saved_result": saved_result,
                "saved_result_digest": saved_result_digest,
                "replayed_result_digest": replayed_result_digest,
            }
    raise ValueError("complete_replay_evidence_missing")


def _artifact_digest_ledger(manifest: dict[str, Any]) -> dict[str, str]:
    digests = manifest.get("artifact_digests")
    if not isinstance(digests, dict):
        raise ValueError("artifact_digests_missing")
    return {str(key): str(value) for key, value in digests.items()}


def _require_digest_match(
    digests: dict[str, str],
    *,
    artifact_id: str,
    payload: dict[str, Any],
) -> None:
    expected = digests.get(artifact_id)
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("artifact_digest_missing")
    if not secrets.compare_digest(expected, _canonical_digest(payload)):
        raise ValueError("artifact_digest_mismatch")


def _read_verified_replay_artifact(
    trusted_root: TrustedArtifactRoot,
    digests: dict[str, str],
    *,
    artifact_id: str,
    relative_path: str,
) -> dict[str, Any]:
    payload = trusted_root.read_bounded_json_object(
        relative_path,
        namespace="artifact",
    )
    _require_digest_match(digests, artifact_id=artifact_id, payload=payload)
    return payload


def _require_replay_payload_identity(
    validated_input: dict[str, Any],
    case_input_payload: dict[str, Any],
    saved_result: dict[str, Any],
    *,
    case_id: str,
    workflow_id: str,
) -> None:
    for payload in (validated_input, case_input_payload, saved_result):
        if str(payload.get("case_id") or "") != case_id:
            raise ValueError("replay_case_mismatch")
        if payload.get("workflow_id") is not None and str(payload.get("workflow_id")) != workflow_id:
            raise ValueError("replay_workflow_mismatch")
    if validated_input.get("tool_id") != "chainladder":
        raise ValueError("replay_tool_mismatch")
    inputs = validated_input.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("replay_validated_input_invalid")
    expected_case_input = build_chainladder_case_payload(
        case_id=case_id,
        tool_inputs=inputs,
    )
    if canonical_json(expected_case_input) != canonical_json(case_input_payload):
        raise ValueError("replay_case_input_mismatch")


def _join_adk_manifest_ref(base: str, raw_ref: Any) -> str:
    if not isinstance(raw_ref, str) or not raw_ref:
        raise ValueError("artifact_ref_invalid")
    normalized = raw_ref.replace("\\", "/")
    if normalized.startswith("/") or any(part == ".." for part in normalized.split("/")):
        raise ValueError("artifact_ref_invalid")
    if "/" in normalized or not base:
        return normalized
    return f"{base}/{normalized}"


def _adk_repeatability_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    compatibility: list[dict[str, Any]] = []
    for entry in entries:
        try:
            provenance = validate_adk_provenance(entry.get("provenance"))
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "repeatability_incompatible",
                    "message": "Runs are not compatible for repeatability comparison.",
                },
            ) from exc
        status = str(entry.get("status"))
        runs.append(
            {
                "run_id": str(entry.get("run_id")),
                "case_id": entry.get("case_id"),
                "workflow_id": entry.get("workflow_id"),
                "status": status,
                "terminal": is_terminal_run_status(status),
            }
        )
        compatibility.append(
            {
                "workspace_id": entry.get("workspace_id"),
                "case_id": entry.get("case_id"),
                "workflow_id": entry.get("workflow_id"),
                "workflow_digest": provenance.get("workflow_digest"),
                "input_digest": provenance.get("input_digest"),
            }
        )
    for field in (
        "workspace_id",
        "case_id",
        "workflow_id",
        "workflow_digest",
        "input_digest",
    ):
        if len({item.get(field) for item in compatibility}) != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "repeatability_incompatible",
                    "message": "Runs are not compatible for repeatability comparison.",
                },
            )

    replays: list[dict[str, Any]] = []
    for entry in entries:
        try:
            replays.append(_adk_replay_payload(entry))
        except HTTPException as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "repeatability_incompatible",
                    "message": "Runs are not compatible for repeatability comparison.",
                },
            ) from exc
    methods = {
        (item.get("deterministic_result") or {}).get("method")
        for item in replays
    }
    if None in methods or len(methods) != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "repeatability_incompatible",
                "message": "Runs are not compatible for repeatability comparison.",
            },
        )
    result_digests = {item["deterministic_result_digest"] for item in replays}
    return {
        "run_count": len(runs),
        "runs": runs,
        "same_case": True,
        "same_workflow": True,
        "all_terminal": True,
        "all_completed": True,
        "stable_terminal_status": True,
        "deterministic_method": next(iter(methods)),
        "result_digest_match": len(result_digests) == 1,
        "repeatability_status": (
            "repeatable" if len(result_digests) == 1 else "different_results"
        ),
    }


def _raise_adk_not_replayable() -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": "not_replayable", "message": "Run is not replayable."},
    )


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
    provenance = operator_params.get("provenance")
    is_adk = isinstance(provenance, dict) and provenance.get("source") == ADK_SOURCE
    artifact_root = (
        Path(os.path.abspath(os.path.expanduser(str(artifact_dir))))
        if is_adk
        else Path(artifact_dir).expanduser().resolve()
    )
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
        errors=["ADK workflow execution failed."] if is_adk else [str(exc)],
        source=ADK_SOURCE if is_adk else None,
        provenance=provenance if isinstance(provenance, dict) else None,
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
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        requested_operator = _normalize_identity_value(operator_id)
        requested_workspace = _normalize_identity_value(workspace_id)
        return {
            "operator_id": (
                requested_operator
                if requested_operator == principal.operator_id
                else principal.operator_id
            ),
            "workspace_id": (
                requested_workspace
                if requested_workspace == principal.workspace_id
                else principal.workspace_id
            ),
        }
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
    principal = getattr(http_request.state, "principal", None)
    if isinstance(principal, Principal):
        return {
            "created_by": principal.operator_id,
            "operator_id": principal.operator_id,
            "workspace_id": principal.workspace_id,
        }
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


def _run_sequential_workflow(**operator_params: Any) -> dict[str, Any]:
    provenance = operator_params.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("source") != ADK_SOURCE:
        return _run_sequential_workflow_impl(**operator_params)

    registered_root = Path(
        os.path.abspath(os.path.expanduser(str(operator_params["artifact_dir"])))
    )
    with PinnedJsonRoot(
        registered_root,
        namespace="artifact",
        allow_nested=True,
        protect_writes=True,
    ) as pinned_root:
        pinned_params = dict(operator_params)
        pinned_params["artifact_dir"] = pinned_root.execution_path()
        pinned_params["_registered_artifact_root"] = registered_root
        pinned_params["_pinned_adk_root"] = pinned_root
        result = _run_sequential_workflow_impl(**pinned_params)
        pinned_root.verify_configured_root_identity(namespace="artifact")
        return result


def _run_sequential_workflow_impl(
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
    provenance: dict[str, Any] | None = None,
    _registered_artifact_root: str | Path | None = None,
    _pinned_adk_root: PinnedJsonRoot | None = None,
) -> dict[str, Any]:
    if workflow_entry is None:
        workflow_catalog = build_builtin_workflow_catalog()
        workflow_entry = workflow_catalog.get_workflow(workflow_id)
    if tool_registry is None:
        tool_registry = build_builtin_tool_registry()
    is_adk_run = isinstance(provenance, dict) and provenance.get("source") == ADK_SOURCE
    if is_adk_run:
        if _registered_artifact_root is None or _pinned_adk_root is None:
            raise ValueError("ADK workflow storage root is not pinned.")
        artifact_root = Path(artifact_dir)
        registered_artifact_root = Path(_registered_artifact_root)
    else:
        artifact_root = Path(artifact_dir).expanduser().resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        registered_artifact_root = artifact_root
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
        artifact_root=str(registered_artifact_root),
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
        artifact_root=str(registered_artifact_root),
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
        artifact_root=registered_artifact_root,
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
        if is_adk_run:
            assert _pinned_adk_root is not None
            _pinned_adk_root.create_directory_exclusive(
                step.step_id,
                namespace="artifact",
            )
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
            artifact_root=registered_artifact_root,
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
                pinned_adk_root=_pinned_adk_root if is_adk_run else None,
                step_id=step.step_id if is_adk_run else None,
            )
        elif is_adk_run:
            assert _pinned_adk_root is not None
            step_result = _run_adk_operator_step_staged(
                pinned_root=_pinned_adk_root,
                step_id=step.step_id,
                case_id=case_id,
                objective=objective,
                step_inputs=step_inputs,
                case_payload=case_payload,
                user_prompt=user_prompt,
                created_by=created_by,
                operator_id=operator_id,
                workspace_id=workspace_id,
                runner_module=runner_module,
                task_contracts_module=task_contracts_module,
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
        step_manifest_path = Path(
                step_result.get("final_output", {}).get("artifact_manifest_path")
                or step_artifact_dir / "run_manifest.json"
            ).expanduser().resolve()
        if is_adk_run:
            assert _pinned_adk_root is not None
            try:
                _pinned_adk_root.stat_regular_artifact(
                    f"{step.step_id}/run_manifest.json",
                    namespace="manifest",
                )
                step_manifest_exists = True
            except SafeJsonReadError as exc:
                if exc.code != "manifest_missing":
                    raise
                step_manifest_exists = False
        else:
            step_manifest_exists = step_manifest_path.exists()
        if step_manifest_exists:
            step_artifact_paths[f"step_{step.step_id}_run_manifest"] = (
                f"{step.step_id}/run_manifest.json"
                if is_adk_run
                else str(step_manifest_path)
            )
        step_record = {
            "step_id": step.step_id,
            "tool_id": step.tool_id,
            "step_kind": step.step_kind,
            "title": step.title,
            "status": step_status,
            "run_id": step_result.get("run_id"),
        }
        if not is_adk_run:
            step_record["artifact_dir"] = str(step_artifact_dir)
        workflow_steps.append(step_record)
        step_finished_event_type = _workflow_step_finished_event_type(step_status)
        _record_workflow_event(
            registry_path=registry_path,
            task_id=task_id,
            case_id=case_id,
            run_id=parent_run_id,
            artifact_root=registered_artifact_root,
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

    if final_status == "needs_review" and isinstance(last_result, dict):
        step_review_packet = last_result.get("review_packet")
        if isinstance(step_review_packet, dict):
            parent_review_packet = {
                key: value
                for key, value in step_review_packet.items()
                if key != "packet_paths"
            }
            parent_review_packet.update(
                {
                    "run_id": parent_run_id,
                    "case_id": case_id,
                    "workspace_id": workspace_id,
                }
            )
            if is_adk_run:
                assert _pinned_adk_root is not None
                _pinned_adk_root.write_json_object_exclusive(
                    "review_packet.json",
                    parent_review_packet,
                    namespace="review_packet",
                )
            else:
                write_json_artifact(
                    resolve_artifact_path(artifact_root, "review_packet.json"),
                    parent_review_packet,
                )
            step_artifact_paths["review_packet"] = "review_packet.json"

    workflow_summary_payload = {
        "workflow_id": workflow_id,
        "case_id": case_id,
        "run_id": parent_run_id,
        "status": final_status,
        "step_count": len(workflow_steps),
        "steps": workflow_steps,
    }
    if provenance is not None:
        workflow_summary_payload["provenance"] = dict(provenance)
    if is_adk_run:
        assert _pinned_adk_root is not None
        _pinned_adk_root.write_json_object_exclusive(
            "workflow_summary.json",
            workflow_summary_payload,
            namespace="artifact",
        )
        workflow_summary_path = registered_artifact_root / "workflow_summary.json"
    else:
        workflow_summary_path = write_json_artifact(
            resolve_artifact_path(artifact_root, "workflow_summary.json"),
            workflow_summary_payload,
        )
    artifact_digests: dict[str, str] = {
        "workflow_summary": _canonical_digest(workflow_summary_payload)
    }
    if is_adk_run:
        assert _pinned_adk_root is not None
        for artifact_id, artifact_ref in step_artifact_paths.items():
            if not artifact_id.endswith("_run_manifest"):
                continue
            step_manifest = _pinned_adk_root.read_bounded_json_object(
                artifact_ref,
                namespace="manifest",
            )
            artifact_digests[artifact_id] = _canonical_digest(step_manifest)
    manifest_payload = {
        "workflow_id": workflow_id,
        "case_id": case_id,
        "run_id": parent_run_id,
        "artifact_paths": {
            "run_manifest": "run_manifest.json",
            "workflow_summary": (
                "workflow_summary.json"
                if is_adk_run
                else str(workflow_summary_path)
            ),
            **step_artifact_paths,
        },
        "artifact_digests": artifact_digests,
    }
    if not is_adk_run:
        manifest_payload["artifact_root"] = str(artifact_root)
    if provenance is not None:
        manifest_payload.update(provenance)
    if is_adk_run:
        assert _pinned_adk_root is not None
        _pinned_adk_root.write_json_object_atomic(
            "run_manifest.json",
            manifest_payload,
            namespace="manifest",
        )
        run_manifest_path = registered_artifact_root / "run_manifest.json"
    else:
        run_manifest_path = write_json_artifact(
            resolve_artifact_path(artifact_root, "run_manifest.json"),
            manifest_payload,
        )

    _record_workflow_event(
        registry_path=registry_path,
        task_id=task_id,
        case_id=case_id,
        run_id=parent_run_id,
        artifact_root=registered_artifact_root,
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
        artifact_root=str(registered_artifact_root),
        summary=final_summary,
        review_required=final_status == "needs_review",
        workflow_id=workflow_id,
        operator_params={
            "case_id": case_id,
            "artifact_dir": str(registered_artifact_root),
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
    if provenance is not None:
        result["provenance"] = dict(provenance)
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


def _run_adk_operator_step_staged(
    *,
    pinned_root: PinnedJsonRoot,
    step_id: str,
    case_id: str,
    objective: str,
    step_inputs: ValidatedToolInput,
    case_payload: dict[str, Any],
    user_prompt: str | None,
    created_by: str | None,
    operator_id: str | None,
    workspace_id: str | None,
    runner_module: Any,
    task_contracts_module: Any,
) -> dict[str, Any]:
    """Run legacy calculation code in private staging, then publish safely."""

    with TemporaryDirectory(prefix="ai-actuary-adk-step-") as temporary_root:
        staging_root = Path(temporary_root)
        step_result = operator_entrypoint.run_operator_flow(
            case_id=case_id,
            artifact_dir=staging_root,
            objective=objective,
            sample_name=step_inputs.inputs.get("sample_name", "RAA"),
            method=step_inputs.inputs.get("method_variant", "chainladder"),
            review_threshold_origin_count=step_inputs.inputs.get(
                "review_threshold_origin_count"
            ),
            case_payload=case_payload,
            user_prompt=user_prompt,
            review_delivery_dir=None,
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
        published_artifacts: set[str] = set()
        with PinnedJsonRoot(
            staging_root,
            namespace="staged_artifact",
        ) as staged_root:
            for filename in ADK_STEP_JSON_ARTIFACTS:
                try:
                    payload = staged_root.read_bounded_json_object(
                        filename,
                        namespace="staged_artifact",
                    )
                except SafeJsonReadError as exc:
                    if exc.code == "staged_artifact_missing":
                        continue
                    raise
                pinned_root.write_json_object_exclusive(
                    f"{step_id}/{filename}",
                    _sanitize_adk_step_payload(filename, payload),
                    namespace="artifact",
                )
                published_artifacts.add(filename)
        if "run_manifest.json" in published_artifacts:
            manifest = pinned_root.read_bounded_json_object(
                f"{step_id}/run_manifest.json",
                namespace="manifest",
            )
            artifact_paths = dict(manifest.get("artifact_paths", {}) or {})
            artifact_digests = dict(manifest.get("artifact_digests", {}) or {})
            for filename in sorted(published_artifacts):
                artifact_id = Path(filename).stem
                artifact_paths.setdefault(artifact_id, filename)
                if filename == "run_manifest.json":
                    continue
                payload = pinned_root.read_bounded_json_object(
                    f"{step_id}/{filename}",
                    namespace="artifact",
                )
                artifact_digests[artifact_id] = _canonical_digest(payload)
            artifact_paths["run_manifest"] = "run_manifest.json"
            manifest["artifact_paths"] = artifact_paths
            manifest["artifact_digests"] = artifact_digests
            pinned_root.write_json_object_atomic(
                f"{step_id}/run_manifest.json",
                manifest,
                namespace="manifest",
            )

    review_packet = step_result.get("review_packet")
    if isinstance(review_packet, dict):
        step_result["review_packet"] = {
            key: value for key, value in review_packet.items() if key != "packet_paths"
        }
    return step_result


def _sanitize_adk_step_payload(
    filename: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    sanitized = dict(payload)
    sanitized.pop("artifact_root", None)
    artifact_paths = sanitized.get("artifact_paths")
    if isinstance(artifact_paths, dict):
        sanitized["artifact_paths"] = {
            str(artifact_id): Path(str(path)).name
            for artifact_id, path in artifact_paths.items()
        }
    if filename == "review_packet.json":
        sanitized.pop("packet_paths", None)
        artifact_links = sanitized.get("artifact_links")
        if isinstance(artifact_links, dict):
            sanitized["artifact_links"] = {
                str(artifact_id): Path(str(path)).name
                for artifact_id, path in artifact_links.items()
            }
    return sanitized


def _run_validation_step(
    *,
    case_id: str,
    artifact_dir: str | Path,
    tool_input: ValidatedToolInput,
    case_payload: dict[str, Any],
    pinned_adk_root: PinnedJsonRoot | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    if pinned_adk_root is None:
        artifact_root = Path(artifact_dir).expanduser().resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
    elif step_id is None:
        raise ValueError("Pinned ADK validation storage requires a step id.")
    else:
        artifact_root = Path(artifact_dir)
    validated_input_payload = {
        "case_id": case_id,
        "tool_id": tool_input.tool_id,
        "inputs": dict(tool_input.inputs),
    }
    if pinned_adk_root is not None:
        assert step_id is not None
        pinned_adk_root.write_json_object_exclusive(
            f"{step_id}/validated_input.json",
            validated_input_payload,
            namespace="artifact",
        )
        pinned_adk_root.write_json_object_exclusive(
            f"{step_id}/case_input.json",
            case_payload,
            namespace="artifact",
        )
        validated_input_path = artifact_root / "validated_input.json"
        case_input_path = artifact_root / "case_input.json"
    else:
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
    if pinned_adk_root is not None:
        assert step_id is not None
        pinned_adk_root.write_json_object_exclusive(
            f"{step_id}/validation_result.json",
            validation_result,
            namespace="artifact",
        )
        validation_result_path = artifact_root / "validation_result.json"
    else:
        validation_result_path = write_json_artifact(
            resolve_artifact_path(artifact_root, "validation_result.json"),
            validation_result,
        )
    manifest_payload = {
        "case_id": case_id,
        "run_id": None,
        "artifact_paths": {
            "validated_input": (
                "validated_input.json"
                if pinned_adk_root is not None
                else str(validated_input_path)
            ),
            "case_input": (
                "case_input.json"
                if pinned_adk_root is not None
                else str(case_input_path)
            ),
            "validation_result": (
                "validation_result.json"
                if pinned_adk_root is not None
                else str(validation_result_path)
            ),
        },
        "artifact_digests": {
            "validated_input": _canonical_digest(validated_input_payload),
            "case_input": _canonical_digest(case_payload),
            "validation_result": _canonical_digest(validation_result),
        },
    }
    if pinned_adk_root is not None:
        assert step_id is not None
        run_manifest_path = artifact_root / "run_manifest.json"
        manifest_payload["artifact_paths"]["run_manifest"] = "run_manifest.json"
        pinned_adk_root.write_json_object_exclusive(
            f"{step_id}/run_manifest.json",
            manifest_payload,
            namespace="manifest",
        )
    else:
        manifest_payload["artifact_root"] = str(artifact_root)
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
    payload = Run(
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
    for key in ("source", "provenance", "recovery_state"):
        if entry.get(key) is not None:
            payload[key] = entry[key]
    return payload


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
    trusted_root: TrustedArtifactRoot | None = None,
    pin_root: bool = True,
) -> dict[str, Any]:
    artifact_root = selected_entry.get("artifact_root") if selected_entry else None
    if pin_root and trusted_root is None and artifact_root:
        try:
            with TrustedArtifactRoot(artifact_root, namespace="artifact") as pinned_root:
                return _console_state_payload(
                    selected_entry,
                    runs,
                    all_runs=all_runs,
                    tool_registry=tool_registry,
                    review_store=review_store,
                    review_store_root=review_store_root,
                    filters=filters,
                    trusted_root=pinned_root,
                    pin_root=False,
                )
        except ArtifactProjectionReadError as exc:
            if exc.code != "artifact_missing":
                raise
            return _console_state_payload(
                selected_entry,
                runs,
                all_runs=all_runs,
                tool_registry=tool_registry,
                review_store=review_store,
                review_store_root=review_store_root,
                filters=filters,
                pin_root=False,
            )
    selected_run_id = str(selected_entry.get("run_id")) if selected_entry else None
    review_inbox = _review_inbox_payload(
        registry_path=None,
        runs=runs,
        review_store=review_store,
        review_store_root=review_store_root,
        selected_run_id=selected_run_id,
        trusted_roots=(
            {selected_run_id: trusted_root}
            if selected_run_id is not None and trusted_root is not None
            else None
        ),
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
        "result_panel": _result_panel_projection(
            selected_entry,
            trusted_root=trusted_root,
        ),
        "artifact_panel": _console_artifact_panel(
            selected_entry,
            trusted_root=trusted_root,
        ),
        "review_inbox": review_inbox,
        "review_panel": _console_review_panel(
            selected_entry,
            review_store=review_store,
            review_store_root=review_store_root,
            trusted_root=trusted_root,
        ),
        "action_panel": _console_action_panel(selected_entry),
    }


def _console_selected_run(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    payload = Run(
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
    payload.pop("artifact_root", None)
    payload["artifact_root_ref"] = _console_artifact_root_ref(entry)
    return payload


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
    with TrustedArtifactRoot(root, namespace="manifest") as trusted_root:
        manifest = trusted_root.read_bounded_json_object(
            "run_manifest.json",
            namespace="manifest",
        )
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
        payload = trusted_root.read_bounded_json_object(
            raw_ref,
            namespace="artifact",
        )
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
        case_id=str(expected_case_id) if expected_case_id is not None else None,
        tool_id=expected_tool_id,
        payload=payload,
    ).model_dump()


def _result_panel_projection(
    entry: dict[str, Any] | None,
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any]:
    """Project registered result artifacts into a path-free Console contract."""

    if entry is None:
        return _empty_result_panel(status="no_run_selected")

    artifact_root = entry.get("artifact_root")
    if trusted_root is None and artifact_root:
        try:
            with TrustedArtifactRoot(artifact_root, namespace="artifact") as pinned_root:
                return _result_panel_projection(entry, trusted_root=pinned_root)
        except ArtifactProjectionReadError as exc:
            return _empty_result_panel(
                status="error",
                tool_id=_registered_tool_id(entry) or UNAVAILABLE,
                errors=[
                    _result_projection_error(
                        "run_manifest",
                        exc.code,
                        "Run manifest could not be read safely.",
                    )
                ],
            )

    registered_tool_id = _registered_tool_id(entry)
    if registered_tool_id not in {None, MINIMAX_EXPERIENCE_STUDY_TOOL_ID}:
        return _empty_result_panel(status="not_available", tool_id=registered_tool_id)

    root, manifest, manifest_error = _load_result_manifest(
        entry,
        trusted_root=trusted_root,
    )
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
            trusted_root=trusted_root,
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
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, str] | None]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        return None, None, _result_projection_error(
            "run_manifest", "artifact_root_missing", "Run artifact root is unavailable."
        )
    try:
        root = Path(str(artifact_root)).expanduser().absolute()
        manifest = (
            trusted_root.read_bounded_json_object(
                "run_manifest.json",
                namespace="artifact",
                max_bytes=MAX_RESULT_ARTIFACT_BYTES,
            )
            if trusted_root is not None
            else read_bounded_json_object(
                root,
                "run_manifest.json",
                namespace="artifact",
                max_bytes=MAX_RESULT_ARTIFACT_BYTES,
            )
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
    trusted_root: TrustedArtifactRoot | None = None,
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
        return (
            trusted_root.read_bounded_json_object(
                raw_ref,
                namespace="artifact",
                max_bytes=MAX_RESULT_ARTIFACT_BYTES,
            )
            if trusted_root is not None
            else read_bounded_json_object(
                artifact_root,
                raw_ref,
                namespace="artifact",
                max_bytes=MAX_RESULT_ARTIFACT_BYTES,
            )
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


def _console_artifact_panel(
    entry: dict[str, Any] | None,
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any]:
    if entry is None:
        return {
            "present": False,
            "status": "no_run_selected",
            "error": None,
            "artifact_root_ref": None,
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
        manifest = _load_manifest_for_entry(entry, trusted_root=trusted_root)
    except ArtifactProjectionReadError:
        manifest = None
        manifest_error = {
            "code": "manifest_unreadable",
            "message": "Run manifest could not be read safely.",
        }
    artifact_root = entry.get("artifact_root")
    root = Path(str(artifact_root)).expanduser().absolute() if artifact_root else None
    primary_refs = _console_expected_artifact_refs(
        root,
        manifest,
        category="primary",
        trusted_root=trusted_root,
    )
    review_refs = _console_expected_artifact_refs(
        root,
        manifest,
        category="review",
        trusted_root=trusted_root,
    )
    decision_refs = _console_expected_artifact_refs(
        root,
        manifest,
        category="decision",
        trusted_root=trusted_root,
    )
    evidence_items = [*primary_refs, *review_refs, *decision_refs]
    projected_manifest = _console_artifact_manifest_projection(manifest, root)
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
        "artifact_root_ref": _console_artifact_root_ref(entry),
        "artifact_manifest": projected_manifest,
        "artifact_paths": projected_manifest.get("artifact_paths", {}) if projected_manifest else {},
        "artifacts": _artifact_logical_metadata_from_manifest(
            manifest,
            artifact_root=artifact_root,
            trusted_root=trusted_root,
        ),
        "primary_artifact_refs": primary_refs,
        "review_artifact_refs": review_refs,
        "decision_artifact_refs": decision_refs,
        "evidence_items": evidence_items,
        "missing_expected_artifacts": [item["artifact_id"] for item in evidence_items if not item["present"]],
        "freshness": _artifact_panel_freshness(evidence_items),
    }


def _console_artifact_root_ref(entry: dict[str, Any] | None) -> str | None:
    if entry is None or not entry.get("artifact_root"):
        return None
    return f"run:{entry.get('run_id')}:artifacts"


def _console_artifact_manifest_projection(
    manifest: dict[str, Any] | None,
    artifact_root: Path | None,
) -> dict[str, Any] | None:
    if not manifest:
        return None
    payload: dict[str, Any] = {}
    for key in ("case_id", "run_id", "workflow_id", "status"):
        if key in manifest:
            payload[key] = manifest[key]
    artifact_paths: dict[str, str] = {}
    for artifact_id, raw_ref in (manifest.get("artifact_paths") or {}).items():
        if not isinstance(artifact_id, str) or not isinstance(raw_ref, str):
            continue
        logical_ref = _trusted_root_relative_artifact_ref(artifact_root, raw_ref)
        if logical_ref is not None:
            artifact_paths[artifact_id] = logical_ref
    payload["artifact_paths"] = artifact_paths
    return payload


def _console_review_panel(
    entry: dict[str, Any] | None,
    *,
    review_store,
    review_store_root: str | Path,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any]:
    if entry is None:
        review = Review(status="not_available", review_required=False)
    else:
        review = Review.model_validate(
            _review_payload_for_run(
                entry,
                review_store=review_store,
                review_store_root=review_store_root,
                trusted_root=trusted_root,
            )
        )
    payload = project_review(review)
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


def _run_detail_payload(
    entry: dict[str, Any],
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any]:
    run_id = str(entry.get("run_id"))
    artifact_manifest = _load_manifest_for_entry(entry, trusted_root=trusted_root)
    review_packet = _load_review_packet_for_entry(entry, trusted_root=trusted_root)
    run_payload = dict(entry)
    run_payload.update(_console_selected_run(entry) or {})
    return {
        "run": run_payload,
        "events": [
            _event_from_history(run_id, item)
            for item in entry.get("status_history", [])
        ],
        "artifact_manifest": artifact_manifest,
        "artifacts": _artifact_refs_from_manifest(
            artifact_manifest,
            artifact_root=entry.get("artifact_root"),
            trusted_root=trusted_root,
        ),
        "review_packet": (
            review_packet.get("packet") if review_packet.get("present") else None
        ),
        "review_delivery": entry.get("review_delivery"),
    }


def _run_artifact_metadata_payload(
    entry: dict[str, Any],
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any]:
    artifact_root = entry.get("artifact_root")
    manifest = _load_manifest_for_entry(entry, trusted_root=trusted_root)
    return {
        "run_id": str(entry.get("run_id")),
        "artifact_root": artifact_root,
        "artifact_manifest": manifest,
        "artifact_paths": manifest.get("artifact_paths", {}) if manifest else {},
        "artifacts": _artifact_logical_metadata_from_manifest(
            manifest,
            artifact_root=artifact_root,
            trusted_root=trusted_root,
        ),
    }


def _load_manifest_for_entry(
    entry: dict[str, Any],
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any] | None:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        return None
    try:
        manifest = (
            trusted_root.read_bounded_json_object(
                "run_manifest.json",
                namespace="manifest",
            )
            if trusted_root is not None
            else read_bounded_json_object(
                artifact_root,
                "run_manifest.json",
                namespace="manifest",
            )
        )
    except ArtifactProjectionReadError as exc:
        if exc.code == "manifest_missing":
            return None
        raise
    artifact_paths = manifest.get("artifact_paths")
    if "artifact_paths" in manifest and not isinstance(artifact_paths, dict):
        raise ArtifactProjectionReadError(
            "manifest_invalid_shape",
            "Run manifest has an invalid artifact registry.",
            status_code=422,
        )
    for field, code, message in (
        ("run_id", "manifest_run_mismatch", "Run manifest does not match the selected run."),
        ("case_id", "manifest_case_mismatch", "Run manifest does not match the selected case."),
    ):
        expected_value = entry.get(field)
        if field not in manifest or expected_value is None:
            continue
        if manifest[field] is None or str(manifest[field]) != str(expected_value):
            raise ArtifactProjectionReadError(code, message, status_code=409)
    return manifest


def _load_review_packet_for_entry(
    entry: dict[str, Any],
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        return {"present": False, "run_id": entry.get("run_id"), "packet": None}
    root = Path(str(artifact_root)).expanduser().absolute()
    packet_json = root / "review_packet.json"
    markdown_path = root / "review_packet.md"
    try:
        packet = (
            trusted_root.read_bounded_json_object(
                "review_packet.json",
                namespace="review_packet",
            )
            if trusted_root is not None
            else read_bounded_json_object(
                root,
                "review_packet.json",
                namespace="review_packet",
            )
        )
    except ArtifactProjectionReadError as exc:
        if exc.code == "review_packet_missing":
            return {
                "present": False,
                "run_id": entry.get("run_id"),
                "packet": None,
                "markdown_path": str(markdown_path),
            }
        raise
    validate_review_packet_identity(packet, run_entry=entry)
    return {
        "present": True,
        "run_id": entry.get("run_id"),
        "packet": packet,
        "json_path": str(packet_json),
        "markdown_path": str(markdown_path),
    }


def _review_payload_for_run(
    entry: dict[str, Any],
    *,
    review_store,
    review_store_root: str | Path,
    trusted_root: TrustedArtifactRoot | None = None,
) -> dict[str, Any]:
    review_packet = _load_review_packet_for_entry(
        entry,
        trusted_root=trusted_root,
    )
    return build_review_snapshot(
        review_store=review_store,
        run_entry=entry,
        review_packet_result=review_packet,
        review_store_root=review_store_root,
        decision_artifacts=_decision_artifacts_for_run(
            entry,
            trusted_root=trusted_root,
        ),
    )


def _list_review_payloads(
    *,
    registry_path: str | Path,
    review_store,
    review_store_root: str | Path,
    principal: Principal,
    operator_id: str | None,
    workspace_id: str | None,
) -> list[dict[str, Any]]:
    scoped_runs = [
        entry
        for entry in run_registry.list_runs(registry_path)
        if object_in_scope(principal, entry)
    ]
    runs = _filter_run_entries(
        scoped_runs,
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
    trusted_roots: dict[str, TrustedArtifactRoot] | None = None,
) -> list[dict[str, Any]]:
    del registry_path
    reviews: list[dict[str, Any]] = []
    seen_review_ids: set[str] = set()
    for entry in runs:
        run_id = str(entry.get("run_id"))
        trusted_root = (trusted_roots or {}).get(run_id)
        artifact_root = entry.get("artifact_root")
        if trusted_root is None and artifact_root:
            try:
                with TrustedArtifactRoot(
                    artifact_root,
                    namespace="review_packet",
                ) as pinned_root:
                    review_payload = _review_payload_for_run(
                        entry,
                        review_store=review_store,
                        review_store_root=review_store_root,
                        trusted_root=pinned_root,
                    )
            except ArtifactProjectionReadError as exc:
                if exc.code != "review_packet_missing":
                    raise
                review_payload = _review_payload_for_run(
                    entry,
                    review_store=review_store,
                    review_store_root=review_store_root,
                )
        else:
            review_payload = _review_payload_for_run(
                entry,
                review_store=review_store,
                review_store_root=review_store_root,
                trusted_root=trusted_root,
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
                "decision_artifacts": _browser_visible_decision_artifacts(
                    (review_payload.get("decision") or {}).get("artifacts", [])
                ),
                "review_required": review_payload.get("review_required", False),
                "reason_codes": list(review_payload.get("reason_codes", []) or []),
                "assigned_to": review_payload.get("assigned_to"),
                "created_at": review_payload.get("created_at"),
                "updated_at": review_payload.get("updated_at"),
                "selected": review_payload.get("run_id") == selected_run_id,
            }
        )
    return sorted(reviews, key=lambda item: item.get("updated_at") or "", reverse=True)


def _browser_visible_decision_artifacts(artifacts: Any) -> list[dict[str, Any]]:
    if not isinstance(artifacts, list):
        return []
    return [
        {
            key: artifact[key]
            for key in ("artifact_id", "label", "present")
            if isinstance(artifact, dict) and key in artifact
        }
        for artifact in artifacts
        if isinstance(artifact, dict)
    ]


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


def _decision_artifacts_for_run(
    entry: dict[str, Any],
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> list[dict[str, Any]]:
    artifact_root = entry.get("artifact_root")
    if not artifact_root:
        return []
    root = Path(str(artifact_root)).expanduser().absolute()
    return [
        ArtifactRef(
            artifact_id="review_decision",
            path=str(root / "review_decision.json"),
            label="review decision",
            present=_artifact_is_regular(
                root,
                "review_decision.json",
                trusted_root=trusted_root,
            ),
        ).model_dump(),
        ArtifactRef(
            artifact_id="review_decision_markdown",
            path=str(root / "review_decision.md"),
            label="review decision markdown",
            present=_artifact_is_regular(
                root,
                "review_decision.md",
                trusted_root=trusted_root,
            ),
        ).model_dump(),
    ]


def _identity_filter_options(runs: list[dict[str, Any]], *, field_name: str) -> list[str]:
    values = {
        value
        for entry in runs
        if (value := _entry_identity_value(entry, field_name)) is not None
    }
    return sorted(values)


def _artifact_refs_from_manifest(
    manifest: dict[str, Any] | None,
    *,
    artifact_root: str | Path | None = None,
    trusted_root: TrustedArtifactRoot | None = None,
) -> list[dict[str, Any]]:
    if manifest is None:
        return []
    artifact_paths = manifest.get("artifact_paths", {}) or {}
    root = (
        Path(str(artifact_root)).expanduser().absolute()
        if artifact_root is not None
        else None
    )
    artifacts = []
    for artifact_id, path in artifact_paths.items():
        relative_ref = _trusted_root_relative_artifact_ref(root, path)
        artifact_path = root / relative_ref if root is not None and relative_ref is not None else None
        artifact = ArtifactRef(
                artifact_id=str(artifact_id),
                label=str(artifact_id).replace("_", " "),
                path=str(artifact_path) if artifact_path is not None else None,
                present=(
                    _artifact_is_regular(
                        root,
                        relative_ref,
                        trusted_root=trusted_root,
                    )
                    if root is not None and relative_ref is not None
                    else False
                ),
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


def _trusted_root_relative_artifact_ref(
    artifact_root: Path | None,
    raw_ref: Any,
) -> str | None:
    if artifact_root is None or not isinstance(raw_ref, str) or not raw_ref.strip():
        return None
    candidate = Path(raw_ref).expanduser()
    if candidate.is_absolute():
        try:
            candidate = candidate.absolute().relative_to(artifact_root)
        except ValueError:
            return None
    raw_relative = str(candidate)
    if "/" in raw_relative or "\\" in raw_relative:
        return None
    if raw_relative in {"", ".", ".."} or ":" in raw_relative:
        return None
    return raw_relative


def _regular_artifact_metadata(
    artifact_root: Path,
    relative_ref: str,
    *,
    trusted_root: TrustedArtifactRoot | None = None,
):
    try:
        if trusted_root is not None:
            return trusted_root.stat_regular_artifact(relative_ref)
        return stat_regular_artifact(artifact_root, relative_ref)
    except ArtifactProjectionReadError:
        return None


def _artifact_is_regular(
    artifact_root: Path,
    relative_ref: str,
    *,
    trusted_root: TrustedArtifactRoot | None = None,
) -> bool:
    return _regular_artifact_metadata(
        artifact_root,
        relative_ref,
        trusted_root=trusted_root,
    ) is not None


def _artifact_logical_metadata_from_manifest(
    manifest: dict[str, Any] | None,
    *,
    artifact_root: str | Path | None = None,
    trusted_root: TrustedArtifactRoot | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            key: artifact[key]
            for key in ("artifact_id", "label", "present", "provenance", "category")
            if key in artifact
        }
        for artifact in _artifact_refs_from_manifest(
            manifest,
            artifact_root=artifact_root,
            trusted_root=trusted_root,
        )
    ]


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
    trusted_root: TrustedArtifactRoot | None = None,
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
        relative_ref = _console_artifact_ref(
            artifact_root,
            manifest_paths.get(spec["artifact_id"]),
            fallback_filename=spec["filename"],
        )
        metadata = (
            _regular_artifact_metadata(
                artifact_root,
                relative_ref,
                trusted_root=trusted_root,
            )
            if artifact_root is not None and relative_ref is not None
            else None
        )
        refs.append(
            {
                "artifact_id": spec["artifact_id"],
                "label": spec["label"],
                "category": category,
                "present": metadata is not None,
                "ref": relative_ref,
                "mtime": (
                    datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc).isoformat()
                    if metadata is not None
                    else None
                ),
            }
        )
    return refs


def _console_artifact_ref(
    artifact_root: Path | None,
    manifest_path: Any,
    *,
    fallback_filename: str,
) -> str | None:
    if manifest_path is not None:
        return _trusted_root_relative_artifact_ref(artifact_root, manifest_path)
    return fallback_filename if artifact_root is not None else None


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


def _load_batch_runner_module():
    import importlib.util

    module_path = Path(__file__).resolve().parents[3] / "benchmarks" / "runners" / "batch_runner.py"
    spec = importlib.util.spec_from_file_location("api_batch_runner", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load batch runner module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
