from __future__ import annotations

import gc
import hashlib
import importlib.util
import inspect
import multiprocessing
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import reserving_workflow.adapters.adk.source_integrity as source_integrity_module
import reserving_workflow.adapters.adk.workflow_lab as workflow_lab_module

pytest.importorskip(
    "google.adk", reason="Workflow Lab is provided by the adk-dev extra"
)

from reserving_workflow.adapters.adk.source_integrity import (  # noqa: I001
    assert_source_integrity_unchanged,
    capture_source_integrity,
)
from reserving_workflow.adapters.adk.workflow_lab import (
    _PinnedOutputDirectory,
    WorkflowLab,
    WorkflowLabError,
    _build_patch,
    _remove_owned_tree,
    _run_windows_low_integrity_process,
    _write_exclusive,
    _validate_relative_names,
)


SAFE_AGENT = """\
agent_class: SequentialAgent
name: workflow_lab_example
description: A model-free declarative workflow used by the Workflow Lab.
"""

SAFE_POLICY = """\
schema_version: ai-actuary.workflow-policy.v1
capability: adk-developer
workspace_id: adk-development
confirmation_required: true
publishing: git-review-only
tool_ids:
  - chainladder
workflow_ids:
  - chainladder-basic
python_fqns: []
write_tool_ids: []
"""


