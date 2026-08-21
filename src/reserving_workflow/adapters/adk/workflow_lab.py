"""Controlled declarative Workflow Lab validation and deterministic export."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Self

import yaml

ADK_VERSION = "2.7.1"
SCHEMA_SHA256 = "871465ebce420115d47af916451d9d36eae97dc3f67a4c3d6c1837c2dc9a2b59"
EXPORTER_VERSION = "ai-actuary-workflow-export-v1"
DIGEST_ALGORITHM = "sha256-v1"

BUILDER_DECISION: dict[str, Any] = {
    "decision": "FALLBACK",
    "adk_version": ADK_VERSION,
    "controlled_surface": "project-cli",
    "native_builder_exposed": False,
    "evidence": (
        "ADK 2.7.1 /dev/apps/{app_name}/builder/save defaults tmp=false and writes app_root",
        "ADK 2.7.1 exposes cancel/read/import-compatible native Builder routes without a deny switch",
        "ADK 2.7.1 special __adk_agent_builder_assistant is loadable by AgentLoader",
        "ADK 2.7.1 Builder assistant write_files/delete_files can mutate Python and arbitrary project files",
    ),
}

EXECUTABLE_REFERENCE_FIELDS = (
    "agent_class",
    "sub_agents[].config_path",
    "sub_agents[].code",
    "before_agent_callbacks[].name",
    "after_agent_callbacks[].name",
    "model_code.name",
    "input_schema.name",
    "output_schema.name",
    "tools[].name",
    "tools[].args",
    "before_model_callbacks[].name",
    "after_model_callbacks[].name",
    "before_tool_callbacks[].name",
    "after_tool_callbacks[].name",
    "generate_content_config",
)

_AGENT_CLASSES = frozenset(
    {
        "LlmAgent",
        "LoopAgent",
        "ParallelAgent",
        "SequentialAgent",
    }
)
_COMMON_AGENT_CONFIG_KEYS = frozenset(
    {"agent_class", "name", "description", "sub_agents"}
)
_APPROVED_AGENT_CONFIG_KEYS = {
    "LlmAgent": _COMMON_AGENT_CONFIG_KEYS
    | frozenset({"model", "instruction", "tools"}),
    "LoopAgent": _COMMON_AGENT_CONFIG_KEYS | frozenset({"max_iterations"}),
    "ParallelAgent": _COMMON_AGENT_CONFIG_KEYS,
    "SequentialAgent": _COMMON_AGENT_CONFIG_KEYS,
}
_APP_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PORTABLE_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_APPROVED_PYTHON_FQNS = frozenset(
    {
        "reserving_workflow.adapters.adk.approved_tools.read_run_status",
    }
)
_APPROVED_TOOL_IDS = frozenset({"chainladder"})
_APPROVED_WORKFLOW_IDS = frozenset({"chainladder-basic", "chainladder-validated"})
_CALLBACK_FIELDS = (
    "before_agent_callbacks",
    "after_agent_callbacks",
    "before_model_callbacks",
    "after_model_callbacks",
    "before_tool_callbacks",
    "after_tool_callbacks",
)
_CODE_CONFIG_FIELDS = ("model_code", "input_schema", "output_schema")
_BLOCKED_KEYS = frozenset({"args"})
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_MAX_FILE_COUNT = 32
_MAX_FILE_BYTES = 256 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024
_MAX_PATH_LENGTH = 240
_MAX_DEPTH = 8
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PUBLISHED_REPO_PREFIX = PurePosixPath("src/reserving_workflow/developer_workflows")
_FaultHook = Callable[[str, Path], None]
_APPLICATION_LOCKS: dict[str, threading.RLock] = {}
_APPLICATION_LOCKS_GUARD = threading.Lock()
_APPLICATION_LOCK_DEPTHS = threading.local()
_EXPORT_REVOKE_LOCKS: dict[str, threading.RLock] = {}
_EXPORT_REVOKE_LOCKS_GUARD = threading.Lock()
_WINDOWS_DIRECTORY_API: tuple[Any, Any, Any, type[Any], type[Any]] | None = None
_WINDOWS_IDENTITY_API: tuple[Any, Any, type[Any], type[Any]] | None = None


class WorkflowLabError(RuntimeError):
    """Expected fail-closed Workflow Lab rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        completed_stages: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.stage = stage
        self.completed_stages = list(completed_stages)
        super().__init__(message)


@dataclass(frozen=True)
class ValidationReport:
    app_name: str
    completed_stages: tuple[str, ...]
    draft_digest: str
    schema_digest: str
    policy_digest: str
    adk_version: str
    python_fqns: tuple[str, ...]
    tool_ids: tuple[str, ...]
    workflow_ids: tuple[str, ...]
    contract_agent_class: str
    model_calls: int
    external_network_calls: int
    snapshot_root: str


@dataclass(frozen=True)
class _ReceiptObjectIdentity:
    device: int
    inode: int
    windows_file_id: bytes | None


@dataclass(frozen=True)
class _ExportCommitBinding:
    directory_chain: tuple[_ReceiptObjectIdentity, ...]
    manifest: _ReceiptObjectIdentity


@dataclass(frozen=True)
class ExportReceipt:
    export_id: str
    export_dir: Path
    bundle_digest: str
    candidate_digest: str
    patch_digest: str
    manifest: bytes
    commit_binding: _ExportCommitBinding
    _lease: _ExportProofLease | None = dataclass_field(
        default=None,
        repr=False,
        compare=False,
    )
    _revoke_callback: Callable[[ExportReceipt], None] | None = dataclass_field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def active(self) -> bool:
        return self._lease is not None and self._lease.active

    def finalize(self) -> None:
        if self._lease is None:
            raise WorkflowLabError(
                "export_receipt_detached",
                "A detached export receipt has no live proof lease to finalize.",
                stage="export",
            )
        self._lease.finalize(self.export_dir)

    def __enter__(self) -> Self:
        if not self.active:
            raise WorkflowLabError(
                "export_receipt_consumed",
                "The export receipt proof lease is no longer active.",
                stage="export",
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, traceback
        if exc is None:
            self.finalize()
            return False
        try:
            if self._revoke_callback is not None:
                self._revoke_callback(self)
            else:
                assert self._lease is not None
                self._lease.revoke(self.export_dir)
        except BaseException as cleanup:  # noqa: BLE001 - preserve caller interrupts
            group_type = (
                ExceptionGroup
                if isinstance(exc, Exception) and isinstance(cleanup, Exception)
                else BaseExceptionGroup
            )
            raise group_type(
                "Caller failure and export commit revocation both failed.",
                [exc, cleanup],
            ) from None
        return False

    def __del__(self) -> None:
        lease = getattr(self, "_lease", None)
        if lease is not None:
            try:
                lease.abandon(self.export_dir)
            except BaseException:  # noqa: BLE001, S110 - destructors cannot propagate
                pass


@dataclass(frozen=True)
class _TreeSnapshot:
    files: Mapping[str, bytes]
    digest: str
    pinned_descriptors: Mapping[str, int] | None = None


@dataclass(frozen=True)
class _PublishedTreeSnapshot:
    files: Mapping[str, bytes]
    retained_files: Mapping[str, bytes]
    digest: str
    guard: _PublishedStateGuard


@dataclass(frozen=True)
class _ValidatedDraft:
    snapshot: _TreeSnapshot
    parsed: Mapping[str, Any]
    policy: Mapping[str, Any]
    report: ValidationReport


@dataclass(frozen=True)
class _PathIdentity:
    device: int
    inode: int
    size: int
    mode: int
    links: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _PathIdentity:
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            size=int(value.st_size),
            mode=int(value.st_mode),
            links=int(value.st_nlink),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
        )


@dataclass(frozen=True)
class WorkflowLabPaths:
    mode: str
    state_root: Path
    draft_root: Path
    exports_root: Path
    published_root: Path | None
    repo_root: Path | None


class _PinnedOutputDirectory:
    def __init__(
        self,
        path: Path,
        *,
        descriptor: int | None = None,
        windows_handle: int | None = None,
        ancestor_descriptors: tuple[int, ...] = (),
        ancestor_windows_handles: tuple[int, ...] = (),
        expected_metadata: os.stat_result | None = None,
        fault_hook: _FaultHook | None = None,
    ) -> None:
        self.path = path
        self.descriptor = descriptor
        self.windows_handle = windows_handle
        self.ancestor_descriptors = list(ancestor_descriptors)
        self.ancestor_windows_handles = list(ancestor_windows_handles)
        self.fault_hook = fault_hook or (lambda event, target: None)
        metadata = os.lstat(path)
        _reject_link_or_reparse(metadata, path, "export")
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkflowLabError(
                "export_directory_invalid",
                f"Pinned output object is not a directory: {path}",
                stage="export",
            )
        self.identity = (int(metadata.st_dev), int(metadata.st_ino))
        if expected_metadata is not None and self.identity != (
            int(expected_metadata.st_dev),
            int(expected_metadata.st_ino),
        ):
            raise WorkflowLabError(
                "output_tree_changed",
                f"Output boundary changed while it was being pinned: {path}",
                stage="export",
            )
        self.windows_identity: tuple[int, int, bytes] | None = None
        if descriptor is not None:
            opened = os.fstat(descriptor)
            if (int(opened.st_dev), int(opened.st_ino)) != self.identity:
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Output boundary changed while it was being pinned: {path}",
                    stage="export",
                )
        if windows_handle is not None:
            self.windows_identity = _windows_directory_handle_identity(windows_handle)
            if self.windows_identity[:2] != self.identity:
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Windows output boundary changed while it was being pinned: {path}",
                    stage="export",
                )

    @classmethod
    def open_boundary(
        cls, path: Path, *, fault_hook: _FaultHook | None = None
    ) -> _PinnedOutputDirectory:
        path = Path(path).absolute()
        anchor = Path(path.anchor)
        try:
            parts = path.relative_to(anchor).parts
        except ValueError as exc:
            raise WorkflowLabError(
                "export_path_escape",
                "Server output boundary must be an absolute filesystem path.",
                stage="export",
            ) from exc
        if os.name == "nt":
            handles: list[int] = []
            current = anchor
            try:
                components = (
                    anchor,
                    *(
                        anchor.joinpath(*parts[:index])
                        for index in range(1, len(parts) + 1)
                    ),
                )
                metadata_by_path: dict[Path, os.stat_result] = {}
                for current in components:
                    if current != anchor:
                        try:
                            current.mkdir()
                        except FileExistsError:
                            pass
                    before = os.lstat(current)
                    _reject_link_or_reparse(before, current, "export")
                    if not stat.S_ISDIR(before.st_mode):
                        raise WorkflowLabError(
                            "export_directory_invalid",
                            f"Server output component is not a directory: {current}",
                            stage="export",
                        )
                    if fault_hook is not None:
                        fault_hook("before_output_directory_open", current)
                    handle = _open_windows_directory_handle(current)
                    handles.append(handle)
                    opened = _windows_directory_handle_identity(handle)
                    if opened[:2] != (int(before.st_dev), int(before.st_ino)):
                        raise WorkflowLabError(
                            "output_tree_changed",
                            f"Output directory changed before its handle was opened: {current}",
                            stage="export",
                        )
                    if fault_hook is not None:
                        fault_hook("after_output_directory_open", current)
                    after = os.lstat(current)
                    if (int(after.st_dev), int(after.st_ino)) != opened[
                        :2
                    ] or _windows_directory_handle_identity(handle) != opened:
                        raise WorkflowLabError(
                            "output_tree_changed",
                            f"Output directory changed while its handle was opened: {current}",
                            stage="export",
                        )
                    metadata_by_path[current] = after
                return cls(
                    path,
                    windows_handle=handles[-1],
                    ancestor_windows_handles=tuple(handles[:-1]),
                    expected_metadata=metadata_by_path[path],
                    fault_hook=fault_hook,
                )
            except Exception as exc:
                for handle in reversed(handles):
                    _close_windows_handle(handle)
                if isinstance(exc, WorkflowLabError):
                    raise
                raise WorkflowLabError(
                    "export_directory_invalid",
                    f"Unable to safely pin server output boundary: {current}",
                    stage="export",
                ) from exc
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        try:
            before = os.lstat(anchor)
            if fault_hook is not None:
                fault_hook("before_output_directory_open", anchor)
            descriptors.append(os.open(anchor, flags))
            if (os.fstat(descriptors[-1]).st_dev, os.fstat(descriptors[-1]).st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Output directory changed before its handle was opened: {anchor}",
                    stage="export",
                )
            if fault_hook is not None:
                fault_hook("after_output_directory_open", anchor)
            final_metadata = os.lstat(anchor)
            if (final_metadata.st_dev, final_metadata.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Output directory changed while its handle was opened: {anchor}",
                    stage="export",
                )
            for index, part in enumerate(parts, start=1):
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
                before = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
                current = anchor.joinpath(*parts[:index])
                if fault_hook is not None:
                    fault_hook("before_output_directory_open", current)
                child = os.open(part, flags, dir_fd=descriptors[-1])
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    os.close(child)
                    raise WorkflowLabError(
                        "output_tree_changed",
                        f"Output directory changed before its handle was opened: {current}",
                        stage="export",
                    )
                descriptors.append(child)
                if fault_hook is not None:
                    fault_hook("after_output_directory_open", current)
                after = os.stat(part, dir_fd=descriptors[-2], follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                    raise WorkflowLabError(
                        "output_tree_changed",
                        f"Output directory changed while its handle was opened: {current}",
                        stage="export",
                    )
                final_metadata = after
            return cls(
                path,
                descriptor=descriptors[-1],
                ancestor_descriptors=tuple(descriptors[:-1]),
                expected_metadata=final_metadata,
                fault_hook=fault_hook,
            )
        except Exception as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(exc, WorkflowLabError):
                raise
            raise WorkflowLabError(
                "export_directory_invalid",
                f"Unable to safely pin server output boundary: {path}",
                stage="export",
            ) from exc

    def create_child(
        self, name: str, *, exist_ok: bool = False
    ) -> _PinnedOutputDirectory:
        if not _PORTABLE_BASENAME.fullmatch(name) or name in {".", ".."}:
            raise WorkflowLabError(
                "export_path_escape",
                f"Unsafe server output directory name: {name!r}",
                stage="export",
            )
        path = self.path / name
        if os.name == "nt":
            try:
                path.mkdir()
            except FileExistsError:
                if not exist_ok:
                    raise WorkflowLabError(
                        "export_exists",
                        "The server-generated output directory already exists.",
                        stage="export",
                    ) from None
            before = os.lstat(path)
            self.fault_hook("before_output_directory_open", path)
            handle = _open_windows_directory_handle(path)
            opened = _windows_directory_handle_identity(handle)
            if opened[:2] != (int(before.st_dev), int(before.st_ino)):
                _close_windows_handle(handle)
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Output directory changed before its handle was opened: {path}",
                    stage="export",
                )
            self.fault_hook("after_output_directory_open", path)
            after = os.lstat(path)
            if (int(after.st_dev), int(after.st_ino)) != opened[
                :2
            ] or _windows_directory_handle_identity(handle) != opened:
                _close_windows_handle(handle)
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Output directory changed while its handle was opened: {path}",
                    stage="export",
                )
            try:
                return type(self)(
                    path,
                    windows_handle=handle,
                    expected_metadata=after,
                    fault_hook=self.fault_hook,
                )
            except Exception:
                _close_windows_handle(handle)
                raise
        assert self.descriptor is not None
        try:
            os.mkdir(name, 0o700, dir_fd=self.descriptor)
        except FileExistsError:
            if not exist_ok:
                raise WorkflowLabError(
                    "export_exists",
                    "The server-generated output directory already exists.",
                    stage="export",
                ) from None
        before = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        self.fault_hook("before_output_directory_open", path)
        try:
            descriptor = os.open(name, flags, dir_fd=self.descriptor)
        except OSError as exc:
            raise WorkflowLabError(
                "export_directory_invalid",
                f"Unable to pin server output directory: {name}",
                stage="export",
            ) from exc
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(descriptor)
            raise WorkflowLabError(
                "output_tree_changed",
                f"Output directory changed before its handle was opened: {path}",
                stage="export",
            )
        self.fault_hook("after_output_directory_open", path)
        after = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(descriptor)
            raise WorkflowLabError(
                "output_tree_changed",
                f"Output directory changed while its handle was opened: {path}",
                stage="export",
            )
        try:
            return type(self)(
                path,
                descriptor=descriptor,
                expected_metadata=after,
                fault_hook=self.fault_hook,
            )
        except Exception:
            os.close(descriptor)
            raise

    def write_file(self, name: str, content: bytes, *, stage: str) -> _PinnedOutputFile:
        return _write_exclusive_guarded(self, name, content, stage=stage)

    def verify(self, *, check_ads: bool = True) -> None:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError as exc:
            raise WorkflowLabError(
                "output_tree_changed",
                f"Pinned output directory disappeared: {self.path.name}",
                stage="export",
            ) from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE_ATTRIBUTE:
            raise WorkflowLabError(
                "output_tree_changed",
                f"Pinned output directory became a link/reparse point: {self.path.name}",
                stage="export",
            )
        if (int(metadata.st_dev), int(metadata.st_ino)) != self.identity:
            raise WorkflowLabError(
                "output_tree_changed",
                f"Pinned output directory was replaced: {self.path.name}",
                stage="export",
            )
        if self.descriptor is not None:
            opened = os.fstat(self.descriptor)
            if (int(opened.st_dev), int(opened.st_ino)) != self.identity:
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Pinned output handle identity changed: {self.path.name}",
                    stage="export",
                )
        if (
            self.windows_handle is not None
            and _windows_directory_handle_identity(self.windows_handle)
            != self.windows_identity
        ):
            raise WorkflowLabError(
                "output_tree_changed",
                f"Pinned Windows output handle identity changed: {self.path.name}",
                stage="export",
            )
        if check_ads and _has_windows_ads(self.path):
            raise WorkflowLabError(
                "output_ads_forbidden",
                f"Alternate data streams are forbidden on output directories: {self.path.name}",
                stage="export",
            )

    def make_read_only(self) -> None:
        if self.descriptor is not None:
            os.fchmod(
                self.descriptor,
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH,
            )
        else:
            self.path.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        for descriptor in reversed(self.ancestor_descriptors):
            os.close(descriptor)
        self.ancestor_descriptors.clear()
        if self.windows_handle is not None:
            _close_windows_handle(self.windows_handle)
            self.windows_handle = None
        for handle in reversed(self.ancestor_windows_handles):
            _close_windows_handle(handle)
        self.ancestor_windows_handles.clear()


