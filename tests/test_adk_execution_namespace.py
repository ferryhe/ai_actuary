from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from reserving_workflow.api import app as api_app
from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.runtime.adk_execution import AdkStartRequest, prepare_isolated_run_root
from reserving_workflow.runtime.run_registry import list_runs
from reserving_workflow.storage import safe_json
from reserving_workflow.storage.safe_json import PinnedJsonRoot, SafeJsonReadError


class _FakeWorkerTask:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeTaskContracts:
    WorkerTask = _FakeWorkerTask


class _ReviewRunner:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        del user_prompt
        artifact_dir = Path(task.inputs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        packet = {
            "case_id": task.case_ref,
            "run_id": task.run_id,
            "status": "review_required",
            "failed_checks": ["threshold"],
        }
        (artifact_dir / "review_packet.json").write_text(
            json.dumps(packet), encoding="utf-8"
        )
        return {
            "route": {"mode": "governed"},
            "trace": {"workflow_name": "test-workflow"},
            "worker_result": {
                "status": "needs_review",
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "summary": "needs review",
                "artifact_paths": {},
                "metrics": {},
                "review_reasons": ["threshold"],
                "errors": [],
                "worker_metadata": {"adapter": "fake"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "needs_review",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 1.0},
                "review_reasons": ["threshold"],
                "artifact_manifest_path": None,
                "narrative_summary": "needs review",
            },
            "review_packet": packet,
        }


class _CompletedRunner:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        del user_prompt
        artifact_dir = Path(task.inputs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        step_manifest = artifact_dir / "run_manifest.json"
        step_manifest.write_text(
            json.dumps(
                {
                    "case_id": task.case_ref,
                    "run_id": task.run_id,
                    "artifact_paths": {"run_manifest": "run_manifest.json"},
                }
            ),
            encoding="utf-8",
        )
        return {
            "route": {"mode": "governed"},
            "trace": {"workflow_name": "test-workflow"},
            "worker_result": {
                "status": "completed",
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "summary": "completed",
                "artifact_paths": {"run_manifest": str(step_manifest)},
                "metrics": {},
                "review_reasons": [],
                "errors": [],
                "worker_metadata": {"adapter": "fake"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "completed",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 1.0},
                "review_reasons": [],
                "artifact_manifest_path": str(step_manifest),
                "narrative_summary": "completed",
            },
        }


class _RootSwapRunner:
    target_root: Path
    outside_root: Path
    replacement: Path | None = None

    @classmethod
    def run_openai_governed_workflow(cls, task, *, user_prompt=None):
        parked = cls.target_root.with_name(f"{cls.target_root.name}-during-run")
        try:
            cls.target_root.rename(parked)
        except OSError:
            pass
        else:
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "cmd",
                        "/c",
                        "mklink",
                        "/J",
                        str(cls.target_root),
                        str(cls.outside_root),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    pytest.skip("junction creation is unavailable")
            else:
                cls.target_root.symlink_to(cls.outside_root, target_is_directory=True)
            cls.replacement = cls.target_root
        return _CompletedRunner.run_openai_governed_workflow(
            task, user_prompt=user_prompt
        )


class _NestedStepAttackRunner:
    target_step: Path
    outside_file: Path
    outside_root: Path
    attack: str
    replacement: Path | None = None

    @classmethod
    def run_openai_governed_workflow(cls, task, *, user_prompt=None):
        if cls.attack == "hardlink":
            os.link(cls.outside_file, cls.target_step / "validated_input.json")
        else:
            parked = cls.target_step.with_name(f"{cls.target_step.name}-parked")
            cls.target_step.rename(parked)
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "cmd",
                        "/c",
                        "mklink",
                        "/J",
                        str(cls.target_step),
                        str(cls.outside_root),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    pytest.skip("junction creation is unavailable")
            else:
                cls.target_step.symlink_to(
                    cls.outside_root, target_is_directory=True
                )
            cls.replacement = cls.target_step
        return _CompletedRunner.run_openai_governed_workflow(
            task, user_prompt=user_prompt
        )


def request(app, method, path, **kwargs):
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(call())


def _start_headers(payload, key="opaque-idempotency-key"):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    grant = hmac.new(
        b"adk-secret-that-is-independent",
        f"{key}:{hashlib.sha256(canonical.encode()).hexdigest()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": "Bearer adk-secret-that-is-independent",
        "Idempotency-Key": key,
        "X-ADK-Confirmation": grant,
    }


def _app(tmp_path, scheduler=lambda fn, params: None):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    return create_app(settings=settings, background_task_runner=scheduler), settings


def _payload(**changes):
    payload = {
        "workflow_id": "chainladder-basic",
        "case_id": "developer-case",
        "inputs": {"sample_name": "RAA", "method_variant": "chainladder"},
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "invocation-1",
    }
    payload.update(changes)
    return payload


def test_adk_start_forces_trusted_namespace_and_is_idempotent(tmp_path):
    app, settings = _app(tmp_path)
    payload = _payload()
    first = request(app, "POST", "/adk/runs", headers=_start_headers(payload), json=payload)
    second = request(app, "POST", "/adk/runs", headers=_start_headers(payload), json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    runs = list_runs(settings.registry_path)
    assert len(runs) == 1
    assert runs[0]["workspace_id"] == "adk-development"
    assert runs[0]["source"] == "adk-developer"
    assert runs[0]["artifact_root"].startswith(str(settings.adk_artifact_root))

    narrowed = request(
        app,
        "GET",
        "/runs?workspace_id=default-workspace",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
    )
    assert narrowed.status_code == 200
    assert narrowed.json()["runs"] == []


def test_idempotency_conflict_and_caller_selected_storage_have_zero_effects(tmp_path):
    app, settings = _app(tmp_path)
    payload = _payload()
    assert request(app, "POST", "/adk/runs", headers=_start_headers(payload), json=payload).status_code == 202
    changed = _payload(case_id="different-case", adk_invocation_id="invocation-2")
    conflict = request(app, "POST", "/adk/runs", headers=_start_headers(changed), json=changed)
    assert conflict.status_code == 409
    injected = _payload(adk_invocation_id="invocation-3", artifact_dir=str(tmp_path / "escape"))
    rejected = request(app, "POST", "/adk/runs", headers=_start_headers(injected, "other-key"), json=injected)
    assert rejected.status_code in {400, 422}
    assert len(list_runs(settings.registry_path)) == 1
    assert not (tmp_path / "escape").exists()


def test_adk_cannot_read_real_operator_object_even_when_ids_are_known(tmp_path):
    app, settings = _app(tmp_path)
    from reserving_workflow.storage.local import LocalRunStore
    store = LocalRunStore(settings.registry_path)
    store.create_run(
        task_id="operator-task", case_id="operator-case", run_id="operator-run-real",
        status="completed", artifact_root=str(tmp_path / "operator-artifacts" / "operator-run-real"),
        operator_id="local-actuary", workspace_id="default-workspace", operator_params={},
    )
    response = request(
        app, "GET", "/runs/operator-run-real",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "object_not_found"
    absent = request(
        app, "GET", "/runs/run-that-does-not-exist",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
    )
    assert absent.status_code == 404
    assert absent.json() == response.json()


def test_concurrent_identical_starts_bind_one_run_and_one_operation(tmp_path):
    app, settings = _app(tmp_path)
    payload = _payload()

    def start(_):
        return request(app, "POST", "/adk/runs", headers=_start_headers(payload), json=payload)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(start, range(4)))
    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["run_id"] for response in responses}) == 1
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    assert len(registry["runs"]) == 1
    assert len(registry["adk_operations"]) == 1


def test_path_url_glob_and_scope_inputs_are_rejected_with_redacted_errors(tmp_path):
    app, settings = _app(tmp_path)
    attacks = (
        {"sample_name": "../outside"},
        {"sample_name": "https://example.invalid/data"},
        {"sample_name": "*.csv"},
        {"workspace_id": "default-workspace"},
    )
    for index, inputs in enumerate(attacks):
        payload = _payload(inputs=inputs, adk_invocation_id=f"attack-{index}")
        response = request(app, "POST", "/adk/runs", headers=_start_headers(payload, f"attack-idempotency-{index}"), json=payload)
        assert response.status_code == 400
        assert str(tmp_path) not in response.text
        assert "example.invalid" not in response.text
    assert list_runs(settings.registry_path) == []


@pytest.mark.parametrize(
    "inputs",
    (
        {"triangle_rows": [[index, index + 1] for index in range(10_000)]},
        {"nested": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}},
        {"value": "x" * 10_000},
    ),
)
def test_unbounded_or_nonfinite_inputs_have_zero_durable_effects(tmp_path, inputs):
    app, settings = _app(tmp_path)
    payload = _payload(inputs=inputs, adk_invocation_id="bounded-rejection")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "bounded-rejection-key"),
        json=payload,
    )
    assert response.status_code in {400, 422}
    assert len(response.content) < 1_000
    assert not settings.registry_path.exists()
    assert not settings.adk_artifact_root.exists()
    assert not settings.review_store_dir.exists()


