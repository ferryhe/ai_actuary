"""Run the local Operator Console and ADK Developer Web together."""

from __future__ import annotations

import argparse
import ctypes
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
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from reserving_workflow.adapters.adk.local_runtime import (
    LOOPBACK_HOST,
    LocalWorkbenchConfig,
    build_adk_command,
    build_control_plane_command,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_HINT = 'pip install -e ".[dev,adk-dev]"'


class LauncherError(RuntimeError):
    """Expected local launcher failure with a user-facing explanation."""


class ChildExited(LauncherError):
    def __init__(self, returncode: int) -> None:
        if returncode < 0:
            self.returncode = 128 + abs(returncode)
        else:
            self.returncode = returncode if returncode != 0 else 1
        super().__init__(f"A workbench child process exited with code {self.returncode}.")


class StopRequested(LauncherError):
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__("Workbench shutdown requested.")


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


def validate_adk_runtime() -> str:
    if (sys.version_info.major, sys.version_info.minor) != (3, 11):
        raise LauncherError(
            f"ADK Developer Web is pinned for Python 3.11; current Python is "
            f"{sys.version_info.major}.{sys.version_info.minor}. Create a Python 3.11 "
            f"environment and run: {INSTALL_HINT}"
        )
    if find_adk_spec() is None:
        raise LauncherError(f"ADK Developer Web is not installed. Run: {INSTALL_HINT}")

    adjacent = Path(sys.executable).with_name("adk.exe" if sys.platform == "win32" else "adk")
    executable = str(adjacent) if adjacent.is_file() else shutil.which("adk")
    if executable is None:
        raise LauncherError(f"The ADK command is unavailable. Reinstall with: {INSTALL_HINT}")
    return executable


def port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        try:
            candidate.bind((host, port))
        except OSError:
            return False
    return True


def ensure_ports_available(config: LocalWorkbenchConfig) -> None:
    conflicts = [
        port
        for port in (config.control_plane_port, config.adk_port)
        if not port_is_available(LOOPBACK_HOST, port)
    ]
    if conflicts:
        ports = ", ".join(str(port) for port in conflicts)
        raise LauncherError(f"Loopback port conflict on {ports}; no child process was started.")


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


def _raise_if_child_exited(children: Sequence[Any]) -> None:
    for child in children:
        returncode = child.poll()
        if returncode is not None:
            raise ChildExited(returncode)


def wait_for_smoke_endpoints(
    *,
    control_plane_port: int,
    adk_port: int,
    children: Sequence[Any],
    timeout: float,
    poll_interval: float = 0.1,
    opener: Callable[..., Any] | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    stop_exit_code: Callable[[], int] = lambda: 0,
) -> None:
    endpoints = [
        f"http://{LOOPBACK_HOST}:{control_plane_port}/health",
        f"http://{LOOPBACK_HOST}:{control_plane_port}/health/preflight",
        f"http://{LOOPBACK_HOST}:{control_plane_port}/console",
        f"http://{LOOPBACK_HOST}:{adk_port}/",
        f"http://{LOOPBACK_HOST}:{adk_port}/list-apps",
        f"http://{LOOPBACK_HOST}:{adk_port}/apps/ai_actuary_developer/app-info",
        f"http://{LOOPBACK_HOST}:{adk_port}/dev/apps/ai_actuary_developer/build_graph",
    ]
    resolved_opener = opener or build_opener(ProxyHandler({})).open
    deadline = time.monotonic() + timeout
    pending = list(endpoints)
    while pending:
        if stop_requested():
            raise StopRequested(stop_exit_code())
        _raise_if_child_exited(children)
        url = pending[0]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LauncherError(f"Timed out waiting for smoke endpoint: {url}")
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
                raise LauncherError(f"Timed out waiting for smoke endpoint: {url}")
            time.sleep(min(poll_interval, remaining))


def supervise_children(
    children: Sequence[Any],
    *,
    smoke_check: Callable[[Sequence[Any]], None] | None,
    stop_requested: Callable[[], bool],
    stop_exit_code: Callable[[], int] = lambda: 0,
    poll_interval: float = 0.1,
) -> int:
    try:
        if smoke_check is not None:
            smoke_check(children)
            return stop_exit_code() if stop_requested() else 0
        while True:
            if stop_requested():
                return stop_exit_code()
            _raise_if_child_exited(children)
            time.sleep(poll_interval)
    except ChildExited as exc:
        return exc.returncode
    except StopRequested as exc:
        return exc.exit_code
    finally:
        terminate_children(children)


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
    smoke_timeout: float = 30.0,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> int:
    try:
        adk_executable = validate_adk_runtime()
        ensure_ports_available(config)
        config.prepare_state_directories()
    except LauncherError as exc:
        print(f"Local workbench failed: {exc}", file=sys.stderr)
        return 1

    signal_state = _SignalState()
    installed_handlers: list[tuple[signal.Signals, Any]] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        installed_handlers.append((signum, signal.getsignal(signum)))
        signal.signal(signum, signal_state.handle)

    children: list[Any] = []
    containment: _WindowsJobObject | None = None
    try:
        containment = create_child_containment()
        commands = [
            build_control_plane_command(config),
            build_adk_command(config, adk_executable=adk_executable),
        ]
        child_environments = _capability_child_environments(config)
        children = start_children(
            commands,
            popen_factory=popen_factory,
            cwd=config.repo_root,
            environments=child_environments,
        )
        print(f"Operator Console: http://{LOOPBACK_HOST}:{config.control_plane_port}/console")
        print(f"ADK Developer Web (development-only): http://{LOOPBACK_HOST}:{config.adk_port}")
        if not smoke and sys.stdin.isatty():
            start_operator_handoff_prompt(
                control_plane_port=config.control_plane_port,
                bootstrap_token=child_environments[0][
                    "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN"
                ],
            )

        def run_smoke_check(processes: Sequence[Any]) -> None:
            wait_for_smoke_endpoints(
                control_plane_port=config.control_plane_port,
                adk_port=config.adk_port,
                children=processes,
                timeout=smoke_timeout,
                stop_requested=signal_state.requested,
                stop_exit_code=signal_state.exit_code,
            )
        smoke_check = run_smoke_check if smoke else None
        return supervise_children(
            children,
            smoke_check=smoke_check,
            stop_requested=signal_state.requested,
            stop_exit_code=signal_state.exit_code,
        )
    except LauncherError as exc:
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
    parser.add_argument("--control-plane-port", type=int, default=8000, help=argparse.SUPPRESS)
    parser.add_argument("--adk-port", type=int, default=8001, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = LocalWorkbenchConfig.from_repo_root(
        REPO_ROOT,
        control_plane_port=args.control_plane_port,
        adk_port=args.adk_port,
    )
    return run_workbench(config, smoke=args.smoke, smoke_timeout=args.smoke_timeout)


def _capability_child_environments(
    config: LocalWorkbenchConfig,
) -> tuple[dict[str, str], dict[str, str]]:
    operator_credential = secrets.token_urlsafe(48)
    adk_credential = secrets.token_urlsafe(48)
    bootstrap_token = secrets.token_urlsafe(48)
    control_plane_environment = dict(os.environ)
    control_plane_environment.update(
        {
            "AI_ACTUARY_OPERATOR_CREDENTIAL": operator_credential,
            "AI_ACTUARY_ADK_CREDENTIAL": adk_credential,
            "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN": bootstrap_token,
            "AI_ACTUARY_OPERATOR_ORIGIN": (
                f"http://{LOOPBACK_HOST}:{config.control_plane_port}"
            ),
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


if __name__ == "__main__":
    raise SystemExit(main())