class _PinnedOutputFile:
    def __init__(
        self,
        parent: _PinnedOutputDirectory,
        name: str,
        descriptor: int,
        identity: tuple[int, int],
        content: bytes,
    ) -> None:
        self.parent = parent
        self.name = name
        self.path = parent.path / name
        self.descriptor = descriptor
        self.identity = identity
        self.expected_size = len(content)
        self.expected_digest = hashlib.sha256(content).digest()
        self.windows_identity: tuple[int, int, bytes] | None = None
        if os.name == "nt":
            import msvcrt

            self.windows_identity = _windows_object_handle_identity(
                msvcrt.get_osfhandle(descriptor)
            )

    def verify(self, *, check_content: bool = True, check_ads: bool = True) -> None:
        try:
            before = os.lstat(self.path)
        except OSError as exc:
            raise WorkflowLabError(
                "output_tree_changed",
                f"Committed output object is unavailable: {self.name}",
                stage="export",
            ) from exc
        _reject_link_or_reparse(before, self.path, "export")
        opened_before = os.fstat(self.descriptor)
        actual = (int(before.st_dev), int(before.st_ino))
        handle = (int(opened_before.st_dev), int(opened_before.st_ino))
        if (
            actual != self.identity
            or handle != self.identity
            or before.st_nlink != 1
            or opened_before.st_nlink != 1
        ):
            raise WorkflowLabError(
                "output_tree_changed",
                f"Committed output object changed: {self.name}",
                stage="export",
            )
        if os.name == "nt":
            import msvcrt

            if (
                _windows_object_handle_identity(msvcrt.get_osfhandle(self.descriptor))
                != self.windows_identity
            ):
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Committed Windows output FileId changed: {self.name}",
                    stage="export",
                )
        if check_ads and _has_windows_ads(self.path):
            raise WorkflowLabError(
                "output_ads_forbidden",
                f"Alternate data streams are forbidden on output files: {self.name}",
                stage="export",
            )
        if not check_content:
            return
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(self.descriptor, 65536)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        try:
            after = os.lstat(self.path)
        except OSError as exc:
            raise WorkflowLabError(
                "output_tree_changed",
                f"Committed output object is unavailable: {self.name}",
                stage="export",
            ) from exc
        _reject_link_or_reparse(after, self.path, "export")
        opened_after = os.fstat(self.descriptor)
        if (
            (int(after.st_dev), int(after.st_ino)) != self.identity
            or (int(opened_after.st_dev), int(opened_after.st_ino)) != self.identity
            or after.st_nlink != 1
            or opened_after.st_nlink != 1
            or size != self.expected_size
            or digest.digest() != self.expected_digest
        ):
            raise WorkflowLabError(
                "output_tree_changed",
                f"Committed output bytes changed: {self.name}",
                stage="export",
            )

    def make_read_only(self) -> None:
        if os.name == "nt":
            self.path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        else:
            os.fchmod(
                self.descriptor,
                stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH,
            )

    def discard(self) -> None:
        # Cleanup is identity-bound, not content-bound: a changed marker must still
        # be removable through its original descriptor without touching a replacement.
        self.verify(check_content=False, check_ads=False)
        self.parent.verify(check_ads=False)
        if os.name == "nt":
            _mark_windows_file_delete(self.descriptor)
            self.close()
            return
        assert self.parent.descriptor is not None
        os.fchmod(self.parent.descriptor, stat.S_IRWXU)
        current = os.stat(
            self.name,
            dir_fd=self.parent.descriptor,
            follow_symlinks=False,
        )
        if (int(current.st_dev), int(current.st_ino)) != self.identity:
            raise WorkflowLabError(
                "output_tree_changed",
                f"Refusing to remove a replaced output object: {self.name}",
                stage="export",
            )
        try:
            os.unlink(self.name, dir_fd=self.parent.descriptor)
            os.fsync(self.parent.descriptor)
        finally:
            os.fchmod(
                self.parent.descriptor,
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH,
            )

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


