from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
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
    project_artifact_payload,
    read_bounded_json_object,
)
from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.storage.local import LocalRunStore


GOLDEN_RUN_DIR = Path(__file__).parent / "fixtures" / "tool_contracts" / "golden_run"


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


def test_authoritative_chainladder_fixtures_remain_projectable(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts" / "golden-raa-20260520T120000Z"
    artifact_root.mkdir(parents=True)
    for artifact_id in (
        "deterministic_result",
        "narrative_draft",
        "constitution_check",
        "review_packet",
    ):
        _write_json(
            artifact_root / f"{artifact_id}.json",
            json.loads((GOLDEN_RUN_DIR / f"{artifact_id}.json").read_text(encoding="utf-8")),
        )
    _write_json(
        artifact_root / "validated_input.json",
        {
            "case_id": "golden-raa",
            "tool_id": "chainladder",
            "inputs": {"sample_name": "RAA", "method_variant": "chainladder"},
        },
    )
    manifest = json.loads((GOLDEN_RUN_DIR / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["artifact_paths"]["validated_input"] = "validated_input.json"
    _write_json(artifact_root / "run_manifest.json", manifest)
    registry_path = tmp_path / "registry" / "runs.json"
    LocalRunStore(registry_path).create_run(
        task_id="golden-task",
        case_id="golden-raa",
        run_id="golden-raa-20260520T120000Z",
        status="needs_review",
        artifact_root=str(artifact_root),
        operator_params={"method": "chainladder"},
        review_required=True,
    )
    client = LocalApiClient(
        create_app(
            settings=ApiSettings(
                registry_path=registry_path,
                artifact_root=tmp_path / "unused-artifacts",
                review_store_dir=tmp_path / "reviews",
            )
        )
    )

    responses = {
        artifact_id: client.get(
            "/runs/golden-raa-20260520T120000Z/"
            f"artifacts/{artifact_id}/projection"
        )
        for artifact_id in (
            "run_manifest",
            "validated_input",
            "deterministic_result",
            "narrative_draft",
            "constitution_check",
            "review_packet",
        )
    }

    assert {artifact_id: response.status_code for artifact_id, response in responses.items()} == {
        artifact_id: 200 for artifact_id in responses
    }


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
        _write_json(
            target,
            {
                "case_id": "case-projection-1",
                "run_id": run_id,
                "tool_id": "chainladder",
                "inputs": [],
            },
        )
    elif mutation == "artifact_run_mismatch":
        _write_json(
            target,
            {
                "case_id": "case-projection-1",
                "run_id": "other-run",
                "tool_id": "chainladder",
                "inputs": {},
            },
        )
    elif mutation == "artifact_tool_mismatch":
        _write_json(
            target,
            {
                "case_id": "case-projection-1",
                "run_id": run_id,
                "tool_id": "other-tool",
                "inputs": {},
            },
        )
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


def test_projection_rejects_nested_windows_drive_component_through_http(
    tmp_path: Path,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    outside = tmp_path / "outside-drive-component"
    outside.mkdir()
    _write_json(
        outside / "validated_input.json",
        {"run_id": run_id, "tool_id": "chainladder", "inputs": {"identity": "outside"}},
    )
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_paths"]["validated_input"] = (
        f"x/D:{outside}/validated_input.json"
    )
    _write_json(manifest_path, manifest)

    response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "artifact_path_rejected",
        "message": "Registered artifact path failed safety validation.",
    }


@pytest.mark.parametrize(
    "registered_path",
    (
        "nested/name:stream/validated_input.json",
        "nested/D:/validated_input.json",
        "nested/?/validated_input.json",
        "nested/??/validated_input.json",
        "nested/Device/validated_input.json",
        "nested/GLOBALROOT/validated_input.json",
        "nested/UNC/validated_input.json",
    ),
)
def test_projection_rejects_colon_and_windows_namespace_in_any_component(
    tmp_path: Path,
    registered_path: str,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_paths"]["validated_input"] = registered_path
    _write_json(manifest_path, manifest)

    response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "artifact_path_rejected"


def test_projection_rejects_intermediate_and_final_symlinks(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_json(
        outside / "validated_input.json",
        {"run_id": run_id, "tool_id": "chainladder", "inputs": {}},
    )
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    final_link = root / "validated_input.json"
    intermediate_link = root / "linked-dir"
    try:
        final_link.unlink()
        final_link.symlink_to(outside / "validated_input.json")
        intermediate_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    manifest["artifact_paths"]["validated_input"] = final_link.name
    _write_json(manifest_path, manifest)
    final_response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    manifest["artifact_paths"]["validated_input"] = "linked-dir/validated_input.json"
    _write_json(manifest_path, manifest)
    intermediate_response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert final_response.status_code == 400
    assert final_response.json()["detail"]["code"] == "artifact_path_rejected"
    assert intermediate_response.status_code == 400
    assert intermediate_response.json()["detail"]["code"] == "artifact_path_rejected"


def test_projection_rejects_all_multicomponent_registered_paths(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    nested = root / "slot" / "validated_input.json"
    nested.parent.mkdir()
    (root / "validated_input.json").replace(nested)
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_paths"]["validated_input"] = "slot/validated_input.json"
    _write_json(manifest_path, manifest)

    response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "artifact_path_rejected",
        "message": "Registered artifact path failed safety validation.",
    }


def test_bounded_reader_rejects_multicomponent_paths_at_the_shared_boundary(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "slot" / "validated_input.json"
    nested.parent.mkdir()
    _write_json(nested, {"identity": "must-not-be-read"})

    with pytest.raises(ArtifactProjectionReadError) as exc_info:
        read_bounded_json_object(tmp_path, "slot/validated_input.json")

    assert exc_info.value.code == "artifact_path_rejected"


@pytest.mark.skipif(os.name != "nt", reason="junction swap regression requires Windows")
def test_windows_endpoint_confines_file_after_intermediate_handle_returns(
    tmp_path: Path,
) -> None:
    from reserving_workflow.adapters.control_plane import projections

    import ctypes
    from ctypes import wintypes

    client, root, _, run_id = _projection_fixture(tmp_path)
    intermediate = root / "slot"
    outside = tmp_path / "outside"
    intermediate.mkdir()
    outside.mkdir()
    (root / "validated_input.json").replace(intermediate / "validated_input.json")
    _write_json(
        outside / "validated_input.json",
        {"run_id": run_id, "tool_id": "chainladder", "inputs": {"identity": "outside"}},
    )
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_paths"]["validated_input"] = "slot/validated_input.json"
    _write_json(manifest_path, manifest)
    parked = root / "parked"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        )
        return int(count.value)

    before = handle_count()
    handle = projections._windows_open_handle(
        intermediate,
        expect_directory=True,
        namespace="artifact",
    )
    swapped = False
    try:
        intermediate.rename(parked)
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(intermediate), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            parked.rename(intermediate)
            pytest.skip("junction creation is unavailable in this Windows environment")
        swapped = True
    finally:
        projections._windows_close_handle(handle)
    assert handle_count() == before

    try:
        response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")
    finally:
        if swapped:
            os.rmdir(intermediate)
            parked.rename(intermediate)

    assert swapped
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "artifact_path_rejected",
        "message": "Registered artifact path failed safety validation.",
    }


@pytest.mark.parametrize("race_namespace", ("manifest", "artifact"))
@pytest.mark.skipif(os.name != "nt", reason="junction swap regression requires Windows")
def test_windows_endpoint_rejects_trusted_root_ancestor_junction_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_namespace: str,
) -> None:
    from reserving_workflow.adapters.control_plane import projections

    client, root, _, run_id = _projection_fixture(tmp_path)
    configured_ancestor = root.parent
    parked_ancestor = tmp_path / "parked-artifacts"
    outside_ancestor = tmp_path / "outside-artifacts"
    outside_root = outside_ancestor / run_id
    outside_root.mkdir(parents=True)
    _write_json(
        outside_root / "validated_input.json",
        {
            "case_id": "case-projection-1",
            "run_id": run_id,
            "tool_id": "chainladder",
            "inputs": {"identity": "outside"},
        },
    )
    _write_json(
        outside_root / "run_manifest.json",
        {
            "case_id": "case-projection-1",
            "run_id": run_id,
            "artifact_paths": {
                "run_manifest": "run_manifest.json",
                "validated_input": "validated_input.json",
            },
        },
    )
    original_open_handle = projections._windows_open_handle
    swapped = False

    def open_then_swap(path: Path, *, expect_directory: bool, namespace: str) -> int:
        nonlocal swapped
        handle = original_open_handle(
            path,
            expect_directory=expect_directory,
            namespace=namespace,
        )
        if not swapped and namespace == race_namespace and path == configured_ancestor:
            configured_ancestor.rename(parked_ancestor)
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(configured_ancestor), str(outside_ancestor)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                projections._windows_close_handle(handle)
                parked_ancestor.rename(configured_ancestor)
                pytest.skip("junction creation is unavailable in this Windows environment")
            swapped = True
        return handle

    monkeypatch.setattr(projections, "_windows_open_handle", open_then_swap)
    try:
        response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")
    finally:
        if swapped:
            os.rmdir(configured_ancestor)
            parked_ancestor.rename(configured_ancestor)

    assert swapped
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": f"{race_namespace}_path_rejected",
        "message": "Registered artifact path failed safety validation.",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows rename sharing regression requires Windows")
def test_windows_reader_blocks_both_trusted_root_swap_windows_and_keeps_original_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reserving_workflow.adapters.control_plane import projections

    import ctypes
    from ctypes import wintypes

    root = tmp_path / "trusted-root"
    replacement = tmp_path / "replacement-root"
    parked = tmp_path / "parked-root"
    staged_original = tmp_path / "staged-original.json"
    root.mkdir()
    replacement.mkdir()
    _write_json(root / "artifact.json", {"identity": "original"})
    _write_json(replacement / "artifact.json", {"identity": "replacement"})

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        )
        return int(count.value)

    original_verify_root = projections._windows_verify_configured_root_handle
    original_open_relative = projections._windows_open_relative_handle
    attack_live = False
    root_swap_attempted = False
    first_object_swap_attempted = False
    first_object_swap_blocked = False
    second_object_swap_attempted = False
    second_object_swap_blocked = False

    def verify_then_attempt_first_swap(
        trusted_root_handle: int,
        configured_root: Path,
        *,
        namespace: str,
    ) -> None:
        nonlocal attack_live, root_swap_attempted
        original_verify_root(
            trusted_root_handle,
            configured_root,
            namespace=namespace,
        )
        if configured_root != root:
            return
        root_swap_attempted = True
        root.rename(parked)
        replacement.rename(root)
        attack_live = True

    def open_then_attempt_both_object_swaps(
        parent_handle: int,
        component: str,
        *,
        expect_directory: bool,
        namespace: str,
    ) -> int:
        nonlocal first_object_swap_attempted, first_object_swap_blocked
        nonlocal second_object_swap_attempted, second_object_swap_blocked
        handle = original_open_relative(
            parent_handle,
            component,
            expect_directory=expect_directory,
            namespace=namespace,
        )
        if expect_directory or component != "artifact.json":
            return handle
        first_object_swap_attempted = True
        try:
            os.replace(root / "artifact.json", parked / "artifact.json")
        except OSError:
            first_object_swap_blocked = True
        second_object_swap_attempted = True
        try:
            (parked / "artifact.json").rename(staged_original)
        except OSError:
            second_object_swap_blocked = True
        return handle

    monkeypatch.setattr(
        projections,
        "_windows_verify_configured_root_handle",
        verify_then_attempt_first_swap,
    )
    monkeypatch.setattr(
        projections,
        "_windows_open_relative_handle",
        open_then_attempt_both_object_swaps,
    )
    before = handle_count()
    try:
        payload = read_bounded_json_object(root, "artifact.json")
    finally:
        if attack_live:
            root.rename(replacement)
            parked.rename(root)

    assert root_swap_attempted is True
    assert attack_live is True
    assert first_object_swap_attempted is True
    assert first_object_swap_blocked is True
    assert second_object_swap_attempted is True
    assert second_object_swap_blocked is True
    assert payload == {"identity": "original"}
    assert handle_count() == before


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
        "case_id": "case-projection-1",
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


@pytest.mark.parametrize(
    "content",
    [
        b'{"inputs":{"value":' + (b"9" * 5_000) + b"}}",
        b'{"inputs":{"value":NaN}}',
        b'{"inputs":{"value":Infinity}}',
        b'{"inputs":{"value":-Infinity}}',
        b'{"inputs":{"value":1e999}}',
    ],
    ids=("oversized-integer", "nan", "infinity", "negative-infinity", "overflow"),
)
def test_bounded_reader_rejects_invalid_and_non_finite_json_numbers(
    tmp_path: Path,
    content: bytes,
) -> None:
    target = tmp_path / "validated_input.json"
    target.write_bytes(content)

    with pytest.raises(ArtifactProjectionReadError) as exc_info:
        read_bounded_json_object(tmp_path, target.name)

    assert exc_info.value.code == "artifact_invalid_json"


def test_projection_removes_paths_secret_fields_and_sensitive_values(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    sentinel = "SENTINEL-API-TOKEN-937"
    _write_json(
        root / "validated_input.json",
        {
            "case_id": "case-projection-1",
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


@pytest.mark.parametrize(
    "artifact_id",
    tuple(
        (
            "run_manifest",
            "validated_input",
            "deterministic_result",
            "narrative_draft",
            "constitution_check",
            "review_packet",
        )
    ),
)
def test_each_artifact_projection_has_an_independent_top_level_allowlist(
    artifact_id: str,
) -> None:
    payloads = {
        "run_manifest": {"case_id": "case-1", "run_id": "run-1", "artifact_paths": {}},
        "validated_input": {"case_id": "case-1", "tool_id": "chainladder", "inputs": {}},
        "deterministic_result": {"case_id": "case-1", "method": "chainladder", "reserve_summary": {}},
        "narrative_draft": {"case_id": "case-1", "summary": "safe"},
        "constitution_check": {"case_id": "case-1", "status": "pass"},
        "review_packet": {"case_id": "case-1", "run_id": "run-1", "status": "review_required"},
    }
    payload = {
        **payloads[artifact_id],
        "unknown_top_level": {"credential": "SENTINEL-UNKNOWN-TOP-LEVEL"},
    }

    projected = project_artifact_payload(artifact_id, payload)

    assert "unknown_top_level" not in projected


def test_nested_free_map_sanitizer_redacts_sensitive_keys_paths_and_credentials() -> None:
    sensitive_keys = (
        "password",
        "passphrase",
        "credential",
        "credentials",
        "cookie",
        "session",
        "auth",
        "header",
        "private_key",
        "access-key",
        "privateAccessKey",
        "authHeader",
        "accessToken",
        "refreshToken",
        "sharedSecret",
        "registryPath",
        "ACCESSTOKEN",
        "REFRESHTOKEN",
        "SHAREDSECRET",
        "REGISTRYPATH",
        "fileName",
        "file_name",
        "BasicValue",
        "secret_key",
        "secretKey",
        "SECRET-KEY",
        "token_value",
        "tokenValue",
        "TOKEN-VALUE",
        "authToken",
        "SECRETKEY",
        "TOKENVALUE",
        "AUTHTOKEN",
        "/var/lib/private/key.json",
        r"C:\private\key.json",
        r"\\server\share\key.json",
        r"\Users\private\key.json",
        r"\Device\HarddiskVolume1\key.json",
        r"\\?\C:\private\key.json",
    )
    sensitive_values = (
        "prefix C:\\private\\validated_input.json suffix",
        "prefix \\\\server\\share\\validated_input.json suffix",
        "prefix file:///var/lib/private/validated_input.json suffix",
        "prefix /var/lib/private/validated_input.json suffix",
        "sk-FAKE000000000000000000000000",
        "ghp_FAKE0000000000000000000000000000000000",
        "gho_FAKE0000000000000000000000000000000000",
        "github_pat_FAKE0000000000000000000000000000",
        "xoxb-FAKE000000000000000000000000",
        "AKIAFAKE000000000000",
        "AIzaFAKE000000000000000000000000000000000",
        "Bearer FAKE000000000000000000000000",
        "Authorization: Basic ZmFrZTpzZWNyZXQ=",
        "Basic ZmFrZTpzZWNyZXQ=",
        "ACCESSTOKEN=SENTINEL-CONCATENATED-ACCESS",
        "REFRESHTOKEN=SENTINEL-CONCATENATED-REFRESH",
        "SHAREDSECRET=SENTINEL-CONCATENATED-SHARED",
        "REGISTRYPATH=SENTINEL-CONCATENATED-REGISTRY",
        "fileName=SENTINEL-CONCATENATED-FILENAME",
        "BasicValue=SENTINEL-CONCATENATED-BASIC",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlLXVzZXIifQ.FAKESIGNATURE000000000000",
        "sessionid=FAKE000000000000000000000000",
        r"\Users\private\validated_input.json",
        r"\Device\HarddiskVolume1\private\validated_input.json",
    )
    inputs = {
        "safe_nested": {"label": "safe"},
        "sensitive_keys": {key: "SENTINEL-SENSITIVE-KEY" for key in sensitive_keys},
        "sensitive_values": {
            f"value_{index}": value for index, value in enumerate(sensitive_values)
        },
    }

    projected = project_artifact_payload(
        "validated_input",
        {"case_id": "case-1", "tool_id": "chainladder", "inputs": inputs},
    )

    assert projected["inputs"]["safe_nested"] == {"label": "safe"}
    assert projected["inputs"]["sensitive_keys"] == {}
    assert set(projected["inputs"]["sensitive_values"].values()) == {"[redacted]"}


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "client_secret=opaque-value",
        "clientSecret: opaque-value",
        "CLIENT-SECRET = opaque-value",
        "private_key=opaque-value",
        "privateKey: opaque-value",
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization=opaque-value",
        "authorization : opaque-value",
        "X-Auth-Header=opaque-value",
        "x_auth_header: opaque-value",
        "https://service-user:opaque-password@example.test",
        "postgresql://service-user@database.example.test",
    ),
)
def test_free_map_sanitizer_redacts_assignment_pem_and_url_userinfo_values(
    unsafe_value: str,
) -> None:
    projected = project_artifact_payload(
        "validated_input",
        {
            "case_id": "case-1",
            "tool_id": "chainladder",
            "inputs": {"note": unsafe_value},
        },
    )

    assert projected["inputs"]["note"] == "[redacted]"


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "secret_key=opaque-value",
        "secretKey: opaque-value",
        "SECRET-KEY = opaque-value",
        "token_value=opaque-value",
        "tokenValue: opaque-value",
        "TOKEN-VALUE = opaque-value",
        "authToken=opaque-value",
        "SECRETKEY=opaque-value",
        "TOKENVALUE=opaque-value",
        "AUTHTOKEN=opaque-value",
    ),
)
def test_free_map_sanitizer_redacts_semantic_sensitive_assignments(
    unsafe_value: str,
) -> None:
    projected = project_artifact_payload(
        "validated_input",
        {
            "case_id": "case-1",
            "tool_id": "chainladder",
            "inputs": {"note": unsafe_value},
        },
    )

    assert projected["inputs"]["note"] == "[redacted]"