def test_nonfinite_input_is_rejected_before_transport():
    with pytest.raises(ValueError, match="adk_input_nonfinite"):
        AdkStartRequest(
            workflow_id="chainladder-basic",
            case_id="case-1",
            inputs={"value": float("nan")},
            adk_app="ai_actuary_developer",
            adk_session_id="session-1",
            adk_invocation_id="invocation-1",
        )


def test_isolated_root_rejects_symlink_or_junction_boundary(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_root = tmp_path / "symlink-root"
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(symlink_root), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("junction creation is unavailable")
    else:
        try:
            symlink_root.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable on this platform: {exc}")
    with pytest.raises(ValueError, match="adk_artifact_boundary_violation"):
        prepare_isolated_run_root(symlink_root, "run-1")


def test_isolated_root_rejects_hardlink_boundary(tmp_path):
    outside_file = tmp_path / "outside-file"
    outside_file.write_text("outside", encoding="utf-8")
    hardlink_root = tmp_path / "hardlink-root"
    os.link(outside_file, hardlink_root)
    with pytest.raises(ValueError, match="adk_artifact_boundary_violation"):
        prepare_isolated_run_root(hardlink_root, "run-2")


def test_initial_manifest_rejects_root_replacement_without_outside_write(
    tmp_path, monkeypatch
):
    app, settings = _app(tmp_path)
    outside = tmp_path / "outside-root"
    outside.mkdir()
    original_prepare = api_app.prepare_isolated_run_root
    replaced_root: list[Path] = []

    def replace_after_prepare(root, run_id):
        target = original_prepare(root, run_id)
        target.rmdir()
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                pytest.skip("junction creation is unavailable")
        else:
            target.symlink_to(outside, target_is_directory=True)
        replaced_root.append(target)
        return target

    monkeypatch.setattr(api_app, "prepare_isolated_run_root", replace_after_prepare)
    payload = _payload(adk_invocation_id="root-replacement")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "root-replacement-key"),
        json=payload,
    )
    assert response.status_code == 202
    assert not (outside / "run_manifest.json").exists()
    assert get_run_status(settings.registry_path, response.json()["run_id"]) == "failed"
    target = replaced_root[0]
    if os.name == "nt":
        os.rmdir(target)
    else:
        target.unlink()