class _ExportProofLease:
    def __init__(
        self,
        guards: list[_PinnedOutputDirectory],
        files: list[_PinnedOutputFile],
        expected_topology: Mapping[str, str],
    ) -> None:
        self.guards = guards
        self.files = files
        self.expected_topology = dict(expected_topology)
        self.export = guards[2]
        self.manifest = next(
            output for output in files if output.name == "manifest.json"
        )
        self._lock = threading.RLock()
        self._state = "active"
        self._shadow_descriptors: list[int] = []
        self._shadow_windows_handles: list[int] = []
        try:
            self._shadow_descriptors.extend(
                os.dup(output.descriptor) for output in files
            )
            for guard in guards:
                self._shadow_descriptors.extend(
                    os.dup(descriptor) for descriptor in guard.ancestor_descriptors
                )
                if guard.descriptor is not None:
                    self._shadow_descriptors.append(os.dup(guard.descriptor))
                self._shadow_windows_handles.extend(
                    _duplicate_windows_handle(handle)
                    for handle in guard.ancestor_windows_handles
                )
                if guard.windows_handle is not None:
                    self._shadow_windows_handles.append(
                        _duplicate_windows_handle(guard.windows_handle)
                    )
        except BaseException:
            self._close_shadows()
            raise

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state == "active"

    def finalize(self, export_dir: Path) -> None:
        with _export_revoke_lock(export_dir), self._lock:
            if self._state in {"finalized", "revoked"}:
                return
            if self._state != "active":
                raise WorkflowLabError(
                    "export_receipt_consumed",
                    "The export proof lease cannot be finalized after a failed consume.",
                    stage="export",
                )
            try:
                self._verify()
            except BaseException as primary:
                try:
                    self._revoke_active()
                except BaseException as cleanup:  # noqa: BLE001 - preserve interrupts
                    group_type = (
                        ExceptionGroup
                        if isinstance(primary, Exception)
                        and isinstance(cleanup, Exception)
                        else BaseExceptionGroup
                    )
                    raise group_type(
                        "Final export proof and commit revocation both failed.",
                        [primary, cleanup],
                    ) from None
                raise
            try:
                self._close()
            except BaseException:
                self._state = "failed"
                raise
            self._state = "finalized"

    def revoke(self, export_dir: Path) -> bool:
        with _export_revoke_lock(export_dir), self._lock:
            if self._state == "revoked":
                return True
            if self._state == "finalized":
                return False
            if self._state != "active":
                return False
            self._revoke_active()
            return True

    def abandon(self, export_dir: Path) -> None:
        if not self.active:
            return
        try:
            self.revoke(export_dir)
        except BaseException:  # noqa: BLE001 - destructor cleanup cannot propagate
            # Destructors cannot safely propagate. The OS still releases every
            # retained descriptor below, allowing a later serialized recovery.
            with self._lock:
                self._state = "failed"
                self._close()

    def _verify(self) -> None:
        _verify_terminal_export(
            self.export,
            self.guards[2:],
            self.files,
            self.expected_topology,
        )

    def _revoke_active(self) -> None:
        try:
            self.manifest.discard()
        except BaseException:
            self._state = "failed"
            self._close()
            raise
        self._state = "revoked"
        self._close()

    def _close(self) -> None:
        first_error: BaseException | None = None
        for output in reversed(self.files):
            try:
                output.close()
            except BaseException as exc:  # noqa: BLE001 - close every proof handle
                first_error = first_error or exc
        for guard in reversed(self.guards):
            try:
                guard.close()
            except BaseException as exc:  # noqa: BLE001 - close every proof handle
                first_error = first_error or exc
        try:
            self._close_shadows()
        except BaseException as exc:  # noqa: BLE001 - close every proof handle
            first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _close_shadows(self) -> None:
        for descriptor in reversed(self._shadow_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._shadow_descriptors.clear()
        for handle in reversed(self._shadow_windows_handles):
            try:
                _close_windows_handle(handle)
            except OSError:
                pass
        self._shadow_windows_handles.clear()


@dataclass
class _PinnedReadDirectory:
    path: Path
    identity: _PathIdentity
    descriptor: int | None = None
    windows_handle: int | None = None
    windows_identity: tuple[int, int, bytes] | None = None

    def verify(self, *, stage: str) -> None:
        try:
            path_identity = _PathIdentity.from_stat(os.lstat(self.path))
        except FileNotFoundError as exc:
            raise WorkflowLabError(
                "tree_changed",
                f"Pinned published directory disappeared: {self.path}",
                stage=stage,
            ) from exc
        if path_identity != self.identity:
            raise WorkflowLabError(
                "tree_changed",
                f"Pinned published directory changed: {self.path}",
                stage=stage,
            )
        if (
            self.descriptor is not None
            and _PathIdentity.from_stat(os.fstat(self.descriptor)) != self.identity
        ):
            raise WorkflowLabError(
                "tree_changed",
                f"Pinned published directory handle changed: {self.path}",
                stage=stage,
            )
        if (
            self.windows_handle is not None
            and _windows_directory_handle_identity(self.windows_handle)
            != self.windows_identity
        ):
            raise WorkflowLabError(
                "tree_changed",
                f"Pinned Windows published directory changed: {self.path}",
                stage=stage,
            )

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        if self.windows_handle is not None:
            _close_windows_handle(self.windows_handle)
            self.windows_handle = None


class _PublishedStateGuard:
    def __init__(self, root: Path, *, stage: str, missing: bool) -> None:
        self.root = root.absolute()
        self.stage = stage
        self.missing = missing
        self.directories: list[_PinnedReadDirectory] = []
        self.file_descriptors: dict[str, int] = {}
        self.file_identities: dict[str, _PathIdentity] = {}
        self.expected_files: dict[str, bytes] = {}
        self.expected_retained: dict[str, bytes] = {}
        self.expected_directories: dict[str, _PathIdentity] = {}

    def verify(self) -> None:
        for directory in self.directories:
            directory.verify(stage=self.stage)
        if self.missing:
            try:
                os.lstat(self.root)
            except FileNotFoundError:
                return
            raise WorkflowLabError(
                "tree_changed",
                "Published workflow root appeared before export commit.",
                stage=self.stage,
            )

        entries, bookkeeping, directories = _scan_published_entries(
            self.root, stage=self.stage
        )
        current_paths = {**entries, **bookkeeping}
        expected_paths = {**self.expected_files, **self.expected_retained}
        if (
            set(current_paths) != set(expected_paths)
            or directories != self.expected_directories
        ):
            raise WorkflowLabError(
                "tree_changed",
                "Published workflow topology changed before export commit.",
                stage=self.stage,
            )
        for relative, expected_content in expected_paths.items():
            descriptor = self.file_descriptors[relative]
            before = _PathIdentity.from_stat(os.fstat(descriptor))
            content = _read_stable_descriptor(
                descriptor, stage=self.stage, label=relative
            )
            after = _PathIdentity.from_stat(os.fstat(descriptor))
            try:
                path_identity = _PathIdentity.from_stat(
                    os.lstat(self.root.joinpath(*PurePosixPath(relative).parts))
                )
            except FileNotFoundError as exc:
                raise WorkflowLabError(
                    "tree_changed",
                    f"Published workflow file disappeared: {relative}",
                    stage=self.stage,
                ) from exc
            if (
                before != self.file_identities[relative]
                or after != self.file_identities[relative]
                or path_identity != self.file_identities[relative]
                or content != expected_content
            ):
                raise WorkflowLabError(
                    "tree_changed",
                    f"Published workflow file changed before commit: {relative}",
                    stage=self.stage,
                )
        for directory in self.directories:
            directory.verify(stage=self.stage)

    def close(self) -> None:
        for descriptor in self.file_descriptors.values():
            os.close(descriptor)
        self.file_descriptors.clear()
        for directory in reversed(self.directories):
            directory.close()
        self.directories.clear()


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueSafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise WorkflowLabError(
                "yaml_invalid_key",
                "YAML mapping keys must be scalar and hashable.",
                stage="safe_yaml",
            ) from exc
        if duplicate:
            raise WorkflowLabError(
                "yaml_duplicate_key",
                f"Duplicate YAML key is forbidden: {key!r}",
                stage="safe_yaml",
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_frozen_agent_config_schema() -> tuple[bytes, dict[str, Any]]:
    """Load the byte-for-byte frozen ADK 2.7.1 AgentConfig schema."""

    schema_path = resources.files("reserving_workflow.adapters.adk").joinpath(
        "data", "AgentConfig-2.7.1.json"
    )
    schema_bytes = schema_path.read_bytes()
    digest = hashlib.sha256(schema_bytes).hexdigest()
    if digest != SCHEMA_SHA256:
        raise WorkflowLabError(
            "schema_digest_mismatch",
            "The packaged ADK AgentConfig schema does not match the frozen 2.7.1 digest.",
            stage="adk_schema",
        )
    schema = json.loads(schema_bytes)
    refs = _collect_refs(schema)
    if any(not ref.startswith("#/") for ref in refs):
        raise WorkflowLabError(
            "schema_network_ref",
            "The frozen schema contains a non-local $ref.",
            stage="adk_schema",
        )
    return schema_bytes, schema


class WorkflowLab:
    """Validate and export only isolated declarative Workflow Lab drafts."""

    def __init__(self, paths: WorkflowLabPaths) -> None:
        self.paths = paths
        self._fault_hook: _FaultHook = lambda event, path: None

    @classmethod
    def for_source_checkout(cls, repo_root: Path) -> WorkflowLab:
        root = Path(repo_root).absolute()
        if not (root / ".git").exists():
            raise WorkflowLabError(
                "source_checkout_required",
                "Git export requires an explicit source checkout root.",
                stage="preflight",
            )
        state = root / "tmp"
        return cls(
            WorkflowLabPaths(
                mode="source-checkout",
                state_root=state,
                draft_root=state / "adk-workflow-drafts",
                exports_root=state / "adk-workflow-exports",
                published_root=root
                / "src"
                / "reserving_workflow"
                / "developer_workflows",
                repo_root=root,
            )
        )

    @classmethod
    def for_installed_runtime(cls, state_root: Path) -> WorkflowLab:
        state = Path(state_root).absolute()
        return cls(
            WorkflowLabPaths(
                mode="installed",
                state_root=state,
                draft_root=state / "adk-workflow-drafts",
                exports_root=state / "adk-workflow-exports",
                published_root=None,
                repo_root=None,
            )
        )

    def draft_path(self, app_name: str) -> Path:
        self._validate_app_name(app_name)
        return self.paths.draft_root / app_name

    @contextmanager
    def draft_write_session(self, app_name: str) -> Iterator[Path]:
        """Serialize a trusted project gateway write with validation/export."""

        draft = self.draft_path(app_name)
        with _application_lock(draft):
            yield draft

    def validate(self, app_name: str) -> ValidationReport:
        with _application_lock(self.draft_path(app_name)):
            return self._validate(app_name).report

    def _validate(self, app_name: str) -> _ValidatedDraft:
        self._validate_app_name(app_name)
        completed: list[str] = []
        snapshot: _TreeSnapshot | None = None
        try:
            snapshot = self._snapshot_declarative_tree(
                self.draft_path(app_name),
                stage="preflight",
            )
            completed.append("preflight")

            parsed = {
                name: _safe_yaml_load(content, name=name)
                for name, content in snapshot.files.items()
            }
            self._validate_yaml_shapes(parsed)
            completed.append("safe_yaml")

            python_fqns = self._validate_code_references(parsed)
            completed.append("code_references")

            schema_bytes, schema = load_frozen_agent_config_schema()
            self._validate_adk_schema(parsed, schema)
            completed.append("adk_schema")

            policy = parsed["workflow_policy.yaml"]
            tool_ids, workflow_ids, policy_fqns = self._validate_project_policy(
                parsed,
                policy,
                python_fqns,
            )
            completed.append("project_policy")

            probe, snapshot_root = self._run_isolated_contract(snapshot)
            completed.append("isolated_contract")
            _verify_pinned_snapshot(
                snapshot,
                self.draft_path(app_name),
                stage="preflight",
            )
        except WorkflowLabError as exc:
            if not exc.completed_stages:
                exc.completed_stages = list(completed)
            raise
        except Exception as exc:
            stage = _next_stage(completed)
            raise WorkflowLabError(
                "validation_failed",
                f"Workflow Lab validation failed during {stage}: {exc}",
                stage=stage,
                completed_stages=completed,
            ) from exc
        finally:
            if snapshot is not None:
                _close_pinned_snapshot(snapshot)

        canonical = _canonicalize_draft(parsed)
        draft_digest = _tree_digest(canonical)
        policy_digest = hashlib.sha256(canonical["workflow_policy.yaml"]).hexdigest()
        report = ValidationReport(
            app_name=app_name,
            completed_stages=tuple(completed),
            draft_digest=draft_digest,
            schema_digest=hashlib.sha256(schema_bytes).hexdigest(),
            policy_digest=policy_digest,
            adk_version=ADK_VERSION,
            python_fqns=tuple(sorted(policy_fqns)),
            tool_ids=tuple(sorted(tool_ids)),
            workflow_ids=tuple(sorted(workflow_ids)),
            contract_agent_class=str(probe["agent_class"]),
            model_calls=int(probe["model_calls"]),
            external_network_calls=int(probe["external_network_calls"]),
            snapshot_root=str(snapshot_root),
        )
        return _ValidatedDraft(snapshot, parsed, policy, report)

    def export(self, app_name: str) -> ExportReceipt:
        if self.paths.mode != "source-checkout" or self.paths.published_root is None:
            raise WorkflowLabError(
                "source_checkout_required",
                "Git diff export is unavailable in installed-wheel runtime mode.",
                stage="export",
            )
        with _application_lock(self.draft_path(app_name)):
            return self._export_locked(app_name)

    def _export_locked(self, app_name: str) -> ExportReceipt:
        validated = self._validate(app_name)
        candidate = _canonicalize_draft(validated.parsed)
        published_path = self.paths.published_root / app_name
        published_guard: _PublishedStateGuard | None = None
        try:
            try:
                published_metadata = os.lstat(published_path)
            except FileNotFoundError:
                self._fault_hook("after_published_final_missing", published_path)
                published_guard = _PublishedStateGuard(
                    published_path, stage="export", missing=True
                )
                published_guard.directories.extend(
                    _pin_read_directory_chain(
                        published_path.parent,
                        self.paths.repo_root,
                        stage="export",
                    )
                )
                published_guard.verify()
                published_files: dict[str, bytes] = {}
                retained_files: dict[str, bytes] = {}
                published_digest = _published_tree_digest({}, {})
            else:
                published_identity = _PathIdentity.from_stat(published_metadata)
                self._fault_hook("after_published_final_lstat", published_path)
                published = self._snapshot_published_tree(
                    published_path,
                    stage="export",
                    boundary=self.paths.repo_root,
                    expected_root=published_identity,
                )
                published_guard = published.guard
                published_files = dict(published.files)
                retained_files = dict(published.retained_files)
                published_digest = published.digest

            patch = _build_patch(app_name, published_files, candidate)
            patch_digest = hashlib.sha256(patch).hexdigest()
            candidate_digest = _tree_digest(candidate)
            manifest_obj = {
                "format_version": "ai-actuary.workflow-export.v1",
                "digest_algorithm": DIGEST_ALGORITHM,
                "exporter_version": EXPORTER_VERSION,
                "adk_version": ADK_VERSION,
                "draft_digest": validated.report.draft_digest,
                "published_tree_digest": published_digest,
                "schema_digest": validated.report.schema_digest,
                "policy_digest": validated.report.policy_digest,
                "candidate_digest": candidate_digest,
                "patch_digest": patch_digest,
                "input_key": _digest_json(
                    {
                        "draft": validated.report.draft_digest,
                        "published": published_digest,
                        "schema": validated.report.schema_digest,
                        "policy": validated.report.policy_digest,
                        "adk": ADK_VERSION,
                        "exporter": EXPORTER_VERSION,
                    }
                ),
                "files": [
                    {
                        "path": name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                    for name, content in sorted(candidate.items())
                ],
                "published_retained_files": [
                    {
                        "path": name,
                        "type": "canonical-inert-python-stub",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                    for name, content in sorted(retained_files.items())
                ],
            }
            manifest = _canonical_json_bytes(manifest_obj)
            bundle_digest = _tree_digest(
                {
                    **{f"candidate/{name}": value for name, value in candidate.items()},
                    "candidate.patch": patch,
                    "manifest.json": manifest,
                }
            )
            export_id = secrets.token_hex(16)
            export_dir = self.paths.exports_root / export_id
            assert published_guard is not None
            published_guard.verify()
            commit_binding, lease = self._write_export_bundle(
                export_dir,
                candidate,
                patch,
                manifest,
                published_guard=published_guard,
            )
            return ExportReceipt(
                export_id=export_id,
                export_dir=export_dir,
                bundle_digest=bundle_digest,
                candidate_digest=candidate_digest,
                patch_digest=patch_digest,
                manifest=manifest,
                commit_binding=commit_binding,
                _lease=lease,
                _revoke_callback=lambda receipt: self.revoke_export_commit(receipt),
            )
        finally:
            if published_guard is not None:
                published_guard.close()

    def revoke_export_commit(self, receipt: ExportReceipt) -> None:
        """Remove only a receipt-bound manifest after a failed outer integrity proof."""

        if (
            self.paths.mode != "source-checkout"
            or not re.fullmatch(r"[0-9a-f]{32}", receipt.export_id)
            or receipt.export_dir.absolute()
            != (self.paths.exports_root / receipt.export_id).absolute()
        ):
            raise WorkflowLabError(
                "export_receipt_invalid",
                "Refusing to revoke an export that is not bound to this Workflow Lab.",
                stage="integrity",
            )
        if (
            receipt._lease is not None
            and receipt._lease.export.path.absolute() == receipt.export_dir.absolute()
            and receipt._lease.revoke(receipt.export_dir)
        ):
            return
        with _export_revoke_lock(receipt.export_dir):
            pins: list[_PinnedReadDirectory] = []
            descriptor = -1
            try:
                try:
                    pins = _pin_read_directory_chain(
                        receipt.export_dir,
                        Path(receipt.export_dir.anchor),
                        stage="integrity",
                    )
                except (OSError, WorkflowLabError) as exc:
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound export directory no longer exists or changed identity.",
                        stage="integrity",
                    ) from exc
                actual_chain = tuple(
                    _receipt_identity_from_read_pin(pin) for pin in pins
                )
                if actual_chain != receipt.commit_binding.directory_chain:
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound export directory was replaced.",
                        stage="integrity",
                    )
                manifest_path = receipt.export_dir / "manifest.json"
                try:
                    if os.name == "nt":
                        path_metadata = os.lstat(manifest_path)
                    else:
                        parent_descriptor = pins[-1].descriptor
                        assert parent_descriptor is not None
                        path_metadata = os.stat(
                            "manifest.json",
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                except FileNotFoundError:
                    return
                _reject_link_or_reparse(path_metadata, manifest_path, "integrity")
                if (
                    not stat.S_ISREG(path_metadata.st_mode)
                    or path_metadata.st_nlink != 1
                ):
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound manifest is no longer a single regular file.",
                        stage="integrity",
                    )
                try:
                    if os.name == "nt":
                        descriptor = _open_windows_existing_descriptor(manifest_path)
                    else:
                        assert pins[-1].descriptor is not None
                        descriptor = os.open(
                            "manifest.json",
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=pins[-1].descriptor,
                        )
                except (OSError, WorkflowLabError) as exc:
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound manifest changed while it was being opened.",
                        stage="integrity",
                    ) from exc
                if (
                    _receipt_identity_from_descriptor(descriptor)
                    != receipt.commit_binding.manifest
                ):
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound manifest was replaced.",
                        stage="integrity",
                    )
                content = _read_stable_descriptor(
                    descriptor,
                    stage="integrity",
                    label="manifest.json",
                )
                if content != receipt.manifest:
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound manifest bytes changed.",
                        stage="integrity",
                    )
                for pin in pins:
                    pin.verify(stage="integrity")
                if (
                    _receipt_identity_from_descriptor(descriptor)
                    != receipt.commit_binding.manifest
                ):
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound manifest changed before revocation.",
                        stage="integrity",
                    )
                current = (
                    os.lstat(manifest_path)
                    if os.name == "nt"
                    else os.stat(
                        "manifest.json",
                        dir_fd=pins[-1].descriptor,
                        follow_symlinks=False,
                    )
                )
                opened = os.fstat(descriptor)
                if (
                    (int(current.st_dev), int(current.st_ino))
                    != (int(opened.st_dev), int(opened.st_ino))
                    or current.st_nlink != 1
                    or opened.st_nlink != 1
                ):
                    raise WorkflowLabError(
                        "export_receipt_changed",
                        "The receipt-bound manifest pathname changed before revocation.",
                        stage="integrity",
                    )
                if os.name == "nt":
                    _mark_windows_file_delete(descriptor)
                    os.close(descriptor)
                    descriptor = -1
                else:
                    parent_descriptor = pins[-1].descriptor
                    assert parent_descriptor is not None
                    os.fchmod(parent_descriptor, stat.S_IRWXU)
                    try:
                        os.unlink("manifest.json", dir_fd=parent_descriptor)
                        os.fsync(parent_descriptor)
                    finally:
                        os.fchmod(
                            parent_descriptor,
                            stat.S_IRUSR
                            | stat.S_IXUSR
                            | stat.S_IRGRP
                            | stat.S_IXGRP
                            | stat.S_IROTH
                            | stat.S_IXOTH,
                        )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                for pin in reversed(pins):
                    pin.close()

    def materialize_published_workflows(self) -> Path:
        source = resources.files("reserving_workflow.developer_workflows")
        files: dict[str, bytes] = {}
        _collect_resource_files(source, "", files)
        digest = _tree_digest(files)
        destination = (
            self.paths.state_root
            / "published-workflows"
            / f"{ADK_VERSION}-{digest[:16]}"
        )
        guards: list[_PinnedOutputDirectory] = []
        outputs: list[_PinnedOutputFile] = []
        destination_created = False
        try:
            state = _PinnedOutputDirectory.open_boundary(
                self.paths.state_root, fault_hook=self._fault_hook
            )
            guards.append(state)
            published = state.create_child("published-workflows", exist_ok=True)
            guards.append(published)
            try:
                os.lstat(destination)
            except FileNotFoundError:
                materialized = published.create_child(destination.name)
                destination_created = True
            else:
                materialized = published.create_child(destination.name, exist_ok=True)
                guards.append(materialized)
                if _tree_digest(_read_plain_tree(destination)) != digest:
                    raise WorkflowLabError(
                        "materialized_digest_mismatch",
                        "Existing published workflow materialization has changed.",
                        stage="materialize",
                    )
                _validate_materialized_read_only(destination)
                return destination
            guards.append(materialized)
            directory_guards: dict[str, _PinnedOutputDirectory] = {"": materialized}
            self._fault_hook("before_materialize_write", materialized.path)
            for relative, content in sorted(files.items()):
                pure = PurePosixPath(relative)
                parent = materialized
                key_parts: list[str] = []
                for part in pure.parts[:-1]:
                    key_parts.append(part)
                    key = "/".join(key_parts)
                    if key not in directory_guards:
                        directory_guards[key] = parent.create_child(part)
                        guards.append(directory_guards[key])
                    parent = directory_guards[key]
                outputs.append(
                    parent.write_file(pure.name, content, stage="materialize")
                )
            for output in outputs:
                output.verify()
                output.make_read_only()
            for guard in reversed(guards[2:]):
                guard.make_read_only()
                guard.verify()
            if os.name == "nt":
                _harden_windows_materialized_tree(destination)
            _validate_materialized_read_only(destination)
        except Exception:
            for output in reversed(outputs):
                output.close()
            if (
                destination_created
                and os.name != "nt"
                and len(guards) >= 3
                and guards[1].descriptor is not None
                and guards[2].descriptor is not None
            ):
                try:
                    _remove_posix_directory_contents(guards[2].descriptor)
                    os.rmdir(destination.name, dir_fd=guards[1].descriptor)
                    destination_created = False
                except OSError:
                    pass
            for guard in reversed(guards[2:]):
                guard.close()
            if destination_created:
                _remove_owned_tree(
                    destination, self.paths.state_root / "published-workflows"
                )
            raise
        finally:
            for output in reversed(outputs):
                output.close()
            for guard in reversed(guards):
                guard.close()
        return destination

    def _snapshot_declarative_tree(
        self,
        root: Path,
        *,
        stage: str,
        require_policy: bool = True,
        boundary: Path | None = None,
    ) -> _TreeSnapshot:
        if not root.exists():
            raise WorkflowLabError(
                "draft_not_found",
                f"Declarative workflow directory does not exist: {root}",
                stage=stage,
            )
        directory_identities = _validate_directory_chain(
            root,
            boundary or self.paths.state_root,
            stage,
        )
        directory_entries: dict[str, _PathIdentity] = {}
        entries = _scan_entries(
            root,
            stage=stage,
            directory_identities=directory_entries,
        )
        if len(entries) > _MAX_FILE_COUNT:
            raise WorkflowLabError(
                "file_count_exceeded",
                f"Draft contains more than {_MAX_FILE_COUNT} files.",
                stage=stage,
            )
        _validate_relative_names(entries, require_policy=require_policy, stage=stage)
        files: dict[str, bytes] = {}
        total = 0
        identities: dict[str, _PathIdentity] = {}
        for relative, path in entries.items():
            content, identity = _read_pinned_file(
                path,
                stage=stage,
                fault_hook=self._fault_hook,
            )
            total += len(content)
            if total > _MAX_TOTAL_BYTES:
                raise WorkflowLabError(
                    "total_bytes_exceeded",
                    f"Draft exceeds {_MAX_TOTAL_BYTES} total bytes.",
                    stage=stage,
                )
            files[relative] = content
            identities[relative] = identity
        final_directory_entries: dict[str, _PathIdentity] = {}
        final_entries = _scan_entries(
            root,
            stage=stage,
            directory_identities=final_directory_entries,
        )
        if set(final_entries) != set(entries):
            raise WorkflowLabError(
                "tree_changed",
                "Declarative workflow tree changed during validation.",
                stage=stage,
            )
        for relative, path in final_entries.items():
            final = _PathIdentity.from_stat(os.lstat(path))
            if final != identities[relative]:
                raise WorkflowLabError(
                    "entry_replaced",
                    f"Input changed while it was being validated: {relative}",
                    stage=stage,
                )
        if final_directory_entries != directory_entries:
            raise WorkflowLabError(
                "tree_changed",
                "A declarative workflow directory changed during validation.",
                stage=stage,
            )
        if (
            _validate_directory_chain(
                root,
                boundary or self.paths.state_root,
                stage,
            )
            != directory_identities
        ):
            raise WorkflowLabError(
                "tree_changed",
                "A declarative workflow ancestor changed during validation.",
                stage=stage,
            )
        pinned: dict[str, int] = {}
        try:
            for relative, path in final_entries.items():
                descriptor = _open_pinned_input(path)
                pinned[relative] = descriptor
                opened = _PathIdentity.from_stat(os.fstat(descriptor))
                if opened != identities[relative]:
                    raise WorkflowLabError(
                        "entry_changed",
                        f"Input changed before its stable snapshot was pinned: {relative}",
                        stage=stage,
                    )
                if (
                    _read_stable_descriptor(
                        descriptor,
                        stage=stage,
                        label=relative,
                    )
                    != files[relative]
                ):
                    raise WorkflowLabError(
                        "entry_changed",
                        f"Input bytes changed before stable snapshot: {relative}",
                        stage=stage,
                    )
        except Exception:
            for descriptor in pinned.values():
                os.close(descriptor)
            raise
        return _TreeSnapshot(
            dict(sorted(files.items())),
            _tree_digest(files),
            dict(sorted(pinned.items())),
        )

    def _snapshot_published_tree(
        self,
        root: Path,
        *,
        stage: str,
        boundary: Path | None,
        expected_root: _PathIdentity,
    ) -> _PublishedTreeSnapshot:
        protected_boundary = boundary or self.paths.state_root
        guard = _PublishedStateGuard(root, stage=stage, missing=False)
        try:
            guard.directories.extend(
                _pin_read_directory_chain(root, protected_boundary, stage=stage)
            )
            _verify_published_final_state(root, expected=expected_root, stage=stage)
            directory_identities = _validate_directory_chain(
                root,
                protected_boundary,
                stage,
            )
        except BaseException:
            guard.close()
            raise
        root_key = root.absolute().relative_to(protected_boundary.absolute()).as_posix()
        if directory_identities[root_key or "."] != expected_root:
            guard.close()
            raise WorkflowLabError(
                "tree_changed",
                "Published workflow root changed before its snapshot was pinned.",
                stage=stage,
            )
        try:
            entries, bookkeeping, directories = _scan_published_entries(
                root, stage=stage
            )
            if len(entries) + len(bookkeeping) > _MAX_FILE_COUNT:
                raise WorkflowLabError(
                    "file_count_exceeded",
                    f"Published workflow contains more than {_MAX_FILE_COUNT} files.",
                    stage=stage,
                )
            _validate_relative_names(entries, require_policy=False, stage=stage)
            files: dict[str, bytes] = {}
            retained_files: dict[str, bytes] = {}
            identities: dict[str, _PathIdentity] = {}
            total = 0
            for relative, path in {**entries, **bookkeeping}.items():
                content, identity = _read_pinned_file(
                    path,
                    stage=stage,
                    fault_hook=self._fault_hook,
                )
                total += len(content)
                if total > _MAX_TOTAL_BYTES:
                    raise WorkflowLabError(
                        "total_bytes_exceeded",
                        f"Published workflow exceeds {_MAX_TOTAL_BYTES} total bytes.",
                        stage=stage,
                    )
                identities[relative] = identity
                if relative in entries:
                    try:
                        content.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as exc:
                        raise WorkflowLabError(
                            "published_encoding_invalid",
                            f"Published workflow YAML must be UTF-8: {relative}",
                            stage=stage,
                        ) from exc
                    files[relative] = content
                else:
                    _validate_canonical_inert_init(content, relative, stage=stage)
                    retained_files[relative] = content
            final_entries, final_bookkeeping, final_directories = (
                _scan_published_entries(root, stage=stage)
            )
            final_paths = {**final_entries, **final_bookkeeping}
            if set(final_paths) != set(identities) or final_directories != directories:
                raise WorkflowLabError(
                    "tree_changed",
                    "Published workflow tree changed while it was snapshotted.",
                    stage=stage,
                )
            for relative, path in final_paths.items():
                if _PathIdentity.from_stat(os.lstat(path)) != identities[relative]:
                    raise WorkflowLabError(
                        "entry_replaced",
                        f"Published workflow changed while snapshotted: {relative}",
                        stage=stage,
                    )
                descriptor = _open_pinned_input(path)
                guard.file_descriptors[relative] = descriptor
                if (
                    _PathIdentity.from_stat(os.fstat(descriptor))
                    != identities[relative]
                    or _read_stable_descriptor(descriptor, stage=stage, label=relative)
                    != {**files, **retained_files}[relative]
                ):
                    raise WorkflowLabError(
                        "entry_changed",
                        f"Published workflow changed before lifetime pin: {relative}",
                        stage=stage,
                    )
                guard.file_identities[relative] = identities[relative]
            for relative in final_directories:
                if relative == ".":
                    continue
                directory = root.joinpath(*PurePosixPath(relative).parts)
                guard.directories.extend(
                    _pin_read_directory_chain(directory, root, stage=stage)
                )
            if (
                _validate_directory_chain(root, protected_boundary, stage)
                != directory_identities
            ):
                raise WorkflowLabError(
                    "tree_changed",
                    "A published workflow ancestor changed during export.",
                    stage=stage,
                )
            _verify_published_final_state(root, expected=expected_root, stage=stage)
            guard.expected_files = dict(sorted(files.items()))
            guard.expected_retained = dict(sorted(retained_files.items()))
            guard.expected_directories = final_directories
            guard.verify()
            return _PublishedTreeSnapshot(
                guard.expected_files,
                guard.expected_retained,
                _published_tree_digest(files, retained_files),
                guard,
            )
        except BaseException:
            guard.close()
            raise

    @staticmethod
    def _validate_app_name(app_name: str) -> None:
        if not _APP_NAME.fullmatch(str(app_name)):
            raise WorkflowLabError(
                "app_name_forbidden",
                "Workflow Lab app names must be lowercase identifiers.",
                stage="preflight",
            )

    @staticmethod
    def _validate_yaml_shapes(parsed: Mapping[str, Any]) -> None:
        for name, value in parsed.items():
            if not isinstance(value, dict):
                raise WorkflowLabError(
                    "yaml_mapping_required",
                    f"{name} must contain exactly one YAML mapping.",
                    stage="safe_yaml",
                )
            _check_depth(value, stage="safe_yaml")

    @staticmethod
    def _validate_code_references(parsed: Mapping[str, Any]) -> set[str]:
        references: set[str] = set()
        for filename, data in parsed.items():
            _reject_blocked_keys(data, filename)
            if filename == "workflow_policy.yaml":
                continue
            selector = data.get("agent_class")
            if selector not in _AGENT_CLASSES:
                raise WorkflowLabError(
                    "agent_class_forbidden",
                    f"Unsupported or executable agent_class in {filename}: {selector!r}",
                    stage="code_references",
                )
            if "generate_content_config" in data:
                raise WorkflowLabError(
                    "executable_config_forbidden",
                    f"generate_content_config is not supported in {filename}.",
                    stage="code_references",
                )
            for field in _CODE_CONFIG_FIELDS:
                if data.get(field) is not None:
                    _code_config_name(data[field], filename, field)
                    raise WorkflowLabError(
                        "python_fqn_forbidden",
                        f"Python reference field {field} is not approved in {filename}.",
                        stage="code_references",
                    )
            for field in _CALLBACK_FIELDS:
                for value in data.get(field) or []:
                    _code_config_name(value, filename, field)
                    raise WorkflowLabError(
                        "python_fqn_forbidden",
                        f"Callback field {field} is not approved in {filename}.",
                        stage="code_references",
                    )
            for tool in data.get("tools") or []:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise WorkflowLabError(
                        "python_fqn_forbidden",
                        f"Invalid tool reference in {filename}.",
                        stage="code_references",
                    )
                references.add(tool["name"])
            for ref in data.get("sub_agents") or []:
                if not isinstance(ref, dict):
                    raise WorkflowLabError(
                        "python_fqn_forbidden",
                        f"Invalid sub-agent reference in {filename}.",
                        stage="code_references",
                    )
                if ref.get("code") is not None:
                    raise WorkflowLabError(
                        "python_fqn_forbidden",
                        f"sub_agents[].code is not approved in {filename}.",
                        stage="code_references",
                    )
                if ref.get("config_path") is not None:
                    _validate_config_path(str(ref["config_path"]), parsed, filename)
        forbidden = references - _APPROVED_PYTHON_FQNS
        if forbidden:
            raise WorkflowLabError(
                "python_fqn_forbidden",
                f"Python FQN is not approved: {min(forbidden)}",
                stage="code_references",
            )
        return references

    @staticmethod
    def _validate_adk_schema(
        parsed: Mapping[str, Any], schema: Mapping[str, Any]
    ) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:
            raise WorkflowLabError(
                "adk_validation_dependencies_missing",
                'Install Python 3.11 dependencies with ".[adk-dev]".',
                stage="adk_schema",
            ) from exc
        for filename, data in parsed.items():
            if filename == "workflow_policy.yaml":
                continue
            selector = str(data["agent_class"])
            selected = dict(schema)
            selected["oneOf"] = [{"$ref": f"#/$defs/{selector}Config"}]
            errors = sorted(
                Draft202012Validator(selected).iter_errors(data),
                key=lambda error: tuple(str(part) for part in error.path),
            )
            if errors:
                location = ".".join(str(part) for part in errors[0].path) or "<root>"
                raise WorkflowLabError(
                    "schema_invalid",
                    f"ADK 2.7.1 schema rejected {filename} at {location}: {errors[0].message}",
                    stage="adk_schema",
                )

    @staticmethod
    def _validate_project_policy(
        parsed: Mapping[str, Any],
        policy: Mapping[str, Any],
        references: set[str],
    ) -> tuple[set[str], set[str], set[str]]:
        allowed_keys = {
            "schema_version",
            "capability",
            "workspace_id",
            "confirmation_required",
            "publishing",
            "tool_ids",
            "workflow_ids",
            "python_fqns",
            "write_tool_ids",
        }
        if set(policy) != allowed_keys:
            raise WorkflowLabError(
                "policy_keys_forbidden",
                f"Workflow policy keys must be exactly {sorted(allowed_keys)!r}.",
                stage="project_policy",
            )
        confirmation = policy.get("confirmation_required")
        if type(confirmation) is not bool or confirmation is not True:
            raise WorkflowLabError(
                "confirmation_required",
                "Workflow policy confirmation_required must be exactly boolean true.",
                stage="project_policy",
            )
        checks = (
            (
                "schema_version",
                "ai-actuary.workflow-policy.v1",
                "policy_version_forbidden",
            ),
            ("capability", "adk-developer", "capability_forbidden"),
            ("workspace_id", "adk-development", "workspace_forbidden"),
            ("publishing", "git-review-only", "publishing_forbidden"),
        )
        for key, expected, code in checks:
            if policy.get(key) != expected:
                raise WorkflowLabError(
                    code,
                    f"Workflow policy {key} must be {expected!r}.",
                    stage="project_policy",
                )
        for filename, data in parsed.items():
            if filename != "workflow_policy.yaml":
                _validate_project_agent_config(filename, data)
        tool_ids = _string_set(policy.get("tool_ids"), "tool_ids")
        workflow_ids = _string_set(policy.get("workflow_ids"), "workflow_ids")
        python_fqns = _string_set(policy.get("python_fqns"), "python_fqns")
        write_tool_ids = _string_set(policy.get("write_tool_ids"), "write_tool_ids")
        _reject_unknown(tool_ids, _APPROVED_TOOL_IDS, "tool_id_forbidden", "tool ID")
        _reject_unknown(
            workflow_ids,
            _APPROVED_WORKFLOW_IDS,
            "workflow_id_forbidden",
            "workflow ID",
        )
        _reject_unknown(
            python_fqns,
            _APPROVED_PYTHON_FQNS,
            "python_fqn_forbidden",
            "Python FQN",
        )
        if write_tool_ids:
            raise WorkflowLabError(
                "write_tool_forbidden",
                "Workflow Lab drafts cannot declare control-plane write tools.",
                stage="project_policy",
            )
        if references != python_fqns:
            raise WorkflowLabError(
                "python_fqn_declaration_mismatch",
                "Every approved Python FQN must be explicitly declared and used.",
                stage="project_policy",
            )
        return tool_ids, workflow_ids, python_fqns

    @staticmethod
    def _run_isolated_contract(
        snapshot: _TreeSnapshot,
    ) -> tuple[dict[str, Any], str]:
        probe_path = Path(__file__).with_name("_workflow_contract_probe.py")
        with tempfile.TemporaryDirectory(
            prefix="ai-actuary-workflow-contract-"
        ) as temp:
            root = Path(temp) / "app"
            root.mkdir()
            for relative, content in snapshot.files.items():
                target = root.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            environment = _offline_environment()
            result = subprocess.run(
                [sys.executable, "-I", str(probe_path), str(root)],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1000:]
                raise WorkflowLabError(
                    "contract_invalid",
                    f"Isolated ADK contract probe failed: {detail}",
                    stage="isolated_contract",
                )
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise WorkflowLabError(
                    "contract_invalid",
                    "Isolated ADK contract probe returned invalid output.",
                    stage="isolated_contract",
                ) from exc
        return payload, "<isolated-temporary-snapshot>"

    def _write_export_bundle(
        self,
        export_dir: Path,
        candidate: Mapping[str, bytes],
        patch: bytes,
        manifest: bytes,
        *,
        published_guard: _PublishedStateGuard,
    ) -> tuple[_ExportCommitBinding, _ExportProofLease]:
        guards: list[_PinnedOutputDirectory] = []
        files: list[_PinnedOutputFile] = []
        manifest_file: _PinnedOutputFile | None = None
        lease: _ExportProofLease | None = None
        try:
            state = _PinnedOutputDirectory.open_boundary(
                self.paths.state_root, fault_hook=self._fault_hook
            )
            guards.append(state)
            exports = state.create_child(self.paths.exports_root.name, exist_ok=True)
            guards.append(exports)
            export = exports.create_child(export_dir.name)
            guards.append(export)
            candidate_guard = export.create_child("candidate")
            guards.append(candidate_guard)
            directory_guards: dict[str, _PinnedOutputDirectory] = {"": candidate_guard}
            self._fault_hook("before_output_write", candidate_guard.path)
            for relative, content in sorted(candidate.items()):
                pure = PurePosixPath(relative)
                parent = candidate_guard
                key_parts: list[str] = []
                for part in pure.parts[:-1]:
                    key_parts.append(part)
                    key = "/".join(key_parts)
                    if key not in directory_guards:
                        directory_guards[key] = parent.create_child(part)
                        guards.append(directory_guards[key])
                    parent = directory_guards[key]
                files.append(parent.write_file(pure.name, content, stage="export"))
            files.append(export.write_file("candidate.patch", patch, stage="export"))
            # The manifest is the exclusive-create commit marker and is written last.
            self._fault_hook("before_manifest_write", export.path)
            published_guard.verify()
            manifest_file = export.write_file("manifest.json", manifest, stage="export")
            files.append(manifest_file)
            self._fault_hook("after_manifest_write", export.path)
            published_guard.verify()
            self._fault_hook("before_output_hardening", export.path)
            published_guard.verify()
            for output in files:
                output.make_read_only()
            self._fault_hook("before_final_output_verify", candidate_guard.path)
            for guard in reversed(guards[2:]):
                guard.make_read_only()
                guard.verify()
            if os.name == "nt":
                _harden_windows_materialized_tree(export.path)
            for guard in guards:
                guard.verify()
            expected_topology = {
                "candidate": "directory",
                "candidate.patch": "regular",
                "manifest.json": "regular",
            }
            for relative in candidate:
                pure = PurePosixPath(relative)
                expected_topology[f"candidate/{relative}"] = "regular"
                for index in range(1, len(pure.parts)):
                    parent = PurePosixPath("candidate", *pure.parts[:index]).as_posix()
                    expected_topology[parent] = "directory"
            _verify_terminal_export(
                export,
                guards[2:],
                files,
                expected_topology,
            )
            self._fault_hook("before_final_published_verify", export.path)
            published_guard.verify()
            _verify_terminal_export(
                export,
                guards[2:],
                files,
                expected_topology,
            )
            commit_binding = _capture_export_commit_binding(
                state,
                exports,
                export,
                manifest_file,
            )
            # Binding capture is deliberately followed by the final, fault-free
            # published/output proof. Nothing that can invoke caller code follows it.
            published_guard.verify()
            _verify_terminal_export(
                export,
                guards[2:],
                files,
                expected_topology,
            )
            lease = _ExportProofLease(guards, files, expected_topology)
            return commit_binding, lease
        except BaseException as original:
            cleanup_error: BaseException | None = None
            if manifest_file is not None and manifest_file.descriptor >= 0:
                try:
                    manifest_file.discard()
                except BaseException as exc:  # noqa: BLE001 - marker safety covers interrupts
                    cleanup_error = exc
            for output in reversed(files):
                output.close()
            for guard in reversed(guards[2:]):
                guard.close()
            if cleanup_error is not None:
                raise WorkflowLabError(
                    "export_revoke_failed",
                    (
                        "Export failed and its descriptor-bound commit marker "
                        f"could not be revoked ({type(original).__name__}; "
                        f"{type(cleanup_error).__name__})."
                    ),
                    stage="export",
                ) from cleanup_error
            raise
        finally:
            if lease is None:
                for output in reversed(files):
                    output.close()
                for guard in reversed(guards):
                    guard.close()