def test_free_map_sanitizer_redacts_compound_api_and_access_key_styles_without_token_false_positive() -> None:
    opaque_value = "opaque-7f0c19d2"
    sensitive_keys = (
        "x_api_key",
        "xApiKey",
        "XAPIKEY",
        "apiKeyValue",
        "api-key-value",
        "personalAccessKey",
        "awsAccessKeyId",
    )
    projected = project_artifact_payload(
        "validated_input",
        {
            "case_id": "case-1",
            "tool_id": "chainladder",
            "inputs": {
                **{key: opaque_value for key in sensitive_keys},
                "note": f"x_api_key={opaque_value}",
                "usage": "Token count is 120 for this model.",
            },
        },
    )

    assert opaque_value not in json.dumps(projected)
    assert projected["inputs"] == {
        "note": "[redacted]",
        "usage": "Token count is 120 for this model.",
    }


@pytest.mark.parametrize(
    "ordinary_value",
    (
        "Token count: 42",
        "The token count is 42 for this model",
        "This model has a token count of 7.",
    ),
)
def test_free_map_sanitizer_preserves_ordinary_token_count_language(
    ordinary_value: str,
) -> None:
    projected = project_artifact_payload(
        "validated_input",
        {
            "case_id": "case-1",
            "tool_id": "chainladder",
            "inputs": {"usage": ordinary_value},
        },
    )

    assert projected["inputs"]["usage"] == ordinary_value


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "Token count: 42; x_api_key=opaque-9e47a2c1",
        "The token count is 42 for this model; Bearer opaque-7c39d0a5",
    ),
)
def test_token_count_language_does_not_bypass_other_secret_detection(
    unsafe_value: str,
) -> None:
    projected = project_artifact_payload(
        "validated_input",
        {
            "case_id": "case-1",
            "tool_id": "chainladder",
            "inputs": {"usage": unsafe_value},
        },
    )

    assert projected["inputs"]["usage"] == "[redacted]"


