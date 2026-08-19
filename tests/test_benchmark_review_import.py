from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

import reserving_workflow.review.benchmark_import as benchmark_import
from reserving_workflow.review.benchmark_import import (
    BenchmarkImportError,
    import_benchmark_review,
)
from reserving_workflow.storage.local import LocalReviewStore, LocalRunStore


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _length_prefixed(label: str, value: bytes) -> bytes:
    return f"{label} {len(value)}\n".encode("ascii") + value + b"\n"


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _benchmark_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "benchmark"
    (root / "benchmark_contracts").mkdir(parents=True)
    files = [
        {"path": "src/example/a.py", "content": "def answer():\n    return 42\n"},
        {"path": "src/example/b.py", "content": "VALUE = 7\n"},
    ]
    submission = {
        "schema_version": "1.0.0",
        "task_id": "c4",
        "status": "completed",
        "summary": "fixture",
        "files": files,
    }
    submission_bytes = json.dumps(submission, separators=(",", ":")).encode("utf-8")
    source_dir = root / "runs" / "source-run"
    submission_path = source_dir / "extracted_submission.json"
    _write_bytes(submission_path, submission_bytes)

    source_manifest = {
        "schema_version": "1.2.0",
        "run_id": "source-run",
        "status": "response_contract_valid",
        "prompt": {"pack_id": "codegen-c4-v1"},
        "provider": {
            "provider": "test",
            "alias": "fixture-model",
            "effective_model_id": "FixtureModel",
        },
        "extracted_output": {"sha256": _sha256(submission_bytes)},
    }
    source_manifest_bytes = json.dumps(source_manifest, separators=(",", ":")).encode("utf-8")
    source_manifest_path = source_dir / "run_manifest.json"
    _write_bytes(source_manifest_path, source_manifest_bytes)

    evaluation_dir = root / "runs" / "sandbox_evaluations" / "eval-001"
    material_root = evaluation_dir / "subject"
    projection = bytearray(b"CODEGEN-SUBJECT/1\n")
    total_bytes = 0
    for item in sorted(files, key=lambda value: value["path"]):
        content = item["content"].encode("utf-8")
        _write_bytes(material_root / item["path"], content)
        projection.extend(_length_prefixed("PATH", item["path"].encode("utf-8")))
        projection.extend(_length_prefixed("CONTENT", content))
        total_bytes += len(content)

    policy_bytes = b'{"policy_id":"fixture"}\n'
    policy_path = root / "configs" / "sandbox" / "fixture.json"
    _write_bytes(policy_path, policy_bytes)
    stdout_bytes = b'{"result":"passed"}\n'
    stderr_bytes = b""
    stdout_path = evaluation_dir / "execution_stdout.json"
    stderr_path = evaluation_dir / "execution_stderr.txt"
    _write_bytes(stdout_path, stdout_bytes)
    _write_bytes(stderr_path, stderr_bytes)

    evidence = {
        "capabilities_dropped": True,
        "network_disabled": True,
        "no_new_privileges": True,
        "non_root_user": True,
        "read_only_root": True,
        "repository_not_mounted": True,
        "secrets_absent": True,
    }
    gates = {
        "public": {"status": "passed"},
        "hidden_actuarial": {"status": "passed"},
        "hidden_protocol": "external_black_box",
        "prompt_injection": {"status": "passed"},
        "exfiltration": {"status": "passed"},
        "reproducibility": {"status": "passed"},
    }
    manifest = {
        "schema_version": "1.0.0",
        "evaluation_id": "eval-001",
        "source": {
            "run_id": "source-run",
            "pack_id": "codegen-c4-v1",
            "run_manifest_path": "runs/source-run/run_manifest.json",
            "run_manifest_sha256": _sha256(source_manifest_bytes),
            "extracted_submission_path": "runs/source-run/extracted_submission.json",
            "extracted_submission_sha256": _sha256(submission_bytes),
            "extracted_submission_bytes": len(submission_bytes),
        },
        "policy": {
            "configuration_state": "frozen",
            "image_digest": "sha256:" + "a" * 64,
            "isolation_backend": "docker_linux",
            "isolation_level": "strong",
            "path": "configs/sandbox/fixture.json",
            "sha256": _sha256(policy_bytes),
        },
        "static_scan": {"status": "passed", "findings": []},
        "materialization": {
            "status": "completed",
            "root": "runs/sandbox_evaluations/eval-001/subject",
            "file_count": len(files),
            "total_bytes": total_bytes,
            "materialized_source_sha256": _sha256(bytes(projection)),
        },
        "execution": {
            "attempted": True,
            "result": "passed",
            "exit_code": 0,
            "timed_out": False,
            "isolation_evidence": evidence,
            "stdout_path": "runs/sandbox_evaluations/eval-001/execution_stdout.json",
            "stdout_sha256": _sha256(stdout_bytes),
            "stderr_path": "runs/sandbox_evaluations/eval-001/execution_stderr.txt",
            "stderr_sha256": _sha256(stderr_bytes),
        },
        "test_gates": gates,
        "machine_disposition": "ready_for_human_review",
        "promotion_eligible": False,
        "failure_codes": [],
    }
    manifest_path = evaluation_dir / "sandbox_evaluation_manifest.json"
    _write_bytes(manifest_path, json.dumps(manifest, separators=(",", ":")).encode("utf-8"))
    return manifest_path