def test_initial_manifest_exclusive_create_does_not_overwrite_hardlink(
    tmp_path, monkeypatch
):
    app, settings = _app(tmp_path)
    outside_file = tmp_path / "outside-manifest.json"
    outside_file.write_text("outside", encoding="utf-8")
    original_prepare = api_app.prepare_isolated_run_root

    def precreate_hardlink(root, run_id):
        target = original_prepare(root, run_id)
        os.link(outside_file, target / "run_manifest.json")
        return target

    monkeypatch.setattr(api_app, "prepare_isolated_run_root", precreate_hardlink)
    payload = _payload(adk_invocation_id="hardlink-precreation")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "hardlink-precreation-key"),
        json=payload,
    )
    assert response.status_code == 202
    assert outside_file.read_text(encoding="utf-8") == "outside"
    assert get_run_status(settings.registry_path, response.json()["run_id"]) == "failed"


@pytest.mark.parametrize("atomic", (False, True))
@pytest.mark.parametrize("failure_mode", ("partial_write", "fsync", "verify"))
def test_pinned_write_failures_remove_only_unpublished_inode_and_retry_succeeds(
    tmp_path, monkeypatch, atomic, failure_mode
):
    root = tmp_path / "pinned-write-root"
    root.mkdir()
    with PinnedJsonRoot(
        root, namespace="artifact", allow_nested=True, protect_writes=True
    ) as pinned:
        writer = (
            pinned.write_json_object_atomic
            if atomic
            else pinned.write_json_object_exclusive
        )
        with monkeypatch.context() as patcher:
            if failure_mode == "partial_write":
                real_write = safe_json.os.write
                calls = 0

                def fail_after_partial_write(descriptor, content):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return real_write(
                            descriptor, content[: max(1, len(content) // 2)]
                        )
                    raise OSError("deterministic partial write failure")

                patcher.setattr(safe_json.os, "write", fail_after_partial_write)
            elif failure_mode == "fsync":
                patcher.setattr(
                    safe_json.os,
                    "fsync",
                    lambda descriptor: (_ for _ in ()).throw(
                        OSError("deterministic fsync failure")
                    ),
                )
            else:
                patcher.setattr(
                    pinned,
                    "verify_configured_root_identity",
                    lambda **kwargs: (_ for _ in ()).throw(
                        SafeJsonReadError(
                            "artifact_path_rejected",
                            "Registered artifact path failed safety validation.",
                        )
                    ),
                )

            with pytest.raises(SafeJsonReadError):
                writer("artifact.json", {"value": 1})

        assert list(root.iterdir()) == []
        writer("artifact.json", {"value": 1})
        assert json.loads((root / "artifact.json").read_text(encoding="utf-8")) == {
            "value": 1
        }


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative race")
@pytest.mark.parametrize("atomic", (False, True))
def test_posix_nested_parent_rename_after_create_fails_and_removes_moved_artifact(
    tmp_path, monkeypatch, atomic
):
    root = tmp_path / "pinned-root"
    step = root / "step"
    outside = tmp_path / "outside"
    moved_step = outside / "moved-step"
    step.mkdir(parents=True)
    outside.mkdir()
    real_create = safe_json._create_anonymous_posix
    moved = False

    def create_then_move(parent_descriptor, *, namespace):
        nonlocal moved
        descriptor = real_create(
            parent_descriptor,
            namespace=namespace,
        )
        if not moved:
            step.rename(moved_step)
            moved = True
        return descriptor

    monkeypatch.setattr(
        safe_json,
        "_create_anonymous_posix",
        create_then_move,
    )
    with PinnedJsonRoot(
        root,
        namespace="artifact",
        allow_nested=True,
        protect_writes=True,
    ) as pinned:
        writer = (
            pinned.write_json_object_atomic
            if atomic
            else pinned.write_json_object_exclusive
        )
        with pytest.raises(SafeJsonReadError) as rejected:
            writer("step/artifact.json", {"value": 1})

    assert rejected.value.code == "artifact_path_rejected"
    assert moved
    assert not (moved_step / "artifact.json").exists()
    assert not list(moved_step.glob(".adk-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
@pytest.mark.parametrize("atomic", (False, True))
def test_windows_nested_parent_cannot_move_during_write(tmp_path, monkeypatch, atomic):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    root = tmp_path / "pinned-root"
    nested = root / "a" / "b"
    movable_ancestor = nested
    outside = tmp_path / "outside"
    moved_parent = outside / "moved-a"
    nested.mkdir(parents=True)
    outside.mkdir()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    attacker_handle = kernel32.CreateFileW(
        str(movable_ancestor),
        0x00010000 | 0x00100000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    assert attacker_handle not in {None, wintypes.HANDLE(-1).value}
    attacker_descriptor = msvcrt.open_osfhandle(int(attacker_handle), 0)
    outside_handle = safe_json._windows_open_handle(
        outside,
        expect_directory=True,
        namespace="artifact",
    )
    real_open_parent = safe_json._open_relative_parent_windows
    moved = False

    def open_then_move(root_handle, parts, *, namespace):
        nonlocal moved
        parent_handle = real_open_parent(root_handle, parts, namespace=namespace)
        safe_json._replace_relative_windows(
            attacker_descriptor,
            outside_handle,
            moved_parent.name,
        )
        moved = True
        return parent_handle

    monkeypatch.setattr(safe_json, "_open_relative_parent_windows", open_then_move)
    try:
        with PinnedJsonRoot(
            root,
            namespace="artifact",
            allow_nested=True,
            protect_writes=True,
        ) as pinned:
            writer = (
                pinned.write_json_object_atomic
                if atomic
                else pinned.write_json_object_exclusive
            )
            with pytest.raises(SafeJsonReadError):
                writer("a/b/artifact.json", {"sensitive": "must-not-escape"})
    finally:
        os.close(attacker_descriptor)
        safe_json._windows_close_handle(outside_handle)

    assert not moved
    assert movable_ancestor.is_dir()
    assert not moved_parent.exists()


@pytest.mark.parametrize("atomic", (False, True))
def test_writable_inode_cannot_be_hardlinked_after_first_write(
    tmp_path, monkeypatch, atomic
):
    root = tmp_path / "pinned-root"
    outside = tmp_path / "outside-payload.json"
    root.mkdir()
    real_write = safe_json.os.write
    linked = False
    link_blocked = False

    def write_then_link(descriptor, payload):
        nonlocal linked, link_blocked
        written = real_write(descriptor, payload)
        if not linked:
            candidates = list(root.iterdir())
            if not candidates:
                link_blocked = True
                return written
            source = candidates[0]
            try:
                os.link(source, outside)
            except OSError:
                link_blocked = True
            else:
                linked = True
        return written

    monkeypatch.setattr(safe_json.os, "write", write_then_link)
    with PinnedJsonRoot(
        root,
        namespace="artifact",
        protect_writes=True,
    ) as pinned:
        writer = (
            pinned.write_json_object_atomic
            if atomic
            else pinned.write_json_object_exclusive
        )
        try:
            writer("artifact.json", {"sensitive": "must-not-escape"})
        except SafeJsonReadError:
            pass

    assert link_blocked or linked
    assert not outside.exists()
    assert json.loads((root / "artifact.json").read_text(encoding="utf-8")) == {
        "sensitive": "must-not-escape"
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cleanup semantics")
@pytest.mark.parametrize("atomic", (False, True))
def test_posix_published_file_relocation_is_rejected_and_exact_inode_is_removed(
    tmp_path, monkeypatch, atomic
):
    root = tmp_path / "pinned-root"
    outside = tmp_path / "outside-payload.json"
    root.mkdir()

    if atomic:
        real_replace = safe_json.os.replace

        def replace_then_move(source, target, **kwargs):
            real_replace(source, target, **kwargs)
            (root / target).rename(outside)

        monkeypatch.setattr(safe_json.os, "replace", replace_then_move)
    else:
        real_publish = safe_json._publish_anonymous_posix

        def publish_then_move(descriptor, parent_descriptor, name):
            published_descriptor = real_publish(
                descriptor, parent_descriptor, name
            )
            (root / name).rename(outside)
            return published_descriptor

        monkeypatch.setattr(
            safe_json,
            "_publish_anonymous_posix",
            publish_then_move,
        )

    with PinnedJsonRoot(
        root,
        namespace="artifact",
        protect_writes=True,
    ) as pinned:
        writer = (
            pinned.write_json_object_atomic
            if atomic
            else pinned.write_json_object_exclusive
        )
        with pytest.raises(SafeJsonReadError):
            writer("artifact.json", {"sensitive": "must-not-escape"})

    assert not outside.exists()
    assert not (root / "artifact.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
@pytest.mark.parametrize("atomic", (False, True))
def test_windows_final_file_cannot_move_during_publish(tmp_path, monkeypatch, atomic):
    root = tmp_path / "pinned-root"
    outside = tmp_path / "outside-payload.json"
    root.mkdir()
    moved = False

    if atomic:
        real_replace = safe_json._replace_relative_windows

        def replace_then_move(descriptor, parent_handle, target_name):
            nonlocal moved
            real_replace(descriptor, parent_handle, target_name)
            (root / target_name).rename(outside)
            moved = True

        monkeypatch.setattr(
            safe_json,
            "_replace_relative_windows",
            replace_then_move,
        )
    else:
        real_write = safe_json.os.write

        def write_then_move(descriptor, payload):
            nonlocal moved
            written = real_write(descriptor, payload)
            (root / "artifact.json").rename(outside)
            moved = True
            return written

        monkeypatch.setattr(safe_json.os, "write", write_then_move)

    with PinnedJsonRoot(
        root,
        namespace="artifact",
        protect_writes=True,
    ) as pinned:
        writer = (
            pinned.write_json_object_atomic
            if atomic
            else pinned.write_json_object_exclusive
        )
        with pytest.raises(SafeJsonReadError):
            writer("artifact.json", {"sensitive": "must-not-escape"})

    assert not moved
    assert not outside.exists()


def test_workflow_rejects_root_replacement_without_outside_write(tmp_path):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    outside = tmp_path / "outside-workflow"
    outside.mkdir()
    replacement: list[Path] = []

    def scheduler(fn, params):
        target = Path(params["artifact_dir"])
        parked = target.with_name(f"{target.name}-parked")
        target.rename(parked)
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                pytest.skip("junction creation is unavailable")
        else:
            target.symlink_to(outside, target_is_directory=True)
        replacement.append(target)
        fn(params)

    app = create_app(
        settings=settings,
        background_task_runner=scheduler,
        runner_module=_CompletedRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id="workflow-root-replacement")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "workflow-root-replacement-key"),
        json=payload,
    )
    assert response.status_code == 202
    assert list(outside.iterdir()) == []
    assert get_run_status(settings.registry_path, response.json()["run_id"]) == "failed"
    if replacement:
        if os.name == "nt":
            os.rmdir(replacement[0])
        else:
            replacement[0].unlink()


def test_pinned_workflow_root_prevents_replacement_race_escape(tmp_path):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    outside = tmp_path / "outside-during-workflow"
    outside.mkdir()
    _RootSwapRunner.target_root = settings.adk_artifact_root
    _RootSwapRunner.outside_root = outside
    _RootSwapRunner.replacement = None

    def scheduler(fn, params):
        _RootSwapRunner.target_root = Path(params["artifact_dir"])
        fn(params)

    app = create_app(
        settings=settings,
        background_task_runner=scheduler,
        runner_module=_RootSwapRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id="during-workflow-root-replacement")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "during-workflow-root-replacement-key"),
        json=payload,
    )
    assert response.status_code == 202
    assert list(outside.iterdir()) == []
    if _RootSwapRunner.replacement is not None:
        assert get_run_status(settings.registry_path, response.json()["run_id"]) == "failed"
        if os.name == "nt":
            os.rmdir(_RootSwapRunner.replacement)
        else:
            _RootSwapRunner.replacement.unlink()


def test_final_manifest_atomic_replace_does_not_mutate_outside_hardlink(tmp_path):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    outside = tmp_path / "outside-hardlink.json"
    initial_bytes: list[bytes] = []

    def scheduler(fn, params):
        manifest = Path(params["artifact_dir"]) / "run_manifest.json"
        initial_bytes.append(manifest.read_bytes())
        os.link(manifest, outside)
        fn(params)

    app = create_app(
        settings=settings,
        background_task_runner=scheduler,
        runner_module=_CompletedRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id="final-hardlink")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "final-hardlink-key"),
        json=payload,
    )
    assert response.status_code == 202
    assert get_run_status(settings.registry_path, response.json()["run_id"]) == "completed"
    assert outside.read_bytes() == initial_bytes[0]
    run_manifest = (
        settings.adk_artifact_root / response.json()["run_id"] / "run_manifest.json"
    )
    assert run_manifest.read_bytes() != initial_bytes[0]
    manifest_payload = json.loads(run_manifest.read_text(encoding="utf-8"))
    assert manifest_payload["artifact_paths"]["workflow_summary"] == (
        "workflow_summary.json"
    )


@pytest.mark.parametrize("attack", ("hardlink", "directory_link"))
def test_real_adk_post_rejects_precreated_nested_step_escape(tmp_path, attack):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    outside = tmp_path / "outside-nested-step"
    outside.mkdir()
    sentinel = outside / "sentinel.json"
    sentinel.write_text("outside", encoding="utf-8")

    def scheduler(fn, params):
        step_root = Path(params["artifact_dir"]) / "chainladder"
        if attack == "hardlink":
            step_root.mkdir()
            os.link(sentinel, step_root / "validated_input.json")
        elif os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(step_root), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                pytest.skip("junction creation is unavailable")
        else:
            step_root.symlink_to(outside, target_is_directory=True)
        fn(params)

    app = create_app(
        settings=settings,
        background_task_runner=scheduler,
        runner_module=_CompletedRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id=f"nested-{attack}")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, f"nested-{attack}-key"),
        json=payload,
    )

    assert response.status_code == 202
    assert sentinel.read_text(encoding="utf-8") == "outside"
    assert list(outside.iterdir()) == [sentinel]
    assert get_run_status(settings.registry_path, response.json()["run_id"]) == "failed"