def test_http_projection_redacts_compound_api_and_access_key_styles_without_token_false_positive(
    tmp_path: Path,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    opaque_value = "opaque-41a8d095"
    _write_json(
        root / "validated_input.json",
        {
            "case_id": "case-projection-1",
            "run_id": run_id,
            "tool_id": "chainladder",
            "inputs": {
                "x_api_key": opaque_value,
                "xApiKey": opaque_value,
                "XAPIKEY": opaque_value,
                "apiKeyValue": opaque_value,
                "api-key-value": opaque_value,
                "personalAccessKey": opaque_value,
                "awsAccessKeyId": opaque_value,
                "note": f"x_api_key={opaque_value}",
                "usage": "Token count is 120 for this model.",
            },
        },
    )

    response = client.get(f"/runs/{run_id}/artifacts/validated_input/projection")

    assert response.status_code == 200
    inputs = response.json()["data"]["inputs"]
    assert opaque_value not in json.dumps(response.json())
    assert inputs == {
        "note": "[redacted]",
        "usage": "Token count is 120 for this model.",
    }


def test_free_map_sanitizer_does_not_match_sensitive_token_substrings() -> None:
    projected = project_artifact_payload(
        "validated_input",
        {
            "case_id": "case-1",
            "tool_id": "chainladder",
            "inputs": {
                "secretaryName": "ordinary-business-value",
                "tokenizationMethod": "ordinary-business-value",
                "authenticityScore": "ordinary-business-value",
            },
        },
    )

    assert projected["inputs"] == {
        "secretaryName": "ordinary-business-value",
        "tokenizationMethod": "ordinary-business-value",
        "authenticityScore": "ordinary-business-value",
    }


@pytest.mark.parametrize(
    ("artifact_id", "missing_field"),
    (
        ("run_manifest", "case_id"),
        ("validated_input", "inputs"),
        ("deterministic_result", "method"),
        ("narrative_draft", "summary"),
        ("constitution_check", "status"),
        ("review_packet", "status"),
    ),
)
@pytest.mark.parametrize("mutation", ("empty", "missing"))
def test_each_allowlisted_artifact_rejects_empty_or_missing_required_schema(
    tmp_path: Path,
    artifact_id: str,
    missing_field: str,
    mutation: str,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    target = root / f"{artifact_id}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    if mutation == "empty":
        payload = {}
    else:
        payload.pop(missing_field)
    _write_json(target, payload)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact_id}/projection")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "artifact_schema_mismatch"


@pytest.mark.parametrize(
    "artifact_id",
    (
        "run_manifest",
        "validated_input",
        "deterministic_result",
        "narrative_draft",
        "constitution_check",
        "review_packet",
    ),
)
def test_each_allowlisted_artifact_rejects_wrong_case_identity(
    tmp_path: Path,
    artifact_id: str,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    target = root / f"{artifact_id}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["case_id"] = "other-case"
    _write_json(target, payload)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact_id}/projection")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "manifest_case_mismatch" if artifact_id == "run_manifest" else "artifact_case_mismatch"
    )