def _import(tmp_path: Path, manifest_path: Path) -> dict:
    return import_benchmark_review(
        manifest_path=manifest_path,
        registry_path=tmp_path / "ai" / "run-registry.json",
        artifact_root=tmp_path / "ai" / "artifacts",
        review_store_root=tmp_path / "ai" / "reviews",
    )


def test_import_creates_needs_review_run_and_review_snapshot(tmp_path: Path):
    manifest_path = _benchmark_fixture(tmp_path)

    result = _import(tmp_path, manifest_path)

    assert result["status"] == "imported"
    assert result["run_status"] == "needs_review"
    assert result["review_status"] == "review_required"
    assert result["promotion_eligible"] is False
    artifact_root = Path(result["artifact_root"])
    assert (artifact_root / "component" / "src" / "example" / "a.py").exists()
    assert (artifact_root / "review_packet.json").exists()
    run = LocalRunStore(tmp_path / "ai" / "run-registry.json").get_run(result["run_id"])
    assert run["review_required"] is True
    review = LocalReviewStore(tmp_path / "ai" / "reviews").get_review(result["review_id"])
    assert review["decision"] is None
    assert review["packet"]["automated_result"]["promotion_eligible"] is False
    visible_review_text = (artifact_root / "review_packet.json").read_text(encoding="utf-8") + (
        artifact_root / "review_packet.md"
    ).read_text(encoding="utf-8")
    assert re.search(r"[\u4e00-\u9fff]", visible_review_text) is None


def test_import_is_idempotent_for_identical_evidence(tmp_path: Path):
    manifest_path = _benchmark_fixture(tmp_path)

    first = _import(tmp_path, manifest_path)
    second = _import(tmp_path, manifest_path)

    assert first["status"] == "imported"
    assert second["status"] == "already_imported"
    assert first["run_id"] == second["run_id"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("promotion_eligible", True, "promotion_eligible must be false"),
        ("machine_disposition", "failed", "machine_disposition must be"),
    ],
)
def test_import_rejects_non_reviewable_disposition(
    tmp_path: Path, field: str, value: object, message: str
):
    manifest_path = _benchmark_fixture(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkImportError, match=message):
        _import(tmp_path, manifest_path)


def test_import_rejects_tampered_materialized_source(tmp_path: Path):
    manifest_path = _benchmark_fixture(tmp_path)
    materialized = manifest_path.parent / "subject" / "src" / "example" / "a.py"
    materialized.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(BenchmarkImportError, match="does not match extracted submission"):
        _import(tmp_path, manifest_path)


def test_import_snapshots_the_bytes_that_were_verified(tmp_path: Path, monkeypatch):
    manifest_path = _benchmark_fixture(tmp_path)
    policy_path = tmp_path / "benchmark" / "configs" / "sandbox" / "fixture.json"
    expected_policy_sha256 = json.loads(manifest_path.read_text(encoding="utf-8"))["policy"][
        "sha256"
    ]
    original_write_snapshot = benchmark_import._write_snapshot

    def mutate_source_then_write(*args, **kwargs):
        policy_path.write_text('{"policy_id":"mutated-after-verification"}\n', encoding="utf-8")
        return original_write_snapshot(*args, **kwargs)

    monkeypatch.setattr(benchmark_import, "_write_snapshot", mutate_source_then_write)

    result = _import(tmp_path, manifest_path)

    snapshot_policy = Path(result["artifact_root"]) / "source" / "sandbox_policy.json"
    assert _sha256(snapshot_policy.read_bytes()) == expected_policy_sha256
    assert _sha256(policy_path.read_bytes()) != expected_policy_sha256