@pytest.mark.parametrize("attack", ("hardlink", "directory_link"))
def test_nested_step_replacement_during_runner_cannot_escape_pinned_writer(
    tmp_path, attack
):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    outside_root = tmp_path / "outside-during-step"
    outside_root.mkdir()
    outside_file = outside_root / "sentinel.json"
    outside_file.write_text("outside", encoding="utf-8")
    _NestedStepAttackRunner.attack = attack
    _NestedStepAttackRunner.outside_file = outside_file
    _NestedStepAttackRunner.outside_root = outside_root
    _NestedStepAttackRunner.replacement = None

    def scheduler(fn, params):
        _NestedStepAttackRunner.target_step = (
            Path(params["artifact_dir"]) / "chainladder"
        )
        fn(params)

    app = create_app(
        settings=settings,
        background_task_runner=scheduler,
        runner_module=_NestedStepAttackRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id=f"during-step-{attack}")
    response = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, f"during-step-{attack}-key"),
        json=payload,
    )

    assert response.status_code == 202
    assert outside_file.read_text(encoding="utf-8") == "outside"
    assert list(outside_root.iterdir()) == [outside_file]
    assert get_run_status(settings.registry_path, response.json()["run_id"]) == "failed"
    if _NestedStepAttackRunner.replacement is not None:
        if os.name == "nt":
            os.rmdir(_NestedStepAttackRunner.replacement)
        else:
            _NestedStepAttackRunner.replacement.unlink()