def _raw_replace_output_bytes(path: Path, content: bytes) -> bool:
    if os.name != "nt":
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            path.write_bytes(content)
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x40000000,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x80,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        return False
    try:
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(content)
        if not kernel32.WriteFile(
            handle,
            buffer,
            len(content),
            ctypes.byref(written),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(content) or not kernel32.SetEndOfFile(handle):
            raise OSError("raw output replacement was incomplete")
        if not kernel32.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        kernel32.CloseHandle(handle)


def _raw_write_windows_ads(path: Path, content: bytes) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        f"{path}:issue38",
        0x40000000,
        0x1 | 0x2 | 0x4,
        None,
        4,  # OPEN_ALWAYS
        0x80,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        return False
    try:
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(content)
        if not kernel32.WriteFile(
            handle,
            buffer,
            len(content),
            ctypes.byref(written),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(content) or not kernel32.FlushFileBuffers(handle):
            raise OSError("raw ADS write was incomplete")
        return True
    finally:
        kernel32.CloseHandle(handle)


def _round8_hold_revoke_lock(
    export_dir: str,
    ready: multiprocessing.synchronize.Event,
) -> None:
    from reserving_workflow.adapters.adk.workflow_lab import _export_revoke_lock

    with _export_revoke_lock(Path(export_dir)):
        ready.set()
        time.sleep(60)


def _round8_revoke_in_child(
    repo_root: str,
    receipt_payload: dict[str, object],
    result: multiprocessing.queues.Queue,
) -> None:
    from reserving_workflow.adapters.adk.workflow_lab import (
        ExportReceipt,
        WorkflowLab,
        _ExportCommitBinding,
        _ReceiptObjectIdentity,
    )

    directories = tuple(
        _ReceiptObjectIdentity(int(device), int(inode), file_id)
        for device, inode, file_id in receipt_payload["directories"]  # type: ignore[union-attr]
    )
    manifest_identity = receipt_payload["manifest_identity"]
    assert isinstance(manifest_identity, tuple)
    binding = _ExportCommitBinding(
        directory_chain=directories,
        manifest=_ReceiptObjectIdentity(
            int(manifest_identity[0]),
            int(manifest_identity[1]),
            manifest_identity[2],
        ),
    )
    receipt = ExportReceipt(
        export_id=str(receipt_payload["export_id"]),
        export_dir=Path(str(receipt_payload["export_dir"])),
        bundle_digest=str(receipt_payload["bundle_digest"]),
        candidate_digest=str(receipt_payload["candidate_digest"]),
        patch_digest=str(receipt_payload["patch_digest"]),
        manifest=receipt_payload["manifest"],  # type: ignore[arg-type]
        commit_binding=binding,
    )
    try:
        WorkflowLab.for_source_checkout(Path(repo_root)).revoke_export_commit(receipt)
    except BaseException as exc:  # noqa: BLE001 - child reports termination failures
        result.put(("error", type(exc).__name__, str(exc)))
    else:
        result.put(("ok",))


def _repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    git = repo / ".git"
    git.mkdir(parents=True)
    (git / "index").write_bytes(b"frozen-index")
    published = repo / "src" / "reserving_workflow" / "developer_workflows" / "example"
    published.mkdir(parents=True)
    (published / "root_agent.yaml").write_text(
        SAFE_AGENT.replace("workflow_lab_example", "published_example"),
        encoding="utf-8",
    )
    (published / "workflow_policy.yaml").write_text(SAFE_POLICY, encoding="utf-8")
    draft = repo / "tmp" / "adk-workflow-drafts" / "example"
    draft.mkdir(parents=True)
    (draft / "root_agent.yaml").write_text(SAFE_AGENT, encoding="utf-8")
    (draft / "workflow_policy.yaml").write_text(SAFE_POLICY, encoding="utf-8")
    return repo, draft, published


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.mark.parametrize(
    "relative_name",
    [
        "agent.py",
        "plugin.zip",
        "sub_agents/evil.exe",
        "sub_agents/con.py",
        "sub_agents/name.yaml:stream",
        "sub_agents/trailing. ",
    ],
)
def test_draft_rejects_executable_binary_ads_and_reserved_names(
    tmp_path: Path, relative_name: str
) -> None:
    repo, draft, _ = _repo(tmp_path)
    candidate = draft.joinpath(*relative_name.split("/"))
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"unsafe")

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.stage == "preflight"
    assert caught.value.code in {"path_forbidden", "file_type_forbidden"}


def test_case_collisions_are_rejected_on_every_platform(tmp_path: Path) -> None:
    if os.name == "nt":
        with pytest.raises(WorkflowLabError) as caught:
            _validate_relative_names(
                {
                    "sub_agents/Reader.yaml": tmp_path / "Reader.yaml",
                    "sub_agents/reader.yaml": tmp_path / "reader.yaml",
                    "root_agent.yaml": tmp_path / "root_agent.yaml",
                    "workflow_policy.yaml": tmp_path / "workflow_policy.yaml",
                },
                require_policy=True,
                stage="preflight",
            )
        assert caught.value.code == "path_case_collision"
        return
    repo, draft, _ = _repo(tmp_path)
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    (sub_agents / "Reader.yaml").write_text(SAFE_AGENT, encoding="utf-8")
    (sub_agents / "reader.yaml").write_text(SAFE_AGENT, encoding="utf-8")

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "path_case_collision"
    assert caught.value.stage == "preflight"


def test_symlink_inputs_are_rejected(tmp_path: Path) -> None:
    repo, draft, _ = _repo(tmp_path)
    external = tmp_path / "outside.yaml"
    external.write_text(SAFE_AGENT, encoding="utf-8")
    link = draft / "sub_agents" / "linked.yaml"
    link.parent.mkdir()
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")
    assert caught.value.code in {"symlink_forbidden", "reparse_point_forbidden"}


def test_hardlink_inputs_are_rejected(tmp_path: Path) -> None:
    repo, draft, _ = _repo(tmp_path)
    external = tmp_path / "outside.yaml"
    external.write_text(SAFE_AGENT, encoding="utf-8")
    link = draft / "sub_agents" / "linked.yaml"
    link.parent.mkdir()
    os.link(external, link)
    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")
    assert caught.value.code == "hardlink_forbidden"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_junction_is_rejected(tmp_path: Path) -> None:
    repo, draft, _ = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = draft / "sub_agents"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        with pytest.raises(WorkflowLabError) as caught:
            WorkflowLab.for_source_checkout(repo).validate("example")
        assert caught.value.code == "reparse_point_forbidden"
    finally:
        os.rmdir(junction)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO semantics")
def test_fifo_is_rejected_without_opening_or_blocking(tmp_path: Path) -> None:
    repo, draft, _ = _repo(tmp_path)
    fifo = draft / "sub_agents" / "blocked.yaml"
    fifo.parent.mkdir()
    os.mkfifo(fifo)

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "non_regular_file"
    assert caught.value.stage == "preflight"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Unix socket semantics")
def test_unix_socket_is_rejected_as_non_regular() -> None:
    temporary_root = "/tmp" if Path("/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="wf-", dir=temporary_root) as temporary:
        repo, draft, _ = _repo(Path(temporary))
        endpoint = draft / "sub_agents" / "blocked.yaml"
        endpoint.parent.mkdir()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(endpoint))
        try:
            with pytest.raises(WorkflowLabError) as caught:
                WorkflowLab.for_source_checkout(repo).validate("example")
            assert caught.value.code == "non_regular_file"
        finally:
            server.close()


def test_required_file_cannot_be_a_directory(tmp_path: Path) -> None:
    repo, draft, _ = _repo(tmp_path)
    (draft / "root_agent.yaml").unlink()
    (draft / "root_agent.yaml").mkdir()

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "non_regular_file"


def test_validated_entry_replacement_is_detected_with_fault_injection(
    tmp_path: Path,
) -> None:
    repo, draft, _ = _repo(tmp_path)
    root_agent = draft / "root_agent.yaml"
    original = root_agent.stat()
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False

    def replace(event: str, path: Path) -> None:
        nonlocal fired
        if event == "after_lstat" and path == root_agent and not fired:
            fired = True
            replacement = draft / "replacement.yaml"
            replacement.write_text(
                SAFE_AGENT.replace("example", "swap"), encoding="utf-8"
            )
            os.replace(replacement, root_agent)

    lab._fault_hook = replace
    with pytest.raises(WorkflowLabError) as caught:
        lab.validate("example")

    assert fired
    assert caught.value.code == "entry_replaced"
    assert root_agent.stat().st_ino != original.st_ino


def test_parent_directory_swap_is_detected_with_fault_injection(tmp_path: Path) -> None:
    repo, draft, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False

    def swap_parent(event: str, path: Path) -> None:
        nonlocal fired
        if event == "after_lstat" and path.name == "root_agent.yaml" and not fired:
            fired = True
            moved = draft.with_name("example-original")
            draft.rename(moved)
            shutil.copytree(moved, draft)

    lab._fault_hook = swap_parent
    with pytest.raises(WorkflowLabError) as caught:
        lab.validate("example")

    assert fired
    assert caught.value.code == "entry_replaced"


def test_nested_directory_swap_is_detected_even_when_file_identity_is_retained(
    tmp_path: Path,
) -> None:
    repo, draft, _ = _repo(tmp_path)
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    child = sub_agents / "child.yaml"
    child.write_text(
        SAFE_AGENT.replace("workflow_lab_example", "child"), encoding="utf-8"
    )
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False

    def swap_directory(event: str, path: Path) -> None:
        nonlocal fired
        if event == "after_lstat" and path == child and not fired:
            fired = True
            moved = draft.parent / "sub_agents-original"
            sub_agents.rename(moved)
            sub_agents.mkdir()
            (moved / "child.yaml").rename(child)

    lab._fault_hook = swap_directory
    with pytest.raises(WorkflowLabError) as caught:
        lab.validate("example")

    assert fired
    assert caught.value.code == "tree_changed"


def test_same_inode_equal_size_transient_content_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, draft, _ = _repo(tmp_path)
    root_agent = draft / "root_agent.yaml"
    original = root_agent.read_bytes()
    transient = original.replace(b"workflow_lab_example", b"workflow_lab_examp1e")
    assert len(transient) == len(original)
    lab = WorkflowLab.for_source_checkout(repo)
    original_read = os.read
    armed = False
    restored = False

    def overwrite_after_lstat(event: str, path: Path) -> None:
        nonlocal armed
        if event == "after_lstat" and path == root_agent and not armed:
            root_agent.write_bytes(transient)
            armed = True

    def restore_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal restored
        content = original_read(descriptor, size)
        if armed and content == transient and not restored:
            root_agent.write_bytes(original)
            restored = True
        return content

    lab._fault_hook = overwrite_after_lstat
    monkeypatch.setattr(workflow_lab_module.os, "read", restore_after_first_read)

    try:
        with pytest.raises(WorkflowLabError) as caught:
            lab.validate("example")
    finally:
        if not restored:
            root_agent.write_bytes(original)

    assert armed
    if os.name != "nt":
        assert restored
    assert caught.value.code in {"entry_changed", "entry_replaced"}
    assert root_agent.read_bytes() == original


def test_gateway_writer_and_validation_share_the_per_app_lock(tmp_path: Path) -> None:
    repo, _, _ = _repo(tmp_path)
    writer = WorkflowLab.for_source_checkout(repo)
    validator = WorkflowLab.for_source_checkout(repo)
    writer_entered = threading.Event()
    release_writer = threading.Event()
    validation_entered = threading.Event()

    def hold_writer() -> None:
        with writer.draft_write_session("example"):
            writer_entered.set()
            assert release_writer.wait(timeout=5)

    def observe_validation(event: str, path: Path) -> None:
        del path
        if event == "after_lstat":
            validation_entered.set()

    validator._fault_hook = observe_validation
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(hold_writer)
        assert writer_entered.wait(timeout=5)
        validating = pool.submit(validator.validate, "example")
        assert not validation_entered.wait(timeout=0.2)
        release_writer.set()
        writing.result(timeout=5)
        report = validating.result(timeout=10)

    assert validation_entered.is_set()
    assert report.completed_stages[-1] == "isolated_contract"


@pytest.mark.parametrize("operation", ["validate", "export"])
def test_nested_gateway_session_acquires_os_lock_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    held = False
    acquisitions = 0

    def non_reentrant_lock(descriptor: int) -> None:
        nonlocal held, acquisitions
        del descriptor
        if held:
            raise RuntimeError("simulated non-reentrant OS file lock")
        held = True
        acquisitions += 1

    def release(descriptor: int) -> None:
        nonlocal held
        del descriptor
        assert held
        held = False

    monkeypatch.setattr(
        workflow_lab_module, "_lock_application_descriptor", non_reentrant_lock
    )
    monkeypatch.setattr(workflow_lab_module, "_unlock_application_descriptor", release)

    with lab.draft_write_session("example"):
        result = getattr(lab, operation)("example")

    assert acquisitions == 1
    assert not held
    if operation == "validate":
        assert result.completed_stages[-1] == "isolated_contract"
    else:
        assert result.export_dir.is_dir()


def test_outermost_application_lock_remains_cross_process_exclusive(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    acquired = tmp_path / "child-acquired"
    lab = WorkflowLab.for_installed_runtime(state)
    code = (
        "from pathlib import Path; "
        "from reserving_workflow.adapters.adk.workflow_lab import WorkflowLab; "
        f"lab=WorkflowLab.for_installed_runtime(Path({str(state)!r})); "
        "\nwith lab.draft_write_session('example'):\n"
        f" Path({str(acquired)!r}).write_text('acquired', encoding='utf-8')\n"
    )
    with lab.draft_write_session("example"):
        child = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.poll() is None
            assert not acquired.exists()
        except Exception:
            child.kill()
            raise
    stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == 0, (stdout, stderr)
    assert acquired.read_text(encoding="utf-8") == "acquired"


def test_nested_application_lock_exception_restores_thread_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    acquisitions = 0

    def record_lock(descriptor: int) -> None:
        nonlocal acquisitions
        del descriptor
        acquisitions += 1

    def record_unlock(descriptor: int) -> None:
        del descriptor

    monkeypatch.setattr(
        workflow_lab_module, "_lock_application_descriptor", record_lock
    )
    monkeypatch.setattr(
        workflow_lab_module,
        "_unlock_application_descriptor",
        record_unlock,
    )

    with (
        pytest.raises(RuntimeError, match="nested failure"),
        lab.draft_write_session("example"),
        lab.draft_write_session("example"),
    ):
        raise RuntimeError("nested failure")
    with lab.draft_write_session("example"):
        pass

    assert acquisitions == 2


@pytest.mark.parametrize(
    "basename",
    [
        "bad?.yaml",
        "bad*.yaml",
        "bad<.yaml",
        "bad>.yaml",
        "bad|.yaml",
        'bad".yaml',
        "bad\\name.yaml",
        "bad\x01.yaml",
    ],
)
def test_portable_basename_allowlist_rejects_cross_platform_characters(
    tmp_path: Path, basename: str
) -> None:
    with pytest.raises(WorkflowLabError) as caught:
        _validate_relative_names(
            {
                "root_agent.yaml": tmp_path / "root_agent.yaml",
                "workflow_policy.yaml": tmp_path / "workflow_policy.yaml",
                f"sub_agents/{basename}": tmp_path / basename,
            },
            require_policy=True,
            stage="preflight",
        )
    assert caught.value.code == "path_forbidden"


@pytest.mark.parametrize("extension", [".yml", ".YAML"])
@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.parametrize("operation", ["validate", "export"])
def test_only_exact_lowercase_yaml_is_accepted_for_draft_entries(
    tmp_path: Path,
    extension: str,
    referenced: bool,
    operation: str,
) -> None:
    repo, draft, _ = _repo(tmp_path)
    relative = f"sub_agents/child{extension}"
    if referenced:
        (draft / "root_agent.yaml").write_text(
            SAFE_AGENT + f"sub_agents:\n  - config_path: {relative}\n",
            encoding="utf-8",
        )
    else:
        child = draft / relative
        child.parent.mkdir()
        child.write_text(SAFE_AGENT, encoding="utf-8")
    lab = WorkflowLab.for_source_checkout(repo)
    exports_before = (
        set(lab.paths.exports_root.iterdir())
        if lab.paths.exports_root.exists()
        else set()
    )

    with pytest.raises(WorkflowLabError) as caught:
        getattr(lab, operation)("example")

    assert caught.value.code in {"file_type_forbidden", "sub_agent_path_forbidden"}
    exports_after = (
        set(lab.paths.exports_root.iterdir())
        if lab.paths.exports_root.exists()
        else set()
    )
    assert exports_after == exports_before


@pytest.mark.parametrize("extension", [".yml", ".YAML"])
def test_published_scanner_rejects_noncanonical_yaml_extensions(
    tmp_path: Path,
    extension: str,
) -> None:
    repo, _, published = _repo(tmp_path)
    sub_agents = published / "sub_agents"
    sub_agents.mkdir()
    (sub_agents / f"child{extension}").write_text(SAFE_AGENT, encoding="utf-8")
    lab = WorkflowLab.for_source_checkout(repo)

    with pytest.raises(WorkflowLabError) as caught:
        lab.export("example")

    assert caught.value.code == "file_type_forbidden"
    assert not lab.paths.exports_root.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX can physically create these names")
@pytest.mark.parametrize("basename", ["bad?.yaml", "bad\\name.yaml", "bad\x01.yaml"])
def test_posix_created_nonportable_basename_is_rejected(
    tmp_path: Path, basename: str
) -> None:
    repo, draft, _ = _repo(tmp_path)
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    (sub_agents / basename).write_text(SAFE_AGENT, encoding="utf-8")

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "path_forbidden"
    assert caught.value.stage == "preflight"


def test_repeated_and_concurrent_exports_are_byte_identical_and_isolated(
    tmp_path: Path,
) -> None:
    repo, draft, published = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    before_published = _tree_digest(published)
    before_index = (repo / ".git" / "index").read_bytes()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: lab.export("example"), range(2)))
    first.finalize()
    second.finalize()

    assert first.export_id != second.export_id
    assert first.bundle_digest == second.bundle_digest
    assert first.candidate_digest == second.candidate_digest
    assert first.patch_digest == second.patch_digest
    assert first.manifest == second.manifest
    for relative in (
        "manifest.json",
        "candidate.patch",
        "candidate/root_agent.yaml",
        "candidate/workflow_policy.yaml",
    ):
        assert (first.export_dir / relative).read_bytes() == (
            second.export_dir / relative
        ).read_bytes()
    assert str(first.export_dir) not in first.manifest.decode("utf-8")
    assert str(repo) not in first.manifest.decode("utf-8")
    assert "codex/" not in first.manifest.decode("utf-8")
    assert b"\\" not in first.manifest
    assert _tree_digest(published) == before_published
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert draft.is_dir()


