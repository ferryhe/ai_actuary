"""Run the local Operator Console and ADK Developer Web together."""

from __future__ import annotations

import argparse
import ctypes
import importlib.metadata
import importlib.util
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


MODULE_PATH = Path(__file__).resolve()
if MODULE_PATH.parents[2].name == "src" and (MODULE_PATH.parents[3] / "pyproject.toml").is_file():
    REPO_ROOT = MODULE_PATH.parents[3]
else:
    REPO_ROOT = Path.cwd().resolve()
SRC_ROOT = REPO_ROOT / "src"
if SRC_ROOT.is_dir() and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from reserving_workflow.adapters.adk.local_runtime import (
    LOOPBACK_HOST,
    LocalWorkbenchConfig,
    _secure_directory,
    build_adk_command,
    build_control_plane_command,
    secure_sensitive_file,
)
from reserving_workflow.runtime.redaction import sanitize_for_runtime


SOURCE_INSTALL_HINT = 'pip install -e ".[dev,adk-dev]"'
INSTALLED_INSTALL_HINT = 'pip install "ai-actuary[api,adk-dev]"'
SUPPORTED_ADK_VERSION = "2.7.1"


class LauncherError(RuntimeError):
    """Expected local launcher failure with a user-facing explanation."""


class ChildExited(LauncherError):
    def __init__(
        self,
        returncode: int,
        *,
        component: str | None = None,
        pid: int | None = None,
        command_label: str | None = None,
        port: int | None = None,
    ) -> None:
        if returncode < 0:
            self.returncode = 128 + abs(returncode)
        else:
            self.returncode = returncode if returncode != 0 else 1
        self.component = component
        self.pid = pid
        self.command_label = command_label
        self.port = port
        if component is None:
            message = f"A workbench child process exited with code {self.returncode}."
        else:
            message = f"Workbench child {component} exited with code {self.returncode}."
        super().__init__(message)


class ReadinessTimeout(LauncherError):
    def __init__(self, pending_endpoint: str) -> None:
        self.pending_endpoint = pending_endpoint
        super().__init__(f"Timed out waiting for smoke endpoint: {pending_endpoint}")


class StopRequested(LauncherError):
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__("Workbench shutdown requested.")


class LauncherDiagnostics:
    """Append sanitized launcher lifecycle events for local troubleshooting."""

    def __init__(self, config: LocalWorkbenchConfig) -> None:
        self.path = config.diagnostics_log
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _secure_directory(self.path.parent)

    def record(self, event: str, details: dict[str, Any] | None = None) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": sanitize_for_runtime(details or {}),
        }
        if self.path.exists():
            secure_sensitive_file(self.path)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        secure_sensitive_file(self.path)


if sys.platform == "win32":
    from ctypes import wintypes

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_ERROR_ACCESS_DENIED = 5