def get_run_status(registry_path, run_id):
    return next(
        run["status"] for run in list_runs(registry_path) if run["run_id"] == run_id
    )


def _storage_snapshot(*roots: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                snapshot[f"{root.name}/{path.relative_to(root).as_posix()}"] = (
                    path.read_bytes()
                )
    return snapshot


def test_cross_scope_review_decisions_have_zero_storage_side_effects(tmp_path):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    app = create_app(
        settings=settings,
        background_task_runner=lambda fn, params: fn(params),
        runner_module=_ReviewRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id="decision-scope-invocation")
    started = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "decision-scope-key"),
        json=payload,
    )
    adk_run_id = started.json()["run_id"]
    roots = (
        settings.registry_path.parent,
        settings.adk_artifact_root,
        settings.review_store_dir,
    )
    before_operator_denial = _storage_snapshot(*roots)

    operator_denial = request(
        app,
        "POST",
        f"/reviews/review-{adk_run_id}/decision",
        headers={
            "Authorization": "Bearer operator-secret-that-is-independent",
            "Origin": "http://testserver",
        },
        json={"decision": "approved"},
    )

    assert operator_denial.status_code == 404
    assert operator_denial.json()["detail"]["code"] == "object_not_found"
    assert _storage_snapshot(*roots) == before_operator_denial

    from reserving_workflow.storage.local import LocalReviewStore, LocalRunStore

    LocalRunStore(settings.registry_path).create_run(
        task_id="operator-review-task",
        case_id="operator-review-case",
        run_id="operator-review-run",
        status="needs_review",
        artifact_root=str(settings.artifact_root / "operator-review-run"),
        operator_id="local-actuary",
        workspace_id="default-workspace",
    )
    LocalReviewStore(settings.review_store_dir).create_review(
        review_id="review-operator-review-run",
        run_id="operator-review-run",
        case_id="operator-review-case",
        status="pending",
        workspace_id="default-workspace",
    )
    before_adk_denial = _storage_snapshot(*roots)

    adk_denial = request(
        app,
        "POST",
        "/reviews/review-operator-review-run/decision",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
        json={"decision": "approved"},
    )

    assert adk_denial.status_code == 403
    assert _storage_snapshot(*roots) == before_adk_denial