def test_dangling_published_link_or_reparse_is_never_treated_as_empty(
    tmp_path: Path,
) -> None:
    repo, _, published = _repo(tmp_path)
    shutil.rmtree(published)
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    sentinel = outside_parent / "sentinel.txt"
    sentinel.write_bytes(b"unchanged")
    missing_target = outside_parent / "missing-target"
    if os.name == "nt":
        missing_target.mkdir()
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(published), str(missing_target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("junction creation is unavailable")
        missing_target.rmdir()
    else:
        published.symlink_to(missing_target, target_is_directory=True)
    lab = WorkflowLab.for_source_checkout(repo)
    exports_before = (
        set(lab.paths.exports_root.iterdir())
        if lab.paths.exports_root.exists()
        else set()
    )
    try:
        with pytest.raises(WorkflowLabError) as caught:
            lab.export("example")
    finally:
        if os.name == "nt":
            os.rmdir(published)
        else:
            published.unlink()

    assert caught.value.stage == "export"
    exports_after = (
        set(lab.paths.exports_root.iterdir())
        if lab.paths.exports_root.exists()
        else set()
    )
    assert exports_after == exports_before
    assert sentinel.read_bytes() == b"unchanged"


def test_published_final_object_replacement_after_probe_is_rejected(
    tmp_path: Path,
) -> None:
    repo, _, published = _repo(tmp_path)
    original = published.with_name("example-original")
    fired = False

    def replace(event: str, path: Path) -> None:
        nonlocal fired
        if event != "after_published_final_lstat" or path != published or fired:
            return
        fired = True
        published.rename(original)
        published.mkdir()
        (published / "root_agent.yaml").write_text(SAFE_AGENT, encoding="utf-8")

    lab = WorkflowLab.for_source_checkout(repo)
    lab._fault_hook = replace
    try:
        with pytest.raises(WorkflowLabError) as caught:
            lab.export("example")
    finally:
        if original.exists():
            shutil.rmtree(published)
            original.rename(published)

    assert fired
    assert caught.value.code == "tree_changed"
    assert not lab.paths.exports_root.exists()


def test_published_object_created_after_missing_probe_is_rejected(
    tmp_path: Path,
) -> None:
    repo, _, published = _repo(tmp_path)
    shutil.rmtree(published)
    fired = False

    def create(event: str, path: Path) -> None:
        nonlocal fired
        if event != "after_published_final_missing" or path != published or fired:
            return
        fired = True
        published.mkdir()
        (published / "root_agent.yaml").write_text(SAFE_AGENT, encoding="utf-8")

    lab = WorkflowLab.for_source_checkout(repo)
    lab._fault_hook = create

    with pytest.raises(WorkflowLabError) as caught:
        lab.export("example")

    assert fired
    assert caught.value.code == "tree_changed"
    assert not lab.paths.exports_root.exists()


@pytest.mark.parametrize(
    "mutation",
    ["root_replace", "file_write_restore", "file_replace", "file_link"],
)
@pytest.mark.parametrize(
    "commit_event",
    [
        "before_manifest_write",
        "after_manifest_write",
        "before_output_hardening",
        "before_final_published_verify",
    ],
)
def test_published_snapshot_remains_pinned_until_manifest_commit(
    tmp_path: Path,
    mutation: str,
    commit_event: str,
) -> None:
    repo, _, published = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    root_agent = published / "root_agent.yaml"
    original_bytes = root_agent.read_bytes()
    transient_bytes = bytes([original_bytes[0] ^ 1]) + original_bytes[1:]
    moved_root = published.with_name("example-original")
    moved_file = published / "root_agent-original.yaml"
    external = tmp_path / "external.yaml"
    external.write_bytes(b"agent_class: SequentialAgent\nname: external\n")
    external_before = external.read_bytes()
    fired = False
    changed = False

    def mutate(event: str, path: Path) -> None:
        nonlocal fired, changed
        if event != commit_event or fired:
            return
        fired = True
        try:
            if mutation == "root_replace":
                published.rename(moved_root)
                published.mkdir()
                (published / "root_agent.yaml").write_bytes(external_before)
            elif mutation == "file_write_restore":
                root_agent.write_bytes(transient_bytes)
                root_agent.write_bytes(original_bytes)
            elif mutation == "file_replace":
                root_agent.rename(moved_file)
                root_agent.write_bytes(external_before)
            else:
                root_agent.rename(moved_file)
                os.link(external, root_agent)
        except OSError:
            return
        changed = True

    lab._fault_hook = mutate
    receipt = None
    error = None
    try:
        try:
            receipt = lab.export("example")
        except WorkflowLabError as exc:
            error = exc
        if changed:
            assert error is not None
            assert not list(lab.paths.exports_root.rglob("manifest.json"))
        else:
            assert receipt is not None
            assert (receipt.export_dir / "manifest.json").is_file()
            receipt.finalize()
    finally:
        if moved_root.exists():
            if published.exists():
                shutil.rmtree(published)
            moved_root.rename(published)
        elif moved_file.exists():
            if root_agent.exists():
                root_agent.unlink()
            moved_file.rename(root_agent)

    assert fired
    assert external.read_bytes() == external_before


@pytest.mark.parametrize("created_type", ["directory", "link"])
@pytest.mark.parametrize(
    "commit_event",
    [
        "before_manifest_write",
        "after_manifest_write",
        "before_output_hardening",
        "before_final_published_verify",
    ],
)
def test_missing_published_state_remains_pinned_until_manifest_commit(
    tmp_path: Path,
    created_type: str,
    commit_event: str,
) -> None:
    repo, _, published = _repo(tmp_path)
    shutil.rmtree(published)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"unchanged")
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False

    def create(event: str, path: Path) -> None:
        nonlocal fired
        if event != commit_event or fired:
            return
        fired = True
        if created_type == "directory":
            published.mkdir()
            return
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(published), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                pytest.skip("junction creation is unavailable")
        else:
            published.symlink_to(outside, target_is_directory=True)

    lab._fault_hook = create
    try:
        with pytest.raises(WorkflowLabError):
            lab.export("example")
    finally:
        try:
            metadata = os.lstat(published)
        except FileNotFoundError:
            pass
        else:
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode):
                published.unlink()
            elif attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                os.rmdir(published)
            else:
                published.rmdir()

    assert fired
    assert not list(lab.paths.exports_root.rglob("manifest.json"))
    assert sentinel.read_bytes() == b"unchanged"


def test_export_cli_does_not_leave_commit_marker_after_published_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, draft, published = _repo(tmp_path)
    shutil.rmtree(published)
    lab = WorkflowLab.for_source_checkout(repo)

    def create_after_manifest(event: str, path: Path) -> None:
        if event == "after_manifest_write" and not published.exists():
            published.mkdir()

    lab._fault_hook = create_after_manifest
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "round6_export_adk_workflow_diff",
        scripts / "export_adk_workflow_diff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_lab_from_draft",
        lambda selected: (lab, "example", repo),
    )
    monkeypatch.setattr(module, "capture_source_integrity", lambda selected: object())
    monkeypatch.setattr(
        sys, "argv", ["export_adk_workflow_diff.py", str(draft), "--check"]
    )

    assert module.main() == 2

    assert '"ok": false' in capsys.readouterr().out
    assert not list(lab.paths.exports_root.rglob("manifest.json"))


def test_validate_cli_recognizes_installed_runtime_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "runtime-state"
    draft = state_root / "adk-workflow-drafts" / "example"
    draft.mkdir(parents=True)
    scripts = Path(__file__).parents[1] / "scripts"
    spec = importlib.util.spec_from_file_location(
        "installed_validate_adk_workflow",
        scripts / "validate_adk_workflow.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    lab, app_name, repo_root = module._lab_from_draft(draft)

    assert lab.paths.mode == "installed"
    assert lab.paths.state_root == state_root.absolute()
    assert app_name == "example"
    assert repo_root is None


@pytest.mark.parametrize("check", [False, True])
def test_export_cli_check_flag_controls_patch_applicability_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    check: bool,
) -> None:
    repo, draft, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        f"check_export_adk_workflow_diff_{check}",
        scripts / "export_adk_workflow_diff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "_lab_from_draft", lambda selected: (lab, "example", repo)
    )
    monkeypatch.setattr(module, "capture_source_integrity", lambda selected: object())
    monkeypatch.setattr(
        module, "assert_source_integrity_unchanged", lambda before, after: None
    )
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        module,
        "_check_patch_applies",
        lambda root, patch: calls.append((root, patch)),
        raising=False,
    )
    argv = ["export_adk_workflow_diff.py", str(draft)]
    if check:
        argv.append("--check")
    monkeypatch.setattr(sys, "argv", argv)

    assert module.main() == 0

    assert len(calls) == int(check)
    if calls:
        assert calls[0][0] == repo
        assert calls[0][1].name == "candidate.patch"
    assert f'"check": {str(check).lower()}' in capsys.readouterr().out


def test_export_cli_revokes_commit_marker_if_post_export_integrity_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, draft, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "round6_post_integrity_export_adk_workflow_diff",
        scripts / "export_adk_workflow_diff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_lab_from_draft",
        lambda selected: (lab, "example", repo),
    )
    monkeypatch.setattr(module, "capture_source_integrity", lambda selected: object())

    def fail_integrity(before: object, after: object) -> None:
        raise WorkflowLabError(
            "source_integrity_changed",
            "protected source changed",
            stage="integrity",
        )

    monkeypatch.setattr(module, "assert_source_integrity_unchanged", fail_integrity)
    monkeypatch.setattr(
        sys, "argv", ["export_adk_workflow_diff.py", str(draft), "--check"]
    )

    assert module.main() == 2

    assert '"code": "source_integrity_changed"' in capsys.readouterr().out
    assert not list(lab.paths.exports_root.rglob("manifest.json"))