def _capture_export_commit_binding(
    state: _PinnedOutputDirectory,
    exports: _PinnedOutputDirectory,
    export: _PinnedOutputDirectory,
    manifest: _PinnedOutputFile,
) -> _ExportCommitBinding:
    if os.name == "nt":
        handles = [
            *state.ancestor_windows_handles,
            state.windows_handle,
            exports.windows_handle,
            export.windows_handle,
        ]
        if any(handle is None for handle in handles):
            raise AssertionError("Windows export commit binding requires live handles.")
        directory_chain = tuple(
            _receipt_identity_from_windows_handle(int(handle)) for handle in handles
        )
    else:
        descriptors = [
            *state.ancestor_descriptors,
            state.descriptor,
            exports.descriptor,
            export.descriptor,
        ]
        if any(descriptor is None for descriptor in descriptors):
            raise AssertionError(
                "POSIX export commit binding requires live descriptors."
            )
        directory_chain = tuple(
            _receipt_identity_from_descriptor(int(descriptor))
            for descriptor in descriptors
        )
    return _ExportCommitBinding(
        directory_chain=directory_chain,
        manifest=_receipt_identity_from_descriptor(manifest.descriptor),
    )


def _verify_terminal_export(
    export: _PinnedOutputDirectory,
    directories: list[_PinnedOutputDirectory],
    files: list[_PinnedOutputFile],
    expected_topology: Mapping[str, str],
) -> None:
    for directory in directories:
        directory.verify()
    _verify_output_topology(export, expected_topology)
    for output in files:
        output.verify()
    _validate_materialized_read_only(export.path)
    # Rebind pathname topology/bytes after the permission proof itself.
    for directory in directories:
        directory.verify()
    _verify_output_topology(export, expected_topology)
    for output in files:
        output.verify()