def test_import_retry_repairs_a_missing_review_record(tmp_path: Path, monkeypatch):
    manifest_path = _benchmark_fixture(tmp_path)
    original_create_review = benchmark_import.LocalReviewStore.create_review
    attempts = 0

    def fail_once(self, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected review-store failure")
        return original_create_review(self, **kwargs)

    monkeypatch.setattr(benchmark_import.LocalReviewStore, "create_review", fail_once)

    with pytest.raises(OSError, match="injected review-store failure"):
        _import(tmp_path, manifest_path)

    result = _import(tmp_path, manifest_path)

    assert result["status"] == "already_imported"
    review = LocalReviewStore(tmp_path / "ai" / "reviews").get_review(result["review_id"])
    assert review["status"] == "review_required"


def test_import_retry_recovers_an_orphaned_snapshot(tmp_path: Path, monkeypatch):
    manifest_path = _benchmark_fixture(tmp_path)
    original_create_run = benchmark_import.LocalRunStore.create_run
    attempts = 0

    def fail_once(self, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected registry failure")
        return original_create_run(self, **kwargs)

    monkeypatch.setattr(benchmark_import.LocalRunStore, "create_run", fail_once)

    with pytest.raises(OSError, match="injected registry failure"):
        _import(tmp_path, manifest_path)

    result = _import(tmp_path, manifest_path)

    assert result["status"] == "imported"
    assert LocalRunStore(tmp_path / "ai" / "run-registry.json").get_run(result["run_id"])
    assert LocalReviewStore(tmp_path / "ai" / "reviews").get_review(result["review_id"])


def test_import_retry_rejects_a_tampered_orphaned_snapshot(tmp_path: Path, monkeypatch):
    manifest_path = _benchmark_fixture(tmp_path)

    def fail_registry(self, **kwargs):
        raise OSError("injected registry failure")

    monkeypatch.setattr(benchmark_import.LocalRunStore, "create_run", fail_registry)
    with pytest.raises(OSError, match="injected registry failure"):
        _import(tmp_path, manifest_path)

    snapshot_packet = next(
        (tmp_path / "ai" / "artifacts").glob("*/benchmark-*/review_packet.json")
    )
    snapshot_packet.write_text('{"status":"tampered"}\n', encoding="utf-8")

    with pytest.raises(BenchmarkImportError, match="review_packet"):
        _import(tmp_path, manifest_path)


def test_import_rejects_symlinked_repository_evidence(tmp_path: Path):
    manifest_path = _benchmark_fixture(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy_path = tmp_path / "benchmark" / payload["policy"]["path"]
    linked_policy = policy_path.with_name("linked-policy.json")
    try:
        linked_policy.symlink_to(policy_path)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    payload["policy"]["path"] = linked_policy.relative_to(tmp_path / "benchmark").as_posix()
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkImportError, match="symlink is not allowed"):
        _import(tmp_path, manifest_path)


def test_imported_review_text_uses_verified_provider_metadata(tmp_path: Path):
    manifest_path = _benchmark_fixture(tmp_path)

    result = _import(tmp_path, manifest_path)

    run = LocalRunStore(tmp_path / "ai" / "run-registry.json").get_run(result["run_id"])
    packet = json.loads(
        (Path(result["artifact_root"]) / "review_packet.json").read_text(encoding="utf-8")
    )
    narrative = json.loads(
        (Path(result["artifact_root"]) / "narrative_draft.json").read_text(encoding="utf-8")
    )
    assert "FixtureModel" in run["summary"]
    assert "FixtureModel" in packet["case_summary"]
    assert "MiniMax-M3" not in run["summary"] + packet["case_summary"]
    assert all("two generated Python files" not in point for point in narrative["key_points"])
