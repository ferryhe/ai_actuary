from __future__ import annotations

import importlib.util
import json
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import httpx
import pytest


REPO_ROOT = Path(__file__).parents[1]


def _start_http_capture(
    response_payload: dict[str, object],
) -> tuple[ThreadingHTTPServer, threading.Thread, list[dict[str, object]]]:
    captured: list[dict[str, object]] = []
    response_body = json.dumps(response_payload).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            captured.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        do_GET = _respond
        do_POST = _respond

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, captured


def _stop_http_capture(
    server: ThreadingHTTPServer,
    thread: threading.Thread,
) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


def _install_hostile_http_proxy(
    monkeypatch: pytest.MonkeyPatch,
    proxy_port: int,
) -> None:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    for name in (
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("http_proxy", proxy_url)


def _google_adk_available() -> bool:
    try:
        return importlib.util.find_spec("google.adk") is not None
    except ModuleNotFoundError:
        return False


def _load_launcher_module() -> Any:
    script_path = REPO_ROOT / "scripts" / "run_local_workbench.py"
    spec = importlib.util.spec_from_file_location("run_local_workbench", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_adk_is_a_python311_only_optional_extra() -> None:
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    optional = project["optional-dependencies"]

    assert all("google-adk" not in dependency for dependency in project["dependencies"])
    assert all("google-adk" not in dependency for dependency in optional["api"])
    assert all("google-adk" not in dependency for dependency in optional["dev"])
    assert optional["adk-dev"] == [
        "google-adk==2.7.1; python_version == '3.11'",
    ]


def test_importing_control_plane_does_not_import_google_adk() -> None:
    probe = """
import sys
sys.path.insert(0, 'src')
from developer_workflows.ai_actuary_developer import tools as adk_read_tools
from reserving_workflow.adapters.control_plane import ReadOnlyControlPlaneClient
from reserving_workflow.api.app import ApiSettings, create_app
create_app(settings=ApiSettings(
    operator_credential='test-operator-capability',
    adk_credential='test-adk-capability',
    operator_bootstrap_token='test-bootstrap-capability',
))
assert adk_read_tools.READ_TOOL_NAMES
assert not hasattr(ReadOnlyControlPlaneClient, 'create_run')
assert not any(name == 'google.adk' or name.startswith('google.adk.') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_local_runtime_commands_pin_loopback_and_keep_state_out_of_agent_sources(
    tmp_path: Path,
) -> None:
    from reserving_workflow.adapters.adk.local_runtime import (
        ADK_DEVELOPER_LOGO_DATA_URL,
        ADK_DEVELOPER_LOGO_TEXT,
        LocalWorkbenchConfig,
        build_adk_command,
        build_control_plane_command,
    )

    config = LocalWorkbenchConfig.from_repo_root(
        tmp_path,
        control_plane_port=8123,
        adk_port=8124,
    )
    api_command = build_control_plane_command(config, python_executable="python-test")
    adk_command = build_adk_command(config, adk_executable="adk-test")

    assert api_command == [
        "python-test",
        "-m",
        "uvicorn",
        "reserving_workflow.api.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
    ]
    assert adk_command[:7] == [
        "adk-test",
        "web",
        "--host",
        "127.0.0.1",
        "--port",
        "8124",
        "--no-reload",
    ]
    assert "--allow_origins" not in adk_command
    assert adk_command[-1] == str((tmp_path / "developer_workflows").resolve())
    assert adk_command[adk_command.index("--logo-text") + 1] == ADK_DEVELOPER_LOGO_TEXT
    assert "(DEV)" in ADK_DEVELOPER_LOGO_TEXT
    assert "http://127.0.0.1:8000/console" in ADK_DEVELOPER_LOGO_TEXT
    assert (
        adk_command[adk_command.index("--logo-image-url") + 1]
        == ADK_DEVELOPER_LOGO_DATA_URL
    )
    assert ADK_DEVELOPER_LOGO_DATA_URL.startswith("data:image/svg+xml,")

    session_uri = adk_command[adk_command.index("--session_service_uri") + 1]
    artifact_uri = adk_command[adk_command.index("--artifact_service_uri") + 1]
    assert session_uri.startswith("sqlite:///")
    assert "tmp/adk-dev/sessions/sessions.db" in session_uri.replace("\\", "/")
    assert artifact_uri.startswith("file:")
    assert "tmp/adk-dev/artifacts" in artifact_uri.replace("\\", "/")
    assert "developer_workflows" not in session_uri
    assert "developer_workflows" not in artifact_uri


def test_runtime_requirement_error_explains_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _load_launcher_module()
    monkeypatch.setattr(launcher.sys, "version_info", SimpleNamespace(major=3, minor=11))
    monkeypatch.setattr(launcher, "find_adk_spec", lambda: None)

    with pytest.raises(launcher.LauncherError, match=r"\[dev,adk-dev\]"):
        launcher.validate_adk_runtime()


def test_launcher_injects_independent_child_capabilities_for_custom_origin() -> None:
    launcher = _load_launcher_module()
    config = launcher.LocalWorkbenchConfig.from_repo_root(
        REPO_ROOT, control_plane_port=8123, adk_port=8124
    )

    control_plane_env, adk_env = launcher._capability_child_environments(config)

    assert control_plane_env["AI_ACTUARY_OPERATOR_ORIGIN"] == "http://127.0.0.1:8123"
    assert control_plane_env["AI_ACTUARY_OPERATOR_CREDENTIAL"]
    assert control_plane_env["AI_ACTUARY_ADK_CREDENTIAL"]
    assert (
        control_plane_env["AI_ACTUARY_OPERATOR_CREDENTIAL"]
        != control_plane_env["AI_ACTUARY_ADK_CREDENTIAL"]
    )
    assert adk_env["AI_ACTUARY_ADK_CREDENTIAL"] == control_plane_env["AI_ACTUARY_ADK_CREDENTIAL"]
    assert adk_env["AI_ACTUARY_CONTROL_PLANE_URL"] == "http://127.0.0.1:8123"
    assert "AI_ACTUARY_OPERATOR_CREDENTIAL" not in adk_env
    assert "AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN" not in adk_env


def test_launcher_approves_browser_handoff_in_body_without_exposing_bootstrap() -> None:
    launcher = _load_launcher_module()
    captured: list[object] = []

    class _Response:
        status = 200

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self) -> bytes:
            return b'{"approved":true}'

    def opener(request: object, timeout: float) -> _Response:
        captured.extend((request, timeout))
        return _Response()

    bootstrap_token = "launcher-only-bootstrap-secret"
    handoff_id = "browser-visible-handoff-id"
    launcher.approve_operator_handoff(
        control_plane_port=8123,
        bootstrap_token=bootstrap_token,
        handoff_id=handoff_id,
        opener=opener,
    )

    request, timeout = captured
    assert request.full_url == "http://127.0.0.1:8123/auth/operator/handoff/approve"
    assert request.method == "POST"
    assert request.headers["Origin"] == "http://127.0.0.1:8123"
    assert request.headers["Content-type"] == "application/json"
    assert json.loads(request.data) == {
        "bootstrap_token": bootstrap_token,
        "handoff_id": handoff_id,
    }
    assert bootstrap_token not in request.full_url
    assert bootstrap_token not in repr(request.headers)
    assert timeout == 2.0


def test_adk_bearer_client_ignores_hostile_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reserving_workflow.adapters.control_plane.client import (
        AdkControlPlaneClient,
    )

    target, target_thread, target_requests = _start_http_capture(
        {"ok": True, "service": "control-plane"}
    )
    proxy, proxy_thread, proxy_requests = _start_http_capture(
        {"ok": True, "service": "hostile-proxy"}
    )
    _install_hostile_http_proxy(monkeypatch, proxy.server_port)
    credential = "adk-proxy-regression-secret"
    client = AdkControlPlaneClient(
        f"http://127.0.0.1:{target.server_port}",
        credential=credential,
    )
    try:
        response = client.get_health()
    finally:
        client.close()
        _stop_http_capture(target, target_thread)
        _stop_http_capture(proxy, proxy_thread)

    assert response.service == "control-plane"
    assert client.is_closed
    assert proxy_requests == []
    assert len(target_requests) == 1
    assert target_requests[0]["headers"]["Authorization"] == (
        f"Bearer {credential}"
    )


def test_default_read_tool_factory_keeps_adk_auth_with_a_read_only_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from developer_workflows.ai_actuary_developer import tools as adk_tools
    from reserving_workflow.adapters.control_plane import ReadOnlyControlPlaneClient

    target, target_thread, target_requests = _start_http_capture(
        {"ok": True, "service": "control-plane"}
    )
    credential = "adk-read-only-regression-secret"
    monkeypatch.setattr(
        adk_tools,
        "CONTROL_PLANE_BASE_URL",
        f"http://127.0.0.1:{target.server_port}",
    )
    monkeypatch.setenv("AI_ACTUARY_ADK_CREDENTIAL", credential)
    client = adk_tools._default_client_factory()
    try:
        assert type(client) is ReadOnlyControlPlaneClient
        assert not hasattr(client, "start_workflow_run")
    finally:
        client.close()
    try:
        result = adk_tools.get_health()
    finally:
        _stop_http_capture(target, target_thread)

    assert result == {
        "ok": True,
        "data": {"ok": True, "service": "control-plane"},
    }
    assert len(target_requests) == 1
    assert target_requests[0]["method"] == "GET"
    assert target_requests[0]["path"] == "/health"
    assert target_requests[0]["headers"]["Authorization"] == (
        f"Bearer {credential}"
    )


def test_launcher_bootstrap_approval_ignores_hostile_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    target, target_thread, target_requests = _start_http_capture(
        {"approved": True}
    )
    proxy, proxy_thread, proxy_requests = _start_http_capture(
        {"approved": True}
    )
    _install_hostile_http_proxy(monkeypatch, proxy.server_port)
    bootstrap_token = "launcher-only-bootstrap-secret"
    handoff_id = "browser-visible-handoff-id"
    try:
        launcher.approve_operator_handoff(
            control_plane_port=target.server_port,
            bootstrap_token=bootstrap_token,
            handoff_id=handoff_id,
        )
    finally:
        _stop_http_capture(target, target_thread)
        _stop_http_capture(proxy, proxy_thread)

    assert proxy_requests == []
    assert len(target_requests) == 1
    assert target_requests[0]["method"] == "POST"
    assert target_requests[0]["headers"]["Origin"] == (
        f"http://127.0.0.1:{target.server_port}"
    )
    assert json.loads(target_requests[0]["body"]) == {
        "bootstrap_token": bootstrap_token,
        "handoff_id": handoff_id,
    }


def test_interactive_launcher_prompt_approves_only_the_browser_visible_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _load_launcher_module()
    approved: list[dict[str, object]] = []
    bootstrap_token = "launcher-only-bootstrap-secret"
    handoff_id = "browser-visible-handoff-id"
    monkeypatch.setattr("builtins.input", lambda prompt: handoff_id)
    monkeypatch.setattr(
        launcher,
        "approve_operator_handoff",
        lambda **kwargs: approved.append(kwargs),
    )

    thread = launcher.start_operator_handoff_prompt(
        control_plane_port=8123,
        bootstrap_token=bootstrap_token,
    )
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert approved == [
        {
            "control_plane_port": 8123,
            "bootstrap_token": bootstrap_token,
            "handoff_id": handoff_id,
        }
    ]
    output = capsys.readouterr()
    assert bootstrap_token not in output.out
    assert bootstrap_token not in output.err


def test_runtime_requirement_error_explains_python_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    monkeypatch.setattr(launcher.sys, "version_info", SimpleNamespace(major=3, minor=12))

    with pytest.raises(launcher.LauncherError, match="Python 3.11"):
        launcher.validate_adk_runtime()


def test_port_conflict_is_detected_before_children_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    monkeypatch.setattr(launcher, "validate_adk_runtime", lambda: "adk")
    monkeypatch.setattr(launcher, "port_is_available", lambda host, port: port != 8001)
    started: list[list[str]] = []

    result = launcher.run_workbench(
        launcher.LocalWorkbenchConfig.from_repo_root(REPO_ROOT),
        smoke=True,
        popen_factory=lambda command, **kwargs: started.append(command),
    )

    assert result == 1
    assert started == []


class _FakeProcess:
    def __init__(
        self,
        returncode: int | None = None,
        *,
        pid: int = 1234,
    ) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.pid = pid

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", 1)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _FakeWindowsJobApi:
    def __init__(self, *, fail_limit: bool = False) -> None:
        self.fail_limit = fail_limit
        self.created: list[int] = []
        self.limits: list[int] = []
        self.assignments: list[tuple[int, int, int]] = []
        self.closed: list[int] = []

    def create_job(self) -> int:
        self.created.append(101)
        return 101

    def enable_kill_on_close(self, job_handle: int) -> None:
        self.limits.append(job_handle)
        if self.fail_limit:
            raise RuntimeError("limit setup failed")

    def current_process(self) -> tuple[int, int]:
        return -1, 303

    def assign_process(self, job_handle: int, process_handle: int, *, pid: int) -> None:
        self.assignments.append((job_handle, process_handle, pid))

    def close_handle(self, job_handle: int) -> None:
        self.closed.append(job_handle)


def test_windows_job_object_assigns_launcher_before_any_children() -> None:
    launcher = _load_launcher_module()
    api = _FakeWindowsJobApi()
    launcher._WindowsJobObject(api=api)

    assert api.created == [101]
    assert api.limits == [101]
    assert api.assignments == [(101, -1, 303)]
    assert api.closed == []


def test_windows_job_limit_failure_closes_created_handle() -> None:
    launcher = _load_launcher_module()
    api = _FakeWindowsJobApi(fail_limit=True)

    with pytest.raises(RuntimeError, match="limit setup failed"):
        launcher._WindowsJobObject(api=api)

    assert api.closed == [101]


def test_windows_job_access_denied_explains_external_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    kernel_api = launcher._WindowsKernelJobApi.__new__(launcher._WindowsKernelJobApi)
    kernel_api._kernel32 = SimpleNamespace(
        AssignProcessToJobObject=lambda job_handle, process_handle: False
    )
    monkeypatch.setattr(launcher.ctypes, "get_last_error", lambda: 5, raising=False)

    with pytest.raises(launcher.LauncherError, match="external job"):
        kernel_api.assign_process(101, 202, pid=303)


def test_windows_job_creation_failure_starts_no_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    started: list[list[str]] = []
    monkeypatch.setattr(launcher, "validate_adk_runtime", lambda: "adk")
    monkeypatch.setattr(launcher, "ensure_ports_available", lambda config: None)
    monkeypatch.setattr(
        launcher,
        "create_child_containment",
        lambda: (_ for _ in ()).throw(launcher.LauncherError("job create failed")),
    )

    result = launcher.run_workbench(
        launcher.LocalWorkbenchConfig.from_repo_root(REPO_ROOT),
        smoke=True,
        popen_factory=lambda command, **kwargs: started.append(command),
    )

    assert result == 1
    assert started == []


def test_windows_job_assignment_failure_happens_before_child_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    api = _FakeWindowsJobApi()
    started: list[list[str]] = []

    def fail_assignment(job_handle: int, process_handle: int, *, pid: int) -> None:
        api.assignments.append((job_handle, process_handle, pid))
        raise launcher.LauncherError("assignment failed")

    api.assign_process = fail_assignment  # type: ignore[method-assign]
    monkeypatch.setattr(launcher, "validate_adk_runtime", lambda: "adk")
    monkeypatch.setattr(launcher, "ensure_ports_available", lambda config: None)
    monkeypatch.setattr(
        launcher,
        "create_child_containment",
        lambda: launcher._WindowsJobObject(api=api),
    )

    result = launcher.run_workbench(
        launcher.LocalWorkbenchConfig.from_repo_root(REPO_ROOT),
        smoke=True,
        popen_factory=lambda command, **kwargs: started.append(command),
    )

    assert result == 1
    assert started == []
    assert api.closed == [101]


def test_run_workbench_holds_containment_until_supervision_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    api = _FakeWindowsJobApi()
    containment = launcher._WindowsJobObject(api=api)
    children = [
        _FakeProcess(pid=301),
        _FakeProcess(pid=302),
    ]
    monkeypatch.setattr(launcher, "validate_adk_runtime", lambda: "adk")
    monkeypatch.setattr(launcher, "ensure_ports_available", lambda config: None)
    monkeypatch.setattr(launcher, "create_child_containment", lambda: containment)
    monkeypatch.setattr(launcher, "wait_for_smoke_endpoints", lambda **kwargs: None)

    result = launcher.run_workbench(
        launcher.LocalWorkbenchConfig.from_repo_root(REPO_ROOT),
        smoke=True,
        popen_factory=lambda command, **kwargs: children.pop(0),
    )

    assert result == 0
    assert api.assignments == [(101, -1, 303)]
    assert api.closed == []


def test_child_start_failure_cleans_up_started_child() -> None:
    launcher = _load_launcher_module()
    first = _FakeProcess()
    calls = 0

    def popen_factory(command: list[str], **kwargs: object) -> _FakeProcess:
        nonlocal calls
        del command, kwargs
        calls += 1
        if calls == 2:
            raise OSError("adk could not start")
        return first

    with pytest.raises(launcher.LauncherError, match="adk could not start"):
        launcher.start_children([["api"], ["adk"]], popen_factory=popen_factory)

    assert first.terminated is True


def test_unexpected_child_exit_propagates_code_and_cleans_sibling() -> None:
    launcher = _load_launcher_module()
    failed = _FakeProcess(returncode=7)
    sibling = _FakeProcess()

    result = launcher.supervise_children(
        [failed, sibling],
        smoke_check=None,
        stop_requested=lambda: False,
        poll_interval=0,
    )

    assert result == 7
    assert sibling.terminated is True


def test_signaled_child_exit_is_normalized_and_cleans_sibling() -> None:
    launcher = _load_launcher_module()
    failed = _FakeProcess(returncode=-signal.SIGTERM)
    sibling = _FakeProcess()

    result = launcher.supervise_children(
        [failed, sibling],
        smoke_check=None,
        stop_requested=lambda: False,
        poll_interval=0,
    )

    assert result == 128 + signal.SIGTERM
    assert sibling.terminated is True


def test_child_exit_message_uses_the_propagated_return_code() -> None:
    launcher = _load_launcher_module()

    signaled = launcher.ChildExited(-signal.SIGTERM)
    zero = launcher.ChildExited(0)

    assert str(signaled) == f"A workbench child process exited with code {128 + signal.SIGTERM}."
    assert str(zero) == "A workbench child process exited with code 1."


def test_successful_smoke_check_always_cleans_both_children() -> None:
    launcher = _load_launcher_module()
    children = [_FakeProcess(), _FakeProcess()]
    checked: list[object] = []

    result = launcher.supervise_children(
        children,
        smoke_check=lambda processes: checked.extend(processes),
        stop_requested=lambda: False,
        poll_interval=0,
    )

    assert result == 0
    assert checked == children
    assert all(process.terminated for process in children)


def test_sigterm_exit_code_is_propagated_after_child_cleanup() -> None:
    launcher = _load_launcher_module()
    children = [_FakeProcess(), _FakeProcess()]
    signal_state = launcher._SignalState()
    signal_state.handle(signal.SIGTERM, None)

    result = launcher.supervise_children(
        children,
        smoke_check=None,
        stop_requested=signal_state.requested,
        stop_exit_code=signal_state.exit_code,
        poll_interval=0,
    )

    assert result == 128 + signal.SIGTERM
    assert all(process.terminated for process in children)


def test_smoke_helper_checks_all_control_plane_and_adk_routes() -> None:
    launcher = _load_launcher_module()
    requested: list[str] = []

    class _Response:
        status = 200

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self) -> bytes:
            return b"{}"

    def opener(request: object, timeout: float) -> _Response:
        del timeout
        requested.append(request.full_url)  # type: ignore[attr-defined]
        return _Response()

    launcher.wait_for_smoke_endpoints(
        control_plane_port=8123,
        adk_port=8124,
        children=[_FakeProcess(), _FakeProcess()],
        timeout=0.2,
        poll_interval=0,
        opener=opener,
    )

    assert requested == [
        "http://127.0.0.1:8123/health",
        "http://127.0.0.1:8123/health/preflight",
        "http://127.0.0.1:8123/console",
        "http://127.0.0.1:8124/",
        "http://127.0.0.1:8124/list-apps",
        "http://127.0.0.1:8124/apps/ai_actuary_developer/app-info",
        "http://127.0.0.1:8124/dev/apps/ai_actuary_developer/build_graph",
    ]


def test_smoke_helper_ignores_hostile_environment_proxy_and_reaches_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    control, control_thread, control_requests = _start_http_capture(
        {"ok": True, "service": "control-plane"}
    )
    adk, adk_thread, adk_requests = _start_http_capture(
        {"ok": True, "service": "adk-developer"}
    )
    proxy, proxy_thread, proxy_requests = _start_http_capture(
        {"ok": True, "service": "hostile-proxy"}
    )
    _install_hostile_http_proxy(monkeypatch, proxy.server_port)
    try:
        launcher.wait_for_smoke_endpoints(
            control_plane_port=control.server_port,
            adk_port=adk.server_port,
            children=[_FakeProcess(), _FakeProcess()],
            timeout=2.0,
            poll_interval=0,
        )
    finally:
        _stop_http_capture(control, control_thread)
        _stop_http_capture(adk, adk_thread)
        _stop_http_capture(proxy, proxy_thread)

    assert proxy_requests == []
    assert [item["path"] for item in control_requests] == [
        "/health",
        "/health/preflight",
        "/console",
    ]
    assert [item["path"] for item in adk_requests] == [
        "/",
        "/list-apps",
        "/apps/ai_actuary_developer/app-info",
        "/dev/apps/ai_actuary_developer/build_graph",
    ]


def test_smoke_helper_bounds_requests_by_the_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    clock_calls = 0
    request_timeouts: list[float] = []

    class _Response:
        status = 200

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self) -> bytes:
            return b"{}"

    def monotonic() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 10.0 if clock_calls == 1 else 10.15

    def opener(request: object, timeout: float) -> _Response:
        del request
        request_timeouts.append(timeout)
        return _Response()

    monkeypatch.setattr(launcher.time, "monotonic", monotonic)

    launcher.wait_for_smoke_endpoints(
        control_plane_port=8123,
        adk_port=8124,
        children=[_FakeProcess(), _FakeProcess()],
        timeout=0.2,
        poll_interval=0,
        opener=opener,
    )

    assert request_timeouts == pytest.approx([0.05] * 7)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _wait_for_http(url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urlopen(url, timeout=1.0) as response:
                response.read()
                if response.status == 200:
                    return
        except (URLError, TimeoutError, OSError):
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {url}")
        time.sleep(0.1)


def _listening_pids(port: int) -> set[int]:
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        check=True,
    )
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) < 5
            or fields[0].upper() != "TCP"
            or fields[-2].upper() != "LISTENING"
            or not fields[-1].isdigit()
        ):
            continue
        local_address = fields[1]
        if local_address.rsplit(":", 1)[-1] == str(port):
            pids.add(int(fields[-1]))
    return pids


def _windows_process_is_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration test")
@pytest.mark.skipif(
    sys.version_info[:2] != (3, 11) or not _google_adk_available(),
    reason="requires the Python 3.11 adk-dev environment",
)
def test_forced_launcher_termination_releases_both_servers() -> None:
    control_plane_port = _unused_loopback_port()
    adk_port = _unused_loopback_port()
    while adk_port == control_plane_port:
        adk_port = _unused_loopback_port()
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_local_workbench.py"),
        "--control-plane-port",
        str(control_plane_port),
        "--adk-port",
        str(adk_port),
    ]
    launcher = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pids: set[int] = set()
    try:
        _wait_for_http(f"http://127.0.0.1:{control_plane_port}/health", timeout=45.0)
        _wait_for_http(f"http://127.0.0.1:{adk_port}/", timeout=45.0)
        child_pids.update(_listening_pids(control_plane_port))
        child_pids.update(_listening_pids(adk_port))
        assert child_pids

        # Popen.terminate uses TerminateProcess on Windows, bypassing Python's
        # signal handlers and proving kernel-owned job cleanup.
        launcher.terminate()
        launcher.wait(timeout=10.0)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            ports_released = all(
                not _listening_pids(port) for port in (control_plane_port, adk_port)
            )
            children_stopped = all(
                not _windows_process_is_running(pid) for pid in child_pids
            )
            if ports_released and children_stopped:
                break
            time.sleep(0.1)
        assert all(not _listening_pids(port) for port in (control_plane_port, adk_port))
        assert all(not _windows_process_is_running(pid) for pid in child_pids)
    finally:
        if launcher.poll() is None:
            launcher.terminate()
            try:
                launcher.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                launcher.kill()
                launcher.wait(timeout=5.0)
        for pid in child_pids:
            if _windows_process_is_running(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object integration test")
@pytest.mark.skipif(
    sys.version_info[:2] != (3, 11) or not _google_adk_available(),
    reason="requires the Python 3.11 adk-dev environment",
)
def test_parent_death_during_first_popen_still_kills_inherited_child(
    tmp_path: Path,
) -> None:
    control_plane_port = _unused_loopback_port()
    adk_port = _unused_loopback_port()
    while adk_port == control_plane_port:
        adk_port = _unused_loopback_port()
    marker = tmp_path / "first-child.pid"
    probe = """
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

repo_root = Path(sys.argv[1])
control_plane_port = int(sys.argv[2])
adk_port = int(sys.argv[3])
marker = Path(sys.argv[4])
spec = importlib.util.spec_from_file_location(
    "race_probe_workbench", repo_root / "scripts" / "run_local_workbench.py"
)
assert spec is not None and spec.loader is not None
launcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = launcher
spec.loader.exec_module(launcher)

def delayed_first_popen(command, **kwargs):
    child = subprocess.Popen(command, **kwargs)
    marker.write_text(str(child.pid), encoding="ascii")
    time.sleep(60.0)
    return child

config = launcher.LocalWorkbenchConfig.from_repo_root(
    repo_root,
    control_plane_port=control_plane_port,
    adk_port=adk_port,
)
raise SystemExit(
    launcher.run_workbench(config, smoke=False, popen_factory=delayed_first_popen)
)
"""
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            probe,
            str(REPO_ROOT),
            str(control_plane_port),
            str(adk_port),
            str(marker),
        ],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    try:
        marker_deadline = time.monotonic() + 15.0
        while not marker.exists():
            if parent.poll() is not None:
                stdout, stderr = parent.communicate()
                raise AssertionError(
                    f"Race probe exited before first Popen returned: {stdout}\n{stderr}"
                )
            if time.monotonic() >= marker_deadline:
                raise AssertionError("Timed out waiting for delayed first child PID")
            time.sleep(0.05)
        child_pid = int(marker.read_text(encoding="ascii"))
        _wait_for_http(f"http://127.0.0.1:{control_plane_port}/health", timeout=30.0)

        # The popen_factory is still sleeping and start_children has not received
        # its return value. Kernel job inheritance must already contain this child.
        parent.terminate()
        parent.wait(timeout=10.0)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if (
                not _listening_pids(control_plane_port)
                and not _windows_process_is_running(child_pid)
            ):
                break
            time.sleep(0.1)
        assert not _listening_pids(control_plane_port)
        assert not _windows_process_is_running(child_pid)
    finally:
        if parent.poll() is None:
            parent.terminate()
            try:
                parent.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                parent.kill()
                parent.wait(timeout=5.0)
        if child_pid is not None and _windows_process_is_running(child_pid):
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )


@pytest.mark.skipif(
    not _google_adk_available(),
    reason="google-adk is intentionally absent from the default dev extra",
)
def test_developer_agent_is_code_first_gemini_with_bounded_phase5_tools() -> None:
    from developer_workflows.ai_actuary_developer import agent, tools
    from reserving_workflow.adapters.control_plane import ReadOnlyControlPlaneClient

    tool_names = [tool.__name__ for tool in agent.root_agent.tools]
    expected_tool_names = tools.READ_TOOL_NAMES + tools.EXECUTION_TOOL_NAMES + tools.DEBUG_TOOL_NAMES

    assert agent.root_agent.name == "ai_actuary_developer"
    assert agent.root_agent.model == "gemini-2.5-flash"
    assert agent.describe_development_environment()["model"] == agent.root_agent.model
    assert "development-only" in agent.root_agent.description.lower()
    assert "http://127.0.0.1:8000/console" in agent.root_agent.description
    assert tool_names == list(expected_tool_names)
    assert len(agent.root_agent.tools) == len(expected_tool_names) == 23
    assert "explicit ADK confirmation" in agent.root_agent.instruction
    assert "two published Chainladder workflows" in agent.root_agent.instruction
    assert "trusted run IDs" in agent.root_agent.instruction
    assert "legacy path-based" in agent.root_agent.instruction
    assert "review decision" in agent.root_agent.instruction
    assert not any(
        forbidden in tool_name
        for tool_name in tool_names
        for forbidden in ("path", "manifest", "decision", "start_tool")
    )

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "service": "control-plane"})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "service": "control-plane",
                "status": "ok",
                "readiness": "ready",
                "warnings": [],
                "errors": [],
                "summary": {"check_count": 0, "ok_count": 0, "warning_count": 0, "error_count": 0},
                "configuration": {"catalog": {}},
                "runtime": {},
                "checks": [],
            },
        )

    def factory() -> ReadOnlyControlPlaneClient:
        return ReadOnlyControlPlaneClient(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )

    with tools.use_read_client_factory(factory):
        assert agent.check_control_plane_health()["data"] == {"ok": True, "service": "control-plane"}
        assert agent.check_control_plane_preflight()["data"]["readiness"] == "ready"
    environment = agent.describe_development_environment()

    assert requested == [
        "http://127.0.0.1:8000/health",
        "http://127.0.0.1:8000/health/preflight",
    ]
    assert environment["console_url"] == "http://127.0.0.1:8000/console"
    assert environment["scope"] == "development-only"
    assert environment["capability"] == "read-only control-plane inspection"