def _verify_output_topology(
    root: _PinnedOutputDirectory,
    expected: Mapping[str, str],
) -> None:
    actual: dict[str, str] = {}

    def walk(directory: Path, prefix: str, descriptor: int | None) -> None:
        try:
            entries = sorted(
                os.scandir(descriptor if descriptor is not None else directory),
                key=lambda entry: entry.name.casefold(),
            )
        except OSError as exc:
            raise WorkflowLabError(
                "output_tree_changed",
                f"Unable to enumerate committed output: {prefix or '.'}",
                stage="export",
            ) from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}".lstrip("/")
            try:
                metadata = (
                    os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if descriptor is not None
                    else os.lstat(Path(entry.path))
                )
            except OSError as exc:
                raise WorkflowLabError(
                    "output_tree_changed",
                    f"Committed output changed during enumeration: {relative}",
                    stage="export",
                ) from exc
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode):
                kind = "symlink"
            elif attributes & _REPARSE_ATTRIBUTE:
                kind = "reparse"
            elif stat.S_ISDIR(metadata.st_mode):
                kind = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                kind = "regular"
            else:
                kind = "special"
            actual[relative] = kind
            if kind != "directory" or expected.get(relative) != "directory":
                continue
            if descriptor is not None:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                child = os.open(entry.name, flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise WorkflowLabError(
                            "output_tree_changed",
                            f"Committed output directory changed: {relative}",
                            stage="export",
                        )
                    walk(Path(entry.path), relative, child)
                finally:
                    os.close(child)
            else:
                child_handle = _open_windows_directory_handle(Path(entry.path))
                try:
                    walk(Path(entry.path), relative, None)
                finally:
                    _close_windows_handle(child_handle)

    walk(root.path, "", root.descriptor)
    if actual != dict(expected):
        raise WorkflowLabError(
            "output_tree_changed",
            "Committed export topology contains a missing, replaced, or unmanifested object.",
            stage="export",
        )


def _safe_yaml_load(content: bytes, *, name: str) -> Any:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkflowLabError(
            "yaml_encoding",
            f"{name} must be UTF-8.",
            stage="safe_yaml",
        ) from exc
    try:
        for token in yaml.scan(text, Loader=_UniqueSafeLoader):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                raise WorkflowLabError(
                    "yaml_alias_forbidden",
                    f"YAML anchors and aliases are forbidden in {name}.",
                    stage="safe_yaml",
                )
        documents = list(yaml.load_all(text, Loader=_UniqueSafeLoader))
    except WorkflowLabError:
        raise
    except yaml.constructor.ConstructorError as exc:
        raise WorkflowLabError(
            "yaml_unsafe_tag",
            f"Unsafe YAML tag is forbidden in {name}.",
            stage="safe_yaml",
        ) from exc
    except yaml.YAMLError as exc:
        raise WorkflowLabError(
            "yaml_invalid",
            f"Invalid YAML in {name}: {exc}",
            stage="safe_yaml",
        ) from exc
    if len(documents) != 1:
        raise WorkflowLabError(
            "yaml_document_count",
            f"{name} must contain exactly one YAML document.",
            stage="safe_yaml",
        )
    return documents[0]


def _verify_published_final_state(
    path: Path,
    *,
    expected: _PathIdentity | None,
    stage: str,
) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if expected is None:
            return
        raise WorkflowLabError(
            "tree_changed",
            "Published workflow root disappeared during export.",
            stage=stage,
        ) from None
    if expected is None:
        raise WorkflowLabError(
            "tree_changed",
            "Published workflow root appeared during export.",
            stage=stage,
        )
    _reject_link_or_reparse(metadata, path, stage)
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkflowLabError(
            "directory_required",
            f"Expected a published workflow directory: {path}",
            stage=stage,
        )
    if _PathIdentity.from_stat(metadata) != expected:
        raise WorkflowLabError(
            "tree_changed",
            "Published workflow root was replaced during export.",
            stage=stage,
        )


def _validate_directory_chain(
    target: Path, boundary: Path, stage: str
) -> dict[str, _PathIdentity]:
    absolute_target = target.absolute()
    absolute_boundary = boundary.absolute()
    try:
        relative = absolute_target.relative_to(absolute_boundary)
    except ValueError as exc:
        raise WorkflowLabError(
            "path_escape",
            f"Path is outside the server-owned state root: {target}",
            stage=stage,
        ) from exc
    current = absolute_boundary
    identities: dict[str, _PathIdentity] = {}
    try:
        boundary_metadata = os.lstat(current)
    except FileNotFoundError as exc:
        raise WorkflowLabError(
            "draft_not_found",
            f"Workflow boundary does not exist: {current}",
            stage=stage,
        ) from exc
    _reject_link_or_reparse(boundary_metadata, current, stage)
    if not stat.S_ISDIR(boundary_metadata.st_mode):
        raise WorkflowLabError(
            "directory_required",
            f"Expected a directory: {current}",
            stage=stage,
        )
    identities["."] = _PathIdentity.from_stat(boundary_metadata)
    for part in relative.parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise WorkflowLabError(
                "draft_not_found",
                f"Workflow path does not exist: {current}",
                stage=stage,
            ) from exc
        _reject_link_or_reparse(metadata, current, stage)
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkflowLabError(
                "directory_required",
                f"Expected a directory: {current}",
                stage=stage,
            )
        identities[current.relative_to(absolute_boundary).as_posix()] = (
            _PathIdentity.from_stat(metadata)
        )
    return identities


def _pin_read_directory_chain(
    target: Path, boundary: Path, *, stage: str
) -> list[_PinnedReadDirectory]:
    absolute_target = target.absolute()
    absolute_boundary = boundary.absolute()
    try:
        relative = absolute_target.relative_to(absolute_boundary)
    except ValueError as exc:
        raise WorkflowLabError(
            "path_escape",
            f"Published path is outside its protected boundary: {target}",
            stage=stage,
        ) from exc

    pins: list[_PinnedReadDirectory] = []
    paths = [absolute_boundary]
    paths.extend(
        absolute_boundary.joinpath(*relative.parts[:index])
        for index in range(1, len(relative.parts) + 1)
    )
    if os.name == "nt":
        try:
            for path in paths:
                before_stat = os.lstat(path)
                _reject_link_or_reparse(before_stat, path, stage)
                before = _PathIdentity.from_stat(before_stat)
                if not stat.S_ISDIR(before.mode):
                    raise WorkflowLabError(
                        "directory_required",
                        f"Expected a published directory: {path}",
                        stage=stage,
                    )
                handle = _open_windows_directory_handle(path)
                windows_identity = _windows_directory_handle_identity(handle)
                if windows_identity[:2] != (before.device, before.inode):
                    _close_windows_handle(handle)
                    raise WorkflowLabError(
                        "tree_changed",
                        f"Published directory changed while being pinned: {path}",
                        stage=stage,
                    )
                pin = _PinnedReadDirectory(
                    path,
                    before,
                    windows_handle=handle,
                    windows_identity=windows_identity,
                )
                pins.append(pin)
                pin.verify(stage=stage)
            return pins
        except Exception:
            for pin in reversed(pins):
                pin.close()
            raise

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        boundary_before = _PathIdentity.from_stat(os.lstat(absolute_boundary))
        descriptor = os.open(absolute_boundary, flags)
        descriptors.append(descriptor)
        if _PathIdentity.from_stat(os.fstat(descriptor)) != boundary_before:
            raise WorkflowLabError(
                "tree_changed",
                "Published boundary changed while it was being pinned.",
                stage=stage,
            )
        pins.append(
            _PinnedReadDirectory(
                absolute_boundary, boundary_before, descriptor=descriptor
            )
        )
        current = absolute_boundary
        for part in relative.parts:
            before_stat = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            _reject_link_or_reparse(before_stat, current / part, stage)
            before = _PathIdentity.from_stat(before_stat)
            child = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(child)
            if _PathIdentity.from_stat(os.fstat(child)) != before:
                raise WorkflowLabError(
                    "tree_changed",
                    f"Published directory changed while being pinned: {current / part}",
                    stage=stage,
                )
            current = current / part
            pins.append(_PinnedReadDirectory(current, before, descriptor=child))
            descriptor = child
        for pin in pins:
            pin.verify(stage=stage)
        return pins
    except Exception:
        pinned_descriptors = {
            pin.descriptor for pin in pins if pin.descriptor is not None
        }
        for pin in reversed(pins):
            pin.close()
        for descriptor in reversed(descriptors):
            if descriptor not in pinned_descriptors:
                os.close(descriptor)
        raise


def _scan_entries(
    root: Path,
    *,
    stage: str,
    directory_identities: dict[str, _PathIdentity] | None = None,
) -> dict[str, Path]:
    entries: dict[str, Path] = {}
    directories = directory_identities if directory_identities is not None else {}
    root_metadata = os.lstat(root)
    _reject_link_or_reparse(root_metadata, root, stage)
    directories["."] = _PathIdentity.from_stat(root_metadata)
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > _MAX_DEPTH:
            raise WorkflowLabError(
                "path_depth_exceeded",
                f"Workflow nesting exceeds {_MAX_DEPTH} levels.",
                stage=stage,
            )
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: item.name.casefold()
            )
        except OSError as exc:
            raise WorkflowLabError(
                "directory_read_failed",
                f"Unable to enumerate workflow directory: {directory}",
                stage=stage,
            ) from exc
        for entry in children:
            path = Path(entry.path)
            metadata = os.lstat(path)
            _reject_link_or_reparse(metadata, path, stage)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                if relative != "sub_agents":
                    code = (
                        "non_regular_file"
                        if relative in {"root_agent.yaml", "workflow_policy.yaml"}
                        else "path_forbidden"
                    )
                    raise WorkflowLabError(
                        code,
                        f"Unsupported directory in declarative workflow: {relative}",
                        stage=stage,
                    )
                directories[relative] = _PathIdentity.from_stat(metadata)
                stack.append((path, depth + 1))
            else:
                entries[relative] = path
    return dict(sorted(entries.items()))


def _scan_published_entries(
    root: Path,
    *,
    stage: str,
) -> tuple[dict[str, Path], dict[str, Path], dict[str, _PathIdentity]]:
    root_metadata = os.lstat(root)
    _reject_link_or_reparse(root_metadata, root, stage)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise WorkflowLabError(
            "directory_required",
            f"Expected a published workflow directory: {root}",
            stage=stage,
        )
    declarative: dict[str, Path] = {}
    bookkeeping: dict[str, Path] = {}
    directories = {".": _PathIdentity.from_stat(root_metadata)}
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > _MAX_DEPTH:
            raise WorkflowLabError(
                "path_depth_exceeded",
                f"Published workflow nesting exceeds {_MAX_DEPTH} levels.",
                stage=stage,
            )
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: item.name.casefold()
            )
        except OSError as exc:
            raise WorkflowLabError(
                "directory_read_failed",
                f"Unable to enumerate published workflow directory: {directory}",
                stage=stage,
            ) from exc
        for entry in children:
            path = Path(entry.path)
            metadata = os.lstat(path)
            _reject_link_or_reparse(metadata, path, stage)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                if entry.name == "__pycache__":
                    raise WorkflowLabError(
                        "file_type_forbidden",
                        f"Bytecode caches are forbidden in published source: {relative}",
                        stage=stage,
                    )
                if relative != "sub_agents":
                    raise WorkflowLabError(
                        "path_forbidden",
                        f"Unsupported directory in published workflow: {relative}",
                        stage=stage,
                    )
                directories[relative] = _PathIdentity.from_stat(metadata)
                stack.append((path, depth + 1))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkflowLabError(
                    "non_regular_file",
                    f"Published workflow object is not a regular file: {relative}",
                    stage=stage,
                )
            pure = PurePosixPath(relative)
            is_declarative = relative in {
                "root_agent.yaml",
                "workflow_policy.yaml",
            } or (
                len(pure.parts) == 2
                and pure.parts[0] == "sub_agents"
                and pure.suffix == ".yaml"
            )
            if pure.suffix.lower() == ".pyc":
                raise WorkflowLabError(
                    "file_type_forbidden",
                    f"Bytecode is forbidden in published source: {relative}",
                    stage=stage,
                )
            is_bookkeeping = relative == "__init__.py"
            if is_declarative:
                declarative[relative] = path
            elif is_bookkeeping:
                bookkeeping[relative] = path
            else:
                raise WorkflowLabError(
                    "file_type_forbidden",
                    f"Unknown executable/non-declarative published file: {relative}",
                    stage=stage,
                )
    return (
        dict(sorted(declarative.items())),
        dict(sorted(bookkeeping.items())),
        dict(sorted(directories.items())),
    )


