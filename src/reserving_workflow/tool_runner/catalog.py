from __future__ import annotations

import sys
from pathlib import Path

from .contracts import PipelineStepSpec


class ToolCatalogError(ValueError):
    pass


_MODULE_BY_TOOL_ID = {
    "chainladder-calc": "reserving_workflow.tools_cli.chainladder_calc",
    "narrative-draft": "reserving_workflow.tools_cli.narrative_draft",
    "constitution-check": "reserving_workflow.tools_cli.constitution_check",
    "review-generator": "reserving_workflow.tools_cli.review_generator",
    "report-export": "reserving_workflow.tools_cli.report_export",
}


class ToolCommandCatalog:
    def __init__(self, *, python_executable: str | None = None):
        self.python_executable = python_executable or sys.executable

    def build_command(
        self,
        *,
        step: PipelineStepSpec,
        artifact_root: Path,
        resolved_inputs: dict[str, Path],
        resolved_outputs: dict[str, Path],
        run_manifest_path: Path,
        registry_path: Path,
        run_id: str,
        review_store_dir: Path,
    ) -> list[str]:
        module_name = _MODULE_BY_TOOL_ID.get(step.toolId)
        if module_name is None:
            raise ToolCatalogError(f"Unsupported tool id for local runner: {step.toolId}")

        command = [self.python_executable, "-m", module_name]
        if step.toolId == "chainladder-calc":
            return command + [
                "--case-input",
                str(_require_input(resolved_inputs, "case_input")),
                "--output",
                str(_require_output(resolved_outputs, "deterministic_result")),
            ]
        if step.toolId == "narrative-draft":
            return command + [
                "--case-input",
                str(_require_input(resolved_inputs, "case_input")),
                "--deterministic-result",
                str(_require_input(resolved_inputs, "deterministic_result")),
                "--output",
                str(_require_output(resolved_outputs, "narrative_draft")),
            ]
        if step.toolId == "constitution-check":
            return command + [
                "--case-input",
                str(_require_input(resolved_inputs, "case_input")),
                "--deterministic-result",
                str(_require_input(resolved_inputs, "deterministic_result")),
                "--narrative-draft",
                str(_require_input(resolved_inputs, "narrative_draft")),
                "--run-manifest",
                str(run_manifest_path),
                "--output",
                str(_require_output(resolved_outputs, "constitution_check")),
            ]
        if step.toolId == "review-generator":
            return command + [
                "--constitution-check",
                str(_require_input(resolved_inputs, "constitution_check")),
                "--deterministic-result",
                str(_require_input(resolved_inputs, "deterministic_result")),
                "--narrative-draft",
                str(_require_input(resolved_inputs, "narrative_draft")),
                "--run-manifest",
                str(run_manifest_path),
                "--output-dir",
                str(artifact_root),
            ]
        if step.toolId == "report-export":
            return command + [
                "--registry-path",
                str(registry_path),
                "--run-id",
                run_id,
                "--review-store-dir",
                str(review_store_dir),
                "--output-dir",
                str(artifact_root),
            ]
        raise ToolCatalogError(f"Unsupported tool id for local runner: {step.toolId}")


def _require_input(resolved_inputs: dict[str, Path], key: str) -> Path:
    try:
        return resolved_inputs[key]
    except KeyError as exc:
        raise ToolCatalogError(f"Missing required input '{key}'") from exc



def _require_output(resolved_outputs: dict[str, Path], key: str) -> Path:
    try:
        return resolved_outputs[key]
    except KeyError as exc:
        raise ToolCatalogError(f"Missing required output '{key}'") from exc