@pytest.mark.parametrize("error_type", [PermissionError, OSError, TimeoutError])
def test_export_cli_revokes_marker_for_raw_post_export_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    repo, draft, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        f"round7_raw_{error_type.__name__}",
        scripts / "export_adk_workflow_diff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_lab_from_draft",
        lambda selected: (lab, "example", repo),
    )
    calls = 0

    def capture(selected: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise error_type("post-export failure")
        return object()

    monkeypatch.setattr(module, "capture_source_integrity", capture)
    monkeypatch.setattr(
        sys, "argv", ["export_adk_workflow_diff.py", str(draft), "--check"]
    )

    assert module.main() == 2

    assert '"code": "post_export_integrity_failed"' in capsys.readouterr().out
    assert not list(lab.paths.exports_root.rglob("manifest.json"))


def test_export_cli_reports_post_export_and_revoke_failures_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, draft, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"unchanged")
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "round7_double_failure_export_adk_workflow_diff",
        scripts / "export_adk_workflow_diff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "_lab_from_draft", lambda selected: (lab, "example", repo)
    )
    calls = 0

    def capture(selected: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("post-export failure")
        return object()

    def fail_revoke(receipt: object) -> None:
        raise OSError("descriptor-bound revoke failure")

    monkeypatch.setattr(module, "capture_source_integrity", capture)
    monkeypatch.setattr(lab, "revoke_export_commit", fail_revoke)
    monkeypatch.setattr(
        sys, "argv", ["export_adk_workflow_diff.py", str(draft), "--check"]
    )

    with pytest.raises(ExceptionGroup) as caught:
        module.main()

    assert [type(member) for member in caught.value.exceptions] == [
        PermissionError,
        OSError,
    ]
    assert capsys.readouterr().out == ""
    assert len(list(lab.paths.exports_root.rglob("manifest.json"))) == 1
    assert outside.read_bytes() == b"unchanged"


@pytest.mark.parametrize(
    "primary_type",
    [PermissionError, KeyboardInterrupt, SystemExit],
)
@pytest.mark.parametrize(
    "revoke_type",
    [OSError, KeyboardInterrupt, SystemExit],
)
def test_export_cli_preserves_primary_and_revoke_exception_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
    revoke_type: type[BaseException],
) -> None:
    repo, draft, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"unchanged")
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        f"round8_cli_{primary_type.__name__}_{revoke_type.__name__}",
        scripts / "export_adk_workflow_diff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipts: list[object] = []
    original_export = lab.export
    original_revoke = lab.revoke_export_commit

    def export(app_name: str) -> object:
        receipt = original_export(app_name)
        receipts.append(receipt)
        return receipt

    calls = 0

    def capture(selected: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise primary_type("primary")
        return object()

    def fail_revoke(receipt: object) -> None:
        raise revoke_type("revoke")

    monkeypatch.setattr(
        module, "_lab_from_draft", lambda selected: (lab, "example", repo)
    )
    monkeypatch.setattr(module, "capture_source_integrity", capture)
    monkeypatch.setattr(lab, "export", export)
    monkeypatch.setattr(lab, "revoke_export_commit", fail_revoke)
    monkeypatch.setattr(
        sys, "argv", ["export_adk_workflow_diff.py", str(draft), "--check"]
    )

    expected_group = (
        ExceptionGroup
        if issubclass(primary_type, Exception) and issubclass(revoke_type, Exception)
        else BaseExceptionGroup
    )
    try:
        with pytest.raises(expected_group) as caught:
            module.main()
        members = caught.value.exceptions
        assert [type(member) for member in members] == [primary_type, revoke_type]
        assert len(list(lab.paths.exports_root.rglob("manifest.json"))) == 1
        assert outside.read_bytes() == b"unchanged"
    finally:
        if receipts:
            original_revoke(receipts[0])  # type: ignore[arg-type]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_export_cli_revokes_marker_without_swallowing_base_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    repo, draft, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        f"round7_interrupt_{interrupt.__name__}",
        scripts / "export_adk_workflow_diff.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_lab_from_draft",
        lambda selected: (lab, "example", repo),
    )
    calls = 0

    def capture(selected: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise interrupt()
        return object()

    monkeypatch.setattr(module, "capture_source_integrity", capture)
    monkeypatch.setattr(
        sys, "argv", ["export_adk_workflow_diff.py", str(draft), "--check"]
    )

    with pytest.raises(interrupt):
        module.main()

    assert not list(lab.paths.exports_root.rglob("manifest.json"))


def test_export_api_cannot_accept_a_caller_output_path_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    assert "output" not in inspect.signature(lab.export).parameters
    monkeypatch.setattr(
        "reserving_workflow.adapters.adk.workflow_lab.secrets.token_hex",
        lambda _: "fixed-export-id",
    )

    first = lab.export("example")
    first.finalize()
    original_manifest = (first.export_dir / "manifest.json").read_bytes()
    with pytest.raises(WorkflowLabError) as caught:
        lab.export("example")

    assert caught.value.code == "export_exists"
    assert (first.export_dir / "manifest.json").read_bytes() == original_manifest


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode lease proof")
def test_export_receipt_retains_live_proof_handles_until_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    close_events: list[str] = []
    changed = False
    original_close = workflow_lab_module._PinnedOutputFile.close

    def close_and_attack(output: object) -> None:
        nonlocal changed
        original_close(output)
        if repo not in output.path.parents:  # type: ignore[attr-defined]
            return
        close_events.append(output.name)  # type: ignore[attr-defined]
        if output.name == "manifest.json":  # type: ignore[attr-defined]
            path = output.path  # type: ignore[attr-defined]
            changed = _raw_replace_output_bytes(path, b"X" * os.lstat(path).st_size)

    monkeypatch.setattr(
        workflow_lab_module._PinnedOutputFile,
        "close",
        close_and_attack,
    )

    receipt = lab.export("example")

    assert close_events == []
    assert receipt.active
    receipt.finalize()
    assert close_events
    assert not changed
    assert not receipt.active
    assert (receipt.export_dir / "manifest.json").read_bytes() == receipt.manifest
    receipt.finalize()


def test_export_receipt_context_and_gc_consume_active_lease(tmp_path: Path) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)

    with lab.export("example") as committed:
        committed_dir = committed.export_dir
        assert committed.active
        assert (committed_dir / "manifest.json").exists()
    assert not committed.active
    assert (committed_dir / "manifest.json").read_bytes() == committed.manifest

    with pytest.raises(RuntimeError), lab.export("example") as failed:
        failed_dir = failed.export_dir
        raise RuntimeError("caller failed before integrity decision")
    assert not (failed_dir / "manifest.json").exists()

    abandoned = lab.export("example")
    abandoned_dir = abandoned.export_dir
    assert (abandoned_dir / "manifest.json").exists()
    del abandoned
    gc.collect()
    assert not (abandoned_dir / "manifest.json").exists()


def test_export_receipt_finalize_and_revoke_are_concurrently_consumable(
    tmp_path: Path,
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    finalized = lab.export("example")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: finalized.finalize(), range(2))) == [None, None]
    assert (finalized.export_dir / "manifest.json").exists()

    raced = lab.export("example")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(raced.finalize),
            pool.submit(lab.revoke_export_commit, raced),
        ]
        assert [future.result(timeout=10) for future in futures] == [None, None]
    assert not (raced.export_dir / "manifest.json").exists()


@pytest.mark.parametrize("replacement", ["export_directory", "manifest"])
def test_revoke_export_commit_rejects_equal_bytes_aba_replacement(
    tmp_path: Path,
    replacement: str,
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    receipt = lab.export("example")
    receipt.finalize()
    moved_export = receipt.export_dir.with_name(receipt.export_id + "-original")
    moved_manifest = receipt.export_dir / "manifest-original.json"

    if replacement == "export_directory":
        receipt.export_dir.rename(moved_export)
        receipt.export_dir.mkdir()
        (receipt.export_dir / "manifest.json").write_bytes(receipt.manifest)
        original_marker = moved_export / "manifest.json"
    else:
        marker = receipt.export_dir / "manifest.json"
        if os.name != "nt":
            _make_owner_directory_writable(receipt.export_dir)
        marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
        marker.rename(moved_manifest)
        marker.write_bytes(receipt.manifest)
        original_marker = moved_manifest

    with pytest.raises(WorkflowLabError):
        lab.revoke_export_commit(receipt)

    assert original_marker.read_bytes() == receipt.manifest
    assert (receipt.export_dir / "manifest.json").read_bytes() == receipt.manifest


def test_revoke_export_commit_is_existing_only_and_repeat_safe(tmp_path: Path) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    receipt = lab.export("example")

    lab.revoke_export_commit(receipt)
    lab.revoke_export_commit(receipt)

    assert not (receipt.export_dir / "manifest.json").exists()
    missing_id = "f" * 32
    forged = replace(
        receipt,
        export_id=missing_id,
        export_dir=lab.paths.exports_root / missing_id,
    )
    with pytest.raises(WorkflowLabError):
        lab.revoke_export_commit(forged)
    assert not forged.export_dir.exists()


def test_revoke_export_commit_is_concurrently_idempotent(tmp_path: Path) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    receipt = lab.export("example")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: lab.revoke_export_commit(receipt), range(2)))

    assert results == [None, None]
    assert not (receipt.export_dir / "manifest.json").exists()


def test_revoke_export_commit_serializes_and_recovers_across_processes(
    tmp_path: Path,
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    receipt = lab.export("example")
    if hasattr(receipt, "finalize"):
        receipt.finalize()
    payload: dict[str, object] = {
        "export_id": receipt.export_id,
        "export_dir": str(receipt.export_dir),
        "bundle_digest": receipt.bundle_digest,
        "candidate_digest": receipt.candidate_digest,
        "patch_digest": receipt.patch_digest,
        "manifest": receipt.manifest,
        "directories": [
            (identity.device, identity.inode, identity.windows_file_id)
            for identity in receipt.commit_binding.directory_chain
        ],
        "manifest_identity": (
            receipt.commit_binding.manifest.device,
            receipt.commit_binding.manifest.inode,
            receipt.commit_binding.manifest.windows_file_id,
        ),
    }
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    result = context.Queue()
    holder = context.Process(
        target=_round8_hold_revoke_lock,
        args=(str(receipt.export_dir), ready),
    )
    revoker = context.Process(
        target=_round8_revoke_in_child,
        args=(str(repo), payload, result),
    )
    holder.start()
    try:
        assert ready.wait(10)
        revoker.start()
        time.sleep(0.75)
        assert revoker.is_alive()
        assert (receipt.export_dir / "manifest.json").exists()
        holder.terminate()
        holder.join(10)
        revoker.join(20)
        assert not revoker.is_alive()
        assert result.get(timeout=2) == ("ok",)
        assert not (receipt.export_dir / "manifest.json").exists()
    finally:
        if holder.is_alive():
            holder.terminate()
        if revoker.is_alive():
            revoker.terminate()
        holder.join(10)
        revoker.join(10)


def test_deleting_draft_or_export_does_not_affect_published_or_history(
    tmp_path: Path,
) -> None:
    repo, draft, published = _repo(tmp_path)
    history = repo / "tmp" / "run-history" / "run.json"
    history.parent.mkdir(parents=True)
    history.write_text('{"status":"completed"}', encoding="utf-8")
    lab = WorkflowLab.for_source_checkout(repo)
    receipt = lab.export("example")
    receipt.finalize()
    before = _tree_digest(published)

    shutil.rmtree(draft)
    shutil.rmtree(receipt.export_dir, onerror=_make_writable)

    assert _tree_digest(published) == before
    assert history.read_text(encoding="utf-8") == '{"status":"completed"}'


def test_installed_mode_materializes_versioned_read_only_published_resources(
    tmp_path: Path,
) -> None:
    lab = WorkflowLab.for_installed_runtime(tmp_path / "state")

    materialized = lab.materialize_published_workflows()

    assert materialized.is_dir()
    assert "2.7.1" in materialized.name
    assert (materialized / "workflow_lab_example" / "root_agent.yaml").is_file()
    declarative_resources = {
        path.relative_to(materialized).as_posix()
        for path in materialized.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".yaml", ".yml"}
    }
    assert declarative_resources == {
        "workflow_lab_example/root_agent.yaml",
        "workflow_lab_example/workflow_policy.yaml",
    }
    mode = (materialized / "workflow_lab_example" / "root_agent.yaml").stat().st_mode
    assert mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    with pytest.raises(WorkflowLabError) as caught:
        lab.export("workflow_lab_example")
    assert caught.value.code == "source_checkout_required"


