"""Read-only source-checkout integrity proof around Workflow Lab operations."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workflow_lab import (
    WorkflowLabError,
    _close_windows_handle,
    _open_pinned_input,
    _open_windows_directory_handle,
)


@dataclass(frozen=True)
class SourceIntegritySnapshot:
    index_digest: str
    tracked_tree_digest: str
    source_tree_digest: str
    published_tree_digest: str
    workflow_catalog_digest: str
    non_allowlisted_state_digest: str
    porcelain_v2: bytes


_IntegrityFaultHook = Callable[[str, Path], None]


def _no_integrity_fault(event: str, path: Path) -> None:
    del event, path


def capture_source_integrity(repo_root: Path) -> SourceIntegritySnapshot:
    root = Path(repo_root).absolute()
    if not (root / ".git").exists():
        raise WorkflowLabError(
            "source_checkout_required",
            "Integrity proof requires a Git source checkout.",
            stage="integrity",
        )
    index = _git_directory(root) / "index"
    index_digest = _file_digest(index)
    tracked = _git(root, "ls-files", "-z")
    tracked_paths = [item for item in tracked.split(b"\0") if item]
    tracked_digest = hashlib.sha256()
    for raw_path in sorted(tracked_paths):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        tracked_digest.update(len(raw_path).to_bytes(4, "big"))
        tracked_digest.update(raw_path)
        content = _tracked_content_nofollow(root, relative)
        tracked_digest.update(len(content).to_bytes(8, "big"))
        tracked_digest.update(content)
    porcelain = _git(
        root,
        "status",
        "--porcelain=v2",
        "--untracked-files=all",
        "--ignore-submodules=all",
    )
    return SourceIntegritySnapshot(
        index_digest=index_digest,
        tracked_tree_digest=tracked_digest.hexdigest(),
        source_tree_digest=_directory_digest_relative(root, "src"),
        published_tree_digest=_directory_digest_relative(
            root,
            "src/reserving_workflow/developer_workflows",
        ),
        workflow_catalog_digest=hashlib.sha256(
            _tracked_content_nofollow(
                root,
                "src/reserving_workflow/workflows/catalog.py",
            )
        ).hexdigest(),
        non_allowlisted_state_digest=_filesystem_state_digest(root, tracked_paths),
        porcelain_v2=porcelain,
    )


def assert_source_integrity_unchanged(
    before: SourceIntegritySnapshot,
    after: SourceIntegritySnapshot,
) -> None:
    if before != after:
        changed = [
            field
            for field in before.__dataclass_fields__
            if getattr(before, field) != getattr(after, field)
        ]
        raise WorkflowLabError(
            "source_integrity_changed",
            f"Workflow Lab operation changed protected source state: {changed}",
            stage="integrity",
        )


def _git_directory(repo_root: Path) -> Path:
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    text = marker.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not text.startswith(prefix):
        raise WorkflowLabError(
            "git_directory_invalid",
            "The source checkout .git pointer is invalid.",
            stage="integrity",
        )
    candidate = Path(text[len(prefix) :])
    return candidate if candidate.is_absolute() else (repo_root / candidate).absolute()


def _git(repo_root: Path, *arguments: str) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise WorkflowLabError(
            "git_integrity_read_failed",
            result.stderr.decode("utf-8", errors="replace").strip(),
            stage="integrity",
        )
    return result.stdout


def _filesystem_state_digest(repo_root: Path, tracked_paths: list[bytes]) -> str:
    digest = hashlib.sha256()
    tracked = {
        raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for raw in tracked_paths
    }
    if os.name == "nt":
        _walk_windows_state(repo_root, "", tracked, digest)
    else:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(repo_root, flags)
        try:
            _walk_posix_state(descriptor, "", tracked, digest, directory_path=repo_root)
        finally:
            os.close(descriptor)
    return digest.hexdigest()


def _walk_windows_state(
    directory: Path,
    prefix: str,
    tracked: set[str],
    digest: Any,
    *,
    skip_allowlisted: bool = True,
    directory_handle: int | None = None,
    fault_hook: _IntegrityFaultHook = _no_integrity_fault,
) -> None:
    own_handle = directory_handle is None
    if directory_handle is None:
        before = os.lstat(directory)
        handle, identity = _open_windows_integrity_directory(
            directory, before, prefix or ".", fault_hook
        )
    else:
        handle = directory_handle
        identity = _windows_directory_handle_identity(handle)
    try:
        children = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        for entry in children:
            relative = f"{prefix}/{entry.name}".lstrip("/").replace("\\", "/")
            if skip_allowlisted and _skip_state_path(relative):
                continue
            path = Path(entry.path)
            metadata = os.lstat(path)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode):
                if relative not in tracked:
                    target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                    _update_state_digest(digest, relative, b"symlink", hashlib.sha256(target).digest())
            elif attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                if relative not in tracked:
                    tag = str(getattr(metadata, "st_reparse_tag", 0)).encode("ascii")
                    _update_state_digest(digest, relative, b"reparse", hashlib.sha256(tag).digest())
            elif stat.S_ISDIR(metadata.st_mode):
                _update_state_digest(digest, relative, b"directory")
                child, child_identity = _open_windows_integrity_directory(
                    path, metadata, relative, fault_hook
                )
                try:
                    _walk_windows_state(
                        path,
                        relative,
                        tracked,
                        digest,
                        skip_allowlisted=skip_allowlisted,
                        directory_handle=child,
                        fault_hook=fault_hook,
                    )
                    _verify_windows_integrity_directory(
                        path, child_identity, relative
                    )
                finally:
                    _close_windows_handle(child)
            elif stat.S_ISREG(metadata.st_mode):
                if relative not in tracked:
                    _update_state_digest(
                        digest,
                        relative,
                        b"regular",
                        _regular_path_digest(path, metadata),
                    )
            elif relative not in tracked:
                _update_state_digest(digest, relative, _special_kind(metadata.st_mode))
        _verify_windows_integrity_directory(directory, identity, prefix or ".")
    finally:
        if own_handle:
            _close_windows_handle(handle)


def _walk_posix_state(
    descriptor: int,
    prefix: str,
    tracked: set[str],
    digest: Any,
    *,
    skip_allowlisted: bool = True,
    fault_hook: _IntegrityFaultHook = _no_integrity_fault,
    directory_path: Path | None = None,
) -> None:
    children = sorted(os.scandir(descriptor), key=lambda entry: entry.name.casefold())
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    for entry in children:
        relative = f"{prefix}/{entry.name}".lstrip("/")
        if skip_allowlisted and _skip_state_path(relative):
            continue
        metadata = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            if relative not in tracked:
                target = os.readlink(entry.name, dir_fd=descriptor).encode(
                    "utf-8", errors="surrogateescape"
                )
                _update_state_digest(
                    digest,
                    relative,
                    b"symlink",
                    hashlib.sha256(target).digest(),
                )
        elif stat.S_ISDIR(metadata.st_mode):
            _update_state_digest(digest, relative, b"directory")
            child_path = (
                directory_path / entry.name
                if directory_path is not None
                else Path(relative)
            )
            fault_hook("before_integrity_directory_open", child_path)
            child = os.open(entry.name, directory_flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if _directory_identity(opened) != _directory_identity(metadata):
                    raise _state_race(relative)
                fault_hook("after_integrity_directory_open", child_path)
                _walk_posix_state(
                    child,
                    relative,
                    tracked,
                    digest,
                    skip_allowlisted=skip_allowlisted,
                    fault_hook=fault_hook,
                    directory_path=child_path,
                )
                final = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if _directory_identity(final) != _directory_identity(metadata):
                    raise _state_race(relative)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            if relative not in tracked:
                _update_state_digest(
                    digest,
                    relative,
                    b"regular",
                    _regular_at_digest(descriptor, entry.name, metadata, relative),
                )
        elif relative not in tracked:
            _update_state_digest(digest, relative, _special_kind(metadata.st_mode))


def _regular_path_digest(path: Path, before: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        return _digest_open_regular(descriptor, before, path.as_posix(), lambda: os.lstat(path))
    finally:
        os.close(descriptor)


def _regular_at_digest(
    directory_descriptor: int,
    name: str,
    before: os.stat_result,
    relative: str,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        return _digest_open_regular(
            descriptor,
            before,
            relative,
            lambda: os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False),
        )
    finally:
        os.close(descriptor)


def _digest_open_regular(
    descriptor: int,
    before: os.stat_result,
    relative: str,
    final_stat: Callable[[], os.stat_result],
) -> bytes:
    opened = os.fstat(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
        before.st_nlink,
        before.st_size,
    )
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        stat.S_IFMT(opened.st_mode),
        opened.st_nlink,
        opened.st_size,
    )
    if not stat.S_ISREG(opened.st_mode) or opened_identity != before_identity:
        raise _state_race(relative)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        digest.update(chunk)
    after_open = os.fstat(descriptor)
    after_path = final_stat()
    after_open_identity = (
        after_open.st_dev,
        after_open.st_ino,
        stat.S_IFMT(after_open.st_mode),
        after_open.st_nlink,
        after_open.st_size,
    )
    after_path_identity = (
        after_path.st_dev,
        after_path.st_ino,
        stat.S_IFMT(after_path.st_mode),
        after_path.st_nlink,
        after_path.st_size,
    )
    if after_open_identity != before_identity or after_path_identity != before_identity:
        raise _state_race(relative)
    return digest.digest()


def _update_state_digest(
    digest: Any,
    relative: str,
    object_type: bytes,
    content_digest: bytes = b"",
) -> None:
    label = relative.encode("utf-8", errors="surrogateescape")
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(object_type).to_bytes(2, "big"))
    digest.update(object_type)
    digest.update(content_digest)


def _special_kind(mode: int) -> bytes:
    if stat.S_ISFIFO(mode):
        return b"fifo"
    if stat.S_ISSOCK(mode):
        return b"socket"
    if stat.S_ISCHR(mode):
        return b"character-device"
    if stat.S_ISBLK(mode):
        return b"block-device"
    return b"other-special"


def _skip_state_path(relative: str) -> bool:
    normalized = relative.strip("/")
    return normalized == ".git" or normalized.startswith(".git/") or _is_allowlisted_workflow_state(normalized)


def _state_race(relative: str) -> WorkflowLabError:
    return WorkflowLabError(
        "integrity_state_changed",
        f"Non-allowlisted filesystem state changed during capture: {relative}",
        stage="integrity",
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_nlink),
    )


_WINDOWS_IDENTITY_API: tuple[Any, Any, type[Any], type[Any]] | None = None


def _windows_identity_api() -> tuple[Any, Any, type[Any], type[Any]]:
    import ctypes
    from ctypes import wintypes

    global _WINDOWS_IDENTITY_API
    if _WINDOWS_IDENTITY_API is not None:
        return _WINDOWS_IDENTITY_API

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("CreationTimeLow", wintypes.DWORD),
            ("CreationTimeHigh", wintypes.DWORD),
            ("LastAccessTimeLow", wintypes.DWORD),
            ("LastAccessTimeHigh", wintypes.DWORD),
            ("LastWriteTimeLow", wintypes.DWORD),
            ("LastWriteTimeHigh", wintypes.DWORD),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        ]

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_legacy = kernel32.GetFileInformationByHandle
    get_legacy.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_legacy.restype = wintypes.BOOL
    get_extended = kernel32.GetFileInformationByHandleEx
    get_extended.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_extended.restype = wintypes.BOOL
    _WINDOWS_IDENTITY_API = (
        get_legacy,
        get_extended,
        _ByHandleFileInformation,
        _FileIdInfo,
    )
    return _WINDOWS_IDENTITY_API


def _windows_directory_handle_identity(handle: int) -> tuple[int, int, bytes]:
    import ctypes
    from ctypes import wintypes

    get_legacy, get_extended, legacy_type, file_id_type = _windows_identity_api()
    legacy = legacy_type()
    if not get_legacy(wintypes.HANDLE(handle), ctypes.byref(legacy)):
        raise _state_race("Windows directory handle")
    if not legacy.FileAttributes & 0x10 or legacy.FileAttributes & 0x400:
        raise _state_race("Windows directory handle")

    file_id = file_id_type()
    if not get_extended(
        wintypes.HANDLE(handle),
        18,
        ctypes.byref(file_id),
        ctypes.sizeof(file_id),
    ):
        raise _state_race("Windows directory handle")
    file_index = (int(legacy.FileIndexHigh) << 32) | int(legacy.FileIndexLow)
    return (
        int(legacy.VolumeSerialNumber),
        file_index,
        bytes(file_id.FileId.Identifier),
    )


def _open_windows_integrity_directory(
    path: Path,
    before: os.stat_result,
    relative: str,
    fault_hook: _IntegrityFaultHook,
) -> tuple[int, tuple[int, int, bytes]]:
    attributes = int(getattr(before, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(before.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISDIR(before.st_mode)
    ):
        raise _state_race(relative)
    fault_hook("before_integrity_directory_open", path)
    try:
        handle = _open_windows_directory_handle(path)
    except (FileNotFoundError, OSError, WorkflowLabError) as exc:
        raise _state_race(relative) from exc
    try:
        opened = _windows_directory_handle_identity(handle)
        if opened[:2] != (int(before.st_dev), int(before.st_ino)):
            raise _state_race(relative)
        fault_hook("after_integrity_directory_open", path)
        return handle, opened
    except Exception:
        _close_windows_handle(handle)
        raise


def _verify_windows_integrity_directory(
    path: Path,
    expected: tuple[int, int, bytes],
    relative: str,
) -> None:
    try:
        metadata = os.lstat(path)
        handle = _open_windows_directory_handle(path)
    except (FileNotFoundError, OSError, WorkflowLabError) as exc:
        raise _state_race(relative) from exc
    try:
        if (int(metadata.st_dev), int(metadata.st_ino)) != expected[:2]:
            raise _state_race(relative)
        if _windows_directory_handle_identity(handle) != expected:
            raise _state_race(relative)
    finally:
        _close_windows_handle(handle)


def _verify_posix_directory_chain(
    root: Path,
    root_descriptor: int,
    root_identity: tuple[int, int, int, int],
    records: list[tuple[int, str, int, tuple[int, int, int, int], str]],
) -> None:
    try:
        if _directory_identity(os.lstat(root)) != root_identity:
            raise _state_race(str(root))
        if _directory_identity(os.fstat(root_descriptor)) != root_identity:
            raise _state_race(str(root))
        for parent, name, child, expected, relative in records:
            path_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _directory_identity(path_metadata) != expected:
                raise _state_race(relative)
            if _directory_identity(os.fstat(child)) != expected:
                raise _state_race(relative)
    except (FileNotFoundError, OSError) as exc:
        raise _state_race(str(root)) from exc


def _is_allowlisted_workflow_state(relative: str) -> bool:
    normalized = relative.strip("/")
    return normalized in {
        "tmp/adk-workflow-drafts",
        "tmp/adk-workflow-exports",
    } or normalized.startswith(
        ("tmp/adk-workflow-drafts/", "tmp/adk-workflow-exports/")
    )


def _tracked_content_nofollow(
    repo_root: Path,
    relative: str,
    *,
    fault_hook: _IntegrityFaultHook = _no_integrity_fault,
) -> bytes:
    parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise WorkflowLabError(
            "git_integrity_read_failed",
            f"Unsafe tracked path returned by Git: {relative}",
            stage="integrity",
        )
    if os.name == "nt":
        handles: list[int] = []
        records: list[tuple[Path, tuple[int, int, bytes], str]] = []
        final_binding: tuple[int, Path, tuple[int, ...]] | None = None
        current = repo_root
        try:
            root_metadata = os.lstat(current)
            root_handle, root_identity = _open_windows_integrity_directory(
                current, root_metadata, ".", fault_hook
            )
            handles.append(root_handle)
            records.append((current, root_identity, "."))
            for part in parts[:-1]:
                current = current / part
                try:
                    metadata = os.lstat(current)
                except FileNotFoundError:
                    return b"<missing>"
                handle, identity = _open_windows_integrity_directory(
                    current, metadata, relative, fault_hook
                )
                handles.append(handle)
                records.append((current, identity, relative))
            target = current / parts[-1]
            metadata = os.lstat(target)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if (
                stat.S_ISREG(metadata.st_mode)
                and not (
                    attributes
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
            ):
                descriptor = _open_pinned_input(target)
                final_binding = (descriptor, target, _stable_identity(metadata))
                return _stable_descriptor_content(
                    descriptor,
                    metadata,
                    relative,
                    lambda: os.lstat(target),
                )
            return _stable_path_content(target, relative)
        except FileNotFoundError:
            return b"<missing>"
        finally:
            try:
                for path, identity, label in records:
                    _verify_windows_integrity_directory(path, identity, label)
                if final_binding is not None:
                    descriptor, target, expected = final_binding
                    if _stable_identity(os.fstat(descriptor)) != expected:
                        raise _state_race(relative)
                    if _stable_identity(os.lstat(target)) != expected:
                        raise _state_race(relative)
            finally:
                if final_binding is not None:
                    os.close(final_binding[0])
                for handle in reversed(handles):
                    _close_windows_handle(handle)

    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    records: list[tuple[int, str, int, tuple[int, int, int, int], str]] = []
    final_binding: tuple[int, int, str, tuple[int, ...]] | None = None
    root_metadata = os.lstat(repo_root)
    fault_hook("before_integrity_directory_open", repo_root)
    descriptor = os.open(repo_root, directory_flags)
    descriptors.append(descriptor)
    root_identity = _directory_identity(root_metadata)
    if _directory_identity(os.fstat(descriptor)) != root_identity:
        os.close(descriptor)
        raise _state_race(relative)
    try:
        fault_hook("after_integrity_directory_open", repo_root)
    except Exception:
        os.close(descriptor)
        raise
    current_path = repo_root
    try:
        for part in parts[:-1]:
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return b"<missing>"
            if not stat.S_ISDIR(metadata.st_mode):
                raise _state_race(relative)
            current_path = current_path / part
            fault_hook("before_integrity_directory_open", current_path)
            child = os.open(part, directory_flags, dir_fd=descriptor)
            child_identity = _directory_identity(os.fstat(child))
            if child_identity != _directory_identity(metadata):
                os.close(child)
                raise _state_race(relative)
            records.append((descriptor, part, child, child_identity, relative))
            descriptors.append(child)
            fault_hook("after_integrity_directory_open", current_path)
            descriptor = child
        try:
            metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return b"<missing>"
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(parts[-1], dir_fd=descriptor).encode(
                "utf-8", errors="surrogateescape"
            )
            return b"<symlink>\0" + target
        if not stat.S_ISREG(metadata.st_mode):
            return b"<object>\0" + _special_kind(metadata.st_mode)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        opened = os.open(parts[-1], flags, dir_fd=descriptor)
        final_binding = (
            opened,
            descriptor,
            parts[-1],
            _stable_identity(metadata),
        )
        return _stable_descriptor_content(
            opened,
            metadata,
            relative,
            lambda: os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False),
        )
    finally:
        try:
            _verify_posix_directory_chain(
                repo_root,
                descriptors[0],
                root_identity,
                records,
            )
            if final_binding is not None:
                opened, parent, name, expected = final_binding
                if _stable_identity(os.fstat(opened)) != expected:
                    raise _state_race(relative)
                final = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _stable_identity(final) != expected:
                    raise _state_race(relative)
        finally:
            if final_binding is not None:
                os.close(final_binding[0])
            for opened_directory in reversed(descriptors):
                os.close(opened_directory)


def _stable_path_content(path: Path, relative: str) -> bytes:
    metadata = os.lstat(path)
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(metadata.st_mode):
        return b"<symlink>\0" + os.readlink(path).encode(
            "utf-8", errors="surrogateescape"
        )
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return b"<reparse>\0" + str(getattr(metadata, "st_reparse_tag", 0)).encode(
            "ascii"
        )
    if not stat.S_ISREG(metadata.st_mode):
        return b"<object>\0" + _special_kind(metadata.st_mode)
    descriptor = _open_pinned_input(path)
    try:
        return _stable_descriptor_content(
            descriptor,
            metadata,
            relative,
            lambda: os.lstat(path),
        )
    finally:
        os.close(descriptor)


def _stable_descriptor_content(
    descriptor: int,
    before: os.stat_result,
    relative: str,
    final_stat: Callable[[], os.stat_result],
) -> bytes:
    expected = _stable_identity(before)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or _stable_identity(opened) != expected:
        raise _state_race(relative)
    first = _read_all(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    second = _read_all(descriptor)
    if first != second:
        raise _state_race(relative)
    if _stable_identity(os.fstat(descriptor)) != expected:
        raise _state_race(relative)
    if _stable_identity(final_stat()) != expected:
        raise _state_race(relative)
    return first


def _stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _directory_digest_relative(
    boundary: Path,
    relative: str,
    *,
    fault_hook: _IntegrityFaultHook = _no_integrity_fault,
) -> str:
    parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
    digest = hashlib.sha256()
    if os.name == "nt":
        handles: list[int] = []
        records: list[tuple[Path, tuple[int, int, bytes], str]] = []
        current = boundary
        try:
            boundary_metadata = os.lstat(current)
            boundary_handle, boundary_identity = _open_windows_integrity_directory(
                current, boundary_metadata, ".", fault_hook
            )
            handles.append(boundary_handle)
            records.append((current, boundary_identity, "."))
            for index, part in enumerate(parts):
                current = current / part
                try:
                    metadata = os.lstat(current)
                except FileNotFoundError:
                    return digest.hexdigest()
                attributes = int(getattr(metadata, "st_file_attributes", 0))
                if stat.S_ISLNK(metadata.st_mode):
                    if index != len(parts) - 1:
                        raise _state_race(relative)
                    target = os.readlink(current).encode(
                        "utf-8", errors="surrogateescape"
                    )
                    _update_state_digest(
                        digest,
                        ".",
                        b"symlink",
                        hashlib.sha256(target).digest(),
                    )
                    return digest.hexdigest()
                if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    if index != len(parts) - 1:
                        raise _state_race(relative)
                    tag = str(getattr(metadata, "st_reparse_tag", 0)).encode("ascii")
                    _update_state_digest(
                        digest,
                        ".",
                        b"reparse",
                        hashlib.sha256(tag).digest(),
                    )
                    return digest.hexdigest()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise _state_race(relative)
                handle, identity = _open_windows_integrity_directory(
                    current, metadata, relative, fault_hook
                )
                handles.append(handle)
                records.append((current, identity, relative))
            _walk_windows_state(
                current,
                "",
                set(),
                digest,
                skip_allowlisted=False,
                directory_handle=handles[-1],
                fault_hook=fault_hook,
            )
            return digest.hexdigest()
        finally:
            try:
                for path, identity, label in records:
                    _verify_windows_integrity_directory(path, identity, label)
            finally:
                for handle in reversed(handles):
                    _close_windows_handle(handle)

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    records: list[tuple[int, str, int, tuple[int, int, int, int], str]] = []
    boundary_metadata = os.lstat(boundary)
    fault_hook("before_integrity_directory_open", boundary)
    descriptor = os.open(boundary, flags)
    descriptors.append(descriptor)
    root_identity = _directory_identity(boundary_metadata)
    if _directory_identity(os.fstat(descriptor)) != root_identity:
        os.close(descriptor)
        raise _state_race(relative)
    try:
        fault_hook("after_integrity_directory_open", boundary)
    except Exception:
        os.close(descriptor)
        raise
    current_path = boundary
    try:
        for index, part in enumerate(parts):
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return digest.hexdigest()
            if stat.S_ISLNK(metadata.st_mode):
                if index != len(parts) - 1:
                    raise _state_race(relative)
                target = os.readlink(part, dir_fd=descriptor).encode(
                    "utf-8", errors="surrogateescape"
                )
                _update_state_digest(
                    digest,
                    ".",
                    b"symlink",
                    hashlib.sha256(target).digest(),
                )
                return digest.hexdigest()
            if not stat.S_ISDIR(metadata.st_mode):
                raise _state_race(relative)
            current_path = current_path / part
            fault_hook("before_integrity_directory_open", current_path)
            child = os.open(part, flags, dir_fd=descriptor)
            child_identity = _directory_identity(os.fstat(child))
            if child_identity != _directory_identity(metadata):
                os.close(child)
                raise _state_race(relative)
            records.append((descriptor, part, child, child_identity, relative))
            descriptors.append(child)
            fault_hook("after_integrity_directory_open", current_path)
            descriptor = child
        _walk_posix_state(
            descriptor,
            "",
            set(),
            digest,
            skip_allowlisted=False,
            fault_hook=fault_hook,
            directory_path=current_path,
        )
        return digest.hexdigest()
    finally:
        try:
            _verify_posix_directory_chain(
                boundary,
                descriptors[0],
                root_identity,
                records,
            )
        finally:
            for opened_directory in reversed(descriptors):
                os.close(opened_directory)


def _file_digest(
    path: Path,
    *,
    fault_hook: _IntegrityFaultHook = _no_integrity_fault,
) -> str:
    absolute = Path(path).absolute()
    anchor = Path(absolute.anchor)
    parts = absolute.relative_to(anchor).parts
    if not parts:
        return _empty_digest()

    if os.name == "nt":
        handles: list[int] = []
        records: list[tuple[Path, tuple[int, int, bytes], str]] = []
        current = anchor
        try:
            root_metadata = os.lstat(current)
            root_handle, root_identity = _open_windows_integrity_directory(
                current, root_metadata, ".", fault_hook
            )
            handles.append(root_handle)
            records.append((current, root_identity, "."))
            for part in parts[:-1]:
                current = current / part
                try:
                    metadata = os.lstat(current)
                except FileNotFoundError:
                    return _empty_digest()
                handle, identity = _open_windows_integrity_directory(
                    current, metadata, str(absolute), fault_hook
                )
                handles.append(handle)
                records.append((current, identity, str(absolute)))
            target = current / parts[-1]
            try:
                metadata = os.lstat(target)
            except FileNotFoundError:
                return _empty_digest()
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode):
                content = os.readlink(target).encode(
                    "utf-8", errors="surrogateescape"
                )
                return hashlib.sha256(b"symlink\0" + content).hexdigest()
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                tag = str(getattr(metadata, "st_reparse_tag", 0)).encode("ascii")
                return hashlib.sha256(b"reparse\0" + tag).hexdigest()
            if not stat.S_ISREG(metadata.st_mode):
                marker = b"non-regular\0" + _special_kind(metadata.st_mode)
                return hashlib.sha256(marker).hexdigest()
            return _regular_path_digest(target, metadata).hex()
        finally:
            try:
                for directory, identity, label in records:
                    _verify_windows_integrity_directory(directory, identity, label)
            finally:
                for handle in reversed(handles):
                    _close_windows_handle(handle)

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    records: list[tuple[int, str, int, tuple[int, int, int, int], str]] = []
    root_metadata = os.lstat(anchor)
    fault_hook("before_integrity_directory_open", anchor)
    descriptor = os.open(anchor, flags)
    descriptors.append(descriptor)
    root_identity = _directory_identity(root_metadata)
    if _directory_identity(os.fstat(descriptor)) != root_identity:
        os.close(descriptor)
        raise _state_race(str(absolute))
    try:
        fault_hook("after_integrity_directory_open", anchor)
    except Exception:
        os.close(descriptor)
        raise
    current_path = anchor
    try:
        for part in parts[:-1]:
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return _empty_digest()
            if not stat.S_ISDIR(metadata.st_mode):
                raise _state_race(str(absolute))
            current_path = current_path / part
            fault_hook("before_integrity_directory_open", current_path)
            child = os.open(part, flags, dir_fd=descriptor)
            child_identity = _directory_identity(os.fstat(child))
            if child_identity != _directory_identity(metadata):
                os.close(child)
                raise _state_race(str(absolute))
            records.append((descriptor, part, child, child_identity, str(absolute)))
            descriptors.append(child)
            fault_hook("after_integrity_directory_open", current_path)
            descriptor = child
        try:
            metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return _empty_digest()
        if stat.S_ISLNK(metadata.st_mode):
            content = os.readlink(parts[-1], dir_fd=descriptor).encode(
                "utf-8", errors="surrogateescape"
            )
            return hashlib.sha256(b"symlink\0" + content).hexdigest()
        if not stat.S_ISREG(metadata.st_mode):
            marker = b"non-regular\0" + _special_kind(metadata.st_mode)
            return hashlib.sha256(marker).hexdigest()
        return _regular_at_digest(
            descriptor,
            parts[-1],
            metadata,
            str(absolute),
        ).hex()
    finally:
        try:
            _verify_posix_directory_chain(
                anchor,
                descriptors[0],
                root_identity,
                records,
            )
        finally:
            for opened_directory in reversed(descriptors):
                os.close(opened_directory)


def _empty_digest() -> str:
    return hashlib.sha256(b"").hexdigest()


__all__ = [
    "SourceIntegritySnapshot",
    "assert_source_integrity_unchanged",
    "capture_source_integrity",
]
