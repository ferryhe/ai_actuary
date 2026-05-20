#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from reserving_workflow.tools_cli import (
    chainladder_calc,
    constitution_check,
    narrative_draft,
    repeatability_check,
    replay_run,
    report_export,
    review_generator,
)

CONTRACT_VERSION = "actuarial-reserving.v1"
SCHEMA_DIR = Path("schemas/actuarial-reserving/v1")
FIXTURE_DIR = Path("tests/fixtures/tool_contracts/golden_run")
PIPELINE_PATH = Path("tests/fixtures/tool_pipelines/actuarial_reserving_review.yaml")
DEFAULT_OUTPUT = Path("tests/fixtures/tool_contracts/actuarial_reserving_v1_compat_manifest.json")

TOOL_ID_MODULES = [
    chainladder_calc,
    narrative_draft,
    constitution_check,
    review_generator,
    replay_run,
    repeatability_check,
    report_export,
]

REQUIRED_ARTIFACT_IDS = [
    "case_input",
    "deterministic_result",
    "narrative_draft",
    "constitution_check",
    "run_manifest",
    "review_packet",
    "review_packet_markdown",
    "operator_handoff",
    "reserve_summary_json",
    "reserve_summary_markdown",
]

SCHEMA_FILES = [
    "ConstitutionCheckResult.schema.json",
    "DeterministicReserveResult.schema.json",
    "NarrativeDraft.schema.json",
    "ReservingCaseInput.schema.json",
    "Review.schema.json",
    "Run.schema.json",
    "RunArtifactManifest.schema.json",
    "RunEvent.schema.json",
    "ToolInvocation.schema.json",
    "Workflow.schema.json",
]

FIXTURE_FILES = [
    "case_input.json",
    "constitution_check.json",
    "deterministic_result.json",
    "narrative_draft.json",
    "operator_handoff.md",
    "reserve_summary.json",
    "reserve_summary.md",
    "review_packet.json",
    "review_packet.md",
    "run_manifest.json",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_entry(relative_path: Path) -> dict[str, Any]:
    path = REPO_ROOT / relative_path
    return {
        "path": relative_path.as_posix(),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _load_json(relative_path: Path) -> dict[str, Any]:
    return json.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8"))


def build_manifest() -> dict[str, Any]:
    run_manifest = _load_json(FIXTURE_DIR / "run_manifest.json")
    pipeline = yaml.safe_load((REPO_ROOT / PIPELINE_PATH).read_text(encoding="utf-8"))
    pipeline_steps = pipeline.get("steps", [])

    return {
        "contractVersion": CONTRACT_VERSION,
        "description": "Portable checksum manifest for ai_actuary/ai_interface compatibility tests. Consumers should treat listed schema, fixture, artifact ID, and canonical pipeline changes as contract-significant.",
        "toolIds": [module.TOOL_ID for module in TOOL_ID_MODULES],
        "requiredArtifactIds": REQUIRED_ARTIFACT_IDS,
        "schemaFiles": [_file_entry(SCHEMA_DIR / name) for name in SCHEMA_FILES],
        "fixtureFiles": [_file_entry(FIXTURE_DIR / name) for name in FIXTURE_FILES],
        "goldenRun": {
            "fixtureRoot": FIXTURE_DIR.as_posix(),
            "caseId": run_manifest["case_id"],
            "runId": run_manifest["run_id"],
            "artifactRoot": run_manifest["artifact_root"],
            "artifactPaths": {key: run_manifest["artifact_paths"][key] for key in sorted(run_manifest["artifact_paths"])},
        },
        "canonicalPipeline": {
            "path": PIPELINE_PATH.as_posix(),
            "pipelineId": pipeline["pipelineId"],
            "version": pipeline["version"],
            "stepToolIds": [step["toolId"] for step in pipeline_steps],
            "stepArtifactOutputs": {
                step["id"]: dict(sorted((step.get("outputs") or {}).items())) for step in pipeline_steps
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the actuarial-reserving v1 compatibility manifest.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output path for the compatibility manifest.")
    args = parser.parse_args()

    manifest = build_manifest()
    output_path = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    output_path = output_path.resolve()
    if output_path == REPO_ROOT or REPO_ROOT not in output_path.parents:
        raise SystemExit(f"--output must resolve to a file inside repository root: {REPO_ROOT}")
    if output_path.exists() and output_path.is_dir():
        raise SystemExit(f"--output must be a file path, not a directory: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output_path), "contractVersion": CONTRACT_VERSION}, sort_keys=True))


if __name__ == "__main__":
    main()
