"""Descriptor-pinned, bounded JSON reads for local trusted roots."""

from __future__ import annotations

import errno
import json
import math
import ntpath
import os
import stat
from pathlib import Path
from typing import Any


MAX_ARTIFACT_BYTES = 1_000_000
MAX_JSON_DEPTH = 20
MAX_JSON_FIELDS = 5_000
MAX_JSON_NODES = 20_000
MAX_JSON_LIST_LENGTH = 2_000
MAX_JSON_STRING_LENGTH = 100_000


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
    ) -> None:
        self.root = Path(os.path.abspath(os.path.expanduser(str(root))))
        self.namespace = namespace
        self.allow_nested = allow_nested
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


def _open_trusted_root_windows(root: Path, *, namespace: str) -> int:
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
        for component in root.parts[1:]:
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


def _windows_open_handle(
    path: Path,
    *,
    expect_directory: bool,
    namespace: str,
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
        file_share_read | file_share_write | file_share_delete,
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
            file_share_read | file_share_write | file_share_delete,
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