def test_unrelated_corrupt_adk_record_cannot_leak_real_operator_object_existence(
    tmp_path
):
    app, settings = _app(
        tmp_path,
        scheduler=lambda fn, params: fn(params),
    )
    app = create_app(
        settings=settings,
        background_task_runner=lambda fn, params: fn(params),
        runner_module=_ReviewRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    operator_headers = {
        "Authorization": "Bearer operator-secret-that-is-independent",
        "Origin": "http://testserver",
    }
    created = request(
        app,
        "POST",
        "/runs",
        headers=operator_headers,
        json={
            "case_id": "operator-route-matrix",
            "workflow_id": "chainladder-basic",
            "inputs": {"sample_name": "RAA"},
        },
    )
    assert created.status_code == 200, created.text
    operator_run_id = created.json()["run_id"]
    operator_review_id = f"review-{operator_run_id}"
    operator_read_paths = (
        f"/runs/{operator_run_id}",
        f"/runs/{operator_run_id}/events",
        f"/runs/{operator_run_id}/artifacts",
        f"/runs/{operator_run_id}/artifacts/run_manifest/projection",
        f"/runs/{operator_run_id}/results",
        f"/runs/{operator_run_id}/review-packet",
        f"/runs/{operator_run_id}/review",
        f"/reviews/{operator_review_id}",
    )
    for path in operator_read_paths:
        materialized = request(
            app,
            "GET",
            path,
            headers={"Authorization": operator_headers["Authorization"]},
        )
        assert materialized.status_code == 200, (path, materialized.text)
    report = request(
        app,
        "POST",
        f"/runs/{operator_run_id}/report-export",
        headers=operator_headers,
    )
    assert report.status_code == 200, report.text

    payload = _payload(adk_invocation_id="unrelated-corrupt-invocation")
    started = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "unrelated-corrupt-key"),
        json=payload,
    )
    corrupt_adk_run_id = started.json()["run_id"]
    registry = json.loads(settings.registry_path.read_text(encoding="utf-8"))
    corrupt_entry = next(
        run for run in registry["runs"] if run["run_id"] == corrupt_adk_run_id
    )
    corrupt_entry.pop("source")
    settings.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    roots = (
        settings.registry_path.parent,
        settings.artifact_root,
        settings.adk_artifact_root,
        settings.review_store_dir,
    )
    before = _storage_snapshot(*roots)
    adk_headers = {
        "Authorization": "Bearer adk-secret-that-is-independent"
    }
    read_pairs = (
        (f"/runs/{operator_run_id}", "/runs/absent-operator-run"),
        (
            f"/runs/{operator_run_id}/events",
            "/runs/absent-operator-run/events",
        ),
        (
            f"/runs/{operator_run_id}/artifacts",
            "/runs/absent-operator-run/artifacts",
        ),
        (
            f"/runs/{operator_run_id}/artifacts/run_manifest/projection",
            "/runs/absent-operator-run/artifacts/run_manifest/projection",
        ),
        (
            f"/runs/{operator_run_id}/results",
            "/runs/absent-operator-run/results",
        ),
        (
            f"/runs/{operator_run_id}/review-packet",
            "/runs/absent-operator-run/review-packet",
        ),
        (
            f"/runs/{operator_run_id}/review",
            "/runs/absent-operator-run/review",
        ),
        (
            f"/reviews/{operator_review_id}",
            "/reviews/review-absent-operator-run",
        ),
    )
    for known_path, absent_path in read_pairs:
        known = request(app, "GET", known_path, headers=adk_headers)
        absent = request(app, "GET", absent_path, headers=adk_headers)
        assert known.status_code == absent.status_code == 404, (
            known_path,
            known.text,
            absent.text,
        )
        assert known.json() == absent.json(), known_path

    for method, path in (
        ("POST", f"/runs/{operator_run_id}/rerun"),
        ("POST", f"/runs/{operator_run_id}/report-export"),
        ("POST", f"/reviews/{operator_review_id}/decision"),
    ):
        forbidden = request(
            app,
            method,
            path,
            headers={**adk_headers, "Origin": "http://testserver"},
            json={"decision": "approved"} if path.endswith("/decision") else None,
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "capability_forbidden"

    in_scope_corrupt = request(
        app,
        "GET",
        f"/runs/{corrupt_adk_run_id}",
        headers=adk_headers,
    )
    assert in_scope_corrupt.status_code == 409
    assert in_scope_corrupt.json()["detail"]["code"] == "adk_provenance_invalid"
    assert _storage_snapshot(*roots) == before


def test_adk_needs_review_is_partitioned_from_operator_inbox(tmp_path):
    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    app = create_app(
        settings=settings,
        background_task_runner=lambda fn, params: fn(params),
        runner_module=_ReviewRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id="review-invocation")
    started = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "review-idempotency-key"),
        json=payload,
    )
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    adk_reviews = request(
        app,
        "GET",
        "/reviews",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
    )
    assert adk_reviews.status_code == 200
    assert [review["run_id"] for review in adk_reviews.json()["reviews"]] == [run_id]
    review_packet = request(
        app,
        "GET",
        f"/runs/{run_id}/review-packet",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
    )
    assert review_packet.status_code == 200
    assert "json_path" not in review_packet.json()
    assert "markdown_path" not in review_packet.json()

    operator_reviews = request(
        app,
        "GET",
        "/reviews",
        headers={"Authorization": "Bearer operator-secret-that-is-independent"},
    )
    assert operator_reviews.status_code == 200
    assert operator_reviews.json()["reviews"] == []

    forbidden_decision = request(
        app,
        "POST",
        f"/reviews/review-{run_id}/decision",
        headers={"Authorization": "Bearer adk-secret-that-is-independent"},
        json={"decision": "approve"},
    )
    assert forbidden_decision.status_code == 403


