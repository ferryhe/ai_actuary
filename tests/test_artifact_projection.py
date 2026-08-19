from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from reserving_workflow.adapters.control_plane.projections import (
    MAX_ARTIFACT_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_FIELDS,
    MAX_JSON_LIST_LENGTH,
    MAX_JSON_NODES,
    MAX_JSON_STRING_LENGTH,
    MAX_PROJECTED_OUTPUT_BYTES,
    ArtifactProjectionReadError,
    read_bounded_json_object,
)
from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.storage.local import LocalRunStore


class LocalApiClient:
    def __init__(self, app: Any):
        self._app = app

    async def _get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app),
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    def get(self, path: str) -> httpx.Response:
        return asyncio.run(self._get(path))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _projection_fixture(tmp_path: Path) -> tuple[LocalApiClient, Path, Path, str]:
    run_id = "run-projection-1"
    case_id = "case-projection-1"
    artifact_root = tmp_path / "artifacts" / run_id
    artifact_root.mkdir(parents=True)
    payloads = {
        "validated_input": {
            "case_id": case_id,
            "run_id": run_id,
            "tool_id": "chainladder",
            "inputs": {"sample_name": "RAA"},
        },
        "deterministic_result": {
            "case_id": case_id,
            "run_id": run_id,
            "tool_id": "chainladder",
            "method": "chainladder",
            "reserve_summary": {"ibnr": 12.5},
            "diagnostics": {"origin_count": 10},
        },
        "narrative_draft": {
            "case_id": case_id,
            "run_id": run_id,
            "summary": "Stable result",
            "key_points": ["point"],
            "cited_values": {"ibnr": 12.5},
        },
        "constitution_check": {
            "case_id": case_id,
            "run_id": run_id,
            "status": "pass",
            "hard_constraints": [],
            "soft_guidance": [],
            "review_triggers": [],
        },
        "review_packet": {
            "case_id": case_id,
            "run_id": run_id,
            "status": "review_required",
            "failed_checks": ["threshold"],
        },
    }
    for artifact_id, payload in payloads.items():
        _write_json(artifact_root / f"{artifact_id}.json", payload)
    manifest = {
        "case_id": case_id,
        "run_id": run_id,
        "created_by": "test",
        "artifact_root": str(artifact_root),
        "artifact_paths": {
            "run_manifest": "run_manifest.json",
            **{artifact_id: f"{artifact_id}.json" for artifact_id in payloads},
        },
    }
    _write_json(artifact_root / "run_manifest.json", manifest)

    registry_path = tmp_path / "registry" / "runs.json"
    LocalRunStore(registry_path).create_run(
        task_id="task-1",
        case_id=case_id,
        run_id=run_id,
        status="needs_review",
        artifact_root=str(artifact_root),
        summary="projection test",
        operator_params={"tool_id": "chainladder"},
        review_required=True,
    )
    app = create_app(
        settings=ApiSettings(
            registry_path=registry_path,
            artifact_root=tmp_path / "unused-artifacts",
            review_store_dir=tmp_path / "reviews",
        )
    )
    return LocalApiClient(app), artifact_root, registry_path, run_id


@pytest.mark.parametrize(
    ("artifact_id", "provenance", "expected_key"),
    [
        ("run_manifest", "system_manifest", "created_by"),
        ("validated_input", "deterministic", "inputs"),
        ("deterministic_result", "deterministic", "reserve_summary"),
        ("narrative_draft", "model_generated", "summary"),
        ("constitution_check", "deterministic", "status"),
        ("review_packet", "review", "failed_checks"),
    ],
)
def test_allowlisted_json_artifacts_have_independent_path_free_projections(
    tmp_path: Path,
    artifact_id: str,
    provenance: str,
    expected_key: str,
) -> None:
    client, artifact_root, _, run_id = _projection_fixture(tmp_path)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact_id}/projection")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert payload["artifact_id"] == artifact_id
    assert payload["status"] == "available"
    assert payload["provenance"] == provenance
    assert expected_key in payload["data"]
    assert payload["errors"] == []
    serialized = json.dumps(payload)
    assert str(artifact_root) not in serialized
    assert "artifact_paths" not in serialized
    assert "artifact_root" not in serialized