@pytest.mark.parametrize(
    "artifact_id",
    (
        "run_manifest",
        "validated_input",
        "deterministic_result",
        "narrative_draft",
        "constitution_check",
        "review_packet",
    ),
)
def test_each_allowlisted_artifact_rejects_wrong_run_identity_when_present(
    tmp_path: Path,
    artifact_id: str,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    target = root / f"{artifact_id}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["run_id"] = "other-run"
    _write_json(target, payload)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact_id}/projection")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "manifest_run_mismatch" if artifact_id == "run_manifest" else "artifact_run_mismatch"
    )


@pytest.mark.parametrize("artifact_id", ("run_manifest", "validated_input", "deterministic_result"))
def test_applicable_artifacts_reject_wrong_tool_identity(
    tmp_path: Path,
    artifact_id: str,
) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    target = root / f"{artifact_id}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["tool_id"] = "other-tool"
    _write_json(target, payload)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact_id}/projection")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "manifest_tool_mismatch" if artifact_id == "run_manifest" else "artifact_tool_mismatch"
    )


def test_legacy_deterministic_result_rejects_wrong_method_identity(tmp_path: Path) -> None:
    client, root, _, run_id = _projection_fixture(tmp_path)
    target = root / "deterministic_result.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload.pop("tool_id")
    payload["method"] = "other-method"
    _write_json(target, payload)

    response = client.get(f"/runs/{run_id}/artifacts/deterministic_result/projection")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "artifact_method_mismatch"


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
        read_bounded_json_object(Path("/dev"), "null")

    assert exc_info.value.code == "artifact_not_regular"


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="POSIX socket fixture is unavailable",
)
def test_bounded_reader_rejects_unix_socket_without_blocking(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(target))
        with pytest.raises(ArtifactProjectionReadError) as exc_info:
            read_bounded_json_object(tmp_path, target.name)
    finally:
        listener.close()

    assert exc_info.value.code == "artifact_unreadable"


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


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle accounting is unavailable")
def test_windows_bounded_reader_closes_handles_on_error(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        )
        return int(count.value)

    target = tmp_path / "validated_input.json"
    target.write_text("{broken", encoding="utf-8")
    before = handle_count()

    for _ in range(50):
        with pytest.raises(ArtifactProjectionReadError) as exc_info:
            read_bounded_json_object(tmp_path, target.name)
        assert exc_info.value.code == "artifact_invalid_json"

    assert handle_count() == before


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
