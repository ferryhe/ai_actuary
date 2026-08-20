"""Descriptor-pinned, bounded JSON reads for local trusted roots."""

from __future__ import annotations

import errno
import json
import math
import ntpath
import os
import secrets
import stat
from pathlib import Path
from typing import Any


MAX_ARTIFACT_BYTES = 1_000_000
MAX_JSON_DEPTH = 20
MAX_JSON_FIELDS = 5_000
MAX_JSON_NODES = 20_000
MAX_JSON_LIST_LENGTH = 2_000
MAX_JSON_STRING_LENGTH = 100_000
MAX_DIRECTORY_ENTRIES = 1_000


class SafeJsonReadError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class PinnedJsonRoot:
    """Pin one configured directory for a complete logical read operation."""

    def __init__(
        self,
        root: str | Path,
        *,
        namespace: str = "artifact",
        allow_nested: bool = False,
        protect_writes: bool = False,
    ) -> None:
        self.root = Path(os.path.abspath(os.path.expanduser(str(root))))
        self.namespace = namespace
        self.allow_nested = allow_nested
        self.protect_writes = protect_writes
        self._root_descriptor: int | None = None
        self._root_handle: int | None = None

    def __enter__(self) -> "PinnedJsonRoot":
        try:
            if (
                os.name == "posix"
                and hasattr(os, "O_NOFOLLOW")
                and os.open in os.supports_dir_fd
            ):
                self._root_descriptor = _open_trusted_root_posix(
                    self.root,
                    namespace=self.namespace,
                )
            elif os.name == "nt":
                self._root_handle = _open_trusted_root_windows(
                    self.root,
                    namespace=self.namespace,
                    protect_from_replacement=self.protect_writes,
                )
            else:
                raise _read_error(
                    self.namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
        except SafeJsonReadError:
            raise
        except FileNotFoundError as exc:
            raise _read_error(
                self.namespace,
                "missing",
                "Registered JSON artifact is missing.",
                status_code=404,
            ) from exc
        except OSError as exc:
            raise _read_error(
                self.namespace,
                "unreadable",
                "Registered JSON artifact could not be read safely.",
            ) from exc
        return self

    def execution_path(self) -> Path:
        """Return a path anchored to the pinned root for legacy path-based writers."""
        if self._root_descriptor is not None:
            candidate = Path("/proc/self/fd") / str(self._root_descriptor)
            try:
                current = candidate.stat()
                pinned = os.fstat(self._root_descriptor)
            except OSError as exc:
                raise _read_error(
                    self.namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                ) from exc
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise _read_error(
                    self.namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
            return candidate
        if self._root_handle is not None:
            return self.root
        raise RuntimeError("Pinned JSON root is not open.")

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self._root_descriptor is not None:
            os.close(self._root_descriptor)
            self._root_descriptor = None
        if self._root_handle is not None:
            _windows_close_handle(self._root_handle)
            self._root_handle = None

    def read_bounded_json_object(
        self,
        relative_path: str,
        *,
        namespace: str | None = None,
        max_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> dict[str, Any]:
        read_namespace = namespace or self.namespace
        descriptor, metadata = self._open_regular(
            relative_path,
            namespace=read_namespace,
        )
        return _read_bounded_json_descriptor(
            descriptor,
            metadata,
            namespace=read_namespace,
            max_bytes=max_bytes,
        )

    def stat_regular_artifact(
        self,
        relative_path: str,
        *,
        namespace: str | None = None,
    ) -> os.stat_result:
        descriptor, metadata = self._open_regular(
            relative_path,
            namespace=namespace or self.namespace,
        )
        os.close(descriptor)
        return metadata

    def create_directory_exclusive(
        self,
        relative_path: str,
        *,
        namespace: str | None = None,
    ) -> None:
        write_namespace = namespace or self.namespace
        parts = _safe_relative_parts(
            relative_path,
            namespace=write_namespace,
            allow_nested=self.allow_nested,
        )
        parent_descriptor: int | None = None
        parent_handle: int | None = None
        parent_handles: tuple[int, ...] = ()
        directory_handle: int | None = None
        parent_identities: tuple[tuple[int, int], ...] = ()
        created_metadata: os.stat_result | None = None
        completed = False
        try:
            if self._root_descriptor is not None:
                parent_descriptor, parent_identities = _open_relative_parent_posix(
                    self._root_descriptor, parts, namespace=write_namespace
                )
                os.mkdir(parts[-1], mode=0o700, dir_fd=parent_descriptor)
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | os.O_NOFOLLOW
                )
                directory_handle = os.open(
                    parts[-1], directory_flags, dir_fd=parent_descriptor
                )
                created_metadata = os.fstat(directory_handle)
            elif self._root_handle is not None:
                parent_handle, parent_handles = _open_relative_parent_windows(
                    self._root_handle, parts, namespace=write_namespace
                )
                directory_handle = _create_relative_directory_windows(
                    parent_handle, parts[-1], namespace=write_namespace
                )
            else:
                raise RuntimeError("Pinned JSON root is not open.")
            if created_metadata is not None and not stat.S_ISDIR(
                created_metadata.st_mode
            ):
                raise _read_error(
                    write_namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
            if self._root_descriptor is not None:
                _verify_relative_ancestry_posix(
                    self._root_descriptor,
                    parts,
                    parent_identities,
                    namespace=write_namespace,
                )
            self.verify_configured_root_identity(namespace=write_namespace)
            completed = True
        except SafeJsonReadError:
            raise
        except (FileExistsError, OSError, ValueError) as exc:
            raise _read_error(
                write_namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            ) from exc
        finally:
            if directory_handle is not None:
                if not completed:
                    if os.name == "nt":
                        try:
                            _windows_delete_handle(directory_handle)
                        except OSError:
                            pass
                    elif parent_descriptor is not None:
                        try:
                            current = os.stat(
                                parts[-1],
                                dir_fd=parent_descriptor,
                                follow_symlinks=False,
                            )
                            if created_metadata is not None and (
                                current.st_dev,
                                current.st_ino,
                            ) == (created_metadata.st_dev, created_metadata.st_ino):
                                os.rmdir(parts[-1], dir_fd=parent_descriptor)
                        except OSError:
                            pass
                if os.name == "nt":
                    _windows_close_handle(directory_handle)
                else:
                    os.close(directory_handle)
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            for handle in reversed(parent_handles):
                _windows_close_handle(handle)

    def write_json_object_exclusive(
        self,
        relative_path: str,
        payload: dict[str, Any],
        *,
        namespace: str | None = None,
        max_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        write_namespace = namespace or self.namespace
        parts = _safe_relative_parts(
            relative_path,
            namespace=write_namespace,
            allow_nested=self.allow_nested,
        )
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(serialized) > max_bytes:
            raise _read_error(
                write_namespace,
                "size_exceeded",
                "Registered JSON artifact exceeds the size limit.",
                status_code=413,
            )
        temporary_name = f".adk-{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        parent_descriptor: int | None = None
        parent_handle: int | None = None
        parent_handles: tuple[int, ...] = ()
        parent_identities: tuple[tuple[int, int], ...] = ()
        created_metadata: os.stat_result | None = None
        published_descriptor: int | None = None
        published = False
        completed = False
        try:
            if self._root_descriptor is not None:
                parent_descriptor, parent_identities = _open_relative_parent_posix(
                    self._root_descriptor, parts, namespace=write_namespace
                )
                descriptor = _create_anonymous_posix(
                    parent_descriptor,
                    namespace=write_namespace,
                )
            elif self._root_handle is not None:
                parent_handle, parent_handles = _open_relative_parent_windows(
                    self._root_handle, parts, namespace=write_namespace
                )
                descriptor = _create_relative_windows(
                    parent_handle, temporary_name, namespace=write_namespace
                )
            else:
                raise RuntimeError("Pinned JSON root is not open.")
            metadata = os.fstat(descriptor)
            created_metadata = metadata
            expected_initial_links = 0
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != expected_initial_links
            ):
                raise _read_error(
                    write_namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
            if self._root_descriptor is not None:
                _require_posix_descriptor_cleanup_locator(
                    descriptor,
                    namespace=write_namespace,
                )
            view = memoryview(serialized)
            written = 0
            while written < len(view):
                written_now = os.write(descriptor, view[written:])
                if written_now <= 0:
                    raise OSError("Artifact write made no progress")
                written += written_now
            os.fsync(descriptor)
            final_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(final_metadata.st_mode)
                or final_metadata.st_nlink != expected_initial_links
                or (final_metadata.st_dev, final_metadata.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise _read_error(
                    write_namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
            if self._root_descriptor is not None:
                assert parent_descriptor is not None
                published_descriptor = _publish_anonymous_posix(
                    descriptor,
                    parent_descriptor,
                    parts[-1],
                )
                published = True
            else:
                assert parent_handle is not None
                _windows_cancel_delete_descriptor(descriptor)
                _replace_relative_windows(
                    descriptor,
                    parent_handle,
                    parts[-1],
                    replace_existing=False,
                )
                published = True
            _verify_published_file_identity(
                descriptor,
                parent_descriptor=parent_descriptor,
                parent_handle=parent_handle,
                name=parts[-1],
                metadata=metadata,
                namespace=write_namespace,
            )
            if self._root_descriptor is not None:
                _verify_relative_ancestry_posix(
                    self._root_descriptor,
                    parts,
                    parent_identities,
                    namespace=write_namespace,
                )
            self.verify_configured_root_identity(namespace=write_namespace)
            completed = True
        except SafeJsonReadError:
            raise
        except (FileExistsError, OSError, ValueError) as exc:
            raise _read_error(
                write_namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            ) from exc
        finally:
            if descriptor is not None:
                if not completed:
                    _cleanup_created_file(
                        published_descriptor or descriptor,
                        parent_descriptor=parent_descriptor,
                        name=(
                            parts[-1]
                            if published
                            else temporary_name
                            if self._root_handle is not None
                            else ""
                        ),
                        metadata=created_metadata,
                    )
                if published_descriptor is not None:
                    os.close(published_descriptor)
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            for handle in reversed(parent_handles):
                _windows_close_handle(handle)

    def write_json_object_atomic(
        self,
        relative_path: str,
        payload: dict[str, Any],
        *,
        namespace: str | None = None,
        max_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        write_namespace = namespace or self.namespace
        parts = _safe_relative_parts(
            relative_path,
            namespace=write_namespace,
            allow_nested=self.allow_nested,
        )
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(serialized) > max_bytes:
            raise _read_error(
                write_namespace,
                "size_exceeded",
                "Registered JSON artifact exceeds the size limit.",
                status_code=413,
            )
        temporary_name = f".adk-{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        parent_descriptor: int | None = None
        parent_handle: int | None = None
        parent_handles: tuple[int, ...] = ()
        parent_identities: tuple[tuple[int, int], ...] = ()
        created_metadata: os.stat_result | None = None
        published_descriptor: int | None = None
        staged_name_published = False
        published = False
        completed = False
        try:
            if self._root_descriptor is not None:
                parent_descriptor, parent_identities = _open_relative_parent_posix(
                    self._root_descriptor, parts, namespace=write_namespace
                )
                descriptor = _create_anonymous_posix(
                    parent_descriptor,
                    namespace=write_namespace,
                )
            elif self._root_handle is not None:
                parent_handle, parent_handles = _open_relative_parent_windows(
                    self._root_handle, parts, namespace=write_namespace
                )
                descriptor = _create_relative_windows(
                    parent_handle,
                    temporary_name,
                    namespace=write_namespace,
                )
            else:
                raise RuntimeError("Pinned JSON root is not open.")
            metadata = os.fstat(descriptor)
            created_metadata = metadata
            expected_initial_links = 0
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != expected_initial_links
            ):
                raise _read_error(
                    write_namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
            if self._root_descriptor is not None:
                _require_posix_descriptor_cleanup_locator(
                    descriptor,
                    namespace=write_namespace,
                )
            view = memoryview(serialized)
            written = 0
            while written < len(view):
                written_now = os.write(descriptor, view[written:])
                if written_now <= 0:
                    raise OSError("Artifact write made no progress")
                written += written_now
            os.fsync(descriptor)
            if self._root_descriptor is not None:
                assert parent_descriptor is not None
                published_descriptor = _publish_anonymous_posix(
                    descriptor,
                    parent_descriptor,
                    temporary_name,
                )
                staged_name_published = True
                os.replace(
                    temporary_name,
                    parts[-1],
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            else:
                assert parent_handle is not None
                _windows_cancel_delete_descriptor(descriptor)
                _replace_relative_windows(
                    descriptor,
                    parent_handle,
                    parts[-1],
                )
            published = True
            final_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(final_metadata.st_mode)
                or final_metadata.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino)
                != (final_metadata.st_dev, final_metadata.st_ino)
            ):
                raise _read_error(
                    write_namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
            _verify_published_file_identity(
                descriptor,
                parent_descriptor=parent_descriptor,
                parent_handle=parent_handle,
                name=parts[-1],
                metadata=metadata,
                namespace=write_namespace,
            )
            if self._root_descriptor is not None:
                _verify_relative_ancestry_posix(
                    self._root_descriptor,
                    parts,
                    parent_identities,
                    namespace=write_namespace,
                )
            self.verify_configured_root_identity(namespace=write_namespace)
            completed = True
        except SafeJsonReadError:
            raise
        except (FileExistsError, OSError, ValueError) as exc:
            raise _read_error(
                write_namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            ) from exc
        finally:
            if descriptor is not None:
                if not completed:
                    _cleanup_created_file(
                        published_descriptor or descriptor,
                        parent_descriptor=parent_descriptor,
                        name=(
                            parts[-1]
                            if published
                            else temporary_name
                            if staged_name_published or self._root_handle is not None
                            else ""
                        ),
                        metadata=created_metadata,
                    )
                if published_descriptor is not None:
                    os.close(published_descriptor)
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            for handle in reversed(parent_handles):
                _windows_close_handle(handle)

    def verify_configured_root_identity(self, *, namespace: str | None = None) -> None:
        verify_namespace = namespace or self.namespace
        if self._root_descriptor is not None:
            reopened = _open_trusted_root_posix(self.root, namespace=verify_namespace)
            try:
                pinned = os.fstat(self._root_descriptor)
                current = os.fstat(reopened)
                if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
                    raise _read_error(
                        verify_namespace,
                        "path_rejected",
                        "Registered artifact path failed safety validation.",
                    )
            finally:
                os.close(reopened)
            return
        if self._root_handle is not None:
            reopened = _open_trusted_root_windows(self.root, namespace=verify_namespace)
            try:
                if _windows_file_identity(self._root_handle) != _windows_file_identity(reopened):
                    raise _read_error(
                        verify_namespace,
                        "path_rejected",
                        "Registered artifact path failed safety validation.",
                    )
            finally:
                _windows_close_handle(reopened)
            return
        raise RuntimeError("Pinned JSON root is not open.")

    def list_directories(
        self,
        *,
        max_entries: int = MAX_DIRECTORY_ENTRIES,
        namespace: str | None = None,
    ) -> list[str]:
        list_namespace = namespace or self.namespace
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
        ):
            raise ValueError("max_entries must be a positive integer")
        try:
            if self._root_descriptor is not None:
                names = _list_directories_posix(
                    self._root_descriptor,
                    namespace=list_namespace,
                    max_entries=max_entries,
                )
            elif self._root_handle is not None:
                names = _list_directories_windows(
                    self._root_handle,
                    namespace=list_namespace,
                    max_entries=max_entries,
                )
            else:
                raise RuntimeError("Pinned JSON root is not open.")
        except SafeJsonReadError:
            raise
        except OSError as exc:
            raise _read_error(
                list_namespace,
                "unreadable",
                "Registered JSON artifact could not be read safely.",
            ) from exc
        return sorted(names)

    def _open_regular(
        self,
        relative_path: str,
        *,
        namespace: str,
    ) -> tuple[int, os.stat_result]:
        parts = _safe_relative_parts(
            relative_path,
            namespace=namespace,
            allow_nested=self.allow_nested,
        )
        try:
            if self._root_descriptor is not None:
                descriptor = _open_relative_posix(
                    self._root_descriptor,
                    parts,
                    namespace=namespace,
                )
            elif self._root_handle is not None:
                descriptor = _open_relative_windows(
                    self._root_handle,
                    parts,
                    namespace=namespace,
                )
            else:
                raise RuntimeError("Pinned JSON root is not open.")
        except SafeJsonReadError:
            raise
        except FileNotFoundError as exc:
            raise _read_error(
                namespace,
                "missing",
                "Registered JSON artifact is missing.",
                status_code=404,
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}:
                raise _read_error(
                    namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                ) from exc
            raise _read_error(
                namespace,
                "unreadable",
                "Registered JSON artifact could not be read safely.",
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise _read_error(
                    namespace,
                    "not_regular",
                    "Registered artifact must be a regular file.",
                )
            return descriptor, metadata
        except BaseException:
            os.close(descriptor)
            raise


def read_bounded_json_object(
    root: str | Path,
    relative_path: str,
    *,
    namespace: str = "artifact",
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> dict[str, Any]:
    with PinnedJsonRoot(root, namespace=namespace) as pinned_root:
        return pinned_root.read_bounded_json_object(
            relative_path,
            namespace=namespace,
            max_bytes=max_bytes,
        )


def stat_regular_artifact(
    root: str | Path,
    relative_path: str,
    *,
    namespace: str = "artifact",
) -> os.stat_result:
    with PinnedJsonRoot(root, namespace=namespace) as pinned_root:
        return pinned_root.stat_regular_artifact(relative_path, namespace=namespace)


def write_json_object_exclusive(
    root: str | Path,
    relative_path: str,
    payload: dict[str, Any],
    *,
    namespace: str = "artifact",
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> None:
    with PinnedJsonRoot(
        root, namespace=namespace, protect_writes=True
    ) as pinned_root:
        pinned_root.write_json_object_exclusive(
            relative_path,
            payload,
            namespace=namespace,
            max_bytes=max_bytes,
        )


def write_json_object_atomic(
    root: str | Path,
    relative_path: str,
    payload: dict[str, Any],
    *,
    namespace: str = "artifact",
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> None:
    with PinnedJsonRoot(
        root, namespace=namespace, protect_writes=True
    ) as pinned_root:
        pinned_root.write_json_object_atomic(
            relative_path,
            payload,
            namespace=namespace,
            max_bytes=max_bytes,
        )


def _read_bounded_json_descriptor(
    descriptor: int,
    metadata: os.stat_result,
    *,
    namespace: str,
    max_bytes: int,
) -> dict[str, Any]:
    try:
        if metadata.st_size > max_bytes:
            raise _read_error(
                namespace,
                "size_exceeded",
                "Registered JSON artifact exceeds the size limit.",
                status_code=413,
            )
        content = bytearray()
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, max_bytes + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise _read_error(
                    namespace,
                    "size_exceeded",
                    "Registered JSON artifact exceeds the size limit.",
                    status_code=413,
                )
    except SafeJsonReadError:
        raise
    except OSError as exc:
        raise _read_error(
            namespace,
            "unreadable",
            "Registered JSON artifact could not be read safely.",
        ) from exc
    finally:
        os.close(descriptor)

    try:
        text = bytes(content).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _read_error(
            namespace,
            "invalid_encoding",
            "Registered JSON artifact is not valid UTF-8.",
        ) from exc
    try:
        payload = json.loads(text, parse_constant=_reject_json_constant)
    except RecursionError as exc:
        raise _read_error(
            namespace,
            "depth_exceeded",
            "Registered JSON artifact is nested too deeply.",
            status_code=422,
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise _read_error(
            namespace,
            "invalid_json",
            "Registered artifact is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise _read_error(
            namespace,
            "invalid_shape",
            "Registered artifact must be a JSON object.",
            status_code=422,
        )
    _validate_json_complexity(payload, namespace=namespace)
    return payload


def _safe_relative_parts(
    relative_path: str,
    *,
    namespace: str,
    allow_nested: bool = False,
) -> tuple[str, ...]:
    raw = str(relative_path)
    parts = raw.replace("\\", "/").split("/")
    if (
        not raw
        or (not allow_nested and len(parts) != 1)
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or any(
            part.casefold() in {"?", "??", "device", "globalroot", "unc"}
            for part in parts
        )
    ):
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    return tuple(parts)


def _open_trusted_root_posix(root: Path, *, namespace: str) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    current_fd = os.open(root.anchor or os.sep, directory_flags)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as exc:
        os.close(current_fd)
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}:
            raise _read_error(
                namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            ) from exc
        raise


def _open_relative_posix(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    namespace: str,
) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    current_fd = os.dup(root_descriptor)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        final_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        return os.open(parts[-1], final_flags, dir_fd=current_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}:
            raise _read_error(
                namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            ) from exc
        raise
    finally:
        os.close(current_fd)


def _create_anonymous_posix(
    parent_descriptor: int,
    *,
    namespace: str,
) -> int:
    temporary_flag = getattr(os, "O_TMPFILE", 0)
    if not temporary_flag:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    flags = os.O_RDWR | temporary_flag | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(".", flags, 0o600, dir_fd=parent_descriptor)
    except OSError as exc:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        ) from exc


def _publish_anonymous_posix(
    descriptor: int,
    parent_descriptor: int,
    name: str,
) -> int:
    import ctypes

    at_empty_path = 0x1000
    libc = ctypes.CDLL(None, use_errno=True)
    libc.linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    libc.linkat.restype = ctypes.c_int
    result = libc.linkat(
        descriptor,
        b"",
        parent_descriptor,
        os.fsencode(name),
        at_empty_path,
    )
    if result != 0 and ctypes.get_errno() in {errno.ENOENT, errno.EPERM}:
        at_fdcwd = -100
        at_symlink_follow = 0x400
        result = libc.linkat(
            at_fdcwd,
            os.fsencode(f"/proc/self/fd/{descriptor}"),
            parent_descriptor,
            os.fsencode(name),
            at_symlink_follow,
        )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "Artifact already exists")
        raise OSError(error, "Artifact could not be published safely")
    flags = os.O_WRONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    published_descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        original = os.fstat(descriptor)
        published = os.fstat(published_descriptor)
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or (published.st_dev, published.st_ino)
            != (original.st_dev, original.st_ino)
        ):
            raise OSError("Published artifact identity changed")
        return published_descriptor
    except BaseException:
        os.close(published_descriptor)
        raise


def _verify_published_file_identity(
    descriptor: int,
    *,
    parent_descriptor: int | None,
    parent_handle: int | None,
    name: str,
    metadata: os.stat_result,
    namespace: str,
) -> None:
    if os.name == "posix":
        if parent_descriptor is None:
            raise OSError("Pinned parent descriptor is unavailable")
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise _read_error(
                namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            )
        return
    if os.name == "nt":
        if parent_handle is None:
            raise OSError("Pinned parent handle is unavailable")
        import msvcrt

        expected_path = _windows_normalized_path(
            ntpath.join(_windows_final_path_for_handle(parent_handle), name)
        )
        current_path = _windows_normalized_path(
            _windows_final_path_for_handle(msvcrt.get_osfhandle(descriptor))
        )
        if current_path != expected_path:
            raise _read_error(
                namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            )
        return
    raise OSError("Secure artifact publishing is unavailable")


def _require_posix_descriptor_cleanup_locator(
    descriptor: int,
    *,
    namespace: str,
) -> None:
    try:
        location = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as exc:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        ) from exc
    if not os.path.isabs(location):
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )


def _open_relative_parent_posix(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    namespace: str,
) -> tuple[int, tuple[tuple[int, int], ...]]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    current_fd = os.dup(root_descriptor)
    identities: list[tuple[int, int]] = []
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            metadata = os.fstat(current_fd)
            identities.append((metadata.st_dev, metadata.st_ino))
        return current_fd, tuple(identities)
    except OSError as exc:
        os.close(current_fd)
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}:
            raise _read_error(
                namespace,
                "path_rejected",
                "Registered artifact path failed safety validation.",
            ) from exc
        raise


def _verify_relative_ancestry_posix(
    root_descriptor: int,
    parts: tuple[str, ...],
    expected_identities: tuple[tuple[int, int], ...],
    *,
    namespace: str,
) -> None:
    if len(expected_identities) != len(parts) - 1:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    current_fd = os.dup(root_descriptor)
    try:
        for component, expected_identity in zip(
            parts[:-1], expected_identities, strict=True
        ):
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            metadata = os.fstat(current_fd)
            if (metadata.st_dev, metadata.st_ino) != expected_identity:
                raise _read_error(
                    namespace,
                    "path_rejected",
                    "Registered artifact path failed safety validation.",
                )
    except SafeJsonReadError:
        raise
    except OSError as exc:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        ) from exc
    finally:
        os.close(current_fd)


def _cleanup_created_file(
    descriptor: int,
    *,
    parent_descriptor: int | None,
    name: str,
    metadata: os.stat_result | None,
) -> None:
    if os.name == "nt":
        try:
            _windows_delete_descriptor(descriptor)
        except OSError:
            pass
        return
    if metadata is None:
        return
    if parent_descriptor is not None and name:
        try:
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (
                metadata.st_dev,
                metadata.st_ino,
            ):
                os.unlink(name, dir_fd=parent_descriptor)
        except OSError:
            pass
    try:
        if os.fstat(descriptor).st_nlink > 0:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
    except OSError:
        pass
    _cleanup_relocated_posix_inode(descriptor, metadata)


def _cleanup_relocated_posix_inode(
    descriptor: int,
    metadata: os.stat_result,
) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    for _ in range(3):
        try:
            if os.fstat(descriptor).st_nlink == 0:
                return
            location = os.readlink(f"/proc/self/fd/{descriptor}")
            if location.endswith(" (deleted)"):
                return
            parent_path, name = os.path.split(location)
            if not parent_path or not name:
                return
            parent = os.open(parent_path, directory_flags)
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    os.unlink(name, dir_fd=parent)
            finally:
                os.close(parent)
        except OSError:
            continue


def _list_directories_posix(
    root_descriptor: int,
    *,
    namespace: str,
    max_entries: int,
) -> list[str]:
    names: list[str] = []
    entry_count = 0
    duplicate = os.dup(root_descriptor)
    try:
        with os.scandir(duplicate) as entries:
            for entry in entries:
                if entry.name in {".", ".."}:
                    continue
                entry_count += 1
                if entry_count > max_entries:
                    raise _read_error(
                        namespace,
                        "entry_limit_exceeded",
                        "Registered directory contains too many entries.",
                        status_code=422,
                    )
                try:
                    metadata = os.stat(
                        entry.name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    names.append(entry.name)
    finally:
        try:
            os.close(duplicate)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
    return names


def _open_trusted_root_windows(
    root: Path,
    *,
    namespace: str,
    protect_from_replacement: bool = False,
) -> int:
    if not root.anchor or not root.is_absolute():
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    current_handle = _windows_open_handle(
        Path(root.anchor),
        expect_directory=True,
        namespace=namespace,
    )
    try:
        components = root.parts[1:]
        for index, component in enumerate(components):
            if protect_from_replacement and index == len(components) - 1:
                next_handle = _windows_open_relative_handle(
                    current_handle,
                    component,
                    expect_directory=True,
                    namespace=namespace,
                    share_delete=False,
                )
            else:
                next_handle = _windows_open_relative_handle(
                    current_handle,
                    component,
                    expect_directory=True,
                    namespace=namespace,
                )
            _windows_close_handle(current_handle)
            current_handle = next_handle
        _windows_verify_configured_root_handle(
            current_handle,
            root,
            namespace=namespace,
        )
        return current_handle
    except BaseException:
        _windows_close_handle(current_handle)
        raise


def _open_relative_windows(
    root_handle: int,
    parts: tuple[str, ...],
    *,
    namespace: str,
) -> int:
    import msvcrt

    parent_handles: list[int] = []
    parent_handle = root_handle
    final_handle: int | None = None
    try:
        for component in parts[:-1]:
            parent_handle = _windows_open_relative_handle(
                parent_handle,
                component,
                expect_directory=True,
                namespace=namespace,
            )
            parent_handles.append(parent_handle)
        final_handle = _windows_open_relative_handle(
            parent_handle,
            parts[-1],
            expect_directory=False,
            namespace=namespace,
        )
        descriptor = msvcrt.open_osfhandle(
            final_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        final_handle = None
        return descriptor
    finally:
        if final_handle is not None:
            _windows_close_handle(final_handle)
        for handle in reversed(parent_handles):
            _windows_close_handle(handle)


def _open_relative_parent_windows(
    root_handle: int,
    parts: tuple[str, ...],
    *,
    namespace: str,
) -> tuple[int, tuple[int, ...]]:
    parent_handle = root_handle
    owned_handles: list[int] = []
    try:
        for component in parts[:-1]:
            next_handle = _windows_open_relative_handle(
                parent_handle,
                component,
                expect_directory=True,
                namespace=namespace,
                share_delete=False,
            )
            owned_handles.append(next_handle)
            parent_handle = next_handle
        return parent_handle, tuple(owned_handles)
    except BaseException:
        for handle in reversed(owned_handles):
            _windows_close_handle(handle)
        raise


def _list_directories_windows(
    root_handle: int,
    *,
    namespace: str,
    max_entries: int,
) -> list[str]:
    names: list[str] = []
    for name in _windows_query_directory_names(
        root_handle,
        namespace=namespace,
        max_entries=max_entries,
    ):
        child_handle: int | None = None
        try:
            child_handle = _windows_open_relative_handle(
                root_handle,
                name,
                expect_directory=True,
                namespace=namespace,
            )
        except (FileNotFoundError, OSError, SafeJsonReadError):
            continue
        finally:
            if child_handle is not None:
                _windows_close_handle(child_handle)
        names.append(name)
    return names


def _windows_query_directory_names(
    root_handle: int,
    *,
    namespace: str,
    max_entries: int,
) -> list[str]:
    import ctypes
    from ctypes import wintypes

    file_names_information = 12
    status_no_more_files = 0x80000006
    status_buffer_overflow = 0x80000005
    buffer_size = 64 * 1024

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("status", ctypes.c_void_p),
            ("information", ctypes.c_size_t),
        ]

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtQueryDirectoryFile.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.BOOLEAN,
        ctypes.c_void_p,
        wintypes.BOOLEAN,
    ]
    ntdll.NtQueryDirectoryFile.restype = wintypes.LONG
    ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    names: list[str] = []
    restart_scan = True
    while True:
        buffer = ctypes.create_string_buffer(buffer_size)
        io_status = IoStatusBlock()
        status = int(
            ntdll.NtQueryDirectoryFile(
                wintypes.HANDLE(root_handle),
                None,
                None,
                None,
                ctypes.byref(io_status),
                buffer,
                buffer_size,
                file_names_information,
                False,
                None,
                restart_scan,
            )
        )
        restart_scan = False
        unsigned_status = status & 0xFFFFFFFF
        if unsigned_status == status_no_more_files:
            break
        if status < 0 and unsigned_status != status_buffer_overflow:
            error = int(ntdll.RtlNtStatusToDosError(status))
            raise OSError(error, "Windows directory could not be enumerated safely")

        returned = int(io_status.information)
        offset = 0
        while offset < returned:
            if offset + 12 > returned:
                raise _read_error(
                    namespace,
                    "unreadable",
                    "Registered JSON artifact could not be read safely.",
                )
            next_offset = int.from_bytes(buffer[offset : offset + 4], "little")
            name_length = int.from_bytes(buffer[offset + 8 : offset + 12], "little")
            name_end = offset + 12 + name_length
            if name_length % 2 or name_end > returned:
                raise _read_error(
                    namespace,
                    "unreadable",
                    "Registered JSON artifact could not be read safely.",
                )
            name = ctypes.wstring_at(
                ctypes.addressof(buffer) + offset + 12,
                name_length // 2,
            )
            if name not in {".", ".."}:
                if len(names) >= max_entries:
                    raise _read_error(
                        namespace,
                        "entry_limit_exceeded",
                        "Registered directory contains too many entries.",
                        status_code=422,
                    )
                names.append(name)
            if next_offset == 0:
                break
            offset += next_offset
        if returned == 0 and unsigned_status != status_buffer_overflow:
            break
    return names


def _windows_open_handle(
    path: Path,
    *,
    expect_directory: bool,
    namespace: str,
    share_delete: bool = True,
) -> int:
    import ctypes
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000

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
    handle = kernel32.CreateFileW(
        str(path),
        file_read_attributes if expect_directory else generic_read,
        file_share_read | file_share_write | (file_share_delete if share_delete else 0),
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_backup_semantics,
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, "Windows path component is missing")
        raise OSError(error, "Windows path component could not be opened safely")
    return _windows_validate_handle(
        int(handle),
        expect_directory=expect_directory,
        namespace=namespace,
    )


def _windows_open_relative_handle(
    parent_handle: int,
    component: str,
    *,
    expect_directory: bool,
    namespace: str,
    share_delete: bool = True,
) -> int:
    import ctypes
    from ctypes import wintypes

    file_list_directory = 0x00000001
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    file_open = 1
    file_directory_file = 0x00000001
    file_synchronous_io_nonalert = 0x00000020
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("status", ctypes.c_void_p),
            ("information", ctypes.c_size_t),
        ]

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    ntdll.NtCreateFile.restype = wintypes.LONG
    ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    name_buffer = ctypes.create_unicode_buffer(component)
    name = UnicodeString(
        length=len(component.encode("utf-16-le")),
        maximum_length=(len(component) + 1) * 2,
        buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        length=ctypes.sizeof(ObjectAttributes),
        root_directory=wintypes.HANDLE(parent_handle),
        object_name=ctypes.pointer(name),
        attributes=obj_case_insensitive,
        security_descriptor=None,
        security_quality_of_service=None,
    )
    handle = wintypes.HANDLE()
    io_status = IoStatusBlock()
    desired_access = (
        file_list_directory | file_read_attributes | synchronize
        if expect_directory
        else generic_read | synchronize
    )
    create_options = (
        file_directory_file if expect_directory else 0
    ) | file_open_reparse_point | file_synchronous_io_nonalert
    status = int(
        ntdll.NtCreateFile(
            ctypes.byref(handle),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            file_share_read
            | file_share_write
            | (file_share_delete if share_delete else 0),
            file_open,
            create_options,
            None,
            0,
        )
    )
    if status < 0:
        error = int(ntdll.RtlNtStatusToDosError(status))
        if error in {2, 3}:
            raise FileNotFoundError(error, "Windows path component is missing")
        raise OSError(error, "Windows path component could not be opened safely")
    return _windows_validate_handle(
        int(handle.value),
        expect_directory=expect_directory,
        namespace=namespace,
    )


def _create_relative_windows(
    parent_handle: int,
    component: str,
    *,
    namespace: str,
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    generic_write = 0x40000000
    delete_access = 0x00010000
    synchronize = 0x00100000
    file_create = 2
    file_attribute_normal = 0x00000080
    file_non_directory_file = 0x00000040
    file_synchronous_io_nonalert = 0x00000020
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    ntdll.NtCreateFile.restype = wintypes.LONG
    ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    name_buffer = ctypes.create_unicode_buffer(component)
    name = UnicodeString(
        length=len(component.encode("utf-16-le")),
        maximum_length=(len(component) + 1) * 2,
        buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        length=ctypes.sizeof(ObjectAttributes),
        root_directory=wintypes.HANDLE(parent_handle),
        object_name=ctypes.pointer(name),
        attributes=obj_case_insensitive,
        security_descriptor=None,
        security_quality_of_service=None,
    )
    handle = wintypes.HANDLE()
    io_status = IoStatusBlock()
    status = int(
        ntdll.NtCreateFile(
            ctypes.byref(handle),
            generic_write | file_read_attributes | delete_access | synchronize,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            file_attribute_normal,
            0,
            file_create,
            file_non_directory_file
            | file_open_reparse_point
            | file_synchronous_io_nonalert,
            None,
            0,
        )
    )
    if status < 0:
        error = int(ntdll.RtlNtStatusToDosError(status))
        if error in {80, 183}:
            raise FileExistsError(error, "Artifact already exists")
        raise OSError(error, "Artifact could not be created safely")
    validated = _windows_validate_handle(
        int(handle.value), expect_directory=False, namespace=namespace
    )
    try:
        descriptor = msvcrt.open_osfhandle(validated, os.O_WRONLY)
    except BaseException:
        _windows_close_handle(validated)
        raise
    try:
        _windows_delete_descriptor(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_relative_directory_windows(
    parent_handle: int,
    component: str,
    *,
    namespace: str,
) -> int:
    import ctypes
    from ctypes import wintypes

    file_list_directory = 0x00000001
    file_read_attributes = 0x00000080
    delete_access = 0x00010000
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_create = 2
    file_attribute_directory = 0x00000010
    file_directory_file = 0x00000001
    file_synchronous_io_nonalert = 0x00000020
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    ntdll.NtCreateFile.restype = wintypes.LONG
    ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    name_buffer = ctypes.create_unicode_buffer(component)
    name = UnicodeString(
        length=len(component.encode("utf-16-le")),
        maximum_length=(len(component) + 1) * 2,
        buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        length=ctypes.sizeof(ObjectAttributes),
        root_directory=wintypes.HANDLE(parent_handle),
        object_name=ctypes.pointer(name),
        attributes=obj_case_insensitive,
        security_descriptor=None,
        security_quality_of_service=None,
    )
    handle = wintypes.HANDLE()
    io_status = IoStatusBlock()
    status = int(
        ntdll.NtCreateFile(
            ctypes.byref(handle),
            file_list_directory
            | file_read_attributes
            | delete_access
            | synchronize,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            file_attribute_directory,
            file_share_read,
            file_create,
            file_directory_file
            | file_open_reparse_point
            | file_synchronous_io_nonalert,
            None,
            0,
        )
    )
    if status < 0:
        error = int(ntdll.RtlNtStatusToDosError(status))
        if error in {80, 183}:
            raise FileExistsError(error, "Artifact directory already exists")
        raise OSError(error, "Artifact directory could not be created safely")
    return _windows_validate_handle(
        int(handle.value), expect_directory=True, namespace=namespace
    )


def _replace_relative_windows(
    descriptor: int,
    root_handle: int,
    target_name: str,
    *,
    replace_existing: bool = True,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    file_rename_information = 10

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BYTE),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
        ]

    encoded_name = target_name.encode("utf-16-le")
    buffer_size = FileRenameInfo.file_name.offset + len(encoded_name)
    buffer = ctypes.create_string_buffer(buffer_size)
    info = ctypes.cast(buffer, ctypes.POINTER(FileRenameInfo)).contents
    info.replace_if_exists = replace_existing
    info.root_directory = wintypes.HANDLE(root_handle)
    info.file_name_length = len(encoded_name)
    name_address = ctypes.addressof(buffer) + FileRenameInfo.file_name.offset
    ctypes.memmove(name_address, encoded_name, len(encoded_name))

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    ntdll.NtSetInformationFile.restype = wintypes.LONG
    ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
    handle = msvcrt.get_osfhandle(descriptor)
    io_status = IoStatusBlock()
    status = int(ntdll.NtSetInformationFile(
        wintypes.HANDLE(handle),
        ctypes.byref(io_status),
        buffer,
        buffer_size,
        file_rename_information,
    ))
    if status < 0:
        error = int(ntdll.RtlNtStatusToDosError(status))
        raise OSError(error, "Artifact could not be replaced safely")


def _windows_delete_descriptor(descriptor: int) -> None:
    import msvcrt
    _windows_delete_handle(msvcrt.get_osfhandle(descriptor))


def _windows_cancel_delete_descriptor(descriptor: int) -> None:
    import msvcrt

    _windows_set_delete_disposition(msvcrt.get_osfhandle(descriptor), delete=False)


def _windows_delete_handle(handle: int) -> None:
    _windows_set_delete_disposition(handle, delete=True)


def _windows_set_delete_disposition(handle: int, *, delete: bool) -> None:
    import ctypes
    from ctypes import wintypes

    file_disposition_info = 4

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    info = FileDispositionInfo(delete_file=delete)
    if not kernel32.SetFileInformationByHandle(
        wintypes.HANDLE(handle),
        file_disposition_info,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Artifact could not be removed safely")


def _windows_validate_handle(
    handle: int,
    *,
    expect_directory: bool,
    namespace: str,
) -> int:
    import ctypes
    from ctypes import wintypes

    file_attribute_directory = 0x00000010
    file_attribute_device = 0x00000040
    file_attribute_reparse_point = 0x00000400
    file_attribute_tag_info_class = 9

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    info = FileAttributeTagInfo()
    if not kernel32.GetFileInformationByHandleEx(
        wintypes.HANDLE(handle),
        file_attribute_tag_info_class,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        _windows_close_handle(handle)
        raise OSError(
            error,
            "Windows path component metadata could not be read safely",
        )

    attributes = int(info.file_attributes)
    if attributes & file_attribute_reparse_point:
        _windows_close_handle(handle)
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    if expect_directory and not attributes & file_attribute_directory:
        _windows_close_handle(handle)
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )
    if not expect_directory and attributes & (
        file_attribute_directory | file_attribute_device
    ):
        _windows_close_handle(handle)
        raise _read_error(
            namespace,
            "not_regular",
            "Registered artifact must be a regular file.",
        )
    return handle


def _windows_verify_configured_root_handle(
    trusted_root_handle: int,
    configured_root: Path,
    *,
    namespace: str,
) -> None:
    try:
        actual_root = _windows_normalized_path(
            _windows_final_path_for_handle(trusted_root_handle)
        )
        lexical_root = _windows_normalized_path(str(configured_root))
    except OSError as exc:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        ) from exc
    if actual_root != lexical_root:
        raise _read_error(
            namespace,
            "path_rejected",
            "Registered artifact path failed safety validation.",
        )


def _windows_file_identity(handle: int) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time_low", wintypes.DWORD),
            ("creation_time_high", wintypes.DWORD),
            ("last_access_time_low", wintypes.DWORD),
            ("last_access_time_high", wintypes.DWORD),
            ("last_write_time_low", wintypes.DWORD),
            ("last_write_time_high", wintypes.DWORD),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    info = ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(
        wintypes.HANDLE(handle), ctypes.byref(info)
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "Windows file identity could not be verified")
    return (
        int(info.volume_serial_number),
        int(info.file_index_high),
        int(info.file_index_low),
    )


def _windows_normalized_path(value: str) -> str:
    candidate = value
    if candidate.startswith("\\\\?\\UNC\\"):
        candidate = "\\\\" + candidate[8:]
    elif candidate.startswith("\\\\?\\") or candidate.startswith("\\??\\"):
        candidate = candidate[4:]
    return ntpath.normcase(ntpath.normpath(ntpath.abspath(candidate)))


def _windows_final_path_for_handle(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            error = ctypes.get_last_error()
            raise OSError(error, "Windows handle path could not be verified safely")
        if length < size:
            value = buffer.value
            break
        size = int(length) + 1
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\") or value.startswith("\\??\\"):
        value = value[4:]
    return ntpath.normpath(value)


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _validate_json_complexity(payload: dict[str, Any], *, namespace: str) -> None:
    field_count = 0
    node_count = 0
    stack: list[tuple[Any, int]] = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > MAX_JSON_NODES:
            raise _read_error(
                namespace,
                "node_limit_exceeded",
                "Registered JSON artifact is too complex.",
                status_code=422,
            )
        if depth > MAX_JSON_DEPTH:
            raise _read_error(
                namespace,
                "depth_exceeded",
                "Registered JSON artifact is nested too deeply.",
                status_code=422,
            )
        if isinstance(value, dict):
            field_count += len(value)
            if field_count > MAX_JSON_FIELDS:
                raise _read_error(
                    namespace,
                    "field_limit_exceeded",
                    "Registered JSON artifact has too many fields.",
                    status_code=422,
                )
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _read_error(
                        namespace,
                        "invalid_shape",
                        "Registered JSON artifact contains an invalid key.",
                        status_code=422,
                    )
                if len(key) > MAX_JSON_STRING_LENGTH:
                    raise _read_error(
                        namespace,
                        "string_limit_exceeded",
                        "Registered JSON artifact contains an oversized string.",
                        status_code=422,
                    )
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            if len(value) > MAX_JSON_LIST_LENGTH:
                raise _read_error(
                    namespace,
                    "list_limit_exceeded",
                    "Registered JSON artifact contains an oversized list.",
                    status_code=422,
                )
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and len(value) > MAX_JSON_STRING_LENGTH:
            raise _read_error(
                namespace,
                "string_limit_exceeded",
                "Registered JSON artifact contains an oversized string.",
                status_code=422,
            )
        elif isinstance(value, float) and not math.isfinite(value):
            raise _read_error(
                namespace,
                "invalid_json",
                "Registered artifact is not valid JSON.",
            )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _read_error(
    namespace: str,
    suffix: str,
    message: str,
    *,
    status_code: int = 400,
) -> SafeJsonReadError:
    return SafeJsonReadError(
        f"{namespace}_{suffix}",
        message,
        status_code=status_code,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_DIRECTORY_ENTRIES",
    "MAX_JSON_DEPTH",
    "MAX_JSON_FIELDS",
    "MAX_JSON_LIST_LENGTH",
    "MAX_JSON_NODES",
    "MAX_JSON_STRING_LENGTH",
    "PinnedJsonRoot",
    "SafeJsonReadError",
    "read_bounded_json_object",
    "stat_regular_artifact",
]
