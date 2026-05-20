from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from reserving_workflow.runtime import run_registry
from reserving_workflow.schemas import ReservingCaseInput, RunArtifactManifest
from reserving_workflow.tools_cli._common import load_json, write_json

from .catalog import ToolCatalogError, ToolCommandCatalog
from .contracts import PipelineRunResult, PipelineStepSpec, StepRunResult, ToolPipelineSpec

_WHEN_PATTERN = re.compile(r"^steps\.([A-Za-z0-9_-]+)\.outputs\.status == '([^']+)'$")
_PATH_TEMPLATE_PATTERN = re.compile(r"^\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)\s*\}\}$")


class ToolRunnerError(RuntimeError):
    def __init__(self, message: str, *, category: str = "runner_error", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.category = category
        self.details = details or {}


class ToolPipelineRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        python_executable: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.src_root = (self.repo_root / "src").resolve()
        self.catalog = ToolCommandCatalog(python_executable=python_executable)
        base_env = dict(os.environ)
        if env:
            base_env.update(env)
        existing_pythonpath = base_env.get("PYTHONPATH")
        base_env["PYTHONPATH"] = (
            str(self.src_root) if not existing_pythonpath else os.pathsep.join([str(self.src_root), existing_pythonpath])
        )
        self.env = base_env

    def run(
        self,
        *,
        pipeline: ToolPipelineSpec,
        input_path: Path,
        artifact_root: Path | None = None,
        run_id: str | None = None,
    ) -> PipelineRunResult:
        run_id = run_id or _build_run_id()
        root = self._resolve_artifact_root(pipeline, artifact_root, run_id)
        logs_root = root / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        review_store_dir = root / "reviews"
        review_store_dir.mkdir(parents=True, exist_ok=True)
        registry_path = root / "run-registry.json"

        input_payload = ReservingCaseInput.model_validate(load_json(input_path))
        case_input_path = root / "case_input.json"
        shutil.copy2(input_path, case_input_path)

        manifest_path = root / "run_manifest.json"
        manifest = RunArtifactManifest(
            case_id=input_payload.case_id,
            run_id=run_id,
            artifact_root=str(root),
            artifact_paths={
                "case_input": _relative_to_root(case_input_path, root),
                "run_manifest": _relative_to_root(manifest_path, root),
            },
            created_by="tool-runner",
            metadata={
                "contract_version": pipeline.version,
                "pipeline_id": pipeline.pipelineId,
            },
        )
        write_json(manifest_path, manifest.model_dump(mode="json"))

        run_registry.record_run_event(
            registry_path=registry_path,
            task_id=pipeline.pipelineId,
            case_id=input_payload.case_id,
            run_id=run_id,
            status="running",
            artifact_root=str(root),
            summary=f"Pipeline {pipeline.pipelineId} started.",
            operator_params={"workflow_id": pipeline.pipelineId, "artifact_dir": str(root)},
            created_by="tool-runner",
            review_required=False,
            workflow_id=pipeline.pipelineId,
        )

        steps: list[StepRunResult] = []
        encountered_failure = False
        failed_step_id: str | None = None
        error_payload: dict[str, Any] | None = None

        for step in pipeline.steps:
            step_result = StepRunResult(
                step_id=step.id,
                tool_id=step.toolId,
                status="pending",
                when=step.when,
            )
            if encountered_failure:
                step_result.status = "skipped"
                step_result.skip_reason = f"upstream step failed: {failed_step_id}"
                steps.append(step_result)
                continue
            if step.when:
                matched, skip_reason = self._evaluate_when(step.when, steps)
                if not matched:
                    step_result.status = "skipped"
                    step_result.skip_reason = skip_reason
                    steps.append(step_result)
                    continue

            try:
                completed = self._run_step(
                    step=step,
                    artifact_root=root,
                    logs_root=logs_root,
                    manifest_path=manifest_path,
                    registry_path=registry_path,
                    review_store_dir=review_store_dir,
                    run_id=run_id,
                    prior_steps=steps,
                )
                steps.append(completed)
                self._merge_manifest_outputs(manifest_path, root, completed.outputs)
                self._sync_registry_status(
                    registry_path=registry_path,
                    pipeline=pipeline,
                    input_payload=input_payload,
                    run_id=run_id,
                    artifact_root=root,
                    steps=steps,
                    summary=f"Step {step.id} completed.",
                )
            except (ToolRunnerError, ToolCatalogError) as exc:
                encountered_failure = True
                failed_step_id = step.id
                step_result.status = "failed"
                if isinstance(exc, ToolRunnerError):
                    details = exc.details
                    if details.get("stdout_log_path"):
                        step_result.stdout_log_path = str(details["stdout_log_path"])
                    if details.get("stderr_log_path"):
                        step_result.stderr_log_path = str(details["stderr_log_path"])
                    if details.get("exit_code") is not None:
                        step_result.exit_code = int(details["exit_code"])
                    if isinstance(details.get("command"), list):
                        step_result.command = [str(item) for item in details["command"]]
                step_result.error = {
                    "category": exc.category if isinstance(exc, ToolRunnerError) else "validation_error",
                    "message": str(exc),
                    "details": exc.details if isinstance(exc, ToolRunnerError) else {},
                }
                steps.append(step_result)
                error_payload = {
                    "category": exc.category if isinstance(exc, ToolRunnerError) else "validation_error",
                    "message": str(exc),
                    "details": exc.details if isinstance(exc, ToolRunnerError) else {},
                }
                continue

        run_status = _derive_run_status(steps)
        final_status = "failed" if encountered_failure else run_status
        summary = (
            f"Pipeline failed at step {failed_step_id}."
            if encountered_failure
            else f"Pipeline {pipeline.pipelineId} finished with run status {run_status}."
        )
        run_registry.record_run_event(
            registry_path=registry_path,
            task_id=pipeline.pipelineId,
            case_id=input_payload.case_id,
            run_id=run_id,
            status=final_status,
            artifact_root=str(root),
            summary=summary,
            operator_params={"workflow_id": pipeline.pipelineId, "artifact_dir": str(root)},
            created_by="tool-runner",
            review_required=run_status == "needs_review",
            error_category=error_payload.get("category") if error_payload else None,
            errors=[error_payload.get("message")] if error_payload else None,
            workflow_id=pipeline.pipelineId,
        )

        return PipelineRunResult(
            ok=not encountered_failure,
            status="error" if encountered_failure else "ok",
            pipeline_id=pipeline.pipelineId,
            version=pipeline.version,
            run_id=run_id,
            artifact_root=str(root),
            command_log_root=str(logs_root),
            run_manifest_path=str(manifest_path),
            registry_path=str(registry_path),
            review_store_dir=str(review_store_dir),
            run_status="failed" if encountered_failure else run_status,
            steps=steps,
            failed_step_id=failed_step_id,
            error=error_payload,
        )

    def _resolve_artifact_root(self, pipeline: ToolPipelineSpec, artifact_root: Path | None, run_id: str) -> Path:
        configured = str(artifact_root or pipeline.artifactRoot).replace("{{run_id}}", run_id)
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = (self.repo_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _evaluate_when(self, expression: str, steps: list[StepRunResult]) -> tuple[bool, str]:
        match = _WHEN_PATTERN.fullmatch(expression.strip())
        if match is None:
            raise ToolRunnerError(
                f"Unsupported when expression: {expression}",
                category="validation_error",
                details={"expression": expression},
            )
        step_id, expected_value = match.groups()
        prior_step = next((item for item in steps if item.step_id == step_id), None)
        if prior_step is None:
            raise ToolRunnerError(
                f"when expression references unknown step: {step_id}",
                category="validation_error",
                details={"expression": expression},
            )
        actual_value = prior_step.output_values.get("status")
        if actual_value == expected_value:
            return True, ""
        return False, f"condition not met: expected {step_id}.outputs.status == {expected_value!r}, found {actual_value!r}"

    def _run_step(
        self,
        *,
        step: PipelineStepSpec,
        artifact_root: Path,
        logs_root: Path,
        manifest_path: Path,
        registry_path: Path,
        review_store_dir: Path,
        run_id: str,
        prior_steps: list[StepRunResult],
    ) -> StepRunResult:
        resolved_inputs = self._resolve_io_paths(step.inputs, artifact_root, allow_missing=False, prior_steps=prior_steps)
        resolved_outputs = self._resolve_io_paths(step.outputs, artifact_root, allow_missing=True, prior_steps=prior_steps)
        command = self.catalog.build_command(
            step=step,
            artifact_root=artifact_root,
            resolved_inputs=resolved_inputs,
            resolved_outputs=resolved_outputs,
            run_manifest_path=manifest_path,
            registry_path=registry_path,
            run_id=run_id,
            review_store_dir=review_store_dir,
        )
        stdout_log = logs_root / f"{step.id}.stdout.log"
        stderr_log = logs_root / f"{step.id}.stderr.log"
        completed = subprocess.run(
            command,
            cwd=self.repo_root,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        stdout_log.write_text(completed.stdout, encoding="utf-8")
        stderr_log.write_text(completed.stderr, encoding="utf-8")

        result = StepRunResult(
            step_id=step.id,
            tool_id=step.toolId,
            status="completed" if completed.returncode == 0 else "failed",
            command=command,
            inputs={key: str(value) for key, value in resolved_inputs.items()},
            outputs={key: str(value) for key, value in resolved_outputs.items()},
            stdout_log_path=str(stdout_log),
            stderr_log_path=str(stderr_log),
            exit_code=completed.returncode,
            when=step.when,
        )

        try:
            payload = _parse_json_payload(completed.stdout)
        except ToolRunnerError as exc:
            details = {
                "step_id": step.id,
                "tool_id": step.toolId,
                "command": command,
                "exit_code": completed.returncode,
                "stdout_log_path": str(stdout_log),
                "stderr_log_path": str(stderr_log),
                **exc.details,
            }
            raise ToolRunnerError(str(exc), category=exc.category, details=details) from exc
        if completed.returncode != 0:
            result.error = {
                "category": payload.get("error_category") if isinstance(payload, dict) else "execution_error",
                "message": payload.get("message") if isinstance(payload, dict) else f"Command exited with code {completed.returncode}",
                "payload": payload,
            }
            raise ToolRunnerError(
                result.error["message"],
                category=result.error["category"] or "execution_error",
                details={
                    "step_id": step.id,
                    "tool_id": step.toolId,
                    "command": command,
                    "exit_code": completed.returncode,
                    "stdout_log_path": str(stdout_log),
                    "stderr_log_path": str(stderr_log),
                    "payload": payload,
                },
            )

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ToolRunnerError(
                f"Tool {step.toolId} returned unrecognized success payload.",
                category="execution_error",
                details={
                    "step_id": step.id,
                    "tool_id": step.toolId,
                    "payload": payload,
                    "stdout_log_path": str(stdout_log),
                    "stderr_log_path": str(stderr_log),
                },
            )

        payload_outputs = payload.get("outputs", {}) if isinstance(payload, dict) else {}
        if isinstance(payload_outputs, dict):
            result.outputs = self._validate_step_outputs(
                step=step,
                declared_outputs=resolved_outputs,
                payload_outputs=payload_outputs,
                artifact_root=artifact_root,
            )
        else:
            result.outputs = self._validate_step_outputs(
                step=step,
                declared_outputs=resolved_outputs,
                payload_outputs={},
                artifact_root=artifact_root,
            )
        result.output_values = self._load_output_values(result.outputs)
        return result

    def _resolve_io_paths(
        self,
        mapping: dict[str, str],
        artifact_root: Path,
        *,
        allow_missing: bool,
        prior_steps: list[StepRunResult],
    ) -> dict[str, Path]:
        resolved: dict[str, Path] = {}
        for key, relative_path in mapping.items():
            path = self._resolve_pipeline_path_template(relative_path, prior_steps=prior_steps, artifact_root=artifact_root)
            if not allow_missing and not path.exists():
                raise ToolRunnerError(
                    f"Input artifact does not exist for '{key}': {path}",
                    category="io_error",
                    details={"artifact_id": key, "path": str(path)},
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            resolved[key] = path
        return resolved

    def _resolve_pipeline_path_template(
        self,
        value: str,
        *,
        prior_steps: list[StepRunResult],
        artifact_root: Path,
    ) -> Path:
        template_match = _PATH_TEMPLATE_PATTERN.fullmatch(value.strip())
        if template_match is None:
            return _resolve_artifact_path(artifact_root, value)
        step_id, output_key = template_match.groups()
        prior_step = next((item for item in prior_steps if item.step_id == step_id), None)
        if prior_step is None:
            raise ToolRunnerError(
                f"Artifact template references unknown step: {step_id}",
                category="validation_error",
                details={"template": value},
            )
        output_value = prior_step.outputs.get(output_key)
        if output_value is None:
            raise ToolRunnerError(
                f"Artifact template references missing output '{output_key}' on step '{step_id}'.",
                category="validation_error",
                details={"template": value, "step_id": step_id, "output_key": output_key},
            )
        return _resolve_artifact_path(artifact_root, output_value)

    def _validate_step_outputs(
        self,
        *,
        step: PipelineStepSpec,
        declared_outputs: dict[str, Path],
        payload_outputs: dict[Any, Any],
        artifact_root: Path,
    ) -> dict[str, str]:
        missing = [artifact_id for artifact_id in declared_outputs if artifact_id not in payload_outputs]
        if missing:
            raise ToolRunnerError(
                f"Tool {step.toolId} did not report required outputs: {', '.join(missing)}",
                category="execution_error",
                details={"step_id": step.id, "tool_id": step.toolId, "missing_outputs": missing},
            )

        validated: dict[str, str] = {key: str(path) for key, path in declared_outputs.items()}
        for raw_key, raw_path in payload_outputs.items():
            key = str(raw_key)
            if raw_path is None:
                raise ToolRunnerError(
                    f"Tool {step.toolId} returned null output path for '{key}'.",
                    category="execution_error",
                    details={"step_id": step.id, "tool_id": step.toolId, "artifact_id": key},
                )
            validated[key] = str(_resolve_artifact_path(artifact_root, str(raw_path)))

        absent = [artifact_id for artifact_id, path in validated.items() if not Path(path).exists()]
        if absent:
            raise ToolRunnerError(
                f"Tool {step.toolId} did not produce required output artifacts: {', '.join(absent)}",
                category="execution_error",
                details={"step_id": step.id, "tool_id": step.toolId, "missing_artifacts": absent},
            )
        return validated

    def _merge_manifest_outputs(self, manifest_path: Path, artifact_root: Path, outputs: dict[str, str]) -> None:
        manifest_payload = load_json(manifest_path)
        artifact_paths = dict(manifest_payload.get("artifact_paths", {}) or {})
        for artifact_id, output_path in outputs.items():
            artifact_paths[artifact_id] = _relative_to_root(Path(output_path), artifact_root)
        manifest_payload["artifact_paths"] = artifact_paths
        write_json(manifest_path, manifest_payload)

    def _load_output_values(self, outputs: dict[str, str]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for output_path in outputs.values():
            path = Path(output_path)
            if path.suffix.lower() != ".json" or not path.exists():
                continue
            try:
                payload = load_json(path)
            except Exception:
                continue
            if isinstance(payload, dict):
                values.update({key: value for key, value in payload.items() if key not in values})
        return values

    def _sync_registry_status(
        self,
        *,
        registry_path: Path,
        pipeline: ToolPipelineSpec,
        input_payload: ReservingCaseInput,
        run_id: str,
        artifact_root: Path,
        steps: list[StepRunResult],
        summary: str,
    ) -> None:
        run_status = _derive_run_status(steps)
        run_registry.record_run_event(
            registry_path=registry_path,
            task_id=pipeline.pipelineId,
            case_id=input_payload.case_id,
            run_id=run_id,
            status=run_status,
            artifact_root=str(artifact_root),
            summary=summary,
            operator_params={"workflow_id": pipeline.pipelineId, "artifact_dir": str(artifact_root)},
            created_by="tool-runner",
            review_required=run_status == "needs_review",
            workflow_id=pipeline.pipelineId,
        )


def load_pipeline_spec(path: str | Path) -> ToolPipelineSpec:
    try:
        payload = yaml.safe_load(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ToolRunnerError(
            "Pipeline YAML was not valid.",
            category="validation_error",
            details={"path": str(path), "yaml_error": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise ToolRunnerError("Pipeline YAML must decode to an object.", category="validation_error")
    try:
        return ToolPipelineSpec.model_validate(payload)
    except ValidationError as exc:
        raise ToolRunnerError(
            "Pipeline YAML failed schema validation.",
            category="validation_error",
            details={"path": str(path), "validation_errors": exc.errors()},
        ) from exc



def run_pipeline(
    *,
    repo_root: Path,
    pipeline_path: str | Path,
    input_path: str | Path,
    artifact_root: str | Path | None = None,
    run_id: str | None = None,
) -> PipelineRunResult:
    runner = ToolPipelineRunner(repo_root=repo_root)
    pipeline = load_pipeline_spec(pipeline_path)
    return runner.run(
        pipeline=pipeline,
        input_path=Path(input_path).expanduser().resolve(),
        artifact_root=Path(artifact_root).expanduser() if artifact_root is not None else None,
        run_id=run_id,
    )



def _build_run_id() -> str:
    return f"pipeline-{uuid.uuid4().hex[:12]}"



def _resolve_artifact_path(artifact_root: Path, relative_path: str) -> Path:
    candidate = (artifact_root / relative_path).resolve()
    try:
        candidate.relative_to(artifact_root.resolve())
    except ValueError as exc:
        raise ToolRunnerError(
            f"Artifact path escapes artifact root: {relative_path}",
            category="validation_error",
            details={"artifact_root": str(artifact_root), "path": relative_path},
        ) from exc
    return candidate



def _parse_json_payload(stdout: str) -> dict[str, Any] | list[Any] | None:
    content = stdout.strip()
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ToolRunnerError(
            "Tool stdout was not valid JSON.",
            category="execution_error",
            details={"stdout": stdout, "decode_error": str(exc)},
        ) from exc
    return parsed



def _relative_to_root(path: Path, artifact_root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(artifact_root.resolve()))
    except ValueError:
        return str(resolved)



def _derive_run_status(steps: list[StepRunResult]) -> str:
    if any(step.status == "failed" for step in steps):
        return "failed"
    for step in reversed(steps):
        if step.output_values.get("status") == "review_required":
            return "needs_review"
    return "completed"