def _validate_canonical_inert_init(
    content: bytes,
    relative: str,
    *,
    stage: str,
) -> None:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise WorkflowLabError(
            "published_python_forbidden",
            f"Published package stub must be canonical ASCII: {relative}",
            stage=stage,
        ) from exc
    if not re.fullmatch(r'"""[A-Za-z0-9 ._-]{1,200}"""\n', text):
        raise WorkflowLabError(
            "published_python_forbidden",
            f"Only a canonical inert package docstring is allowed: {relative}",
            stage=stage,
        )


def _validate_relative_names(
    entries: Mapping[str, Path], *, require_policy: bool, stage: str
) -> None:
    folded: dict[str, str] = {}
    for relative in entries:
        if len(relative) > _MAX_PATH_LENGTH:
            raise WorkflowLabError(
                "path_forbidden",
                f"Workflow path is too long: {relative}",
                stage=stage,
            )
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
            raise WorkflowLabError(
                "path_forbidden",
                f"Unsafe workflow path: {relative}",
                stage=stage,
            )
        for part in pure.parts:
            stem = part.split(".", 1)[0].upper()
            if (
                not _PORTABLE_BASENAME.fullmatch(part)
                or "\\" in part
                or ":" in part
                or part.endswith((" ", "."))
                or stem in _WINDOWS_RESERVED
            ):
                raise WorkflowLabError(
                    "path_forbidden",
                    f"Cross-platform unsafe workflow path: {relative}",
                    stage=stage,
                )
        canonical = relative.casefold()
        previous = folded.setdefault(canonical, relative)
        if previous != relative:
            raise WorkflowLabError(
                "path_case_collision",
                f"Case-colliding workflow paths: {previous!r} and {relative!r}",
                stage=stage,
            )
        allowed = relative in {"root_agent.yaml", "workflow_policy.yaml"} or (
            len(pure.parts) == 2
            and pure.parts[0] == "sub_agents"
            and pure.suffix == ".yaml"
        )
        if not allowed:
            raise WorkflowLabError(
                "file_type_forbidden",
                f"Only declarative root/policy/sub-agent YAML is allowed: {relative}",
                stage=stage,
            )
    required = {"root_agent.yaml"}
    if require_policy:
        required.add("workflow_policy.yaml")
    missing = required - set(entries)
    if missing:
        raise WorkflowLabError(
            "required_file_missing",
            f"Missing required workflow file: {min(missing)}",
            stage=stage,
        )


def _read_pinned_file(
    path: Path,
    *,
    stage: str,
    fault_hook: _FaultHook,
) -> tuple[bytes, _PathIdentity]:
    before_stat = os.lstat(path)
    _reject_link_or_reparse(before_stat, path, stage)
    before = _PathIdentity.from_stat(before_stat)
    if not stat.S_ISREG(before.mode):
        raise WorkflowLabError(
            "non_regular_file",
            f"Workflow input is not a regular file: {path}",
            stage=stage,
        )
    if before.links != 1:
        raise WorkflowLabError(
            "hardlink_forbidden",
            f"Hard-linked workflow input is forbidden: {path}",
            stage=stage,
        )
    if before.size > _MAX_FILE_BYTES:
        raise WorkflowLabError(
            "file_bytes_exceeded",
            f"Workflow file exceeds {_MAX_FILE_BYTES} bytes: {path}",
            stage=stage,
        )
    if _has_windows_ads(path):
        raise WorkflowLabError(
            "path_forbidden",
            f"Windows alternate data streams are forbidden: {path}",
            stage=stage,
        )
    fault_hook("after_lstat", path)
    try:
        descriptor = _open_pinned_input(path)
    except OSError as exc:
        raise WorkflowLabError(
            "input_open_failed",
            f"Unable to safely open workflow input: {path}",
            stage=stage,
        ) from exc
    try:
        opened = _PathIdentity.from_stat(os.fstat(descriptor))
        same_opened_object = (
            opened.device == before.device
            and opened.inode == before.inode
            and opened.size == before.size
            and stat.S_IFMT(opened.mode) == stat.S_IFMT(before.mode)
            and opened.links == before.links
        )
        if not same_opened_object or (os.name == "nt" and opened != before):
            raise WorkflowLabError(
                "entry_replaced",
                f"Workflow input was replaced before open: {path}",
                stage=stage,
            )
        if not stat.S_ISREG(opened.mode):
            raise WorkflowLabError(
                "non_regular_file",
                f"Opened workflow input is not regular: {path}",
                stage=stage,
            )
        if opened.links != 1:
            raise WorkflowLabError(
                "hardlink_forbidden",
                f"Opened workflow input is hard-linked: {path}",
                stage=stage,
            )
        content = _read_limited_descriptor(descriptor)
        if len(content) > _MAX_FILE_BYTES:
            raise WorkflowLabError(
                "file_bytes_exceeded",
                f"Workflow file exceeds {_MAX_FILE_BYTES} bytes: {path}",
                stage=stage,
            )
        fault_hook("after_input_read", path)
        os.lseek(descriptor, 0, os.SEEK_SET)
        stable_content = _read_limited_descriptor(descriptor)
        if stable_content != content:
            raise WorkflowLabError(
                "entry_changed",
                f"Workflow input bytes changed between pinned reads: {path}",
                stage=stage,
            )
        after_handle = _PathIdentity.from_stat(os.fstat(descriptor))
        if after_handle != opened:
            raise WorkflowLabError(
                "entry_changed",
                f"Workflow input changed while being read: {path}",
                stage=stage,
            )
    finally:
        os.close(descriptor)
    try:
        after_path = _PathIdentity.from_stat(os.lstat(path))
    except FileNotFoundError as exc:
        raise WorkflowLabError(
            "entry_replaced",
            f"Workflow input disappeared while being read: {path}",
            stage=stage,
        ) from exc
    if after_path != opened:
        raise WorkflowLabError(
            "entry_replaced",
            f"Workflow input was replaced while being read: {path}",
            stage=stage,
        )
    return content, opened


def _read_limited_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_FILE_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_stable_descriptor(descriptor: int, *, stage: str, label: str) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    first = _read_limited_descriptor(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    second = _read_limited_descriptor(descriptor)
    if first != second:
        raise WorkflowLabError(
            "entry_changed",
            f"Pinned workflow bytes changed between reads: {label}",
            stage=stage,
        )
    return first


def _verify_pinned_snapshot(snapshot: _TreeSnapshot, root: Path, *, stage: str) -> None:
    descriptors = snapshot.pinned_descriptors or {}
    if set(descriptors) != set(snapshot.files):
        raise WorkflowLabError(
            "entry_changed",
            "The server-owned input snapshot lost a pinned object.",
            stage=stage,
        )
    identities: dict[str, _PathIdentity] = {}
    for relative, descriptor in descriptors.items():
        before = _PathIdentity.from_stat(os.fstat(descriptor))
        content = _read_stable_descriptor(descriptor, stage=stage, label=relative)
        after = _PathIdentity.from_stat(os.fstat(descriptor))
        try:
            path_identity = _PathIdentity.from_stat(
                os.lstat(root.joinpath(*PurePosixPath(relative).parts))
            )
        except FileNotFoundError as exc:
            raise WorkflowLabError(
                "entry_replaced",
                f"Pinned workflow path disappeared: {relative}",
                stage=stage,
            ) from exc
        if (
            before != after
            or after != path_identity
            or content != snapshot.files[relative]
        ):
            raise WorkflowLabError(
                "entry_changed",
                f"Pinned workflow snapshot changed before validation completed: {relative}",
                stage=stage,
            )
        identities[relative] = after
    for relative, descriptor in descriptors.items():
        final_handle = _PathIdentity.from_stat(os.fstat(descriptor))
        final_path = _PathIdentity.from_stat(
            os.lstat(root.joinpath(*PurePosixPath(relative).parts))
        )
        if final_handle != identities[relative] or final_path != identities[relative]:
            raise WorkflowLabError(
                "entry_changed",
                f"Pinned workflow changed during final stability check: {relative}",
                stage=stage,
            )


def _close_pinned_snapshot(snapshot: _TreeSnapshot) -> None:
    for descriptor in (snapshot.pinned_descriptors or {}).values():
        os.close(descriptor)


def _open_pinned_input(path: Path) -> int:
    if os.name != "nt":
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        return os.open(path, flags)

    import ctypes
    import msvcrt
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
        str(Path(path).absolute()),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ: deny write/delete while pinned
        None,
        3,  # OPEN_EXISTING
        0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        _close_windows_handle(int(handle))
        raise


def _reject_link_or_reparse(metadata: os.stat_result, path: Path, stage: str) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkflowLabError(
            "symlink_forbidden",
            f"Symbolic links are forbidden in Workflow Lab paths: {path}",
            stage=stage,
        )
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if attributes & _REPARSE_ATTRIBUTE:
        raise WorkflowLabError(
            "reparse_point_forbidden",
            f"Windows reparse points/junctions are forbidden: {path}",
            stage=stage,
        )


def _has_windows_ads(path: Path) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class _FindStreamData(ctypes.Structure):
        _fields_ = [
            ("StreamSize", ctypes.c_longlong),
            ("cStreamName", wintypes.WCHAR * 296),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(_FindStreamData),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FindStreamData)]
    find_next.restype = wintypes.BOOL
    close = kernel32.FindClose
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    data = _FindStreamData()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        return False
    try:
        while True:
            if data.cStreamName not in ("", "::$DATA"):
                return True
            if not find_next(handle, ctypes.byref(data)):
                return False
    finally:
        close(handle)


def _reject_blocked_keys(value: Any, filename: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _BLOCKED_KEYS:
                raise WorkflowLabError(
                    "blocked_key",
                    f"Blocked key {key!r} in {filename}.",
                    stage="code_references",
                )
            _reject_blocked_keys(child, filename)
    elif isinstance(value, list):
        for child in value:
            _reject_blocked_keys(child, filename)


def _code_config_name(value: Any, filename: str, field: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise WorkflowLabError(
            "python_fqn_forbidden",
            f"Invalid Python reference at {field} in {filename}.",
            stage="code_references",
        )
    return value["name"]


def _validate_config_path(value: str, parsed: Mapping[str, Any], filename: str) -> None:
    del parsed
    pure = PurePosixPath(value)
    referencing_directory = PurePosixPath(filename).parent
    base_parts = (
        []
        if referencing_directory == PurePosixPath(".")
        else list(referencing_directory.parts)
    )
    resolved_parts = list(base_parts)
    unsafe = "\\" in value or pure.is_absolute() or pure.suffix != ".yaml"
    for part in pure.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if len(resolved_parts) <= len(base_parts):
                unsafe = True
                break
            resolved_parts.pop()
            continue
        if not _PORTABLE_BASENAME.fullmatch(part):
            unsafe = True
            break
        resolved_parts.append(part)
    if (
        unsafe
        or resolved_parts[: len(base_parts)] != base_parts
        or len(resolved_parts) == len(base_parts)
    ):
        raise WorkflowLabError(
            "sub_agent_path_forbidden",
            f"Unsafe sub-agent config_path in {filename}: {value!r}",
            stage="code_references",
        )


def _validate_project_agent_config(filename: str, data: Mapping[str, Any]) -> None:
    selector = str(data.get("agent_class"))
    allowed = _APPROVED_AGENT_CONFIG_KEYS[selector]
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise WorkflowLabError(
            "agent_config_key_forbidden",
            (
                f"Project policy does not approve {selector} config key "
                f"{unknown[0]!r} in {filename}."
            ),
            stage="project_policy",
        )

    sub_agents = data.get("sub_agents", [])
    if not isinstance(sub_agents, list) or any(
        not isinstance(reference, dict)
        or set(reference) != {"config_path"}
        or not isinstance(reference["config_path"], str)
        for reference in sub_agents
    ):
        raise WorkflowLabError(
            "agent_config_shape_forbidden",
            f"Project policy permits only config_path sub-agent references in {filename}.",
            stage="project_policy",
        )

    if selector == "LlmAgent":
        if data.get("model") != "gemini-2.5-flash":
            raise WorkflowLabError(
                "model_forbidden",
                f"Model is not approved in {filename}.",
                stage="project_policy",
            )
        if not isinstance(data.get("instruction"), str):
            raise WorkflowLabError(
                "agent_config_shape_forbidden",
                f"LlmAgent instruction must be plain text in {filename}.",
                stage="project_policy",
            )
        tools = data.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict)
            or set(tool) != {"name"}
            or not isinstance(tool["name"], str)
            for tool in tools
        ):
            raise WorkflowLabError(
                "agent_config_shape_forbidden",
                f"Project policy permits only name-only tools in {filename}.",
                stage="project_policy",
            )
    elif selector == "LoopAgent" and "max_iterations" in data:
        iterations = data["max_iterations"]
        if type(iterations) is not int or iterations < 1:
            raise WorkflowLabError(
                "agent_config_shape_forbidden",
                f"LoopAgent max_iterations must be a positive integer in {filename}.",
                stage="project_policy",
            )


