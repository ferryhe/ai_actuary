from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from reserving_workflow.schemas import (
    ConstitutionCheckResult,
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    RunArtifactManifest,
)
from reserving_workflow.tool_runner.contracts import ToolPipelineSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "export_contract_compat_manifest.py"
COMPAT_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "tool_contracts" / "actuarial_reserving_v1_compat_manifest.json"
)
GOLDEN_RUN_DIR = REPO_ROOT / "tests" / "fixtures" / "tool_contracts" / "golden_run"
PIPELINE_PATH = REPO_ROOT / "tests" / "fixtures" / "tool_pipelines" / "actuarial_reserving_review.yaml"

EXPECTED_TOOL_IDS = [
    "chainladder-calc",
    "narrative-draft",
    "constitution-check",
    "review-generator",
    "replay-run",
    "repeatability-check",
    "report-export",
]
EXPECTED_CANONICAL_PIPELINE_TOOL_IDS = [
    "chainladder-calc",
    "narrative-draft",
    "constitution-check",
    "review-generator",
    "report-export",
]
EXPECTED_ARTIFACT_IDS = [
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
EXPECTED_SCHEMA_NAMES = [
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


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_file_entries_are_current(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        path = REPO_ROOT / entry["path"]
        assert path.exists(), entry["path"]
        assert entry["sha256"] == _sha256(path), entry["path"]
        assert entry["bytes"] == path.stat().st_size, entry["path"]


def test_compat_manifest_is_reproducible_and_checksum_pinned() -> None:
    output_dir = REPO_ROOT / "tmp" / "contract-compat-test"
    output = output_dir / "compat-manifest.json"
    shutil.rmtree(output_dir, ignore_errors=True)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--output", str(output.relative_to(REPO_ROOT))],
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
        )
    finally:
        if output.exists():
            generated = _load_json(output)
        else:
            generated = None
        shutil.rmtree(output_dir, ignore_errors=True)

    payload = json.loads(completed.stdout)
    assert payload == {
        "contractVersion": "actuarial-reserving.v1",
        "ok": True,
        "output": str(output.resolve()),
    }
    assert generated == _load_json(COMPAT_MANIFEST_PATH)


def test_compat_manifest_export_rejects_paths_outside_repo(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--output", str(tmp_path / "compat-manifest.json")],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert completed.returncode != 0
    assert "--output must resolve to a file inside repository root" in completed.stderr


def test_compat_manifest_export_rejects_directory_output() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--output", "tests"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert completed.returncode != 0
    assert "--output must be a file path, not a directory" in completed.stderr


def test_compat_manifest_declares_contract_significant_schema_fixture_and_artifact_sets() -> None:
    manifest = _load_json(COMPAT_MANIFEST_PATH)

    assert manifest["contractVersion"] == "actuarial-reserving.v1"
    assert set(manifest["toolIds"]) == set(EXPECTED_TOOL_IDS)
    assert set(manifest["requiredArtifactIds"]) == set(EXPECTED_ARTIFACT_IDS)
    assert {Path(entry["path"]).name for entry in manifest["schemaFiles"]} == set(EXPECTED_SCHEMA_NAMES)
    assert {Path(entry["path"]).name for entry in manifest["fixtureFiles"]} == {
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
    }

    _assert_file_entries_are_current(manifest["schemaFiles"])
    _assert_file_entries_are_current(manifest["fixtureFiles"])


def test_golden_run_artifacts_match_compat_manifest_and_models() -> None:
    manifest = _load_json(COMPAT_MANIFEST_PATH)
    golden = manifest["goldenRun"]
    run_manifest = RunArtifactManifest.model_validate(_load_json(GOLDEN_RUN_DIR / "run_manifest.json"))

    assert golden["caseId"] == "golden-raa"
    assert golden["runId"] == run_manifest.run_id == "golden-raa-20260520T120000Z"
    assert golden["artifactRoot"] == "."
    assert golden["artifactPaths"] == dict(sorted(run_manifest.artifact_paths.items()))
    assert set(golden["artifactPaths"]) == set(EXPECTED_ARTIFACT_IDS)

    ReservingCaseInput.model_validate(_load_json(GOLDEN_RUN_DIR / golden["artifactPaths"]["case_input"]))
    DeterministicReserveResult.model_validate(
        _load_json(GOLDEN_RUN_DIR / golden["artifactPaths"]["deterministic_result"])
    )
    NarrativeDraft.model_validate(_load_json(GOLDEN_RUN_DIR / golden["artifactPaths"]["narrative_draft"]))
    ConstitutionCheckResult.model_validate(
        _load_json(GOLDEN_RUN_DIR / golden["artifactPaths"]["constitution_check"])
    )

    missing = [
        artifact_id
        for artifact_id, relative_path in golden["artifactPaths"].items()
        if not (GOLDEN_RUN_DIR / relative_path).exists()
    ]
    assert missing == []


def test_canonical_pipeline_contract_matches_compat_manifest() -> None:
    manifest = _load_json(COMPAT_MANIFEST_PATH)
    canonical = manifest["canonicalPipeline"]
    pipeline_payload = yaml.safe_load(PIPELINE_PATH.read_text(encoding="utf-8"))
    pipeline = ToolPipelineSpec.model_validate(pipeline_payload)

    assert canonical["path"] == "tests/fixtures/tool_pipelines/actuarial_reserving_review.yaml"
    assert canonical["pipelineId"] == pipeline.pipelineId == "actuarial-reserving-review"
    assert canonical["version"] == pipeline.version == "actuarial-reserving.v1"
    assert canonical["stepToolIds"] == EXPECTED_CANONICAL_PIPELINE_TOOL_IDS
    assert canonical["stepToolIds"] == [step.toolId for step in pipeline.steps]
    assert canonical["stepArtifactOutputs"] == {
        step.id: dict(sorted(step.outputs.items())) for step in pipeline.steps
    }
