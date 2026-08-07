"""Import an externally evaluated code candidate into the local review queue.

The importer deliberately treats a benchmark result as evidence for a human
review, never as permission to promote or execute the generated component.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from reserving_workflow.review.store import build_review_id
from reserving_workflow.storage.local import (
    LocalReviewStore,
    LocalRunStore,
    RunNotFoundError,
    resolve_artifact_root,
)


EXPECTED_SCHEMA_VERSION = "1.0.0"
EXPECTED_GATES = (
    "public",
    "hidden_actuarial",
    "prompt_injection",
    "exfiltration",
    "reproducibility",
)
EXPECTED_ISOLATION_EVIDENCE = (
    "capabilities_dropped",
    "network_disabled",
    "no_new_privileges",
    "non_root_user",
    "read_only_root",
    "repository_not_mounted",
    "secrets_absent",
)
REVIEW_REASONS = (
    "human_code_review_required",
    "actuarial_rule_review_required",
    "production_promotion_not_approved",
)
SAFE_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class BenchmarkImportError(ValueError):
    """Raised when benchmark evidence is not safe or complete enough to import."""


def import_benchmark_review(
    *,
    manifest_path: str | Path,
    registry_path: str | Path,
    artifact_root: str | Path,
    review_store_root: str | Path,
    operator_id: str = "local-actuary",
    workspace_id: str = "default-workspace",
    case_id: str | None = None,
) -> dict[str, Any]:
    """Verify and snapshot a benchmark result as an AI Actuary review item."""

    source_manifest_path = Path(manifest_path).expanduser()
    if source_manifest_path.name != "sandbox_evaluation_manifest.json":
        raise BenchmarkImportError("manifest must be named sandbox_evaluation_manifest.json")
    try:
        source_manifest_path = source_manifest_path.resolve(strict=True)
    except OSError as exc:
        raise BenchmarkImportError(f"benchmark manifest is not readable: {source_manifest_path}") from exc
    if not source_manifest_path.is_file() or source_manifest_path.is_symlink():
        raise BenchmarkImportError("benchmark manifest must be a regular, non-symlink file")

    repository_root = _find_benchmark_repository(source_manifest_path)
    manifest_bytes = source_manifest_path.read_bytes()
    manifest = _load_json_object(manifest_bytes, label="sandbox evaluation manifest")
    verified = _verify_evaluation_manifest(manifest, repository_root=repository_root)

    evaluation_id = _safe_component(manifest.get("evaluation_id"), field_name="evaluation_id")
    run_id = _safe_component(f"benchmark-{evaluation_id}", field_name="run_id")
    resolved_case_id = _safe_component(
        case_id or _default_case_id(verified["source_run_manifest"]),
        field_name="case_id",
    )
    manifest_sha256 = _sha256(manifest_bytes)

    registry = LocalRunStore(registry_path)
    reviews = LocalReviewStore(review_store_root)
    try:
        existing_run = registry.get_run(run_id)
    except RunNotFoundError:
        existing_run = None
    if existing_run is not None:
        recorded_sha256 = (existing_run.get("operator_params") or {}).get(
            "benchmark_evaluation_manifest_sha256"
        )
        if recorded_sha256 != manifest_sha256:
            raise BenchmarkImportError(
                f"run id {run_id!r} already exists for different benchmark evidence"
            )
        return {
            "status": "already_imported",
            "case_id": existing_run.get("case_id"),
            "run_id": run_id,
            "review_id": build_review_id(run_id),
            "artifact_root": existing_run.get("artifact_root"),
            "machine_disposition": manifest["machine_disposition"],
            "promotion_eligible": False,
        }

    root = resolve_artifact_root(artifact_root)
    destination = (root / resolved_case_id / run_id).resolve()
    _require_within(destination, root, label="import destination")
    if destination.exists():
        raise BenchmarkImportError(
            f"import destination already exists without a matching registry run: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=destination.parent))
    try:
        artifact_paths = _write_snapshot(
            staging,
            final_destination=destination,
            case_id=resolved_case_id,
            run_id=run_id,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
            verified=verified,
            repository_root=repository_root,
            operator_id=operator_id,
            workspace_id=workspace_id,
        )
        staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    summary = (
        "MiniMax-M3 C4 component candidate passed automated sandbox gates; "
        "human code and actuarial review are required before promotion."
    )
    operator_params = {
        "source_kind": "benchmark_component_candidate",
        "component_id": "experience-study-c4",
        "benchmark_evaluation_id": evaluation_id,
        "benchmark_evaluation_manifest_sha256": manifest_sha256,
        "benchmark_source_run_id": manifest["source"]["run_id"],
        "benchmark_pack_id": manifest["source"]["pack_id"],
        "artifact_dir": str(destination),
        "operator_id": operator_id,
        "workspace_id": workspace_id,
    }
    run_entry = registry.create_run(
        task_id="benchmark-review-codegen-c4-v1",
        case_id=resolved_case_id,
        run_id=run_id,
        status="needs_review",
        artifact_root=str(destination),
        summary=summary,
        operator_params=operator_params,
        created_by=operator_id,
        operator_id=operator_id,
        workspace_id=workspace_id,
        review_required=True,
        errors=list(REVIEW_REASONS),
        event_type="run.needs_review",
        event_payload={
            "source_kind": "benchmark_component_candidate",
            "machine_disposition": "ready_for_human_review",
            "promotion_eligible": False,
        },
    )

    review_packet = _read_json(destination / artifact_paths["review_packet"])
    review_id = build_review_id(run_id)
    review_record = reviews.create_review(
        review_id=review_id,
        run_id=run_id,
        case_id=resolved_case_id,
        status="review_required",
        reason_codes=list(REVIEW_REASONS),
        assigned_to=operator_id,
        workspace_id=workspace_id,
        packet=review_packet,
    )
    return {
        "status": "imported",
        "case_id": resolved_case_id,
        "run_id": run_id,
        "review_id": review_id,
        "review_status": review_record["status"],
        "run_status": run_entry["status"],
        "artifact_root": str(destination),
        "machine_disposition": manifest["machine_disposition"],
        "promotion_eligible": False,
    }


def _verify_evaluation_manifest(
    manifest: dict[str, Any], *, repository_root: Path
) -> dict[str, Any]:
    _require_equal(manifest.get("schema_version"), EXPECTED_SCHEMA_VERSION, "schema_version")
    _safe_component(manifest.get("evaluation_id"), field_name="evaluation_id")
    _require_equal(
        manifest.get("machine_disposition"),
        "ready_for_human_review",
        "machine_disposition",
    )
    if manifest.get("promotion_eligible") is not False:
        raise BenchmarkImportError("promotion_eligible must be false for a review import")
    if manifest.get("failure_codes") != []:
        raise BenchmarkImportError("failure_codes must be an empty list")

    static_scan = _require_object(manifest, "static_scan")
    _require_equal(static_scan.get("status"), "passed", "static_scan.status")
    if static_scan.get("findings") != []:
        raise BenchmarkImportError("static_scan.findings must be empty")

    policy = _require_object(manifest, "policy")
    _require_equal(policy.get("configuration_state"), "frozen", "policy.configuration_state")
    _require_equal(policy.get("isolation_level"), "strong", "policy.isolation_level")
    _require_equal(policy.get("isolation_backend"), "docker_linux", "policy.isolation_backend")
    image_digest = policy.get("image_digest")
    if not isinstance(image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise BenchmarkImportError("policy.image_digest must be a pinned sha256 digest")

    gates = _require_object(manifest, "test_gates")
    for gate_name in EXPECTED_GATES:
        gate = _require_object(gates, gate_name, prefix="test_gates")
        _require_equal(gate.get("status"), "passed", f"test_gates.{gate_name}.status")

    execution = _require_object(manifest, "execution")
    if execution.get("attempted") is not True:
        raise BenchmarkImportError("execution.attempted must be true")
    _require_equal(execution.get("result"), "passed", "execution.result")
    _require_equal(execution.get("exit_code"), 0, "execution.exit_code")
    if execution.get("timed_out") is not False:
        raise BenchmarkImportError("execution.timed_out must be false")
    isolation = _require_object(execution, "isolation_evidence", prefix="execution")
    for evidence_name in EXPECTED_ISOLATION_EVIDENCE:
        if isolation.get(evidence_name) is not True:
            raise BenchmarkImportError(
                f"execution.isolation_evidence.{evidence_name} must be true"
            )

    source = _require_object(manifest, "source")
    source_run_path = _verified_repo_file(
        repository_root,
        source.get("run_manifest_path"),
        expected_sha256=source.get("run_manifest_sha256"),
        label="source run manifest",
    )
    extracted_submission_path = _verified_repo_file(
        repository_root,
        source.get("extracted_submission_path"),
        expected_sha256=source.get("extracted_submission_sha256"),
        expected_bytes=source.get("extracted_submission_bytes"),
        label="extracted submission",
    )
    source_run_manifest = _read_json(source_run_path)
    _require_equal(
        source_run_manifest.get("run_id"), source.get("run_id"), "source run id linkage"
    )
    _require_equal(
        (source_run_manifest.get("prompt") or {}).get("pack_id"),
        source.get("pack_id"),
        "source pack linkage",
    )
    _require_equal(
        (source_run_manifest.get("extracted_output") or {}).get("sha256"),
        source.get("extracted_submission_sha256"),
        "source extracted output linkage",
    )

    policy_path = _verified_repo_file(
        repository_root,
        policy.get("path"),
        expected_sha256=policy.get("sha256"),
        label="sandbox policy",
    )
    stdout_path = _verified_repo_file(
        repository_root,
        execution.get("stdout_path"),
        expected_sha256=execution.get("stdout_sha256"),
        label="execution stdout",
    )
    stderr_path = _verified_repo_file(
        repository_root,
        execution.get("stderr_path"),
        expected_sha256=execution.get("stderr_sha256"),
        label="execution stderr",
    )

    submission = _read_json(extracted_submission_path)
    materialization = _require_object(manifest, "materialization")
    _require_equal(materialization.get("status"), "completed", "materialization.status")
    materialization_root = _safe_repo_path(
        repository_root, materialization.get("root"), label="materialization root"
    )
    if not materialization_root.is_dir() or materialization_root.is_symlink():
        raise BenchmarkImportError("materialization root must be a regular directory")
    component_files = _verify_materialized_submission(
        submission,
        materialization_root=materialization_root,
        expected_file_count=materialization.get("file_count"),
        expected_total_bytes=materialization.get("total_bytes"),
        expected_source_sha256=materialization.get("materialized_source_sha256"),
    )
    return {
        "source_run_path": source_run_path,
        "source_run_manifest": source_run_manifest,
        "extracted_submission_path": extracted_submission_path,
        "submission": submission,
        "policy_path": policy_path,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "materialization_root": materialization_root,
        "component_files": component_files,
    }


def _verify_materialized_submission(
    submission: dict[str, Any],
    *,
    materialization_root: Path,
    expected_file_count: Any,
    expected_total_bytes: Any,
    expected_source_sha256: Any,
) -> list[tuple[str, Path, bytes]]:
    files = submission.get("files")
    if not isinstance(files, list) or not files:
        raise BenchmarkImportError("extracted submission files must be a non-empty list")
    if expected_file_count != len(files):
        raise BenchmarkImportError("materialization.file_count does not match the submission")

    projection = bytearray(b"CODEGEN-SUBJECT/1\n")
    component_files: list[tuple[str, Path, bytes]] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for item in sorted(files, key=lambda value: value.get("path", "") if isinstance(value, dict) else ""):
        if not isinstance(item, dict):
            raise BenchmarkImportError("each extracted submission file must be an object")
        relative_text = _safe_posix_path(item.get("path"), label="submission file path")
        if relative_text in seen_paths:
            raise BenchmarkImportError(f"duplicate submission file path: {relative_text}")
        seen_paths.add(relative_text)
        content_text = item.get("content")
        if not isinstance(content_text, str):
            raise BenchmarkImportError(f"submission content must be text: {relative_text}")
        content = content_text.encode("utf-8")
        source_path = (materialization_root / Path(*PurePosixPath(relative_text).parts)).resolve()
        _require_within(source_path, materialization_root, label="materialized source")
        _assert_no_symlinks(materialization_root, source_path)
        if not source_path.is_file() or source_path.read_bytes() != content:
            raise BenchmarkImportError(
                f"materialized source does not match extracted submission: {relative_text}"
            )
        projection.extend(_length_prefixed("PATH", relative_text.encode("utf-8")))
        projection.extend(_length_prefixed("CONTENT", content))
        total_bytes += len(content)
        component_files.append((relative_text, source_path, content))

    if expected_total_bytes != total_bytes:
        raise BenchmarkImportError("materialization.total_bytes does not match the submission")
    expected_hash = _validated_sha256(expected_source_sha256, "materialized_source_sha256")
    if _sha256(bytes(projection)) != expected_hash:
        raise BenchmarkImportError("materialized source projection hash does not match")
    return component_files


def _write_snapshot(
    staging: Path,
    *,
    final_destination: Path,
    case_id: str,
    run_id: str,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    manifest_sha256: str,
    verified: dict[str, Any],
    repository_root: Path,
    operator_id: str,
    workspace_id: str,
) -> dict[str, str]:
    artifact_paths: dict[str, str] = {
        "run_manifest": "run_manifest.json",
        "validated_input": "validated_input.json",
        "deterministic_result": "deterministic_result.json",
        "narrative_draft": "narrative_draft.json",
        "constitution_check": "constitution_check.json",
        "review_packet": "review_packet.json",
        "review_packet_markdown": "review_packet.md",
        "sandbox_evaluation_manifest": "source/sandbox_evaluation_manifest.json",
        "source_run_manifest": "source/source_run_manifest.json",
        "extracted_submission": "source/extracted_submission.json",
        "sandbox_policy": "source/sandbox_policy.json",
        "execution_stdout": "source/execution_stdout.json",
        "execution_stderr": "source/execution_stderr.txt",
    }
    source_dir = staging / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "sandbox_evaluation_manifest.json").write_bytes(manifest_bytes)
    shutil.copy2(verified["source_run_path"], source_dir / "source_run_manifest.json")
    shutil.copy2(verified["extracted_submission_path"], source_dir / "extracted_submission.json")
    shutil.copy2(verified["policy_path"], source_dir / "sandbox_policy.json")
    shutil.copy2(verified["stdout_path"], source_dir / "execution_stdout.json")
    shutil.copy2(verified["stderr_path"], source_dir / "execution_stderr.txt")

    component_artifacts: list[dict[str, Any]] = []
    for index, (relative_text, _source_path, content) in enumerate(verified["component_files"], start=1):
        target_relative = Path("component") / Path(*PurePosixPath(relative_text).parts)
        target = staging / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        artifact_id = f"component_source_{index}"
        artifact_paths[artifact_id] = target_relative.as_posix()
        component_artifacts.append(
            {
                "artifact_id": artifact_id,
                "path": target_relative.as_posix(),
                "source_path": relative_text,
                "sha256": _sha256(content),
                "bytes": len(content),
            }
        )

    source_run = verified["source_run_manifest"]
    provider = source_run.get("provider") or {}
    validated_input = {
        "case_id": case_id,
        "run_id": run_id,
        "source_kind": "benchmark_component_candidate",
        "component_id": "experience-study-c4",
        "benchmark_repository": str(repository_root),
        "benchmark_evaluation_id": manifest["evaluation_id"],
        "benchmark_evaluation_manifest_sha256": manifest_sha256,
        "benchmark_source_run_id": manifest["source"]["run_id"],
        "benchmark_pack_id": manifest["source"]["pack_id"],
        "model": provider.get("effective_model_id") or provider.get("requested_model_id"),
        "provider": provider.get("provider"),
        "component_files": component_artifacts,
    }
    deterministic_result = {
        "case_id": case_id,
        "run_id": run_id,
        "automated_evaluation_status": "passed",
        "human_review_status": "pending",
        "machine_disposition": manifest["machine_disposition"],
        "promotion_eligible": False,
        "static_scan": manifest["static_scan"],
        "test_gates": manifest["test_gates"],
        "execution": manifest["execution"],
        "policy": manifest["policy"],
    }
    narrative_draft = {
        "case_id": case_id,
        "run_id": run_id,
        "status": "draft_pending_human_review",
        "summary": "Isolated execution and all automated gates passed; the result is eligible only for human review.",
        "key_points": [
            "The two generated Python files were imported as a non-executable review snapshot.",
            "Machine results do not approve actuarial correctness or production readiness.",
            "The component must not be promoted or used in production before a human decision.",
        ],
    }
    constitution_check = {
        "case_id": case_id,
        "run_id": run_id,
        "status": "review_required",
        "hard_constraints": [],
        "review_triggers": list(REVIEW_REASONS),
        "promotion_eligible": False,
    }
    checklist = _review_checklist()
    review_packet = {
        "case_id": case_id,
        "run_id": run_id,
        "status": "review_required",
        "assigned_to": operator_id,
        "workspace_id": workspace_id,
        "case_summary": "MiniMax-M3 experience-study C4 component candidate awaiting human code and actuarial-rule review.",
        "review_reasons": list(REVIEW_REASONS),
        "automated_result": {
            "machine_disposition": "ready_for_human_review",
            "static_scan": "passed",
            "test_gates": {name: manifest["test_gates"][name]["status"] for name in EXPECTED_GATES},
            "strong_isolation": True,
            "promotion_eligible": False,
        },
        "component_candidate": {
            "component_id": "experience-study-c4",
            "model": provider.get("effective_model_id") or provider.get("requested_model_id"),
            "source_run_id": manifest["source"]["run_id"],
            "evaluation_id": manifest["evaluation_id"],
            "files": component_artifacts,
        },
        "review_checklist": checklist,
        "decision_options": ["approved", "changes_requested", "rejected"],
        "decision_note": "Approved means the human review passed; production promotion still requires a separate release workflow.",
    }

    _write_json(staging / "validated_input.json", validated_input)
    _write_json(staging / "deterministic_result.json", deterministic_result)
    _write_json(staging / "narrative_draft.json", narrative_draft)
    _write_json(staging / "constitution_check.json", constitution_check)
    _write_json(staging / "review_packet.json", review_packet)
    (staging / "review_packet.md").write_text(
        _render_review_markdown(review_packet), encoding="utf-8"
    )

    run_manifest = {
        "schema_version": "1.0.0",
        "case_id": case_id,
        "run_id": run_id,
        "task_id": "benchmark-review-codegen-c4-v1",
        "status": "needs_review",
        "review_required": True,
        "promotion_eligible": False,
        "source_kind": "benchmark_component_candidate",
        "component_id": "experience-study-c4",
        "artifact_root": str(final_destination),
        "artifact_paths": artifact_paths,
        "imported_at_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark_evaluation_manifest_sha256": manifest_sha256,
    }
    _write_json(staging / "run_manifest.json", run_manifest)
    return artifact_paths


def _review_checklist() -> list[dict[str, str]]:
    return [
        {
            "id": "code_structure",
            "title": "Code structure and interfaces",
            "question": "Do the public interfaces, types, error handling, and component conventions match the project requirements?",
        },
        {
            "id": "actual_to_expected",
            "title": "Actual-to-Expected calculations",
            "question": "Do death counts, exposures, expected=0 handling, grouped totals, and input validation match the business definition?",
        },
        {
            "id": "credibility",
            "title": "Credibility rules",
            "question": "Do the credibility formula, bounds, full-credibility threshold, and missing-data handling match the selected standard?",
        },
        {
            "id": "uncertainty",
            "title": "Uncertainty intervals",
            "question": "Are the Poisson or normal approximations, amount-based metrics, and small-sample boundaries actuarially reasonable?",
        },
        {
            "id": "numeric_limits",
            "title": "Numeric capacity and precision",
            "question": "Are integer death counts, Decimal or float conversions, extreme values, and rounding behavior acceptable?",
        },
        {
            "id": "production_fit",
            "title": "Production fit and testing",
            "question": "Beyond the hidden tests, are more regression cases, documentation, monitoring, or caller adaptations required?",
        },
    ]


def _render_review_markdown(packet: dict[str, Any]) -> str:
    automated = packet["automated_result"]
    lines = [
        "# Component Candidate Human Review",
        "",
        f"- Case: `{packet['case_id']}`",
        f"- Run: `{packet['run_id']}`",
        f"- Status: `{packet['status']}`",
        f"- Machine disposition: `{automated['machine_disposition']}`",
        f"- Promotion eligible: `{str(automated['promotion_eligible']).lower()}`",
        "",
        "The static scan, strongly isolated Docker execution, and all five automated gates passed. This result permits human review; it does not approve actuarial rules or production release.",
        "",
        "## Review Checklist",
        "",
    ]
    for item in packet["review_checklist"]:
        lines.append(f"- [ ] **{item['title']}**：{item['question']}")
    lines.extend(
        [
            "",
            "## Decision Options",
            "",
            "- `approved`: human review passed",
            "- `changes_requested`: revise and submit again",
            "- `rejected`: reject the component candidate",
            "",
            packet["decision_note"],
            "",
        ]
    )
    return "\n".join(lines)


def _find_benchmark_repository(manifest_path: Path) -> Path:
    for candidate in manifest_path.parents:
        if (candidate / "benchmark_contracts").is_dir() and (candidate / "runs").is_dir():
            return candidate.resolve()
    raise BenchmarkImportError(
        "could not identify benchmark repository (expected benchmark_contracts/ and runs/)"
    )


def _default_case_id(source_run_manifest: dict[str, Any]) -> str:
    pack_id = str((source_run_manifest.get("prompt") or {}).get("pack_id") or "codegen")
    provider = source_run_manifest.get("provider") or {}
    model_alias = str(provider.get("alias") or provider.get("effective_model_id") or "model")
    raw = f"benchmark-{pack_id}-{model_alias}".lower()
    return re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-.")


def _verified_repo_file(
    repository_root: Path,
    relative_path: Any,
    *,
    expected_sha256: Any,
    label: str,
    expected_bytes: Any | None = None,
) -> Path:
    path = _safe_repo_path(repository_root, relative_path, label=label)
    _assert_no_symlinks(repository_root, path)
    if not path.is_file():
        raise BenchmarkImportError(f"{label} is not a regular file: {path}")
    data = path.read_bytes()
    if expected_bytes is not None and expected_bytes != len(data):
        raise BenchmarkImportError(f"{label} byte length does not match")
    expected_hash = _validated_sha256(expected_sha256, f"{label} sha256")
    if _sha256(data) != expected_hash:
        raise BenchmarkImportError(f"{label} sha256 does not match")
    return path


def _safe_repo_path(repository_root: Path, value: Any, *, label: str) -> Path:
    relative_text = _safe_posix_path(value, label=label)
    path = (repository_root / Path(*PurePosixPath(relative_text).parts)).resolve()
    _require_within(path, repository_root, label=label)
    return path


def _safe_posix_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BenchmarkImportError(f"{label} must be a non-empty repository-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BenchmarkImportError(f"{label} must not escape its root: {value!r}")
    return path.as_posix()


def _assert_no_symlinks(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BenchmarkImportError(f"symlink is not allowed in imported evidence: {current}")


def _require_within(path: Path, root: Path, *, label: str) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise BenchmarkImportError(f"{label} escapes its allowed root") from exc


def _safe_component(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not SAFE_COMPONENT_PATTERN.fullmatch(value):
        raise BenchmarkImportError(
            f"{field_name} may contain only letters, numbers, dot, underscore, and hyphen"
        )
    if value in {".", ".."}:
        raise BenchmarkImportError(f"{field_name} is not a safe path component")
    return value


def _validated_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise BenchmarkImportError(f"{label} must be a lowercase sha256 hex digest")
    return value


def _load_json_object(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkImportError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BenchmarkImportError(f"{label} must be a JSON object")
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    return _load_json_object(path.read_bytes(), label=str(path))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _require_object(payload: dict[str, Any], key: str, *, prefix: str = "") -> dict[str, Any]:
    value = payload.get(key)
    label = f"{prefix}.{key}" if prefix else key
    if not isinstance(value, dict):
        raise BenchmarkImportError(f"{label} must be an object")
    return value


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise BenchmarkImportError(f"{label} must be {expected!r}; got {actual!r}")


def _length_prefixed(label: str, value: bytes) -> bytes:
    return f"{label} {len(value)}\n".encode("ascii") + value + b"\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