def test_materialized_tree_denies_an_independent_consumer_identity(
    tmp_path: Path,
) -> None:
    lab = WorkflowLab.for_installed_runtime(tmp_path / "state")
    materialized = lab.materialize_published_workflows()
    app = materialized / "workflow_lab_example"
    target = app / "root_agent.yaml"
    before = target.read_bytes()
    injected = app / "injected.py"
    renamed = app / "renamed.yaml"
    new_directory = app / "injected-directory"
    read_code = f"from pathlib import Path; assert Path({str(target)!r}).read_bytes()"
    attacks = [
        f"from pathlib import Path; Path({str(injected)!r}).write_bytes(b'bad')",
        f"from pathlib import Path; Path({str(target)!r}).write_bytes(b'bad')",
        f"from pathlib import Path; Path({str(target)!r}).rename({str(renamed)!r})",
        f"from pathlib import Path; Path({str(target)!r}).unlink()",
        f"from pathlib import Path; Path({str(new_directory)!r}).mkdir()",
    ]
    if os.name == "nt":

        def run(code: str) -> int:
            return _run_windows_low_integrity_process(
                [sys.executable, "-I", "-c", code],
                cwd=tmp_path,
            )
    else:
        temporary_root = Path(tempfile.gettempdir()).absolute()
        ancestor = tmp_path
        while ancestor != temporary_root:
            ancestor.chmod(0o755)
            ancestor = ancestor.parent
        for ancestor in (
            tmp_path / "state",
            tmp_path / "state" / "published-workflows",
        ):
            ancestor.chmod(0o755)
        if os.geteuid() == 0 and shutil.which("runuser"):
            prefix = ["runuser", "-u", "nobody", "--"]
        elif (
            shutil.which("sudo")
            and subprocess.run(
                ["sudo", "-n", "true"], capture_output=True, check=False
            ).returncode
            == 0
        ):
            prefix = ["sudo", "-n", "-u", "nobody", "--"]
        else:
            pytest.skip("an independent low-privilege POSIX consumer is unavailable")
        python = shutil.which("python3") or sys.executable

        def run(code: str) -> int:
            return subprocess.run(
                [*prefix, python, "-I", "-c", code],
                cwd=tmp_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            ).returncode

    assert run(read_code) == 0
    assert all(run(code) != 0 for code in attacks)
    assert target.read_bytes() == before
    assert not injected.exists()
    assert not renamed.exists()
    assert not new_directory.exists()


def test_materialized_resources_reject_same_content_hardlink_replacement(
    tmp_path: Path,
) -> None:
    lab = WorkflowLab.for_installed_runtime(tmp_path / "state")
    materialized = lab.materialize_published_workflows()
    target = materialized / "workflow_lab_example" / "root_agent.yaml"
    external = tmp_path / "same-content.yaml"
    external.write_bytes(target.read_bytes())
    if os.name != "nt":
        _make_owner_directory_writable(target.parent)
    target.chmod(stat.S_IWRITE | stat.S_IREAD)
    target.unlink()
    os.link(external, target)

    with pytest.raises(WorkflowLabError) as caught:
        lab.materialize_published_workflows()

    assert caught.value.code == "hardlink_forbidden"
    assert caught.value.stage == "materialize"


def test_materialized_resources_reject_writable_permission_drift(
    tmp_path: Path,
) -> None:
    lab = WorkflowLab.for_installed_runtime(tmp_path / "state")
    materialized = lab.materialize_published_workflows()
    target = materialized / "workflow_lab_example" / "root_agent.yaml"
    target.chmod(stat.S_IWRITE | stat.S_IREAD)

    with pytest.raises(WorkflowLabError) as caught:
        lab.materialize_published_workflows()

    assert caught.value.code == "materialized_permissions_changed"
    assert caught.value.stage == "materialize"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL drift check")