def test_projection_rejects_unknown_artifact_and_missing_run(tmp_path: Path) -> None:
    client, _, _, run_id = _projection_fixture(tmp_path)

    unsupported = client.get(f"/runs/{run_id}/artifacts/operator_handoff/projection")
    missing_run = client.get("/runs/missing-run/artifacts/validated_input/projection")

    assert unsupported.status_code == 400
    assert unsupported.json()["detail"] == {
        "code": "artifact_unsupported",
        "message": "Artifact projection is not supported.",
    }
    assert missing_run.status_code == 404
    assert missing_run.json()["detail"]["code"] == "run_not_found"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("manifest_missing", "manifest_missing"),
        ("manifest_corrupt", "manifest_invalid_json"),
        ("manifest_run_mismatch", "manifest_run_mismatch"),
        ("artifact_unregistered", "artifact_not_registered"),
        ("artifact_missing", "artifact_missing"),
        ("artifact_list", "artifact_invalid_shape"),
        ("artifact_scalar", "artifact_invalid_shape"),
        ("artifact_utf8", "artifact_invalid_encoding"),
        ("artifact_corrupt", "artifact_invalid_json"),
        ("artifact_schema_mismatch", "artifact_schema_mismatch"),
        ("artifact_run_mismatch", "artifact_run_mismatch"),
        ("artifact_tool_mismatch", "artifact_tool_mismatch"),
        ("absolute_path", "artifact_path_rejected"),
        ("parent_path", "artifact_path_rejected"),
        ("directory", "artifact_not_regular"),
    ],
)
def test_projection_security_failures_have_stable_non_disclosing_codes(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    manifest_path = root / "run_manifest.json"
    target = root / "validated_input.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if mutation == "manifest_missing":
        manifest_path.unlink()
    elif mutation == "manifest_corrupt":
        manifest_path.write_text("{broken", encoding="utf-8")
    elif mutation == "manifest_run_mismatch":
        manifest["run_id"] = "other-run"
        _write_json(manifest_path, manifest)
    elif mutation == "artifact_unregistered":
        manifest["artifact_paths"].pop("validated_input")
        _write_json(manifest_path, manifest)
    elif mutation == "artifact_missing":
        target.unlink()
    elif mutation == "artifact_list":
        _write_json(target, [])
    elif mutation == "artifact_scalar":
        _write_json(target, 7)
    elif mutation == "artifact_utf8":
        target.write_bytes(b"\xff")
    elif mutation == "artifact_corrupt":
        target.write_text("{broken", encoding="utf-8")
    elif mutation == "artifact_schema_mismatch":
        _write_json(target, {"run_id": run_id, "tool_id": "chainladder", "inputs": []})
    elif mutation == "artifact_run_mismatch":
        _write_json(target, {"run_id": "other-run", "tool_id": "chainladder", "inputs": {}})
    elif mutation == "artifact_tool_mismatch":
        _write_json(target, {"run_id": run_id, "tool_id": "other-tool", "inputs": {}})
    elif mutation == "absolute_path":
        manifest["artifact_paths"]["validated_input"] = str(target)
        _write_json(manifest_path, manifest)
    elif mutation == "parent_path":
        manifest["artifact_paths"]["validated_input"] = "../validated_input.json"
        _write_json(manifest_path, manifest)
    elif mutation == "directory":
        target.unlink()
        target.mkdir()

    response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert response.status_code in {400, 404, 409, 413, 422}
    payload = response.json()
    assert payload["detail"]["code"] == expected_code
    serialized = json.dumps(payload)
    assert str(tmp_path) not in serialized
    assert "Traceback" not in serialized


def test_projection_rejects_intermediate_and_final_symlinks(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_json(outside / "payload.json", {"run_id": run_id, "tool_id": "chainladder", "inputs": {}})
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    final_link = root / "final-link.json"
    intermediate_link = root / "linked-dir"
    try:
        final_link.symlink_to(outside / "payload.json")
        intermediate_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    manifest["artifact_paths"]["validated_input"] = final_link.name
    _write_json(manifest_path, manifest)
    final_response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    manifest["artifact_paths"]["validated_input"] = "linked-dir/payload.json"
    _write_json(manifest_path, manifest)
    intermediate_response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert final_response.status_code == 400
    assert final_response.json()["detail"]["code"] == "artifact_path_rejected"
    assert intermediate_response.status_code == 400
    assert intermediate_response.json()["detail"]["code"] == "artifact_path_rejected"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_projection_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    target = root / "validated_input.json"
    target.unlink()
    os.mkfifo(target)

    response = client.get(
        f"/runs/{run_id}/artifacts/validated_input/projection",
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "artifact_not_regular"


def test_projection_enforces_file_and_json_complexity_limits(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    target = root / "validated_input.json"

    cases = [
        (b"{" + b'"x":"' + b"a" * MAX_ARTIFACT_BYTES + b'"}', "artifact_size_exceeded"),
        (json.dumps({"inputs": {"x": "a" * (MAX_JSON_STRING_LENGTH + 1)}}).encode(), "artifact_string_limit_exceeded"),
        (json.dumps({"inputs": list(range(MAX_JSON_LIST_LENGTH + 1))}).encode(), "artifact_list_limit_exceeded"),
        (json.dumps({"inputs": {str(index): index for index in range(MAX_JSON_FIELDS + 1)}}).encode(), "artifact_field_limit_exceeded"),
        (
            b'{"inputs":' + (b"[" * 2_000) + b"0" + (b"]" * 2_000) + b"}",
            "artifact_depth_exceeded",
        ),
    ]
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(MAX_JSON_DEPTH + 1):
        cursor["child"] = {}
        cursor = cursor["child"]
    cases.append((json.dumps({"inputs": nested}).encode(), "artifact_depth_exceeded"))

    for content, expected_code in cases:
        target.write_bytes(content)
        response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")
        assert response.status_code in {400, 413, 422}
        assert response.json()["detail"]["code"] == expected_code


def test_projection_enforces_node_and_projected_output_limits(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)

    node_payload = {
        "run_id": run_id,
        "tool_id": "chainladder",
        "results": [[0] * 1_900 for _ in range((MAX_JSON_NODES // 1_900) + 1)],
    }
    _write_json(root / "deterministic_result.json", node_payload)
    node_response = client.get(f"/runs/{run_id}/artifacts/deterministic_result/projection")

    oversized_projection = {
        "run_id": run_id,
        "tool_id": "chainladder",
        "inputs": {
            f"field-{index}": "x" * 90_000
            for index in range((MAX_PROJECTED_OUTPUT_BYTES // 90_000) + 1)
        },
    }
    _write_json(root / "validated_input.json", oversized_projection)
    output_response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert node_response.status_code == 422
    assert node_response.json()["detail"]["code"] == "artifact_node_limit_exceeded"
    assert output_response.status_code == 413
    assert output_response.json()["detail"]["code"] == "artifact_output_limit_exceeded"


def test_projection_removes_paths_secret_fields_and_sensitive_values(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    sentinel = "SENTINEL-API-TOKEN-937"
    _write_json(
        root / "validated_input.json",
        {
            "run_id": run_id,
            "tool_id": "chainladder",
            "inputs": {
                "sample_name": "RAA",
                "path": str(tmp_path / "private"),
                "api_key": sentinel,
                "nested": {"authorization": sentinel, "value": "safe"},
                "note": f"untrusted token marker {sentinel}",
            },
        },
    )

    response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert response.status_code == 200
    serialized = json.dumps(response.json())
    assert str(tmp_path) not in serialized
    assert sentinel not in serialized
    assert "authorization" not in serialized.lower()
    assert "api_key" not in serialized.lower()


@pytest.mark.skipif(os.name != "posix", reason="descriptor replacement semantics require POSIX")
def test_bounded_reader_uses_open_descriptor_when_entry_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reserving_workflow.adapters.control_plane import projections

    target = tmp_path / "artifact.json"
    replacement = tmp_path / "replacement.json"
    _write_json(target, {"identity": "original"})
    _write_json(replacement, {"identity": "replacement"})
    original_open = projections._open_descriptor_no_follow

    def open_then_replace(root: Path, parts: tuple[str, ...], *, namespace: str) -> int:
        descriptor = original_open(root, parts, namespace=namespace)
        os.replace(replacement, target)
        return descriptor

    monkeypatch.setattr(projections, "_open_descriptor_no_follow", open_then_replace)

    assert read_bounded_json_object(tmp_path, target.name) == {"identity": "original"}
    assert json.loads(target.read_text(encoding="utf-8")) == {"identity": "replacement"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX device fixture is unavailable")
def test_bounded_reader_rejects_device_file() -> None:
    with pytest.raises(ArtifactProjectionReadError) as exc_info:
        read_bounded_json_object(Path("/"), "dev/null")

    assert exc_info.value.code == "artifact_not_regular"


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="fd accounting is unavailable")
def test_bounded_reader_closes_descriptors_on_error(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_text("{broken", encoding="utf-8")
    before = len(tuple(Path("/proc/self/fd").iterdir()))

    for _ in range(50):
        with pytest.raises(ArtifactProjectionReadError) as exc_info:
            read_bounded_json_object(tmp_path, target.name)
        assert exc_info.value.code == "artifact_invalid_json"

    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


def test_projection_read_does_not_change_registry_artifacts_or_missing_review_root(tmp_path: Path) -> None:
    client, artifact_root, registry_path, run_id = _projection_fixture(tmp_path)
    review_root = tmp_path / "reviews"
    before = _storage_snapshot(registry_path.parent, artifact_root, review_root)

    response = client.get(f"/runs/{run_id}/artifacts/deterministic_result/projection")

    assert response.status_code == 200
    assert _storage_snapshot(registry_path.parent, artifact_root, review_root) == before
    assert not review_root.exists()


def _storage_snapshot(*roots: Path) -> tuple[tuple[str, bool, tuple[tuple[str, str], ...]], ...]:
    snapshots = []
    for root in roots:
        files: list[tuple[str, str]] = []
        if root.exists():
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                files.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
        snapshots.append((root.name, root.exists(), tuple(files)))
    return tuple(snapshots)