class _WindowsKernelJobApi:
    """Small ctypes boundary around the Windows Job Object calls we need."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise LauncherError("Windows Job Objects are only available on Windows.")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcessId.argtypes = []
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32

    @staticmethod
    def _last_error(action: str) -> LauncherError:
        error_code = ctypes.get_last_error()
        detail = ctypes.FormatError(error_code).strip() if error_code else "unknown Windows error"
        return LauncherError(f"Unable to {action}: {detail} (Windows error {error_code}).")

    def create_job(self) -> int:
        # A NULL security descriptor creates a non-inheritable handle, so children
        # cannot keep the kill-on-close owner alive after this launcher exits.
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise self._last_error("create the workbench Windows Job Object")
        return int(handle)

    def enable_kill_on_close(self, job_handle: int) -> None:
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        succeeded = self._kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not succeeded:
            raise self._last_error("enable kill-on-close for the workbench Windows Job Object")

    def assign_process(self, job_handle: int, process_handle: int, *, pid: int) -> None:
        if self._kernel32.AssignProcessToJobObject(job_handle, process_handle):
            return
        error_code = ctypes.get_last_error()
        if error_code == _ERROR_ACCESS_DENIED:
            raise LauncherError(
                f"Unable to attach process PID {pid} to the workbench Windows Job Object: "
                "access denied (Windows error 5). The launcher may already be inside an "
                "external job that does not permit nested process assignment."
            )
        detail = ctypes.FormatError(error_code).strip() if error_code else "unknown Windows error"
        raise LauncherError(
            f"Unable to attach process PID {pid} to the workbench Windows Job Object: "
            f"{detail} (Windows error {error_code})."
        )

    def current_process(self) -> tuple[int, int]:
        return (
            int(self._kernel32.GetCurrentProcess()),
            int(self._kernel32.GetCurrentProcessId()),
        )

    def close_handle(self, job_handle: int) -> None:
        if not self._kernel32.CloseHandle(job_handle):
            raise self._last_error("close the workbench Windows Job Object")


class _WindowsJobObject:
    """Keep the launcher and every inherited child in one kernel-owned tree."""

    def __init__(self, *, api: Any | None = None) -> None:
        self._api = api or _WindowsKernelJobApi()
        self._handle: int | None = self._api.create_job()
        try:
            self._api.enable_kill_on_close(self._handle)
            process_handle, pid = self._api.current_process()
            self._api.assign_process(self._handle, process_handle, pid=pid)
        except Exception:
            self._close_after_setup_failure()
            raise

    def _close_after_setup_failure(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._api.close_handle(handle)

def create_child_containment() -> _WindowsJobObject | None:
    if sys.platform == "win32":
        return _WindowsJobObject()
    return None


def find_adk_spec() -> Any | None:
    try:
        return importlib.util.find_spec("google.adk")
    except ModuleNotFoundError:
        return None


def running_from_source_checkout() -> bool:
    return MODULE_PATH.parents[2].name == "src" and (MODULE_PATH.parents[3] / "pyproject.toml").is_file()


def install_hint() -> str:
    return SOURCE_INSTALL_HINT if running_from_source_checkout() else INSTALLED_INSTALL_HINT


def adk_distribution_version() -> str:
    try:
        return importlib.metadata.version("google-adk")
    except importlib.metadata.PackageNotFoundError as exc:
        raise LauncherError(f"ADK Developer Web is not installed. Run: {install_hint()}") from exc


def reserving_workflow_distribution_version() -> str:
    try:
        return importlib.metadata.version("ai-actuary")
    except importlib.metadata.PackageNotFoundError:
        return "source-checkout"


def launcher_version_summary(*, adk_executable: str | None, disable_adk: bool) -> dict[str, Any]:
    if disable_adk:
        google_adk_version = "disabled"
    else:
        try:
            google_adk_version = adk_distribution_version()
        except LauncherError:
            google_adk_version = "unavailable"
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "reserving_workflow": reserving_workflow_distribution_version(),
        "google_adk": google_adk_version,
        "adk_executable": "disabled" if disable_adk else Path(str(adk_executable)).name,
    }


def launcher_exit_code_mapping() -> dict[str, int]:
    return {
        "ready_or_clean_smoke": 0,
        "startup_or_runtime_failure": 1,
        "sigint": 128 + int(signal.SIGINT),
        "sigterm": 128 + int(signal.SIGTERM),
    }


def diagnostics_ref(config: LocalWorkbenchConfig) -> str:
    return f"diagnostics:{config.diagnostics_log.parent.name}/{config.diagnostics_log.name}"


def _pending_endpoint_ref(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    if "/health/preflight" in endpoint:
        return "endpoint:control_plane_preflight"
    if "/health" in endpoint:
        return "endpoint:control_plane_health"
    if "/console" in endpoint:
        return "endpoint:operator_console"
    if (
        "/dev-ui" in endpoint
        or "/list-apps" in endpoint
        or "/apps/" in endpoint
        or "/dev/apps/" in endpoint
    ):
        return "endpoint:adk_developer_web"
    return "endpoint:loopback"


def _failure_category(exc: LauncherError, *, component: str) -> str:
    message = str(exc)
    if isinstance(exc, ReadinessTimeout):
        return "readiness_timeout"
    if isinstance(exc, ChildExited):
        return "child_exit"
    if "Loopback port conflict" in message:
        return "port_conflict"
    if "ADK Developer Web is not installed" in message:
        return "missing_adk"
    if "google-adk==" in message or "pinned to" in message:
        return "incompatible_adk"
    if "ADK command is unavailable" in message:
        return "adk_executable_unavailable"
    return f"startup_{component}"


def _recovery_hint_for_failure(exc: LauncherError, *, category: str) -> str:
    if category in {"missing_adk", "incompatible_adk", "adk_executable_unavailable"}:
        return install_hint()
    if category == "port_conflict":
        return (
            "Free the occupied loopback port or rerun with explicit unused ports, "
            "for example --api-port 8123 --adk-port 8124."
        )
    if category == "readiness_timeout":
        pending = _pending_endpoint_ref(getattr(exc, "pending_endpoint", None))
        return (
            f"Inspect the pending endpoint ({pending or 'endpoint:unknown'}), child "
            "diagnostics, and retained launcher logs; then rerun after fixing the "
            "stalled component."
        )
    if category == "child_exit":
        return (
            "Inspect the child component, exit code, command label, and launcher logs; "
            "fix that component before rerunning the workbench."
        )
    return "Inspect launcher diagnostics for the failed component and rerun after correcting it."


def _default_component_status(*, disable_adk: bool) -> dict[str, Any]:
    return {
        "control_plane": {"status": "not_started"},
        "adk_developer_web": (
            {"status": "disabled"} if disable_adk else {"status": "not_started"}
        ),
    }


def _component_status_from_children(
    children: Sequence[Any],
    child_identities: Sequence[dict[str, Any]],
    *,
    disable_adk: bool,
) -> dict[str, Any]:
    components = _default_component_status(disable_adk=disable_adk)
    for index, identity in enumerate(child_identities):
        component = str(identity.get("component") or f"child_{index}")
        child = children[index] if index < len(children) else None
        returncode = child.poll() if child is not None and hasattr(child, "poll") else None
        status = "exited" if returncode is not None else "started"
        components[component] = {
            "status": status,
            "pid": identity.get("pid"),
            "port": identity.get("port"),
            **({"exit_code": returncode} if returncode is not None else {}),
        }
    return components


def startup_failure_details(
    config: LocalWorkbenchConfig,
    exc: LauncherError,
    *,
    component: str,
    disable_adk: bool,
    terminal_exit_code: int = 1,
    component_status: dict[str, Any] | None = None,
    child_identities: Sequence[dict[str, Any]] | None = None,
    pending_endpoint: str | None = None,
) -> dict[str, Any]:
    category = _failure_category(exc, component=component)
    pending = pending_endpoint or getattr(exc, "pending_endpoint", None)
    return {
        "readiness": (
            "preflight_failed"
            if component == "preflight"
            else ("timeout" if category == "readiness_timeout" else "startup_failed")
        ),
        "versions": launcher_version_summary(adk_executable=None, disable_adk=disable_adk),
        "components": component_status or _default_component_status(disable_adk=disable_adk),
        "failure": {
            "category": category,
            "component": component,
            "reason": str(exc),
            "recovery_hint": _recovery_hint_for_failure(exc, category=category),
        },
        **({"pending_endpoint": _pending_endpoint_ref(pending)} if pending else {}),
        **({"child_identities": list(child_identities)} if child_identities else {}),
        "terminal_exit_code": terminal_exit_code,
        "exit_code_mapping": launcher_exit_code_mapping(),
        "shutdown_cause": category,
        "diagnostics_ref": diagnostics_ref(config),
    }


def validate_adk_runtime() -> str:
    if (sys.version_info.major, sys.version_info.minor) != (3, 11):
        raise LauncherError(
            f"ADK Developer Web is pinned for Python 3.11; current Python is "
            f"{sys.version_info.major}.{sys.version_info.minor}. Create a Python 3.11 "
            f"environment and run: {install_hint()}"
        )
    if find_adk_spec() is None:
        raise LauncherError(f"ADK Developer Web is not installed. Run: {install_hint()}")
    installed_adk_version = adk_distribution_version()
    if installed_adk_version != SUPPORTED_ADK_VERSION:
        raise LauncherError(
            "ADK Developer Web is pinned to "
            f"google-adk=={SUPPORTED_ADK_VERSION}; installed version is "
            f"{installed_adk_version}. Reinstall with: {install_hint()}"
        )

    adjacent = Path(sys.executable).with_name("adk.exe" if sys.platform == "win32" else "adk")
    executable = str(adjacent) if adjacent.is_file() else shutil.which("adk")
    if executable is None:
        raise LauncherError(f"The ADK command is unavailable. Reinstall with: {install_hint()}")
    return executable


def port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        try:
            candidate.bind((host, port))
        except OSError:
            return False
    return True


def ensure_ports_available(config: LocalWorkbenchConfig, *, include_adk: bool = True) -> None:
    ports = [config.control_plane_port]
    if include_adk:
        ports.append(config.adk_port)
    if len(set(ports)) != len(ports):
        raise LauncherError("Loopback port conflict: API and ADK ports must differ.")
    conflicts = [
        port
        for port in ports
        if not port_is_available(LOOPBACK_HOST, port)
    ]
    if conflicts:
        ports = ", ".join(str(port) for port in conflicts)
        raise LauncherError(f"Loopback port conflict on {ports}; no child process was started.")


def _ensure_ports_available_for_mode(config: LocalWorkbenchConfig, *, include_adk: bool) -> None:
    try:
        ensure_ports_available(config, include_adk=include_adk)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        ensure_ports_available(config)


def terminate_children(children: Sequence[Any], *, timeout: float = 5.0) -> None:
    running = [child for child in children if child.poll() is None]
    for child in running:
        child.terminate()
    for child in running:
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=timeout)


def start_children(
    commands: Sequence[list[str]],
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    cwd: Path | None = None,
    environments: Sequence[dict[str, str]] | None = None,
) -> list[Any]:
    if environments is not None and len(environments) != len(commands):
        raise LauncherError("Each workbench child requires one isolated environment.")
    children: list[Any] = []
    try:
        for index, command in enumerate(commands):
            child_env = environments[index] if environments is not None else None
            child = popen_factory(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                **({"env": child_env} if child_env is not None else {}),
            )
            children.append(child)
    except Exception as exc:
        terminate_children(children)
        raise LauncherError(f"Unable to start local workbench: {exc}") from exc
    return children


def child_identities_for_commands(
    commands: Sequence[list[str]],
    children: Sequence[Any],
    *,
    config: LocalWorkbenchConfig,
    disable_adk: bool,
) -> list[dict[str, Any]]:
    components: list[tuple[str, int | None]] = [("control_plane", config.control_plane_port)]
    if not disable_adk:
        components.append(("adk_developer_web", config.adk_port))
    identities: list[dict[str, Any]] = []
    for index, child in enumerate(children):
        component, port = components[index] if index < len(components) else (f"child_{index}", None)
        identities.append(
            {
                "component": component,
                "pid": getattr(child, "pid", None),
                "command_label": _command_label(commands[index]) if index < len(commands) else component,
                "port": port,
            }
        )
    return identities


def _command_label(command: Sequence[str]) -> str:
    if not command:
        return "unknown"
    if len(command) >= 3 and command[1] == "-m":
        return f"{Path(command[0]).name} -m {command[2]}"
    return " ".join(Path(part).name if index == 0 else part for index, part in enumerate(command[:2]))


def _raise_if_child_exited(
    children: Sequence[Any],
    child_identities: Sequence[dict[str, Any]] | None = None,
) -> None:
    for index, child in enumerate(children):
        returncode = child.poll()
        if returncode is not None:
            identity = (
                child_identities[index]
                if child_identities is not None and index < len(child_identities)
                else {}
            )
            raise ChildExited(
                returncode,
                component=identity.get("component"),
                pid=identity.get("pid"),
                command_label=identity.get("command_label"),
                port=identity.get("port"),
            )


def wait_for_smoke_endpoints(
    *,
    control_plane_port: int,
    adk_port: int | None,
    children: Sequence[Any],
    timeout: float,
    poll_interval: float = 0.1,
    opener: Callable[..., Any] | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    stop_exit_code: Callable[[], int] = lambda: 0,
    child_identities: Sequence[dict[str, Any]] | None = None,
) -> None:
    endpoints = [
        f"http://{LOOPBACK_HOST}:{control_plane_port}/health",
        f"http://{LOOPBACK_HOST}:{control_plane_port}/health/preflight",
        f"http://{LOOPBACK_HOST}:{control_plane_port}/console",
    ]
    if adk_port is not None:
        endpoints.extend(
            [
                f"http://{LOOPBACK_HOST}:{adk_port}/",
                f"http://{LOOPBACK_HOST}:{adk_port}/list-apps",
                f"http://{LOOPBACK_HOST}:{adk_port}/apps/ai_actuary_developer/app-info",
                f"http://{LOOPBACK_HOST}:{adk_port}/dev/apps/ai_actuary_developer/build_graph",
            ]
        )
    resolved_opener = opener or build_opener(ProxyHandler({})).open
    deadline = time.monotonic() + timeout
    pending = list(endpoints)
    while pending:
        if stop_requested():
            raise StopRequested(stop_exit_code())
        _raise_if_child_exited(children, child_identities=child_identities)
        url = pending[0]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadinessTimeout(url)
            request = Request(url, method="GET")
            with resolved_opener(
                request, timeout=min(2.0, remaining)
            ) as response:
                response.read()
                if response.status != 200:
                    raise LauncherError(f"Smoke endpoint returned HTTP {response.status}: {url}")
            pending.pop(0)
        except (HTTPError, URLError, TimeoutError, OSError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadinessTimeout(url)
            time.sleep(min(poll_interval, remaining))


def supervise_children(
    children: Sequence[Any],
    *,
    smoke_check: Callable[[Sequence[Any]], None] | None,
    stop_requested: Callable[[], bool],
    stop_exit_code: Callable[[], int] = lambda: 0,
    poll_interval: float = 0.1,
    diagnostics: LauncherDiagnostics | None = None,
    child_identities: Sequence[dict[str, Any]] | None = None,
) -> int:
    exit_code: int | None = None
    shutdown_cause = "runtime_failure"
    try:
        if smoke_check is not None:
            smoke_check(children)
            exit_code = stop_exit_code() if stop_requested() else 0
            shutdown_cause = "signal" if stop_requested() else "smoke_completed"
            return exit_code
        while True:
            if stop_requested():
                exit_code = stop_exit_code()
                shutdown_cause = "signal"
                return exit_code
            _raise_if_child_exited(children, child_identities=child_identities)
            time.sleep(poll_interval)
    except ChildExited as exc:
        exit_code = exc.returncode
        shutdown_cause = "child_exit"
        if diagnostics is not None:
            diagnostics.record(
                "child_exit",
                {
                    "component": exc.component,
                    "pid": exc.pid,
                    "command_label": exc.command_label,
                    "port": exc.port,
                    "exit_code": exc.returncode,
                    "reason": "child_exited",
                },
            )
        return exit_code
    except StopRequested as exc:
        exit_code = exc.exit_code
        shutdown_cause = "signal" if exc.exit_code >= 128 else "stop_requested"
        return exit_code
    finally:
        terminate_children(children)
        if diagnostics is not None:
            diagnostics.record(
                "launcher_exit",
                {
                    "exit_code": 1 if exit_code is None else exit_code,
                    "shutdown_cause": shutdown_cause,
                    "exit_code_mapping": launcher_exit_code_mapping(),
                },
            )


def approve_operator_handoff(
    *,
    control_plane_port: int,
    bootstrap_token: str,
    handoff_id: str,
    opener: Callable[..., Any] | None = None,
) -> None:
    origin = f"http://{LOOPBACK_HOST}:{control_plane_port}"
    request = Request(
        f"{origin}/auth/operator/handoff/approve",
        data=json.dumps(
            {"bootstrap_token": bootstrap_token, "handoff_id": handoff_id}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": origin},
        method="POST",
    )
    resolved_opener = opener or build_opener(ProxyHandler({})).open
    try:
        with resolved_opener(request, timeout=2.0) as response:
            response.read()
            if response.status != 200:
                raise LauncherError("Operator Console handoff approval failed.")
    except LauncherError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise LauncherError("Operator Console handoff approval failed.") from exc


def start_operator_handoff_prompt(
    *,
    control_plane_port: int,
    bootstrap_token: str,
) -> threading.Thread:
    def approve_from_terminal() -> None:
        print(
            "In the Operator Console, choose Connect and paste its handoff ID below."
        )
        while True:
            try:
                handoff_id = input("Operator Console handoff ID: ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not handoff_id:
                continue
            try:
                approve_operator_handoff(
                    control_plane_port=control_plane_port,
                    bootstrap_token=bootstrap_token,
                    handoff_id=handoff_id,
                )
            except LauncherError:
                print(
                    "Operator Console handoff was not approved; request a new handoff and retry.",
                    file=sys.stderr,
                )
                continue
            print("Operator Console handoff approved.")
            return

    thread = threading.Thread(
        target=approve_from_terminal,
        name="operator-console-handoff",
        daemon=True,
    )
    thread.start()
    return thread


class _SignalState:
    def __init__(self) -> None:
        self.signum: int | None = None

    def handle(self, signum: int, frame: FrameType | None) -> None:
        del frame
        self.signum = signum

    def requested(self) -> bool:
        return self.signum is not None

    def exit_code(self) -> int:
        return 128 + self.signum if self.signum is not None else 0


def run_workbench(
    config: LocalWorkbenchConfig,
    *,
    smoke: bool,
    disable_adk: bool = False,
    smoke_timeout: float = 30.0,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> int:
    diagnostics = LauncherDiagnostics(config)
    try:
        adk_executable = None if disable_adk else validate_adk_runtime()
        _ensure_ports_available_for_mode(config, include_adk=not disable_adk)
        config.prepare_state_directories()
        diagnostics.record(
            "preflight_ready",
            {
                "readiness": "preflight_ready",
                "versions": launcher_version_summary(
                    adk_executable=adk_executable,
                    disable_adk=disable_adk,
                ),
                "components": {
                    "control_plane": {
                        "status": "pending_start",
                        "url": f"http://{LOOPBACK_HOST}:{config.control_plane_port}/console",
                    },
                    "adk_developer_web": (
                        {"status": "disabled"}
                        if disable_adk
                        else {
                            "status": "pending_start",
                            "url": f"http://{LOOPBACK_HOST}:{config.adk_port}",
                        }
                    ),
                },
                "diagnostics_ref": diagnostics_ref(config),
                "exit_code_mapping": launcher_exit_code_mapping(),
            },
        )
    except LauncherError as exc:
        diagnostics.record(
            "startup_failed",
            startup_failure_details(
                config,
                exc,
                component="preflight",
                disable_adk=disable_adk,
            ),
        )
        print(f"Local workbench failed: {exc}", file=sys.stderr)
        return 1

    signal_state = _SignalState()
    installed_handlers: list[tuple[signal.Signals, Any]] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        installed_handlers.append((signum, signal.getsignal(signum)))
        signal.signal(signum, signal_state.handle)

    children: list[Any] = []
    child_identities: list[dict[str, Any]] = []
    containment: _WindowsJobObject | None = None
    try:
        containment = create_child_containment()
        commands = [build_control_plane_command(config)]
        if not disable_adk:
            assert adk_executable is not None
            commands.append(build_adk_command(config, adk_executable=adk_executable))
        child_environments = _capability_child_environments(config)
        selected_environments: Sequence[dict[str, str]] = (
            (child_environments[0],)
            if disable_adk
            else child_environments
        )
        diagnostics.record(
            "startup_begin",
            {
                "api_port": config.control_plane_port,
                "adk_port": None if disable_adk else config.adk_port,
                "adk_enabled": not disable_adk,
            },
        )
        children = start_children(
            commands,
            popen_factory=popen_factory,
            cwd=config.repo_root,
            environments=selected_environments,
        )
        child_identities = child_identities_for_commands(
            commands,
            children,
            config=config,
            disable_adk=disable_adk,
        )
        print(f"Operator Console: http://{LOOPBACK_HOST}:{config.control_plane_port}/console")
        if disable_adk:
            print("ADK Developer Web: disabled for this run")
        else:
            print(f"ADK Developer Web (development-only): http://{LOOPBACK_HOST}:{config.adk_port}")
        print(f"Diagnostics ref: {diagnostics_ref(config)}")
        diagnostics.record(
            "children_started",
            {
                "child_count": len(children),
                "children": child_identities,
                "api_url": f"http://{LOOPBACK_HOST}:{config.control_plane_port}/console",
                "adk_url": None if disable_adk else f"http://{LOOPBACK_HOST}:{config.adk_port}",
            },
        )
        if not smoke and sys.stdin.isatty():
            start_operator_handoff_prompt(
                control_plane_port=config.control_plane_port,
                bootstrap_token=selected_environments[0][
                    "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN"
                ],
            )

        def run_smoke_check(processes: Sequence[Any]) -> None:
            try:
                wait_for_smoke_endpoints(
                    control_plane_port=config.control_plane_port,
                    adk_port=None if disable_adk else config.adk_port,
                    children=processes,
                    timeout=smoke_timeout,
                    stop_requested=signal_state.requested,
                    stop_exit_code=signal_state.exit_code,
                    child_identities=child_identities,
                )
            except ReadinessTimeout as exc:
                diagnostics.record(
                    "readiness_timeout",
                    {
                        "readiness": "timeout",
                        "pending_endpoint": exc.pending_endpoint,
                        "api_port": config.control_plane_port,
                        "adk_port": None if disable_adk else config.adk_port,
                    },
                )
                raise
            diagnostics.record(
                "readiness_ready",
                {
                    "readiness": "ready",
                    "api_port": config.control_plane_port,
                    "adk_port": None if disable_adk else config.adk_port,
                    "adk_enabled": not disable_adk,
                    "components": {
                        "control_plane": {"status": "ready"},
                        "adk_developer_web": (
                            {"status": "disabled"}
                            if disable_adk
                            else {"status": "ready"}
                        ),
                    },
                },
            )
        smoke_check = run_smoke_check if smoke else None
        exit_code = supervise_children(
            children,
            smoke_check=smoke_check,
            stop_requested=signal_state.requested,
            stop_exit_code=signal_state.exit_code,
            diagnostics=diagnostics,
            child_identities=child_identities,
        )
        return exit_code
    except LauncherError as exc:
        failure_component = (
            "readiness"
            if isinstance(exc, ReadinessTimeout)
            else ("child_exit" if isinstance(exc, ChildExited) else "runtime")
        )
        diagnostics.record(
            "startup_failed",
            startup_failure_details(
                config,
                exc,
                component=failure_component,
                disable_adk=disable_adk,
                component_status=_component_status_from_children(
                    children,
                    child_identities,
                    disable_adk=disable_adk,
                ),
                child_identities=child_identities,
                pending_endpoint=getattr(exc, "pending_endpoint", None),
            ),
        )
        print(f"Local workbench failed: {exc}", file=sys.stderr)
        return 1
    finally:
        terminate_children(children)
        # Once the launcher itself belongs to the Job, closing its last handle
        # would terminate this process too. Keep the non-inheritable handle open
        # until process teardown; Windows then closes it and kills any descendants.
        del containment
        for signum, previous in installed_handlers:
            signal.signal(signum, previous)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Start, verify both interfaces, and stop.")
    parser.add_argument("--smoke-timeout", type=float, default=30.0)
    parser.add_argument("--api-port", dest="control_plane_port", type=int, default=8000, help="Operator Console/API loopback port.")
    parser.add_argument("--control-plane-port", type=int, default=8000, help=argparse.SUPPRESS)
    parser.add_argument("--adk-port", type=int, default=8001, help="ADK Developer Web loopback port.")
    parser.add_argument("--disable-adk", action="store_true", help="Start only the local API and Operator Console.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = LocalWorkbenchConfig.from_repo_root(
        REPO_ROOT,
        control_plane_port=args.control_plane_port,
        adk_port=args.adk_port,
    )
    return run_workbench(
        config,
        smoke=args.smoke,
        disable_adk=args.disable_adk,
        smoke_timeout=args.smoke_timeout,
    )


def _capability_child_environments(
    config: LocalWorkbenchConfig,
) -> tuple[dict[str, str], dict[str, str]]:
    operator_credential = _capability_secret_from_environment(
        "AI_ACTUARY_OPERATOR_CREDENTIAL"
    )
    adk_credential = _capability_secret_from_environment(
        "AI_ACTUARY_ADK_CREDENTIAL"
    )
    bootstrap_token = _capability_secret_from_environment(
        "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN"
    )
    control_plane_environment = dict(os.environ)
    control_plane_environment.update(
        {
            "AI_ACTUARY_OPERATOR_CREDENTIAL": operator_credential,
            "AI_ACTUARY_ADK_CREDENTIAL": adk_credential,
            "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN": bootstrap_token,
            "AI_ACTUARY_OPERATOR_ORIGIN": (
                f"http://{LOOPBACK_HOST}:{config.control_plane_port}"
            ),
            "AI_ACTUARY_ADK_URL": f"http://{LOOPBACK_HOST}:{config.adk_port}",
        }
    )
    adk_environment = dict(os.environ)
    adk_environment.pop("AI_ACTUARY_OPERATOR_CREDENTIAL", None)
    adk_environment.pop("AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN", None)
    adk_environment["AI_ACTUARY_ADK_CREDENTIAL"] = adk_credential
    adk_environment["AI_ACTUARY_CONTROL_PLANE_URL"] = (
        f"http://{LOOPBACK_HOST}:{config.control_plane_port}"
    )
    return control_plane_environment, adk_environment


def _capability_secret_from_environment(name: str) -> str:
    configured = os.environ.get(name)
    if configured is not None and len(configured) >= 8:
        return configured
    return secrets.token_urlsafe(48)


if __name__ == "__main__":
    raise SystemExit(main())