def test_review_list_applies_trusted_source_before_legacy_identity_filters(
    tmp_path,
):
    from reserving_workflow.storage.local import LocalRunStore

    settings = ApiSettings(
        registry_path=tmp_path / "registry.json",
        artifact_root=tmp_path / "operator-artifacts",
        adk_artifact_root=tmp_path / "adk-artifacts",
        review_store_dir=tmp_path / "reviews",
        operator_credential="operator-secret-that-is-independent",
        adk_credential="adk-secret-that-is-independent",
        operator_bootstrap_token="bootstrap-secret",
        operator_origin="http://testserver",
    )
    app = create_app(
        settings=settings,
        background_task_runner=lambda fn, params: fn(params),
        runner_module=_ReviewRunner,
        task_contracts_module=_FakeTaskContracts,
    )
    payload = _payload(adk_invocation_id="review-source-scope-invocation")
    started = request(
        app,
        "POST",
        "/adk/runs",
        headers=_start_headers(payload, "review-source-scope-key"),
        json=payload,
    )
    assert started.status_code == 202
    adk_run_id = started.json()["run_id"]
    store = LocalRunStore(settings.registry_path)
    store.create_run(
        task_id="legacy-collision-task",
        case_id="legacy-collision-case",
        run_id="legacy-collision-run",
        status="needs_review",
        operator_id="adk-developer",
        workspace_id="adk-development",
    )
    store.create_run(
        task_id="legacy-operator-task",
        case_id="legacy-operator-case",
        run_id="legacy-operator-run",
        status="needs_review",
        operator_id="local-actuary",
        workspace_id="default-workspace",
    )
    roots = (
        settings.registry_path.parent,
        settings.artifact_root,
        settings.adk_artifact_root,
        settings.review_store_dir,
    )
    before = _storage_snapshot(*roots)
    adk_headers = {"Authorization": "Bearer adk-secret-that-is-independent"}

    adk_reviews = request(app, "GET", "/reviews", headers=adk_headers)
    collision = request(
        app,
        "GET",
        "/reviews/review-legacy-collision-run",
        headers=adk_headers,
    )
    absent = request(
        app,
        "GET",
        "/reviews/review-absent-legacy-run",
        headers=adk_headers,
    )
    operator_reviews = request(
        app,
        "GET",
        "/reviews",
        headers={"Authorization": "Bearer operator-secret-that-is-independent"},
    )

    assert adk_reviews.status_code == 200
    assert [item["run_id"] for item in adk_reviews.json()["reviews"]] == [
        adk_run_id
    ]
    assert collision.status_code == absent.status_code == 404
    assert collision.json() == absent.json()
    assert [item["run_id"] for item in operator_reviews.json()["reviews"]] == [
        "legacy-operator-run"
    ]
    assert _storage_snapshot(*roots) == before