def test_materialized_resources_reject_windows_acl_drift(tmp_path: Path) -> None:
    lab = WorkflowLab.for_installed_runtime(tmp_path / "state")
    materialized = lab.materialize_published_workflows()
    changed = subprocess.run(
        ["icacls", str(materialized), "/grant", "*S-1-1-0:(F)", "/C"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert changed.returncode == 0, changed.stderr

    with pytest.raises(WorkflowLabError) as caught:
        lab.materialize_published_workflows()

    assert caught.value.code == "materialized_security_changed"
    assert caught.value.stage == "materialize"


@pytest.mark.parametrize(
    ("sddl", "is_root", "is_directory"),
    [
        (
            (
                "O:S-1-5-21-1D:P"
                "(A;OICI;FA;;;S-1-5-21-1)(A;OICI;FR;;;BU)"
                "S:AI(ML;OICI;NW;;;ME)"
            ),
            True,
            True,
        ),
        (
            (
                "O:S-1-5-21-1D:AI"
                "(A;OICI;0x1f01ff;;;S-1-5-21-1)"
                "(A;OICI;0x1200a9;;;S-1-5-32-545)"
                "S:AI(ML;OICI;NW;;;S-1-16-8192)"
            ),
            False,
            True,
        ),
        (
            (
                "O:S-1-5-21-1D:AI"
                "(A;ID;FA;;;S-1-5-21-1)(A;ID;FRFX;;;BU)"
                "S:AI(ML;ID;NW;;;ME)"
            ),
            False,
            False,
        ),
    ],
)
def test_windows_materialized_security_accepts_semantic_sddl_renderings(
    sddl: str,
    is_root: bool,
    is_directory: bool,
) -> None:
    assert workflow_lab_module._windows_materialized_sddl_is_valid(
        actual_owner="S-1-5-21-1",
        owner_sid="S-1-5-21-1",
        sddl=sddl,
        is_root=is_root,
        is_directory=is_directory,
    )


@pytest.mark.parametrize(
    ("sddl", "actual_owner", "expected_reason"),
    [
        (
            (
                "O:S-1-5-21-1D:AI"
                "(A;OICI;FA;;;S-1-5-21-1)(A;OICI;FR;;;BU)"
                "S:AI(ML;OICI;NW;;;ME)"
            ),
            "S-1-5-21-1",
            "dacl_control_flags",
        ),
        (
            (
                "O:S-1-5-21-1D:P"
                "(A;OICI;FA;;;S-1-5-21-1)(D;OICI;FR;;;BU)"
                "S:AI(ML;OICI;NW;;;ME)"
            ),
            "S-1-5-21-1",
            "dacl_ace_boundary",
        ),
        (
            (
                "O:S-1-5-21-1D:P"
                "(A;OICI;FA;;;S-1-5-21-1)(A;OICI;FW;;;BU)"
                "S:AI(ML;OICI;NW;;;ME)"
            ),
            "S-1-5-21-1",
            "consumer_rights",
        ),
        (
            (
                "O:S-1-5-21-1D:P"
                "(A;OICI;FA;;;S-1-5-21-1)(A;OICI;FR;;;BU)"
                "S:AI(ML;OICI;NW;;;HI)"
            ),
            "S-1-5-21-1",
            "label_sid",
        ),
        (
            (
                "O:S-1-5-21-1D:P"
                "(A;OICI;FA;;;S-1-5-21-1)(A;OICI;FR;;;BU)"
                "S:AI(ML;OICI;NW;;;ME)"
            ),
            "S-1-5-21-2",
            "owner_mismatch",
        ),
    ],
)
def test_windows_materialized_security_rejects_boundary_changes(
    sddl: str,
    actual_owner: str,
    expected_reason: str,
) -> None:
    assert not workflow_lab_module._windows_materialized_sddl_is_valid(
        actual_owner=actual_owner,
        owner_sid="S-1-5-21-1",
        sddl=sddl,
        is_root=True,
        is_directory=True,
    )
    assert (
        workflow_lab_module._windows_materialized_sddl_validation_reason(
            actual_owner=actual_owner,
            owner_sid="S-1-5-21-1",
            sddl=sddl,
            is_root=True,
            is_directory=True,
        )
        == expected_reason
    )


def test_windows_hardening_sets_exact_owner_before_acl_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "materialized"
    root.mkdir()
    target = root / "workflow.yaml"
    target.write_text("workflow: safe\n", encoding="utf-8")
    owner_sid = "S-1-5-21-1"
    owners = {root: "S-1-5-32-544", target: "S-1-5-32-544"}
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(arguments)
        if "/setowner" in arguments:
            owners.update({root: owner_sid, target: owner_sid})
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def security(path: Path) -> tuple[str, str]:
        if path == root:
            sddl = (
                f"O:{owner_sid}D:P"
                f"(A;OICI;FA;;;{owner_sid})(A;OICI;FR;;;BU)"
                "S:AI(ML;OICI;NW;;;ME)"
            )
        else:
            sddl = (
                f"O:{owner_sid}D:AI"
                f"(A;ID;FA;;;{owner_sid})(A;ID;FR;;;BU)"
                "S:AI(ML;ID;NW;;;ME)"
            )
        return owners[path], sddl

    monkeypatch.setattr(
        workflow_lab_module, "_windows_current_user_sid", lambda: owner_sid
    )
    monkeypatch.setattr(workflow_lab_module.subprocess, "run", run)
    monkeypatch.setattr(workflow_lab_module, "_windows_security_sddl", security)

    with pytest.raises(WorkflowLabError) as caught:
        workflow_lab_module._validate_windows_materialized_security([root, target])
    assert "reason=owner_mismatch" in str(caught.value)
    assert "S-1-5-32-544" not in str(caught.value)
    assert "O:S-1-5-21-1D:P" not in str(caught.value)
    assert "(A;OICI" not in str(caught.value)

    workflow_lab_module._harden_windows_materialized_tree(root)
    workflow_lab_module._validate_windows_materialized_security([root, target])

    assert calls[0] == [
        "icacls",
        str(root),
        "/setowner",
        f"*{owner_sid}",
        "/T",
        "/C",
    ]
    assert calls[1] == ["icacls", str(root), "/reset", "/C"]
    assert calls[2][2:] == [
        "/inheritance:r",
        "/grant:r",
        f"*{owner_sid}:(OI)(CI)(F)",
        "*S-1-5-32-545:(OI)(CI)(RX)",
        "/C",
    ]


def test_windows_hardening_wraps_owner_command_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "materialized"
    root.mkdir()

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(["icacls"], 20)

    monkeypatch.setattr(
        workflow_lab_module, "_windows_current_user_sid", lambda: "S-1-5-21-1"
    )
    monkeypatch.setattr(workflow_lab_module.subprocess, "run", fail)

    with pytest.raises(WorkflowLabError) as caught:
        workflow_lab_module._harden_windows_materialized_tree(root)

    assert caught.value.code == "materialized_security_failed"
    assert caught.value.stage == "materialize"
    assert "step=set_owner" in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows explicit DACL normalization")
def test_windows_hardening_removes_preexisting_explicit_ace_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "materialized"
    root.mkdir()
    target = root / "workflow.yaml"
    target.write_text("workflow: safe\n", encoding="utf-8")
    workflow_lab_module._harden_windows_materialized_tree(root)
    changed = subprocess.run(
        [
            "icacls",
            str(root),
            "/grant",
            "*S-1-1-0:(OI)(CI)(F)",
            "/C",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert changed.returncode == 0, changed.stderr

    owner_sid = workflow_lab_module._windows_current_user_sid()
    actual_owner, runner_like_sddl = workflow_lab_module._windows_security_sddl(root)
    dacl = workflow_lab_module._parse_sddl_section(runner_like_sddl, "D")
    assert dacl is not None
    assert len(dacl[1]) == 3
    assert (
        workflow_lab_module._windows_materialized_sddl_validation_reason(
            actual_owner=actual_owner,
            owner_sid=owner_sid,
            sddl=runner_like_sddl,
            is_root=True,
            is_directory=True,
        )
        == "dacl_ace_boundary"
    )

    workflow_lab_module._harden_windows_materialized_tree(root)
    workflow_lab_module._validate_windows_materialized_security([root, target])


def test_byte_governed_packaged_workflows_disable_checkout_conversion() -> None:
    repo = Path(__file__).parents[1]
    relative_paths = [
        "src/reserving_workflow/developer_workflows/workflow_lab_example/__init__.py",
        "src/reserving_workflow/developer_workflows/workflow_lab_example/root_agent.yaml",
        "src/reserving_workflow/developer_workflows/workflow_lab_example/workflow_policy.yaml",
    ]

    attributes = (repo / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert all(f"{relative} -text" in attributes for relative in relative_paths)


def test_shipped_packaged_workflow_exports_only_declarative_git_surface(
    tmp_path: Path,
) -> None:
    source_app = (
        Path(__file__).parents[1]
        / "src"
        / "reserving_workflow"
        / "developer_workflows"
        / "workflow_lab_example"
    )
    repo = tmp_path / "repo"
    published = (
        repo
        / "src"
        / "reserving_workflow"
        / "developer_workflows"
        / "workflow_lab_example"
    )
    shutil.copytree(
        source_app,
        published,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    draft = repo / "tmp" / "adk-workflow-drafts" / "workflow_lab_example"
    draft.mkdir(parents=True)
    for name in ("root_agent.yaml", "workflow_policy.yaml"):
        shutil.copy2(published / name, draft / name)
    root = (draft / "root_agent.yaml").read_text(encoding="utf-8")
    (draft / "root_agent.yaml").write_text(
        root.replace("packaged model-free", "reviewed model-free"),
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    before_index = (repo / ".git" / "index").read_bytes()
    before_python = (published / "__init__.py").read_bytes()

    receipt = WorkflowLab.for_source_checkout(repo).export("workflow_lab_example")
    receipt.finalize()
    candidate = receipt.export_dir / "candidate"
    patch = receipt.export_dir / "candidate.patch"

    assert sorted(path.name for path in candidate.iterdir()) == [
        "root_agent.yaml",
        "workflow_policy.yaml",
    ]
    patch_bytes = patch.read_bytes()
    prefix = b"src/reserving_workflow/developer_workflows/workflow_lab_example/"
    assert b"--- a/" + prefix + b"root_agent.yaml" in patch_bytes
    assert b"+++ b/" + prefix + b"root_agent.yaml" in patch_bytes
    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", str(patch)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    assert (published / "root_agent.yaml").read_bytes() == (
        candidate / "root_agent.yaml"
    ).read_bytes()
    assert (published / "workflow_policy.yaml").read_bytes() == (
        candidate / "workflow_policy.yaml"
    ).read_bytes()
    assert (published / "__init__.py").read_bytes() == before_python
    manifest = __import__("json").loads(receipt.manifest)
    assert manifest["published_retained_files"] == [
        {
            "path": "__init__.py",
            "sha256": hashlib.sha256(before_python).hexdigest(),
            "size": len(before_python),
            "type": "canonical-inert-python-stub",
        }
    ]
    assert (repo / ".git" / "index").read_bytes() == before_index


def test_published_bytecode_cache_is_rejected(tmp_path: Path) -> None:
    repo, _, published = _repo(tmp_path)
    cache = published / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-311.pyc").write_bytes(b"not-authoritative-source")

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).export("example")

    assert caught.value.code == "file_type_forbidden"
    assert caught.value.stage == "export"


def test_canonical_inert_init_is_bound_into_published_and_bundle_digests(
    tmp_path: Path,
) -> None:
    repo, _, published = _repo(tmp_path)
    init = published / "__init__.py"
    init.write_bytes(b'"""First inert package marker."""\n')
    lab = WorkflowLab.for_source_checkout(repo)
    first = lab.export("example")
    first.finalize()
    init.write_bytes(b'"""Second inert package marker."""\n')
    second = lab.export("example")
    second.finalize()

    first_manifest = __import__("json").loads(first.manifest)
    second_manifest = __import__("json").loads(second.manifest)
    assert (
        first_manifest["published_tree_digest"]
        != second_manifest["published_tree_digest"]
    )
    assert first.bundle_digest != second.bundle_digest
    assert (
        b"__init__.py" not in first.export_dir.joinpath("candidate.patch").read_bytes()
    )


def test_published_init_must_be_a_canonical_inert_stub(tmp_path: Path) -> None:
    repo, _, published = _repo(tmp_path)
    (published / "__init__.py").write_text("import os\n", encoding="utf-8")

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).export("example")

    assert caught.value.code == "published_python_forbidden"


def test_published_unknown_python_is_never_silently_filtered(tmp_path: Path) -> None:
    repo, _, published = _repo(tmp_path)
    (published / "plugin.py").write_text("UNTRUSTED = True\n", encoding="utf-8")

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).export("example")

    assert caught.value.code == "file_type_forbidden"
    assert caught.value.stage == "export"


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("root_agent.yaml", b"\xffagent_class: SequentialAgent\nname: invalid\n"),
        (
            "sub_agents/child.yaml",
            b"agent_class: SequentialAgent\nname: child\n\x80",
        ),
        ("workflow_policy.yaml", b"schema_version: invalid\xfe\n"),
    ],
)
def test_invalid_utf8_published_yaml_releases_every_guard_and_fails_structured(
    tmp_path: Path,
    relative: str,
    content: bytes,
) -> None:
    repo, _, published = _repo(tmp_path)
    target = published.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    before_index = (repo / ".git" / "index").read_bytes()
    before_published = _tree_digest(published)
    descriptor_root = Path("/proc/self/fd")
    before_descriptors = (
        len(list(descriptor_root.iterdir())) if descriptor_root.is_dir() else None
    )
    lab = WorkflowLab.for_source_checkout(repo)

    with pytest.raises(WorkflowLabError) as caught:
        lab.export("example")

    assert caught.value.code == "published_encoding_invalid"
    assert caught.value.stage == "export"
    assert not list(lab.paths.exports_root.rglob("manifest.json"))
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert _tree_digest(published) == before_published
    if before_descriptors is not None:
        assert len(list(descriptor_root.iterdir())) == before_descriptors
    moved = published.with_name("published-released")
    published.rename(moved)
    shutil.rmtree(moved)
    assert not moved.exists()


@pytest.mark.parametrize(
    ("failure_point", "target", "failure_type"),
    [
        ("build_patch", "_build_patch", RuntimeError),
        ("input_digest", "_digest_json", RuntimeError),
        ("canonical_json", "_canonical_json_bytes", RuntimeError),
        ("build_patch_interrupt", "_build_patch", KeyboardInterrupt),
    ],
)
def test_post_snapshot_export_failures_release_published_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    target: str,
    failure_type: type[BaseException],
) -> None:
    repo, _, published = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)

    def fail(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise failure_type(f"injected {failure_point} failure")

    monkeypatch.setattr(workflow_lab_module, target, fail)

    with pytest.raises(failure_type, match=failure_point):
        lab.export("example")

    assert not list(lab.paths.exports_root.rglob("manifest.json"))
    moved = published.with_name("published-released")
    published.rename(moved)
    shutil.rmtree(moved)
    assert not moved.exists()


def test_exclusive_output_is_not_visible_when_atomic_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest.json"

    def fail_commit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("injected atomic commit failure")

    if os.name == "nt":

        def fail_create(path: Path, *, stage: str) -> int:
            del path
            raise WorkflowLabError(
                "export_commit_failed",
                "injected atomic commit failure",
                stage=stage,
            )

        monkeypatch.setattr(
            workflow_lab_module,
            "_open_windows_new_output_descriptor",
            fail_create,
        )
    else:
        monkeypatch.setattr(os, "link", fail_commit)
    with pytest.raises(WorkflowLabError) as caught:
        _write_exclusive(destination, b"{}\n")

    assert caught.value.code == "export_commit_failed"
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows CREATE_NEW identity proof")
def test_windows_exclusive_output_never_links_a_closed_replaceable_pending_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "manifest.json"
    expected = b'{"stable":true}\n'
    original_link = os.link
    substituted = False

    def substitute_pending(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal substituted
        source_directory = kwargs.get("src_dir_fd")
        assert isinstance(source_directory, int)
        opened = os.open(source, os.O_RDONLY, dir_fd=source_directory)
        try:
            content = os.read(opened, 1024)
        finally:
            os.close(opened)
        os.unlink(source, dir_fd=source_directory)
        replacement = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=source_directory,
        )
        try:
            os.write(replacement, content)
            os.fsync(replacement)
        finally:
            os.close(replacement)
        substituted = True
        original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", substitute_pending)

    _write_exclusive(destination, expected)

    assert not substituted
    assert destination.read_bytes() == expected
    metadata = os.lstat(destination)
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX linkat identity proof")
def test_posix_exclusive_output_rejects_equal_byte_pending_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "manifest.json"
    original_link = os.link
    substituted = False

    def substitute_pending(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal substituted
        source_directory = kwargs.get("src_dir_fd")
        assert isinstance(source_directory, int)
        opened = os.open(source, os.O_RDONLY, dir_fd=source_directory)
        try:
            content = os.read(opened, 1024)
        finally:
            os.close(opened)
        os.unlink(source, dir_fd=source_directory)
        replacement = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=source_directory,
        )
        try:
            os.write(replacement, content)
            os.fsync(replacement)
        finally:
            os.close(replacement)
        substituted = True
        original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", substitute_pending)

    with pytest.raises(WorkflowLabError):
        _write_exclusive(destination, b'{"stable":true}\n')

    assert substituted
    assert not destination.exists()


def test_export_ancestor_swap_cannot_write_outside_server_owned_root(
    tmp_path: Path,
) -> None:
    repo, _, _ = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False
    rename_blocked = False

    def swap_candidate(event: str, candidate: Path) -> None:
        nonlocal fired, rename_blocked
        if event != "before_output_write" or fired:
            return
        fired = True
        moved = candidate.with_name("candidate-original")
        try:
            candidate.rename(moved)
        except OSError:
            rename_blocked = True
            return
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(candidate), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        else:
            candidate.symlink_to(outside, target_is_directory=True)

    lab._fault_hook = swap_candidate
    try:
        receipt = lab.export("example")
    except WorkflowLabError as exc:
        assert not rename_blocked
        assert exc.code == "output_tree_changed"
    else:
        assert rename_blocked
        assert receipt.export_dir.is_dir()
        receipt.finalize()

    assert fired
    assert list(outside.iterdir()) == []


def test_final_output_name_is_bound_after_readonly_transition(tmp_path: Path) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False
    replacement_blocked = False

    def replace_final_name(event: str, candidate: Path) -> None:
        nonlocal fired, replacement_blocked
        if event != "before_final_output_verify" or fired:
            return
        fired = True
        target = candidate / "root_agent.yaml"
        moved = candidate / "moved-original.yaml"
        try:
            target.rename(moved)
        except OSError:
            replacement_blocked = True
            return
        target.write_bytes(b"agent_class: SequentialAgent\nname: replacement\n")

    lab._fault_hook = replace_final_name
    if os.name == "nt":
        receipt = lab.export("example")
        assert replacement_blocked
        receipt.finalize()
        assert (receipt.export_dir / "candidate" / "root_agent.yaml").read_bytes() != (
            b"agent_class: SequentialAgent\nname: replacement\n"
        )
    else:
        with pytest.raises(WorkflowLabError) as caught:
            lab.export("example")
        assert caught.value.code == "output_tree_changed"
        assert not replacement_blocked
    assert fired


@pytest.mark.parametrize("injected_type", ["file", "directory"])
def test_final_output_topology_rejects_unmanifested_injection(
    tmp_path: Path, injected_type: str
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False

    def inject(event: str, candidate: Path) -> None:
        nonlocal fired
        if event != "before_final_output_verify" or fired:
            return
        fired = True
        target = candidate / "injected.py"
        if injected_type == "file":
            target.write_bytes(b"unmanifested = True\n")
        else:
            target.mkdir()

    lab._fault_hook = inject
    with pytest.raises(WorkflowLabError) as caught:
        lab.export("example")

    assert fired
    assert caught.value.code == "output_tree_changed"
    assert not list(lab.paths.exports_root.rglob("manifest.json"))


@pytest.mark.parametrize(
    "mutation",
    ["candidate_bytes", "patch_bytes", "manifest_bytes", "file_mode", "directory_mode"],
)
def test_terminal_output_proof_has_no_post_verify_write_or_permission_gap(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo, _, _ = _repo(tmp_path)
    lab = WorkflowLab.for_source_checkout(repo)
    fired = False
    changed = False

    def mutate(event: str, export: Path) -> None:
        nonlocal fired, changed
        if event != "before_final_published_verify" or fired:
            return
        fired = True
        if mutation == "candidate_bytes":
            target = export / "candidate" / "root_agent.yaml"
            changed = _raw_replace_output_bytes(target, b"X" * os.lstat(target).st_size)
        elif mutation == "patch_bytes":
            target = export / "candidate.patch"
            changed = _raw_replace_output_bytes(target, b"X" * os.lstat(target).st_size)
        elif mutation == "manifest_bytes":
            target = export / "manifest.json"
            changed = _raw_replace_output_bytes(target, b"X" * os.lstat(target).st_size)
        elif mutation == "file_mode":
            target = export / "candidate" / "root_agent.yaml"
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
            metadata = os.lstat(target)
            changed = bool(metadata.st_mode & stat.S_IWUSR) or not bool(
                int(getattr(metadata, "st_file_attributes", 0)) & 0x1
            )
        elif os.name == "nt":
            result = subprocess.run(
                ["icacls", str(export / "candidate"), "/grant", "*S-1-1-0:(F)"],
                capture_output=True,
                text=True,
                check=False,
            )
            changed = result.returncode == 0
        else:
            target = export / "candidate"
            target.chmod(stat.S_IRWXU)
            changed = bool(os.lstat(target).st_mode & stat.S_IWUSR)

    lab._fault_hook = mutate
    receipt = None
    error = None
    try:
        receipt = lab.export("example")
    except WorkflowLabError as exc:
        error = exc

    assert fired
    if changed:
        assert error is not None
        assert not list(lab.paths.exports_root.rglob("manifest.json"))
    else:
        assert receipt is not None
        assert (receipt.export_dir / "manifest.json").is_file()
        receipt.finalize()


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate data stream matrix")
@pytest.mark.parametrize(
    "target_name",
    [
        "export_directory",
        "candidate_file",
        "candidate_directory",
        "nested_directory",
        "patch",
        "manifest",
    ],
)
def test_terminal_output_proof_rejects_alternate_data_streams(
    tmp_path: Path,
    target_name: str,
) -> None:
    repo, draft, _ = _repo(tmp_path)
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    (sub_agents / "child.yaml").write_text(
        "agent_class: SequentialAgent\nname: child\ndescription: Nested child.\n",
        encoding="utf-8",
    )
    (draft / "root_agent.yaml").write_text(
        SAFE_AGENT + "sub_agents:\n  - config_path: sub_agents/child.yaml\n",
        encoding="utf-8",
    )
    lab = WorkflowLab.for_source_checkout(repo)
    injected = False

    def inject(event: str, export: Path) -> None:
        nonlocal injected
        if event != "after_manifest_write" or injected:
            return
        targets = {
            "export_directory": export,
            "candidate_file": export / "candidate" / "root_agent.yaml",
            "candidate_directory": export / "candidate",
            "nested_directory": export / "candidate" / "sub_agents",
            "patch": export / "candidate.patch",
            "manifest": export / "manifest.json",
        }
        injected = _raw_write_windows_ads(targets[target_name], b"hidden")
        assert injected

    lab._fault_hook = inject
    with pytest.raises(WorkflowLabError):
        lab.export("example")

    assert injected
    assert not list(lab.paths.exports_root.rglob("manifest.json"))


def test_output_boundary_pins_every_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = tmp_path / "server-state" / "exports"
    opened: list[object] = []
    if os.name == "nt":
        original = workflow_lab_module._open_windows_directory_handle

        def record_windows_open(path: Path) -> int:
            opened.append(Path(path))
            return original(path)

        monkeypatch.setattr(
            workflow_lab_module,
            "_open_windows_directory_handle",
            record_windows_open,
        )
    else:
        original_open = os.open

        def record_posix_open(
            path: str | bytes | Path,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            opened.append((path, dir_fd))
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", record_posix_open)

    guard = _PinnedOutputDirectory.open_boundary(boundary)
    guard.close()

    relative_parts = boundary.absolute().relative_to(boundary.anchor).parts
    assert len(opened) == len(relative_parts) + 1
    if os.name == "nt":
        assert opened[0] == Path(boundary.anchor)
        assert opened[-1] == boundary.absolute()
    else:
        assert opened[0] == (Path(boundary.anchor), None)
        assert [value[0] for value in opened[1:]] == list(relative_parts)


@pytest.mark.parametrize(
    "component",
    ["state", "exports", "export_id", "candidate", "nested_candidate"],
)
@pytest.mark.parametrize(
    "event",
    ["before_output_directory_open", "after_output_directory_open"],
)
def test_every_output_directory_is_bound_to_its_open_handle(
    tmp_path: Path,
    component: str,
    event: str,
) -> None:
    repo, draft, _ = _repo(tmp_path)
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    (sub_agents / "child.yaml").write_text(
        "agent_class: SequentialAgent\nname: child\ndescription: Nested child.\n",
        encoding="utf-8",
    )
    (draft / "root_agent.yaml").write_text(
        SAFE_AGENT + "sub_agents:\n  - config_path: sub_agents/child.yaml\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"unchanged")
    lab = WorkflowLab.for_source_checkout(repo)
    lab.paths.exports_root.mkdir()
    fired = False
    changed = False
    blocked = False
    moved: Path | None = None
    target: Path | None = None

    def matches(path: Path) -> bool:
        if component == "state":
            return path == lab.paths.state_root
        if component == "exports":
            return path == lab.paths.exports_root
        if component == "export_id":
            return path.parent == lab.paths.exports_root
        if component == "candidate":
            return (
                path.name == "candidate"
                and path.parent.parent == lab.paths.exports_root
            )
        return path.name == "sub_agents" and path.parent.name == "candidate"

    def replace(component_event: str, path: Path) -> None:
        nonlocal fired, changed, blocked, moved, target
        if component_event != event or fired or not matches(path):
            return
        fired = True
        target = path
        moved = path.with_name(path.name + "-server-owned")
        try:
            path.rename(moved)
        except OSError:
            blocked = True
            return
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(path), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        else:
            path.symlink_to(outside, target_is_directory=True)
        changed = True

    lab._fault_hook = replace
    receipt = None
    error = None
    try:
        try:
            receipt = lab.export("example")
        except WorkflowLabError as exc:
            error = exc
        if changed:
            assert error is not None
            assert moved is not None and moved.exists()
            assert not list(lab.paths.exports_root.rglob("manifest.json"))
        else:
            assert blocked
            assert receipt is not None
            receipt.finalize()
    finally:
        if changed:
            assert target is not None and moved is not None
            try:
                metadata = os.lstat(target)
            except FileNotFoundError:
                pass
            else:
                attributes = int(getattr(metadata, "st_file_attributes", 0))
                if stat.S_ISLNK(metadata.st_mode):
                    target.unlink()
                elif attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    os.rmdir(target)
            if moved.exists() and not target.exists():
                moved.rename(target)

    assert fired
    assert sentinel.read_bytes() == b"unchanged"
    assert set(outside.iterdir()) == {sentinel}


def test_owned_tree_cleanup_never_descends_reparse_or_symlink(
    tmp_path: Path,
) -> None:
    boundary = tmp_path / "owned"
    target = boundary / "export-id"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")
    sentinel.chmod(stat.S_IREAD)
    before_mode = sentinel.stat().st_mode
    linked = target / "candidate"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("junction creation is unavailable")
    else:
        linked.symlink_to(outside, target_is_directory=True)

    _remove_owned_tree(target, boundary)

    assert not target.exists()
    assert sentinel.read_text(encoding="utf-8") == "external"
    assert sentinel.stat().st_mode == before_mode


def test_source_checkout_validation_and_export_preserve_tracked_surfaces(
    tmp_path: Path,
) -> None:
    repo, _, published = _repo(tmp_path)
    catalog = repo / "src" / "reserving_workflow" / "workflows" / "catalog.py"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text("CATALOG = ('chainladder-basic',)\n", encoding="utf-8")
    phase3 = repo / "developer_workflows" / "ai_actuary_developer" / "tools.py"
    phase3.parent.mkdir(parents=True)
    phase3.write_text("PHASE3 = 'immutable'\n", encoding="utf-8")
    before = {
        "published": _tree_digest(published),
        "catalog": catalog.read_bytes(),
        "phase3": phase3.read_bytes(),
        "index": (repo / ".git" / "index").read_bytes(),
    }

    lab = WorkflowLab.for_source_checkout(repo)
    lab.validate("example")
    receipt = lab.export("example")
    receipt.finalize()

    assert _tree_digest(published) == before["published"]
    assert catalog.read_bytes() == before["catalog"]
    assert phase3.read_bytes() == before["phase3"]
    assert (repo / ".git" / "index").read_bytes() == before["index"]


def test_real_git_integrity_proof_excludes_only_ignored_workflow_state(
    tmp_path: Path,
) -> None:
    repo, _, _ = _repo(tmp_path)
    (repo / ".git" / "index").unlink()
    (repo / ".git").rmdir()
    (repo / ".gitignore").write_text("tmp/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    before = capture_source_integrity(repo)

    lab = WorkflowLab.for_source_checkout(repo)
    lab.validate("example")
    receipt = lab.export("example")
    receipt.finalize()
    after = capture_source_integrity(repo)

    assert_source_integrity_unchanged(before, after)


def test_integrity_capture_does_not_refresh_a_stale_git_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "tracked.txt"
    tracked.write_text("stable\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True
    )
    index = repo / ".git" / "index"
    before = index.read_bytes()
    metadata = tracked.stat()
    os.utime(
        tracked,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000),
    )

    snapshot = capture_source_integrity(repo)

    assert index.read_bytes() == before
    assert snapshot.index_digest == hashlib.sha256(before).hexdigest()


@pytest.mark.parametrize(
    ("relative", "initial", "changed"),
    [
        ("tmp/run-history/run.json", "before", "after"),
        ("tmp/artifacts/result.json", "before", "after"),
        ("tmp/reviews/review.json", "before", "after"),
        (".env", "SECRET=before", "SECRET=after"),
    ],
)
def test_integrity_detects_nonallowlisted_ignored_mutation(
    tmp_path: Path, relative: str, initial: str, changed: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("tmp/\n.env\n", encoding="utf-8")
    tracked = repo / "tracked.txt"
    tracked.write_text("stable\n", encoding="utf-8")
    target = repo.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(initial, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    before = capture_source_integrity(repo)

    target.write_text(changed, encoding="utf-8")
    after = capture_source_integrity(repo)

    with pytest.raises(WorkflowLabError) as caught:
        assert_source_integrity_unchanged(before, after)
    assert caught.value.code == "source_integrity_changed"


def test_integrity_excludes_only_workflow_draft_and_export_roots(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("tmp/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("stable\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    (repo / "tmp").mkdir()
    before = capture_source_integrity(repo)
    for relative in (
        "tmp/adk-workflow-drafts/example/root_agent.yaml",
        "tmp/adk-workflow-exports/export-id/manifest.json",
    ):
        target = repo.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("allowlisted", encoding="utf-8")

    after = capture_source_integrity(repo)

    assert_source_integrity_unchanged(before, after)


def test_integrity_detects_ignored_empty_directory_create_and_delete(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("tmp/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("stable\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    (repo / "tmp").mkdir()
    before = capture_source_integrity(repo)
    empty = repo / "tmp" / "run-history" / "empty-evidence"
    empty.mkdir(parents=True)

    created = capture_source_integrity(repo)
    with pytest.raises(WorkflowLabError):
        assert_source_integrity_unchanged(before, created)

    empty.rmdir()
    empty.parent.rmdir()
    deleted = capture_source_integrity(repo)
    assert_source_integrity_unchanged(before, deleted)


@pytest.mark.parametrize("replacement_type", ["directory", "symlink"])
def test_integrity_detects_ignored_object_type_replacement(
    tmp_path: Path, replacement_type: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("tmp/\n", encoding="utf-8")
    tracked = repo / "tracked.txt"
    tracked.write_text("stable\n", encoding="utf-8")
    target = repo / "tmp" / "run-history" / "evidence"
    target.parent.mkdir(parents=True)
    target.write_text("evidence", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    before = capture_source_integrity(repo)
    target.unlink()
    if replacement_type == "directory":
        target.mkdir()
    else:
        try:
            target.symlink_to(tracked)
        except OSError:
            pytest.skip("symlink creation is unavailable")

    after = capture_source_integrity(repo)

    with pytest.raises(WorkflowLabError):
        assert_source_integrity_unchanged(before, after)


def test_protected_source_digest_never_follows_external_link_or_junction(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "src"
    source.mkdir(parents=True)
    (source / "tracked.py").write_text("STABLE = True\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("first-secret", encoding="utf-8")
    linked = source / "external"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("junction creation is unavailable")
    else:
        linked.symlink_to(outside, target_is_directory=True)
    try:
        before = capture_source_integrity(repo)
        secret.write_text("second-secret-with-different-size", encoding="utf-8")
        after = capture_source_integrity(repo)
    finally:
        if os.name == "nt":
            os.rmdir(linked)

    assert before.source_tree_digest == after.source_tree_digest


@pytest.mark.parametrize("surface", ["tracked", "src", "published", "catalog"])
@pytest.mark.parametrize(
    "event", ["before_integrity_directory_open", "after_integrity_directory_open"]
)
def test_protected_path_components_are_rebound_to_open_handles(
    tmp_path: Path,
    surface: str,
    event: str,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "attacker-tree"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_bytes(b"ATTACKER-SECRET")
    if surface == "tracked":
        protected_root = repo / "tracked_area"
        target = protected_root
        protected_root.mkdir(parents=True)
        (protected_root / "payload.txt").write_bytes(b"trusted")
        operation = lambda hook: source_integrity_module._tracked_content_nofollow(
            repo, "tracked_area/payload.txt", fault_hook=hook
        )
        trusted_result: bytes | str = b"trusted"
    elif surface == "src":
        protected_root = repo / "src"
        target = protected_root / "intermediate"
        target.mkdir(parents=True)
        (target / "payload.txt").write_bytes(b"trusted")
        operation = lambda hook: source_integrity_module._directory_digest_relative(
            repo, "src", fault_hook=hook
        )
        trusted_result = source_integrity_module._directory_digest_relative(repo, "src")
    elif surface == "published":
        protected_root = repo / "src" / "reserving_workflow" / "developer_workflows"
        target = protected_root / "intermediate"
        target.mkdir(parents=True)
        (target / "payload.txt").write_bytes(b"trusted")
        operation = lambda hook: source_integrity_module._directory_digest_relative(
            repo,
            "src/reserving_workflow/developer_workflows",
            fault_hook=hook,
        )
        trusted_result = source_integrity_module._directory_digest_relative(
            repo, "src/reserving_workflow/developer_workflows"
        )
    else:
        protected_root = repo / "src" / "reserving_workflow" / "workflows"
        target = protected_root
        protected_root.mkdir(parents=True)
        (protected_root / "catalog.py").write_bytes(b"trusted")
        operation = lambda hook: source_integrity_module._tracked_content_nofollow(
            repo,
            "src/reserving_workflow/workflows/catalog.py",
            fault_hook=hook,
        )
        trusted_result = b"trusted"
    moved = target.with_name(target.name + "-trusted")
    fired = False
    swap_blocked = False

    def swap(component_event: str, component: Path) -> None:
        nonlocal fired, swap_blocked
        if component_event != event or component != target or fired:
            return
        fired = True
        try:
            target.rename(moved)
        except OSError:
            swap_blocked = True
            return
        shutil.copytree(outside, target)

    try:
        if event == "after_integrity_directory_open" and os.name == "nt":
            result = operation(swap)
            assert swap_blocked
            assert result == trusted_result
        else:
            with pytest.raises(WorkflowLabError) as caught:
                operation(swap)
            assert caught.value.code == "integrity_state_changed"
            assert not swap_blocked
    finally:
        if moved.exists():
            if target.exists():
                shutil.rmtree(target)
            moved.rename(target)

    assert fired
    assert (outside / "secret.txt").read_bytes() == b"ATTACKER-SECRET"


@pytest.mark.parametrize(
    ("published", "candidate"),
    [
        ({"agent.yaml": b"name: old"}, {"agent.yaml": b"name: new\n"}),
        ({}, {"agent.yaml": b"name: added"}),
        ({"agent.yaml": b"name: deleted"}, {}),
    ],
)
def test_patch_is_git_apply_parseable_for_no_newline_add_and_delete(
    tmp_path: Path,
    published: dict[str, bytes],
    candidate: dict[str, bytes],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    for relative, content in published.items():
        target = (
            repo
            / "src"
            / "reserving_workflow"
            / "developer_workflows"
            / "example"
            / relative
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    patch = _build_patch("example", published, candidate)
    patch_path = repo / "candidate.patch"
    patch_path.write_bytes(patch)

    result = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    if any(
        not content.endswith(b"\n")
        for content in (*published.values(), *candidate.values())
    ):
        assert b"\\ No newline at end of file\n" in patch


def _make_writable(function, path, exc_info) -> None:  # type: ignore[no-untyped-def]
    del exc_info
    selected = Path(path)
    if os.name != "nt":
        _make_owner_directory_writable(selected.parent)
        metadata = os.lstat(selected)
        if stat.S_ISDIR(metadata.st_mode):
            _make_owner_directory_writable(selected)
        elif not stat.S_ISLNK(metadata.st_mode):
            selected.chmod(stat.S_IWRITE | stat.S_IREAD)
    else:
        selected.chmod(stat.S_IWRITE | stat.S_IREAD)
    function(path)


def _make_owner_directory_writable(directory: Path) -> None:
    metadata = os.lstat(directory)
    assert stat.S_ISDIR(metadata.st_mode)
    assert not stat.S_ISLNK(metadata.st_mode)
    directory.chmod(stat.S_IRWXU)