def _string_set(value: Any, field: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WorkflowLabError(
            "policy_value_invalid",
            f"Workflow policy {field} must be a list of strings.",
            stage="project_policy",
        )
    if len(value) != len(set(value)):
        raise WorkflowLabError(
            "policy_value_invalid",
            f"Workflow policy {field} cannot contain duplicates.",
            stage="project_policy",
        )
    return set(value)


def _reject_unknown(
    actual: set[str], approved: frozenset[str], code: str, label: str
) -> None:
    unknown = actual - approved
    if unknown:
        raise WorkflowLabError(
            code,
            f"Unknown or unapproved {label}: {min(unknown)}",
            stage="project_policy",
        )


def _check_depth(value: Any, *, stage: str, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise WorkflowLabError(
            "data_depth_exceeded",
            f"Workflow data exceeds {_MAX_DEPTH} levels.",
            stage=stage,
        )
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise WorkflowLabError(
                    "yaml_key_type",
                    "Workflow YAML keys must be strings.",
                    stage=stage,
                )
            _check_depth(child, stage=stage, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_depth(child, stage=stage, depth=depth + 1)


def _canonicalize_draft(parsed: Mapping[str, Any]) -> dict[str, bytes]:
    return {
        name: yaml.safe_dump(
            data,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=True,
            line_break="\n",
        )
        .replace("\r\n", "\n")
        .encode("utf-8")
        for name, data in sorted(parsed.items())
    }


@contextmanager
def _application_lock(root: Path) -> Iterator[None]:
    key = os.path.normcase(str(Path(root).absolute()))
    with _APPLICATION_LOCKS_GUARD:
        process_lock = _APPLICATION_LOCKS.setdefault(key, threading.RLock())
    with process_lock:
        depths = getattr(_APPLICATION_LOCK_DEPTHS, "values", None)
        if depths is None:
            depths = {}
            _APPLICATION_LOCK_DEPTHS.values = depths
        if key in depths:
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        descriptor = _open_application_lock_file(key)
        locked = False
        try:
            _lock_application_descriptor(descriptor)
            locked = True
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            try:
                if locked:
                    _unlock_application_descriptor(descriptor)
            finally:
                os.close(descriptor)


@contextmanager
def _export_revoke_lock(export_dir: Path) -> Iterator[None]:
    key = os.path.normcase(str(Path(export_dir).absolute()))
    with _EXPORT_REVOKE_LOCKS_GUARD:
        lock = _EXPORT_REVOKE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        descriptor, boundary = _open_export_revoke_lock_file(key)
        locked = False
        try:
            _lock_application_descriptor(descriptor)
            locked = True
            yield
        finally:
            try:
                if locked:
                    _unlock_application_descriptor(descriptor)
            finally:
                try:
                    os.close(descriptor)
                finally:
                    boundary.close()


def _open_export_revoke_lock_file(
    key: str,
) -> tuple[int, _PinnedOutputDirectory]:
    lock_root = Path(tempfile.gettempdir()) / "ai-actuary-workflow-lab-revoke-locks"
    boundary = _PinnedOutputDirectory.open_boundary(lock_root)
    descriptor = -1
    try:
        metadata = os.lstat(lock_root)
        _reject_link_or_reparse(metadata, lock_root, "integrity")
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkflowLabError(
                "export_revoke_lock_invalid",
                "The server-owned export revoke lock root is invalid.",
                stage="integrity",
            )
        if os.name != "nt" and (
            metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise WorkflowLabError(
                "export_revoke_lock_invalid",
                "The export revoke lock root must be private to the server identity.",
                stage="integrity",
            )
        lock_name = f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.lock"
        lock_path = lock_root / lock_name
        if os.name == "nt":
            descriptor = _open_windows_lock_descriptor(lock_path)
        else:
            assert boundary.descriptor is not None
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                lock_name,
                flags,
                0o600,
                dir_fd=boundary.descriptor,
            )
        opened = os.fstat(descriptor)
        path_metadata = (
            os.lstat(lock_path)
            if os.name == "nt"
            else os.stat(
                lock_name,
                dir_fd=boundary.descriptor,
                follow_symlinks=False,
            )
        )
        _reject_link_or_reparse(path_metadata, lock_path, "integrity")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
            or (os.name != "nt" and opened.st_uid != os.getuid())
        ):
            raise WorkflowLabError(
                "export_revoke_lock_invalid",
                "The per-export revoke lock object is unsafe.",
                stage="integrity",
            )
        boundary.verify()
        return descriptor, boundary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        boundary.close()
        raise


def _open_application_lock_file(key: str) -> int:
    lock_root = Path(tempfile.gettempdir()) / "ai-actuary-workflow-lab-locks"
    try:
        lock_root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = os.lstat(lock_root)
    _reject_link_or_reparse(metadata, lock_root, "preflight")
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkflowLabError(
            "draft_lock_invalid",
            "The server-owned Workflow Lab lock root is invalid.",
            stage="preflight",
        )
    if os.name != "nt" and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WorkflowLabError(
            "draft_lock_invalid",
            "The Workflow Lab lock root must be private to the server identity.",
            stage="preflight",
        )
    lock_path = lock_root / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    opened = os.fstat(descriptor)
    path_metadata = os.lstat(lock_path)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
        or (os.name != "nt" and opened.st_uid != os.getuid())
    ):
        os.close(descriptor)
        raise WorkflowLabError(
            "draft_lock_invalid",
            "The Workflow Lab per-app lock object is unsafe.",
            stage="preflight",
        )
    return descriptor


def _lock_application_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_application_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _published_tree_digest(
    declarative: Mapping[str, bytes],
    retained: Mapping[str, bytes],
) -> str:
    return _tree_digest(
        {
            **{f"declarative/{name}": content for name, content in declarative.items()},
            **{f"retained/{name}": content for name, content in retained.items()},
        }
    )


def _tree_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(f"{DIGEST_ALGORITHM}\n".encode("ascii"))
    for name, content in sorted(files.items()):
        label = PurePosixPath(name).as_posix().encode("utf-8")
        digest.update(len(label).to_bytes(4, "big"))
        digest.update(label)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


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


def _windows_object_handle_identity(
    handle: int, *, require_directory: bool = False
) -> tuple[int, int, bytes]:
    import ctypes
    from ctypes import wintypes

    get_legacy, get_extended, legacy_type, file_id_type = _windows_identity_api()
    legacy = legacy_type()
    if not get_legacy(wintypes.HANDLE(handle), ctypes.byref(legacy)):
        raise WorkflowLabError(
            "output_tree_changed",
            "Unable to verify a pinned Windows directory identity.",
            stage="export",
        )
    if (
        require_directory and not legacy.FileAttributes & 0x10
    ) or legacy.FileAttributes & _REPARSE_ATTRIBUTE:
        raise WorkflowLabError(
            "output_tree_changed",
            "Pinned Windows output handle has an unexpected object type.",
            stage="export",
        )
    file_id = file_id_type()
    if not get_extended(
        wintypes.HANDLE(handle),
        18,
        ctypes.byref(file_id),
        ctypes.sizeof(file_id),
    ):
        raise WorkflowLabError(
            "output_tree_changed",
            "Unable to verify a pinned Windows directory file ID.",
            stage="export",
        )
    file_index = (int(legacy.FileIndexHigh) << 32) | int(legacy.FileIndexLow)
    return (
        int(legacy.VolumeSerialNumber),
        file_index,
        bytes(file_id.FileId.Identifier),
    )


def _windows_directory_handle_identity(handle: int) -> tuple[int, int, bytes]:
    return _windows_object_handle_identity(handle, require_directory=True)


def _receipt_identity_from_descriptor(descriptor: int) -> _ReceiptObjectIdentity:
    metadata = os.fstat(descriptor)
    windows_file_id: bytes | None = None
    device = int(metadata.st_dev)
    inode = int(metadata.st_ino)
    if os.name == "nt":
        import msvcrt

        volume, file_index, windows_file_id = _windows_object_handle_identity(
            msvcrt.get_osfhandle(descriptor)
        )
        device = volume
        inode = file_index
    return _ReceiptObjectIdentity(device, inode, windows_file_id)


def _receipt_identity_from_windows_handle(handle: int) -> _ReceiptObjectIdentity:
    volume, file_index, windows_file_id = _windows_object_handle_identity(handle)
    return _ReceiptObjectIdentity(volume, file_index, windows_file_id)


def _receipt_identity_from_read_pin(
    pin: _PinnedReadDirectory,
) -> _ReceiptObjectIdentity:
    if pin.windows_identity is not None:
        return _ReceiptObjectIdentity(*pin.windows_identity)
    return _ReceiptObjectIdentity(pin.identity.device, pin.identity.inode, None)


def _windows_directory_api() -> tuple[Any, Any, Any, type[Any], type[Any]]:
    import ctypes
    from ctypes import wintypes

    global _WINDOWS_DIRECTORY_API
    if _WINDOWS_DIRECTORY_API is not None:
        return _WINDOWS_DIRECTORY_API

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

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
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    _WINDOWS_DIRECTORY_API = (
        create_file,
        get_info,
        close_handle,
        _FileAttributeTagInfo,
        wintypes.HANDLE,
    )
    return _WINDOWS_DIRECTORY_API


def _open_windows_directory_handle(path: Path) -> int:
    import ctypes

    create_file, get_info, close_handle, info_type, _ = _windows_directory_api()
    handle = create_file(
        str(path),
        0x80000000,
        0x1 | 0x2,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise WorkflowLabError(
            "export_directory_invalid",
            f"Unable to pin Windows output directory: {path}",
            stage="export",
        )
    info = info_type()
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        close_handle(handle)
        raise WorkflowLabError(
            "export_directory_invalid",
            f"Unable to inspect Windows output directory handle: {path}",
            stage="export",
        )
    if not info.FileAttributes & 0x10 or info.FileAttributes & _REPARSE_ATTRIBUTE:
        close_handle(handle)
        raise WorkflowLabError(
            "reparse_point_forbidden",
            f"Windows output directory is a reparse point: {path}",
            stage="export",
        )
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    _, _, close_handle, _, handle_type = _windows_directory_api()
    close_handle(handle_type(handle))


def _duplicate_windows_handle(handle: int) -> int:
    if os.name != "nt":
        raise AssertionError("Windows handles can only be duplicated on Windows.")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    duplicate = kernel32.DuplicateHandle
    duplicate.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    duplicate.restype = wintypes.BOOL
    process = get_current_process()
    result = wintypes.HANDLE()
    if not duplicate(
        process,
        wintypes.HANDLE(handle),
        process,
        ctypes.byref(result),
        0,
        False,
        0x2,  # DUPLICATE_SAME_ACCESS
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(result.value)


def _open_windows_existing_descriptor(path: Path) -> int:
    import ctypes
    import msvcrt
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
        0x80000000 | 0x00010000 | 0x00000100,  # READ | DELETE | WRITE_ATTRIBUTES
        0x1,  # FILE_SHARE_READ: deny new writers and delete/rename while pinned
        None,
        3,
        0x80 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise WorkflowLabError(
            "export_verify_failed",
            f"Unable to pin committed Windows output: {path.name}",
            stage="export",
        )
    try:
        return msvcrt.open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _open_windows_new_output_descriptor(path: Path, *, stage: str) -> int:
    import ctypes
    import msvcrt
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
        str(Path(path).absolute()),
        0x80000000 | 0x40000000 | 0x00010000 | 0x00000100,
        0x1,  # allow readers only; deny write/delete/rename for the proof lifetime
        None,
        1,  # CREATE_NEW
        0x80 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise WorkflowLabError(
                "export_object_exists",
                f"Refusing to overwrite existing output object: {path.name}",
                stage=stage,
            )
        raise WorkflowLabError(
            "export_commit_failed",
            f"Unable to create an exclusive Windows output object: {path.name}",
            stage=stage,
        ) from ctypes.WinError(error)
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _open_windows_lock_descriptor(path: Path) -> int:
    import ctypes
    import msvcrt
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
        str(Path(path).absolute()),
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x1 | 0x2,  # share read/write but deny delete or pathname replacement
        None,
        4,  # OPEN_ALWAYS
        0x80 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise WorkflowLabError(
            "export_revoke_lock_invalid",
            "Unable to pin the per-export Windows revoke lock object.",
            stage="integrity",
        ) from ctypes.WinError(ctypes.get_last_error())
    try:
        _windows_object_handle_identity(int(handle))
        return msvcrt.open_osfhandle(
            int(handle), os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _write_and_verify_descriptor(
    descriptor: int,
    content: bytes,
    *,
    name: str,
    stage: str,
) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Output write made no progress.")
        view = view[written:]
    os.fsync(descriptor)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise WorkflowLabError(
            "export_object_invalid",
            f"New output object is not an exclusive regular file: {name}",
            stage=stage,
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    if size != len(content) or digest.digest() != hashlib.sha256(content).digest():
        raise WorkflowLabError(
            "export_verify_failed",
            f"Output staging verification failed: {name}",
            stage=stage,
        )


def _mark_windows_file_delete(descriptor: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_information.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(descriptor)
    basic = _FileBasicInfo()
    if not get_information(
        wintypes.HANDLE(handle),
        0,
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if basic.FileAttributes & 0x1:
        basic.FileAttributes &= ~0x1
        if not set_information(
            wintypes.HANDLE(handle),
            0,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    information = _FileDispositionInfo(True)
    if not set_information(
        wintypes.HANDLE(handle),
        4,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _write_exclusive_guarded(
    directory: _PinnedOutputDirectory,
    name: str,
    content: bytes,
    *,
    stage: str,
) -> _PinnedOutputFile:
    if not _PORTABLE_BASENAME.fullmatch(name) or name in {".", ".."}:
        raise WorkflowLabError(
            "export_path_escape",
            f"Unsafe output filename: {name!r}",
            stage=stage,
        )
    if os.name == "nt":
        final_path = directory.path / name
        descriptor = _open_windows_new_output_descriptor(final_path, stage=stage)
        try:
            _write_and_verify_descriptor(
                descriptor,
                content,
                name=name,
                stage=stage,
            )
            metadata = os.fstat(descriptor)
            result = _PinnedOutputFile(
                directory,
                name,
                descriptor,
                (int(metadata.st_dev), int(metadata.st_ino)),
                content,
            )
            result.verify()
            return result
        except BaseException:
            try:
                _mark_windows_file_delete(descriptor)
            finally:
                os.close(descriptor)
            raise
    pending_name = f".{name}.{secrets.token_hex(8)}.pending"
    assert directory.descriptor is not None
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(pending_name, flags, 0o600, dir_fd=directory.descriptor)
    committed = False
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Output write made no progress.")
            view = view[written:]
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise WorkflowLabError(
                "export_object_invalid",
                f"Output staging object is not an exclusive regular file: {name}",
                stage=stage,
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        verified = b""
        while len(verified) < len(content):
            chunk = os.read(descriptor, min(65536, len(content) - len(verified)))
            if not chunk:
                break
            verified += chunk
        if verified != content or os.read(descriptor, 1):
            raise WorkflowLabError(
                "export_verify_failed",
                f"Output staging verification failed: {name}",
                stage=stage,
            )
        try:
            os.link(
                pending_name,
                name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise WorkflowLabError(
                "export_object_exists",
                f"Refusing to overwrite existing output object: {name}",
                stage=stage,
            ) from exc
        os.unlink(pending_name, dir_fd=directory.descriptor)
        committed = True
        metadata = os.fstat(descriptor)
        result = _PinnedOutputFile(
            directory,
            name,
            descriptor,
            (int(metadata.st_dev), int(metadata.st_ino)),
            content,
        )
        result.verify()
        return result
    except BaseException:
        os.close(descriptor)
        if not committed:
            try:
                os.unlink(pending_name, dir_fd=directory.descriptor)
            except FileNotFoundError:
                pass
        raise


def _build_patch(
    app_name: str,
    published: Mapping[str, bytes],
    candidate: Mapping[str, bytes],
) -> bytes:
    chunks: list[str] = []
    app_root = _PUBLISHED_REPO_PREFIX / app_name
    for name in sorted(set(published) | set(candidate)):
        before_exists = name in published
        after_exists = name in candidate
        before = _text_lines(published.get(name, b""))
        after = _text_lines(candidate.get(name, b""))
        if before == after:
            continue
        diff = difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{app_root / name}" if before_exists else "/dev/null",
            tofile=f"b/{app_root / name}" if after_exists else "/dev/null",
            lineterm="\n",
        )
        for line in diff:
            if line.endswith("\n"):
                chunks.append(line)
            else:
                chunks.append(f"{line}\n")
                if line.startswith((" ", "+", "-")):
                    chunks.append("\\ No newline at end of file\n")
    return "".join(chunks).replace("\r\n", "\n").encode("utf-8")


def _text_lines(content: bytes) -> list[str]:
    text = content.decode("utf-8", errors="strict").replace("\r\n", "\n")
    return text.splitlines(keepends=True)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _write_exclusive(path: Path, content: bytes, *, stage: str = "export") -> None:
    path = Path(path).absolute()
    if os.name == "nt":
        descriptor = _open_windows_new_output_descriptor(path, stage=stage)
        try:
            _write_and_verify_descriptor(
                descriptor,
                content,
                name=path.name,
                stage=stage,
            )
            before = os.lstat(path)
            opened = os.fstat(descriptor)
            _reject_link_or_reparse(before, path, stage)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or opened.st_nlink != 1
                or (int(before.st_dev), int(before.st_ino))
                != (int(opened.st_dev), int(opened.st_ino))
                or _has_windows_ads(path)
            ):
                raise WorkflowLabError(
                    "export_verify_failed",
                    f"Committed Windows output identity changed: {path.name}",
                    stage=stage,
                )
        except BaseException:
            try:
                _mark_windows_file_delete(descriptor)
            finally:
                os.close(descriptor)
            raise
        os.close(descriptor)
        return

    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = os.open(path.parent, directory_flags)
    pending_name = f".{path.name}.{secrets.token_hex(8)}.pending"
    descriptor = -1
    linked_identity: tuple[int, int] | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(pending_name, flags, 0o600, dir_fd=parent_descriptor)
        except FileExistsError as exc:
            raise WorkflowLabError(
                "export_object_exists",
                f"Refusing to reuse an existing output staging object: {path.name}",
                stage=stage,
            ) from exc
        _write_and_verify_descriptor(
            descriptor,
            content,
            name=path.name,
            stage=stage,
        )
        original = os.fstat(descriptor)
        try:
            os.link(
                pending_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise WorkflowLabError(
                "export_object_exists",
                f"Refusing to overwrite existing output object: {path.name}",
                stage=stage,
            ) from exc
        except OSError as exc:
            raise WorkflowLabError(
                "export_commit_failed",
                f"Unable to atomically commit output object: {path.name}",
                stage=stage,
            ) from exc
        linked = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        linked_identity = (int(linked.st_dev), int(linked.st_ino))
        if linked_identity != (int(original.st_dev), int(original.st_ino)):
            raise WorkflowLabError(
                "output_tree_changed",
                f"Committed output did not retain its pinned staging identity: {path.name}",
                stage=stage,
            )
        os.unlink(pending_name, dir_fd=parent_descriptor)
        final = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (
            (int(final.st_dev), int(final.st_ino)) != linked_identity
            or (int(opened.st_dev), int(opened.st_ino)) != linked_identity
            or final.st_nlink != 1
            or opened.st_nlink != 1
        ):
            raise WorkflowLabError(
                "output_tree_changed",
                f"Committed output identity changed before release: {path.name}",
                stage=stage,
            )
    except BaseException:
        if linked_identity is not None:
            try:
                current = os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if (int(current.st_dev), int(current.st_ino)) == linked_identity:
                    os.unlink(path.name, dir_fd=parent_descriptor)
        try:
            os.unlink(pending_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _secure_mkdirs(target: Path, boundary: Path) -> None:
    target = target.absolute()
    boundary = boundary.absolute()
    try:
        relative = target.relative_to(boundary)
    except ValueError as exc:
        raise WorkflowLabError(
            "export_path_escape",
            "Server output path escapes its owned state root.",
            stage="export",
        ) from exc
    boundary.mkdir(parents=True, exist_ok=True)
    boundary_metadata = os.lstat(boundary)
    _reject_link_or_reparse(boundary_metadata, boundary, "export")
    if not stat.S_ISDIR(boundary_metadata.st_mode):
        raise WorkflowLabError(
            "export_directory_invalid",
            f"Server output boundary is not a directory: {boundary}",
            stage="export",
        )
    current = boundary
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        metadata = os.lstat(current)
        _reject_link_or_reparse(metadata, current, "export")
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkflowLabError(
                "export_directory_invalid",
                f"Server output component is not a directory: {current}",
                stage="export",
            )


def _validate_materialized_read_only(root: Path) -> None:
    entries, directories = _scan_plain_entries(root, stage="materialize")
    paths = [root, *(root / relative for relative in directories if relative != ".")]
    paths.extend(entries.values())
    for path in paths:
        metadata = os.lstat(path)
        _reject_link_or_reparse(metadata, path, "materialize")
        if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise WorkflowLabError(
                "materialized_permissions_changed",
                f"Materialized workflow object is writable: {path.name}",
                stage="materialize",
            )
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if os.name == "nt" and stat.S_ISREG(metadata.st_mode) and not attributes & 0x1:
            raise WorkflowLabError(
                "materialized_permissions_changed",
                f"Materialized workflow file lost its read-only attribute: {path.name}",
                stage="materialize",
            )
    if os.name == "nt":
        _validate_windows_materialized_security(paths)


def _harden_windows_materialized_tree(root: Path) -> None:
    owner_sid = _windows_current_user_sid()
    commands = (
        (
            str(root),
            "/inheritance:r",
            "/grant:r",
            f"*{owner_sid}:(OI)(CI)(F)",
            "*S-1-5-32-545:(OI)(CI)(RX)",
            "/C",
        ),
        (str(root / "*"), "/reset", "/T", "/C"),
        (str(root), "/setintegritylevel", "(OI)(CI)M", "/T", "/C"),
    )
    for arguments in commands:
        result = subprocess.run(
            ["icacls", *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowLabError(
                "materialized_security_failed",
                "Unable to establish the Windows materialized consumer boundary.",
                stage="materialize",
            )


def _validate_windows_materialized_security(paths: list[Path]) -> None:
    owner_sid = _windows_current_user_sid()
    for index, path in enumerate(paths):
        actual_owner, sddl = _windows_security_sddl(path)
        metadata = os.lstat(path)
        _reject_link_or_reparse(metadata, path, "materialize")
        if not _windows_materialized_sddl_is_valid(
            actual_owner=actual_owner,
            owner_sid=owner_sid,
            sddl=sddl,
            is_root=index == 0,
            is_directory=stat.S_ISDIR(metadata.st_mode),
        ):
            raise WorkflowLabError(
                "materialized_security_changed",
                f"Windows materialized owner/ACL/integrity label changed: {path.name}",
                stage="materialize",
            )


def _windows_materialized_sddl_is_valid(
    *,
    actual_owner: str,
    owner_sid: str,
    sddl: str,
    is_root: bool,
    is_directory: bool,
) -> bool:
    if actual_owner != owner_sid:
        return False
    dacl = _parse_sddl_section(sddl, "D")
    label = _parse_sddl_section(sddl, "S")
    if dacl is None or label is None:
        return False
    dacl_flags, dacl_aces = dacl
    label_flags, label_aces = label
    del label_flags
    if not _valid_dacl_control_flags(dacl_flags, is_root=is_root):
        return False
    if len(dacl_aces) != 2 or any(ace[0] != "A" for ace in dacl_aces):
        return False

    owner_found = False
    consumer_found = False
    for _, raw_flags, raw_rights, object_guid, inherited_guid, sid in dacl_aces:
        if object_guid or inherited_guid:
            return False
        ace_flags = _parse_sddl_ace_flags(raw_flags)
        if ace_flags is None or not ace_flags <= {"OI", "CI", "ID"}:
            return False
        if is_root and ("ID" in ace_flags or not {"OI", "CI"} <= ace_flags):
            return False
        if is_directory and not {"OI", "CI"} <= ace_flags:
            return False
        rights = _parse_sddl_file_rights(raw_rights)
        if sid == owner_sid and rights == 0x1F01FF:
            if owner_found:
                return False
            owner_found = True
        elif sid in {"BU", "S-1-5-32-545"} and rights in {0x120089, 0x1200A9}:
            if consumer_found:
                return False
            consumer_found = True
        else:
            return False

    if not owner_found or not consumer_found or len(label_aces) != 1:
        return False
    ace_type, raw_flags, rights, object_guid, inherited_guid, sid = label_aces[0]
    if (
        ace_type != "ML"
        or rights != "NW"
        or object_guid
        or inherited_guid
        or sid not in {"ME", "S-1-16-8192"}
    ):
        return False
    label_ace_flags = _parse_sddl_ace_flags(raw_flags)
    if label_ace_flags is None or not label_ace_flags <= {"OI", "CI", "ID"}:
        return False
    if is_root and ("ID" in label_ace_flags or not {"OI", "CI"} <= label_ace_flags):
        return False
    return not is_directory or {"OI", "CI"} <= label_ace_flags


def _parse_sddl_section(
    sddl: str, section: str
) -> tuple[str, list[tuple[str, str, str, str, str, str]]] | None:
    marker = f"{section}:"
    start = sddl.find(marker)
    if start < 0:
        return None
    start += len(marker)
    endings = [
        position
        for candidate in "OGDS"
        if (position := sddl.find(f"{candidate}:", start)) >= 0
    ]
    end = min(endings, default=len(sddl))
    payload = sddl[start:end]
    ace_start = payload.find("(")
    if ace_start < 0:
        return payload, []
    flags = payload[:ace_start]
    raw_aces = re.findall(r"\(([^()]*)\)", payload[ace_start:])
    if "".join(f"({ace})" for ace in raw_aces) != payload[ace_start:]:
        return None
    aces: list[tuple[str, str, str, str, str, str]] = []
    for raw_ace in raw_aces:
        fields = raw_ace.split(";")
        if len(fields) != 6:
            return None
        aces.append((fields[0], fields[1], fields[2], fields[3], fields[4], fields[5]))
    return flags, aces


def _valid_dacl_control_flags(flags: str, *, is_root: bool) -> bool:
    remainder = flags.replace("AI", "").replace("AR", "")
    if remainder not in {"", "P"}:
        return False
    if is_root:
        return remainder == "P"
    return remainder == "" and "AI" in flags


def _parse_sddl_ace_flags(flags: str) -> set[str] | None:
    if len(flags) % 2:
        return None
    return {flags[index : index + 2] for index in range(0, len(flags), 2)}


def _parse_sddl_file_rights(rights: str) -> int | None:
    if rights.casefold().startswith("0x"):
        try:
            return int(rights, 16)
        except ValueError:
            return None
    aliases = {"FA": 0x1F01FF, "FR": 0x120089, "FX": 0x1200A0}
    if len(rights) % 2:
        return None
    result = 0
    for index in range(0, len(rights), 2):
        value = aliases.get(rights[index : index + 2])
        if value is None:
            return None
        result |= value
    return result


def _windows_security_sddl(path: Path) -> tuple[str, str]:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    information = 0x1 | 0x4 | 0x10
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    error = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        information,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        ctypes.byref(sacl),
        ctypes.byref(descriptor),
    )
    if error:
        raise OSError(error, "GetNamedSecurityInfoW")
    owner_text = wintypes.LPWSTR()
    descriptor_text = wintypes.LPWSTR()
    try:
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(owner_text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW(Owner)")
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            1,
            information,
            ctypes.byref(descriptor_text),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "ConvertSecurityDescriptorToString")
        return owner_text.value, descriptor_text.value
    finally:
        if descriptor_text:
            kernel32.LocalFree(descriptor_text)
        if owner_text:
            kernel32.LocalFree(owner_text)
        if descriptor:
            kernel32.LocalFree(descriptor)


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user = 1
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(required))
        buffer = (ctypes.c_ubyte * required.value)()
        if not advapi32.GetTokenInformation(
            token,
            token_user,
            ctypes.byref(buffer),
            required,
            ctypes.byref(required),
        ):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation(TokenUser)")
        sid_pointer = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_void_p)
        ).contents.value
        string_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(string_sid)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
        try:
            return string_sid.value
        finally:
            kernel32.LocalFree(string_sid)
    finally:
        kernel32.CloseHandle(token)


def _run_windows_low_integrity_process(arguments: list[str], *, cwd: Path) -> int:
    import ctypes
    from ctypes import wintypes

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TokenMandatoryLabel(ctypes.Structure):
        _fields_ = [("Label", _SidAndAttributes)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source = wintypes.HANDLE()
    restricted = wintypes.HANDLE()
    low_sid = ctypes.c_void_p()
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.SetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    advapi32.SetTokenInformation.restype = wintypes.BOOL
    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfo),
        ctypes.POINTER(_ProcessInformation),
    ]
    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    desired = 0x0001 | 0x0002 | 0x0008 | 0x0080
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), desired, ctypes.byref(source)
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    try:
        if not advapi32.CreateRestrictedToken(
            source,
            0x1,
            0,
            None,
            0,
            None,
            0,
            None,
            ctypes.byref(restricted),
        ):
            raise OSError(ctypes.get_last_error(), "CreateRestrictedToken")
        if not advapi32.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(low_sid)):
            raise OSError(ctypes.get_last_error(), "ConvertStringSidToSidW(Low)")
        label = _TokenMandatoryLabel(_SidAndAttributes(low_sid.value, 0x60))
        length = ctypes.sizeof(label) + advapi32.GetLengthSid(low_sid)
        if not advapi32.SetTokenInformation(
            restricted,
            25,
            ctypes.byref(label),
            length,
        ):
            raise OSError(ctypes.get_last_error(), "SetTokenInformation(Low)")
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        process = _ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
        environment_text = (
            "\0".join(
                f"{key}={value}"
                for key, value in sorted(_offline_environment().items())
            )
            + "\0\0"
        )
        environment = ctypes.create_unicode_buffer(environment_text)
        if not advapi32.CreateProcessAsUserW(
            restricted,
            None,
            command,
            None,
            None,
            False,
            0x08000400,
            environment,
            str(cwd),
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            raise OSError(ctypes.get_last_error(), "CreateProcessAsUserW(Low)")
        try:
            if kernel32.WaitForSingleObject(process.hProcess, 20000) != 0:
                raise TimeoutError("Low-integrity Workflow Lab consumer timed out.")
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(
                process.hProcess, ctypes.byref(exit_code)
            ):
                raise OSError(ctypes.get_last_error(), "GetExitCodeProcess")
            return int(exit_code.value)
        finally:
            kernel32.CloseHandle(process.hThread)
            kernel32.CloseHandle(process.hProcess)
    finally:
        if low_sid:
            kernel32.LocalFree(low_sid)
        if restricted:
            kernel32.CloseHandle(restricted)
        kernel32.CloseHandle(source)


def _remove_owned_tree(target: Path, boundary: Path) -> None:
    try:
        target.absolute().relative_to(boundary.absolute())
    except ValueError:
        return
    try:
        os.lstat(target)
    except FileNotFoundError:
        return
    _remove_path_no_follow(target)


def _remove_path_no_follow(path: Path) -> None:
    metadata = os.lstat(path)
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(metadata.st_mode):
        path.unlink()
        return
    if attributes & _REPARSE_ATTRIBUTE:
        if stat.S_ISDIR(metadata.st_mode):
            os.rmdir(path)
        else:
            path.unlink()
        return
    if not stat.S_ISDIR(metadata.st_mode):
        try:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        path.unlink()
        return
    if os.name != "nt":
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        try:
            _remove_posix_directory_contents(descriptor)
        finally:
            os.close(descriptor)
    else:
        handle = _open_windows_directory_handle(path)
        try:
            for entry in sorted(
                os.scandir(path), key=lambda value: value.name.casefold()
            ):
                _remove_path_no_follow(Path(entry.path))
        finally:
            _close_windows_handle(handle)
    try:
        path.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass
    os.rmdir(path)


def _remove_posix_directory_contents(descriptor: int) -> None:
    os.fchmod(descriptor, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    for name in sorted(os.listdir(descriptor)):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                child = os.open(name, flags, dir_fd=descriptor)
            except OSError:
                continue
            try:
                os.fchmod(child, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                _remove_posix_directory_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _collect_resource_files(
    resource: Any, prefix: str, output: dict[str, bytes]
) -> None:
    for child in sorted(resource.iterdir(), key=lambda item: item.name):
        relative = f"{prefix}/{child.name}".lstrip("/")
        if child.is_dir():
            _collect_resource_files(child, relative, output)
        elif child.name != "__pycache__" and not child.name.endswith((".py", ".pyc")):
            output[relative] = child.read_bytes()


def _read_plain_tree(root: Path) -> dict[str, bytes]:
    initial_entries, initial_directories = _scan_plain_entries(
        root, stage="materialize"
    )
    files: dict[str, bytes] = {}
    identities: dict[str, _PathIdentity] = {}
    for relative, path in initial_entries.items():
        content, identity = _read_pinned_file(
            path,
            stage="materialize",
            fault_hook=lambda event, target: None,
        )
        files[relative] = content
        identities[relative] = identity
    final_entries, final_directories = _scan_plain_entries(root, stage="materialize")
    if (
        set(final_entries) != set(initial_entries)
        or final_directories != initial_directories
    ):
        raise WorkflowLabError(
            "tree_changed",
            "Materialized workflow tree changed while it was verified.",
            stage="materialize",
        )
    for relative, path in final_entries.items():
        if _PathIdentity.from_stat(os.lstat(path)) != identities[relative]:
            raise WorkflowLabError(
                "entry_replaced",
                f"Materialized workflow changed while verified: {relative}",
                stage="materialize",
            )
    return dict(sorted(files.items()))


def _scan_plain_entries(
    root: Path, *, stage: str
) -> tuple[dict[str, Path], dict[str, _PathIdentity]]:
    root_metadata = os.lstat(root)
    _reject_link_or_reparse(root_metadata, root, stage)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise WorkflowLabError(
            "directory_required",
            f"Expected a server-owned directory: {root}",
            stage=stage,
        )
    entries: dict[str, Path] = {}
    directories = {".": _PathIdentity.from_stat(root_metadata)}
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > _MAX_DEPTH:
            raise WorkflowLabError(
                "path_depth_exceeded",
                f"Materialized workflow nesting exceeds {_MAX_DEPTH} levels.",
                stage=stage,
            )
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: item.name.casefold()
            )
        except OSError as exc:
            raise WorkflowLabError(
                "directory_read_failed",
                f"Unable to enumerate materialized workflow directory: {directory}",
                stage=stage,
            ) from exc
        for entry in children:
            path = Path(entry.path)
            metadata = os.lstat(path)
            _reject_link_or_reparse(metadata, path, stage)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                directories[relative] = _PathIdentity.from_stat(metadata)
                stack.append((path, depth + 1))
            else:
                entries[relative] = path
    return dict(sorted(entries.items())), dict(sorted(directories.items()))


def _offline_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "COMSPEC",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "NO_PROXY": "*",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
        }
    )
    return environment


def _collect_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                refs.append(child)
            refs.extend(_collect_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_collect_refs(child))
    return refs


def _next_stage(completed: list[str]) -> str:
    stages = (
        "preflight",
        "safe_yaml",
        "code_references",
        "adk_schema",
        "project_policy",
        "isolated_contract",
    )
    return stages[len(completed)] if len(completed) < len(stages) else "validation"


__all__ = [
    "ADK_VERSION",
    "BUILDER_DECISION",
    "EXECUTABLE_REFERENCE_FIELDS",
    "ExportReceipt",
    "ValidationReport",
    "WorkflowLab",
    "WorkflowLabError",
    "WorkflowLabPaths",
    "load_frozen_agent_config_schema",
]
