"""Real browser smoke for the local Operator Console and ADK workbench."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import sys
import zipfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if SRC_ROOT.is_dir() and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
LOOPBACK_HOST = "127.0.0.1"
EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_ENVIRONMENT_UNAVAILABLE = 2
EXIT_STARTUP_FAILED = 3
LOCAL_OPERATOR_CREDENTIAL = "browser-smoke-operator-credential"
LOCAL_ADK_CREDENTIAL = "browser-smoke-adk-credential"
EXPECTED_PLAYWRIGHT_VERSION = "1.59.0"
EXPECTED_CHROMIUM_VERSION = "147.0.7727.15"
ADK_WORKSPACE_ID = "adk-development"
ADK_SOURCE = "adk-developer"
ADK_BROWSER_SMOKE_USER = "browser-smoke-user"
TEXT_EVIDENCE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".log",
    ".md",
    ".network",
    ".trace",
    ".txt",
}
LEAK_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "credential",
        re.compile(
            rb"browser-smoke-(?:adk|operator)-credential|"
            rb"browser-smoke-bootstrap-token|"
            rb"(?i:Bearer\s+(?!\[redacted\])[A-Za-z0-9._~+/=-]{8,})"
        ),
    ),
    (
        "auth_header",
        re.compile(rb"(?i)\b(?:authorization|proxy-authorization)\s*:"),
    ),
    (
        "cookie",
        re.compile(rb"(?i)\b(?:cookie\s*:|set-cookie\s*:|sessionid\s*=)"),
    ),
    (
        "host_path",
        re.compile(
            rb"(?i)(?:[A-Z]:(?:\\\\|\\|/)(?:Project|Users|Windows|Temp)(?:\\\\|\\|/)|"
            rb"/(?:tmp|var/tmp|private/var/(?:tmp|folders)|run/user)/[^\s\"'<>]+|"
            rb"file:(?://|\\\\)|"
            rb'\"artifact_root\"\s*:|\bartifact_root\s*[=:])'
        ),
    ),
)

from reserving_workflow.runtime.redaction import sanitize_for_runtime, sanitize_text


class BrowserSmokeError(RuntimeError):
    """A real browser smoke assertion failed."""


class EnvironmentUnavailable(BrowserSmokeError):
    """The requested browser smoke mode cannot run in this environment."""


@dataclass(frozen=True)
class SmokeTarget:
    api_url: str
    adk_url: str
    api_port: int
    adk_port: int


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url",
        help="Connect to an already running Operator Console/API origin.",
    )
    parser.add_argument(
        "--adk-url",
        help="Connect to an already running ADK Developer Web origin.",
    )
    parser.add_argument(
        "--connect-only",
        action="store_true",
        help="Do not start the local workbench; use the supplied or port-derived URLs.",
    )
    parser.add_argument(
        "--disable-adk",
        action="store_true",
        help="Run API-only smoke. This mode does not require google.adk.",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=0,
        help="Loopback API port for local start; 0 selects an unused port.",
    )
    parser.add_argument(
        "--adk-port",
        type=int,
        default=0,
        help="Loopback ADK port for local start; 0 selects an unused port.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Maximum seconds to wait for startup and browser checks.",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="Directory for screenshots, trace, network, console, and cleanup evidence.",
    )
    parser.add_argument(
        "--operator-bootstrap-token",
        help="Bootstrap token for an already running API. Local starts generate one.",
    )
    parser.add_argument(
        "--adk-credential",
        help="ADK bearer credential for review-boundary checks against an existing API.",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        help=(
            "Owned local workbench state root. Source starts default to the checkout; "
            "installed starts should pass the same --state-root supplied to ai-actuary-workbench."
        ),
    )
    parser.add_argument(
        "--workbench-command",
        help=(
            "Workbench command to start instead of the source script, for example "
            "'ai-actuary-workbench' from an installed wheel."
        ),
    )
    parser.add_argument(
        "--exercise-review-boundary",
        action="store_true",
        help=(
            "Deprecated no-op retained for older drill commands; the review "
            "boundary is now required in full mode."
        ),
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium with a visible window.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    evidence_dir = _prepare_evidence_dir(args.evidence_dir)
    result: dict[str, Any] = {
        "ok": False,
        "mode": "api-only" if args.disable_adk else "full",
        "evidence_ref": ".",
        "started_local_workbench": False,
    }
    workbench: subprocess.Popen[bytes] | None = None
    target: SmokeTarget | None = None
    exit_code = EXIT_VALIDATION_FAILED
    try:
        target = _resolve_target(args)
        effective_state_root = args.state_root
        if args.workbench_command and effective_state_root is None:
            effective_state_root = REPO_ROOT / "tmp" / "installed-workbench"
        result["target"] = {
            "api_url": target.api_url,
            "adk_url": target.adk_url,
            "api_port": target.api_port,
            "adk_port": target.adk_port,
            "state_root": "installed-workbench-state"
            if args.workbench_command
            else "source-checkout-state",
        }
        start_local = args.api_url is None and not args.connect_only
        if start_local and not args.disable_adk:
            _require_local_adk_runtime()
        bootstrap_token = args.operator_bootstrap_token
        if start_local:
            bootstrap_token = bootstrap_token or _local_smoke_bootstrap_token()
        adk_credential = args.adk_credential or (
            LOCAL_ADK_CREDENTIAL if start_local else None
        )
        if start_local:
            workbench = _start_local_workbench(
                target,
                disable_adk=args.disable_adk,
                evidence_dir=evidence_dir,
                bootstrap_token=bootstrap_token,
                workbench_command=args.workbench_command,
                state_root=effective_state_root,
            )
            result["started_local_workbench"] = True
        _wait_for_readiness(
            target,
            disable_adk=args.disable_adk,
            timeout=args.timeout,
            workbench=workbench,
        )
        browser_result = _run_playwright_checks(
            target,
            disable_adk=args.disable_adk,
            evidence_dir=evidence_dir,
            timeout=args.timeout,
            bootstrap_token=bootstrap_token,
            adk_credential=adk_credential,
            state_root=effective_state_root,
            headed=args.headed,
        )
        result.update(browser_result)
        result["ok"] = True
        exit_code = EXIT_OK
    except EnvironmentUnavailable as exc:
        result.update({"ok": False, "error_code": "environment_unavailable", "message": _safe_message(str(exc))})
        exit_code = EXIT_ENVIRONMENT_UNAVAILABLE
    except BrowserSmokeError as exc:
        result.update({"ok": False, "error_code": "browser_smoke_failed", "message": _safe_message(str(exc))})
        exit_code = EXIT_VALIDATION_FAILED
    except Exception as exc:
        result.update({"ok": False, "error_code": "startup_or_script_failed", "message": _safe_message(str(exc))})
        exit_code = EXIT_STARTUP_FAILED
    finally:
        cleanup = _cleanup_local_workbench(
            workbench,
            target=target,
            disable_adk=args.disable_adk,
            evidence_dir=evidence_dir,
        )
        result["cleanup"] = cleanup
        if exit_code == EXIT_OK and not _cleanup_ports_released(cleanup):
            result.update(
                {
                    "ok": False,
                    "error_code": "cleanup_failed",
                    "message": "Local workbench cleanup did not release all owned ports.",
                }
            )
            exit_code = EXIT_VALIDATION_FAILED
        _write_json(evidence_dir / "result.json", result)
        leak_scan = scan_evidence_tree(evidence_dir)
        result["evidence_leak_scan"] = {
            "ok": leak_scan["ok"],
            "leak_count": leak_scan["leak_count"],
            "scanned_file_count": leak_scan["scanned_file_count"],
        }
        if exit_code == EXIT_OK and not leak_scan["ok"]:
            result.update(
                {
                    "ok": False,
                    "error_code": "evidence_leak_detected",
                    "message": "Browser smoke retained evidence contains secrets, cookies, or host paths.",
                }
            )
            exit_code = EXIT_VALIDATION_FAILED
        _write_json(evidence_dir / "evidence_leak_scan.json", leak_scan)
        _write_json(evidence_dir / "result.json", result)
        print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


def _prepare_evidence_dir(configured: Path | None) -> Path:
    if configured is not None:
        evidence_dir = configured
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        evidence_dir = REPO_ROOT / "tmp" / "browser-smoke-local-workbench" / stamp
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return evidence_dir.resolve()


def _resolve_target(args: argparse.Namespace) -> SmokeTarget:
    api_port = args.api_port if args.api_port != 0 else _unused_loopback_port()
    adk_port = args.adk_port if args.adk_port != 0 else _unused_loopback_port(excluding={api_port})
    if api_port == adk_port:
        raise BrowserSmokeError("API and ADK ports must differ.")
    api_url = (
        _normalize_loopback_origin(args.api_url, purpose="API URL")
        if args.api_url
        else f"http://{LOOPBACK_HOST}:{api_port}"
    )
    adk_url = (
        _normalize_loopback_origin(args.adk_url, purpose="ADK URL")
        if args.adk_url
        else f"http://{LOOPBACK_HOST}:{adk_port}"
    )
    return SmokeTarget(
        api_url=api_url,
        adk_url=adk_url,
        api_port=_port_from_origin(api_url),
        adk_port=_port_from_origin(adk_url),
    )


def _require_local_adk_runtime() -> None:
    missing: list[str] = []
    if importlib.util.find_spec("google.adk") is None:
        missing.append("google.adk")
    if _find_adk_executable() is None:
        missing.append("adk executable")
    if missing:
        raise EnvironmentUnavailable(
            "Full two-UI browser smoke requires "
            + ", ".join(missing)
            + ". Run in the disposable [api,adk-dev] Profile B venv."
        )


def _find_adk_executable() -> str | None:
    adjacent = Path(sys.executable).with_name("adk.exe" if sys.platform == "win32" else "adk")
    if adjacent.is_file():
        return str(adjacent)
    return shutil.which("adk")


def _start_local_workbench(
    target: SmokeTarget,
    *,
    disable_adk: bool,
    evidence_dir: Path,
    bootstrap_token: str,
    workbench_command: str | None,
    state_root: Path | None,
) -> subprocess.Popen[bytes]:
    stdout = (evidence_dir / "local_workbench.stdout.raw.log").open("wb")
    stderr = (evidence_dir / "local_workbench.stderr.raw.log").open("wb")
    if workbench_command:
        command = shlex.split(workbench_command, posix=os.name != "nt")
    else:
        if str(SRC_ROOT) not in sys.path:
            sys.path.insert(0, str(SRC_ROOT))
        command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_local_workbench.py"),
        ]
    command.extend(
        [
            "--api-port",
            str(target.api_port),
            "--adk-port",
            str(target.adk_port),
        ]
    )
    if disable_adk:
        command.append("--disable-adk")
    if state_root is not None and workbench_command:
        command.extend(["--state-root", str(state_root.expanduser().resolve())])
    env = dict(os.environ)
    if not workbench_command:
        env["PYTHONPATH"] = (
            str(SRC_ROOT)
            if not env.get("PYTHONPATH")
            else str(SRC_ROOT) + os.pathsep + env["PYTHONPATH"]
        )
    env["AI_ACTUARY_OPERATOR_CREDENTIAL"] = LOCAL_OPERATOR_CREDENTIAL
    env["AI_ACTUARY_ADK_CREDENTIAL"] = LOCAL_ADK_CREDENTIAL
    env["AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN"] = bootstrap_token
    env["AI_ACTUARY_ADK_URL"] = target.adk_url
    env["AI_ACTUARY_BROWSER_SMOKE_RUNNER"] = "1"
    _write_json(
        evidence_dir / "local_workbench_start.json",
        {
            "command": [
                Path(item).name if index in {0, 1} else item
                for index, item in enumerate(command)
            ],
            "api_url": target.api_url,
            "adk_url": target.adk_url,
            "disable_adk": disable_adk,
            "state_root": "installed-workbench-state" if workbench_command else "source-checkout-state",
            "workbench_command": "installed" if workbench_command else "source",
            "env_overrides": {
                "PYTHONPATH_starts_with_src": (
                    env.get("PYTHONPATH", "").split(os.pathsep)[0] == str(SRC_ROOT)
                ),
                "operator_credential_configured": True,
                "adk_credential_configured": True,
                "bootstrap_token_configured": True,
                "adk_url": target.adk_url,
                "browser_smoke_runner": True,
            },
        },
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            env=env,
        )
        setattr(process, "_ai_actuary_stdout_handle", stdout)
        setattr(process, "_ai_actuary_stderr_handle", stderr)
        return process
    except Exception:
        stdout.close()
        stderr.close()
        raise


def _wait_for_readiness(
    target: SmokeTarget,
    *,
    disable_adk: bool,
    timeout: float,
    workbench: subprocess.Popen[bytes] | None,
) -> None:
    endpoints = [
        f"{target.api_url}/health",
        f"{target.api_url}/health/preflight",
        f"{target.api_url}/console",
    ]
    if not disable_adk:
        endpoints.append(target.adk_url)
    deadline = time.monotonic() + timeout
    pending = list(endpoints)
    while pending:
        if workbench is not None and workbench.poll() is not None:
            raise BrowserSmokeError(
                f"Local workbench exited before readiness with code {workbench.returncode}."
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserSmokeError(f"Timed out waiting for {pending[0]}.")
        try:
            _read_url(pending[0], timeout=min(2.0, remaining))
            pending.pop(0)
        except (HTTPError, URLError, TimeoutError, OSError):
            time.sleep(min(0.1, max(remaining, 0.0)))


def _run_playwright_checks(
    target: SmokeTarget,
    *,
    disable_adk: bool,
    evidence_dir: Path,
    timeout: float,
    bootstrap_token: str | None,
    adk_credential: str | None,
    state_root: Path | None,
    headed: bool,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise EnvironmentUnavailable("Playwright Python package is not installed.") from exc
    playwright_version = importlib.metadata.version("playwright")
    if playwright_version != EXPECTED_PLAYWRIGHT_VERSION:
        raise EnvironmentUnavailable(
            "Playwright version "
            f"{playwright_version} is installed; expected {EXPECTED_PLAYWRIGHT_VERSION}. "
            "Install the repository browser-smoke extra before running this drill."
        )

    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []
    request_failures: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    screenshot_path = evidence_dir / "operator_console.png"
    adk_screenshot_path = evidence_dir / "adk_developer_web.png"
    adk_post_run_screenshot_path = evidence_dir / "adk_developer_web_post_run.png"
    trace_path = evidence_dir / "trace.zip"
    raw_trace_path = evidence_dir / "trace.raw.zip"
    browser_version = "unknown"
    csrf_token: str | None = None
    parity_result: dict[str, Any] | None = None

    def attach_page_events(page: Any, surface: str) -> None:
        page.on(
            "console",
            lambda message: console_messages.append(
                {
                    "surface": surface,
                    "type": message.type,
                    "text": message.text,
                    "location": message.location,
                }
            ),
        )
        page.on("pageerror", lambda exc: page_errors.append(f"{surface}: {exc}"))
        page.on(
            "requestfailed",
            lambda request: request_failures.append(
                {
                    "surface": surface,
                    "route": _safe_route_label(request.url),
                    "method": request.method,
                    "failure": request.failure,
                }
            ),
        )
        page.on(
            "response",
            lambda response: responses.append(
                {
                    "surface": surface,
                    "route": _safe_route_label(response.url),
                    "status": response.status,
                }
            ),
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not headed)
            browser_version = browser.version
            if browser_version != EXPECTED_CHROMIUM_VERSION:
                raise EnvironmentUnavailable(
                    "Playwright Chromium version "
                    f"{browser_version} is installed; expected {EXPECTED_CHROMIUM_VERSION}. "
                    "Run the pinned repository Playwright browser install command."
                )
            context = browser.new_context()
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            try:
                if bootstrap_token:
                    auth_response = context.request.post(
                        f"{target.api_url}/auth/operator/bootstrap",
                        data=json.dumps({"bootstrap_token": bootstrap_token}),
                        headers={
                            "Content-Type": "application/json",
                            "Origin": target.api_url,
                        },
                        timeout=timeout * 1000,
                    )
                    if auth_response.status != 200:
                        raise BrowserSmokeError(
                            f"Operator bootstrap failed with HTTP {auth_response.status}."
                        )
                    auth_payload = auth_response.json()
                    csrf_token = str(auth_payload.get("csrf_token") or "")
                    if not csrf_token:
                        raise BrowserSmokeError("Operator bootstrap did not return a CSRF token.")
                page = context.new_page()
                attach_page_events(page, "operator_console")
                page.goto(f"{target.api_url}/console", wait_until="domcontentloaded", timeout=timeout * 1000)
                heading = page.get_by_role("heading", name="AI Actuary Operator Console")
                heading.wait_for(state="visible", timeout=timeout * 1000)
                link = page.locator("a.developer-web-link")
                link.wait_for(state="visible", timeout=timeout * 1000)
                href = _normalize_loopback_origin(
                    link.get_attribute("href") or "",
                    purpose="Operator Console ADK link",
                )
                if href != target.adk_url:
                    raise BrowserSmokeError(
                        f"Operator Console ADK link used {href}; expected {target.adk_url}."
                    )
                body_text = page.locator("body").inner_text(timeout=timeout * 1000)
                if "Development-only" not in body_text:
                    raise BrowserSmokeError("Operator Console development label is not visible.")
                page.wait_for_timeout(500)
                page.screenshot(path=str(screenshot_path), full_page=True)

                adk_visible = False
                if not disable_adk:
                    adk_page = context.new_page()
                    attach_page_events(adk_page, "adk_developer_web")
                    adk_page.goto(target.adk_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                    adk_page.wait_for_timeout(1000)
                    adk_text = adk_page.locator("body").inner_text(timeout=timeout * 1000)
                    adk_visible = "DEV" in adk_text or "AI Actuary Developer" in adk_text
                    if not adk_visible:
                        raise BrowserSmokeError("ADK Developer Web DEV label is not visible.")
                    adk_page.screenshot(path=str(adk_screenshot_path), full_page=True)
                if not disable_adk:
                    if not csrf_token or not adk_credential:
                        raise BrowserSmokeError(
                            "Full browser smoke requires Operator bootstrap and ADK credentials "
                            "to prove review-boundary parity."
                        )
                    parity_result = _verify_adk_console_api_parity_and_review_boundary(
                        context,
                        page,
                        adk_page,
                        target=target,
                        csrf_token=csrf_token,
                        bootstrap_token=bootstrap_token,
                        adk_credential=adk_credential,
                        state_root=state_root,
                        evidence_dir=evidence_dir,
                        timeout=timeout,
                    )
            finally:
                context.tracing.stop(path=str(raw_trace_path))
                _sanitize_trace_archive(raw_trace_path, trace_path)
                context.close()
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise BrowserSmokeError(f"Timed out during Playwright browser checks: {exc}") from exc
    except PlaywrightError as exc:
        raise BrowserSmokeError(f"Playwright browser checks failed: {exc}") from exc
    finally:
        unexpected_console_errors = [
            item
            for item in console_messages
            if item.get("type") == "error" and not _is_allowed_adk_dev_ui_console_error(item)
        ]
        allowed_console_errors = [
            item
            for item in console_messages
            if item.get("type") == "error" and _is_allowed_adk_dev_ui_console_error(item)
        ]
        unexpected_request_failures = [
            item for item in request_failures if not _is_allowed_adk_dev_ui_request_failure(item)
        ]
        allowed_request_failures = [
            item for item in request_failures if _is_allowed_adk_dev_ui_request_failure(item)
        ]
        _write_json(
            evidence_dir / "console_summary.json",
            {
                "message_count": len(console_messages),
                "allowed_error_count": len(allowed_console_errors),
                "unexpected_error_count": len(unexpected_console_errors),
                "messages": console_messages,
                "page_errors": page_errors,
            },
        )
        _write_json(
            evidence_dir / "network_summary.json",
            {
                "response_count": len(responses),
                "request_failure_count": len(request_failures),
                "allowed_request_failure_count": len(allowed_request_failures),
                "unexpected_request_failure_count": len(unexpected_request_failures),
                "responses": responses,
                "request_failures": request_failures,
            },
        )

    unexpected_console_errors = [
        item
        for item in console_messages
        if item.get("type") == "error" and not _is_allowed_adk_dev_ui_console_error(item)
    ]
    allowed_console_errors = [
        item
        for item in console_messages
        if item.get("type") == "error" and _is_allowed_adk_dev_ui_console_error(item)
    ]
    unexpected_request_failures = [
        item for item in request_failures if not _is_allowed_adk_dev_ui_request_failure(item)
    ]
    allowed_request_failures = [
        item for item in request_failures if _is_allowed_adk_dev_ui_request_failure(item)
    ]
    server_errors = [item for item in responses if int(item.get("status", 0)) >= 500]
    if unexpected_console_errors:
        raise BrowserSmokeError("Unexpected browser console errors were captured.")
    if page_errors:
        raise BrowserSmokeError("Unexpected page errors were captured.")
    if unexpected_request_failures:
        raise BrowserSmokeError("Unexpected browser network request failures were captured.")
    if server_errors:
        raise BrowserSmokeError("Unexpected server error responses were captured.")
    return {
        "browser": {
            "playwright_version": playwright_version,
            "expected_playwright_version": EXPECTED_PLAYWRIGHT_VERSION,
            "chromium_version": browser_version,
            "expected_chromium_version": EXPECTED_CHROMIUM_VERSION,
        },
        "operator_console": {
            "visible": True,
            "developer_link": target.adk_url,
            "development_label_visible": True,
            "screenshot": _evidence_ref(screenshot_path, evidence_dir),
        },
        "adk_developer_web": (
            {
                "mode": "disabled",
                "expected_url": target.adk_url,
                "visible": False,
            }
            if disable_adk
            else {
                "mode": "enabled",
                "expected_url": target.adk_url,
                "visible": True,
                "screenshot": _evidence_ref(adk_screenshot_path, evidence_dir),
            }
        ),
        "adk_console_api_parity": parity_result
        or {
            "checked": False,
            "mode": "disabled" if disable_adk else "unavailable",
        },
        "review_boundary": (
            parity_result.get("review_boundary")
            if parity_result is not None
            else {"checked": False, "mode": "disabled" if disable_adk else "unavailable"}
        ),
        "evidence": {
            "trace": _evidence_ref(trace_path, evidence_dir),
            "network_summary": "network_summary.json",
            "console_summary": "console_summary.json",
            "allowed_adk_dev_ui_console_errors": len(allowed_console_errors),
            "allowed_adk_dev_ui_request_failures": len(allowed_request_failures),
        },
    }


def _verify_adk_console_api_parity_and_review_boundary(
    context: Any,
    page: Any,
    adk_page: Any,
    *,
    target: SmokeTarget,
    csrf_token: str,
    bootstrap_token: str | None,
    adk_credential: str,
    state_root: Path | None,
    evidence_dir: Path,
    timeout: float,
) -> dict[str, Any]:
    case_id = f"browser-smoke-adk-{int(time.time())}"
    session_id = f"browser-smoke-conversation-{int(time.time())}"
    invocation_id = f"browser-smoke-invocation-{int(time.time())}"
    start_result = _start_workflow_run_through_adk_developer_web(
        context,
        target=target,
        workflow_id="chainladder-basic",
        case_id=case_id,
        inputs={
            "sample_name": "RAA",
            "method_variant": "chainladder",
            "review_threshold_origin_count": 2,
        },
        session_id=session_id,
        invocation_id=invocation_id,
        timeout=timeout,
    )
    run_id = str(start_result.get("run_id") or "")
    operation_id = str(start_result.get("operation_id") or "")
    correlation_id = str(start_result.get("correlation_id") or "")
    if not run_id or not operation_id or not correlation_id:
        raise BrowserSmokeError(
            "ADK workflow start did not return run_id, operation_id, and correlation_id."
        )
    _write_json(
        evidence_dir / "adk_developer_protocol_start.json",
        {
            "session_id": session_id,
            "invocation_id": invocation_id,
            "run_id": run_id,
            "operation_id": operation_id,
            "correlation_id": correlation_id,
            "source": "adk_developer_web_protocol",
            "events": start_result.get("adk_events", []),
        },
    )

    operator_run = _wait_for_run_status(
        context,
        target=target,
        run_id=run_id,
        timeout=timeout,
    )
    run_status = str((operator_run.get("run") or {}).get("status") or "")
    if run_status == "failed":
        raise BrowserSmokeError("ADK workflow execution failed before review-boundary checks.")
    review_state_source = "workflow"
    if run_status != "needs_review":
        seeded = _seed_review_required_state(
            run_id=run_id,
            state_root=state_root,
            fallback_case_id=case_id,
            evidence_dir=evidence_dir,
        )
        review_state_source = seeded["source"]

    review_id = f"review-{run_id}"
    adk_headers = _adk_bearer_headers(adk_credential)
    operator_run = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}",
        expected_status=200,
        timeout=timeout,
        description="Operator run detail",
    )
    adk_run = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}",
        expected_status=200,
        timeout=timeout,
        headers=adk_headers,
        description="ADK run detail",
    )
    operator_review = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}/review",
        expected_status=200,
        timeout=timeout,
        description="Operator run review",
    )
    adk_review = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}/review",
        expected_status=200,
        timeout=timeout,
        headers=adk_headers,
        description="ADK run review",
    )
    operator_artifacts = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}/artifacts",
        expected_status=200,
        timeout=timeout,
        description="Operator run artifacts",
    )
    adk_artifacts = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}/artifacts",
        expected_status=200,
        timeout=timeout,
        headers=adk_headers,
        description="ADK run artifacts",
    )

    _assert_run_parity(
        operator_run=operator_run,
        adk_run=adk_run,
        run_id=run_id,
        expected_status="needs_review",
        correlation_id=correlation_id,
    )
    _assert_review_parity(
        operator_review=operator_review,
        adk_review=adk_review,
        run_id=run_id,
        review_id=review_id,
        expected_status="review_required",
    )
    _assert_artifact_parity(
        operator_artifacts=operator_artifacts,
        adk_artifacts=adk_artifacts,
        required_ids={"run_manifest", "workflow_summary", "review_packet"},
    )
    adk_developer_protocol_evidence = _capture_adk_developer_session_evidence(
        context,
        target=target,
        session_id=session_id,
        invocation_id=invocation_id,
        run_id=run_id,
        operation_id=operation_id,
        correlation_id=correlation_id,
        review_id=review_id,
        expected_status="needs_review",
        expected_review_status="review_required",
        expected_artifact_ids=_artifact_ids(operator_artifacts),
        evidence_dir=evidence_dir,
        timeout=timeout,
    )
    rendered_adk_evidence = _capture_rendered_adk_post_run_evidence(
        adk_page,
        target=target,
        session_id=session_id,
        run_id=run_id,
        operation_id=operation_id,
        correlation_id=correlation_id,
        expected_status="needs_review",
        expected_review_status="review_required",
        expected_artifact_ids=_artifact_ids(operator_artifacts),
        evidence_dir=evidence_dir,
        timeout=timeout,
    )

    workflow_isolation = _verify_draft_published_isolation(
        context,
        target=target,
        adk_headers=adk_headers,
        adk_credential=adk_credential,
        timeout=timeout,
    )
    negative_security_checks = {
        "host_origin": _verify_negative_host_origin_rejection(
            context,
            target=target,
            bootstrap_token=bootstrap_token,
            timeout=timeout,
        ),
    }

    page.goto(f"{target.api_url}/console?run_id={run_id}", wait_until="domcontentloaded", timeout=timeout * 1000)
    page.wait_for_function(
        """([expectedRunId, expectedReviewId]) => {
          const body = document.body ? document.body.innerText : "";
          const reviewInput = document.getElementById("review-id-input");
          return body.includes(expectedRunId)
            && reviewInput
            && reviewInput.value === expectedReviewId;
        }""",
        arg=[run_id, review_id],
        timeout=timeout * 1000,
    )
    page.screenshot(path=str(evidence_dir / "operator_console_review_required.png"), full_page=True)

    decision_payload = {
        "decision": "approved",
        "comment": "Browser smoke approval.",
        "decided_by": "browser-smoke-operator",
    }
    adk_forbidden = context.request.post(
        f"{target.api_url}/reviews/{review_id}/decision",
        data=_canonical_json(decision_payload),
        headers={
            "Content-Type": "application/json",
            "Origin": target.api_url,
            "Authorization": f"Bearer {adk_credential}",
        },
        timeout=timeout * 1000,
    )
    if adk_forbidden.status != 403:
        raise BrowserSmokeError(
            f"ADK review decision boundary returned HTTP {adk_forbidden.status}; expected 403."
        )
    negative_security_checks["csrf_mutation"] = _verify_csrf_mutation_rejection(
        context,
        target=target,
        review_id=review_id,
        decision_payload=decision_payload,
        timeout=timeout,
    )

    operator_decision = context.request.post(
        f"{target.api_url}/reviews/{review_id}/decision",
        data=_canonical_json(decision_payload),
        headers={
            "Content-Type": "application/json",
            "Origin": target.api_url,
            "X-CSRF-Token": csrf_token,
        },
        timeout=timeout * 1000,
    )
    if operator_decision.status != 200:
        raise BrowserSmokeError(
            f"Operator review decision returned HTTP {operator_decision.status}; expected 200."
        )
    operator_payload = operator_decision.json()
    decision = operator_payload.get("decision") or {}
    review = operator_payload.get("review") or {}
    if decision.get("decision") != "approved":
        raise BrowserSmokeError("Operator review decision did not persist as approved.")
    decision_artifacts = ((review.get("decision") or {}).get("artifacts") or [])
    if not decision_artifacts:
        raise BrowserSmokeError("Operator review decision did not expose decision artifacts.")

    post_decision_operator_review = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}/review",
        expected_status=200,
        timeout=timeout,
        description="Operator decided review",
    )
    post_decision_adk_review = _fetch_json(
        context,
        f"{target.api_url}/runs/{run_id}/review",
        expected_status=200,
        timeout=timeout,
        headers=adk_headers,
        description="ADK decided review",
    )
    _assert_review_parity(
        operator_review=post_decision_operator_review,
        adk_review=post_decision_adk_review,
        run_id=run_id,
        review_id=review_id,
        expected_status="review_decided",
    )
    negative_security_checks["adk_rotation"] = (
        _verify_rotated_credential_rejects_formerly_valid_adk(
            context,
            target=target,
            run_id=run_id,
            formerly_valid_adk_credential=adk_credential,
            evidence_dir=evidence_dir,
            timeout=timeout,
        )
    )

    page.goto(f"{target.api_url}/console?run_id={run_id}", wait_until="domcontentloaded", timeout=timeout * 1000)
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Decision: approved")""",
        timeout=timeout * 1000,
    )
    page.screenshot(path=str(evidence_dir / "operator_console_review_decided.png"), full_page=True)

    result = {
        "checked": True,
        "run_id": run_id,
        "operation_id": operation_id,
        "correlation_id": correlation_id,
        "review_state_source": review_state_source,
        "operator_status": (operator_run.get("run") or {}).get("status"),
        "adk_status": (adk_run.get("run") or {}).get("status"),
        "operator_review_status": (operator_review.get("review") or {}).get("status"),
        "adk_review_status": (adk_review.get("review") or {}).get("status"),
        "artifact_ids": sorted(_artifact_ids(operator_artifacts)),
        "adk_artifact_ids": sorted(_artifact_ids(adk_artifacts)),
        "adk_developer_protocol_evidence": adk_developer_protocol_evidence,
        "rendered_adk_evidence": rendered_adk_evidence,
        "workflow_isolation": workflow_isolation,
        "negative_security_checks": negative_security_checks,
        "review_boundary": {
            "checked": True,
            "run_id": run_id,
            "run_status": "needs_review",
            "review_id": review_id,
            "adk_decision_status": adk_forbidden.status,
            "operator_decision_status": operator_decision.status,
            "operator_decision": decision.get("decision"),
            "decision_artifact_count": len(decision_artifacts),
            "post_decision_operator_review_status": (
                post_decision_operator_review.get("review") or {}
            ).get("status"),
            "post_decision_adk_review_status": (
                post_decision_adk_review.get("review") or {}
            ).get("status"),
        },
        "console": {
            "review_required_screenshot": "operator_console_review_required.png",
            "review_decided_screenshot": "operator_console_review_decided.png",
        },
    }
    _write_json(evidence_dir / "adk_console_api_parity.json", result)
    _write_json(evidence_dir / "review_boundary.json", result["review_boundary"])
    return result


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _adk_start_headers(
    *,
    payload: dict[str, Any],
    idempotency_key: str,
    adk_credential: str,
) -> dict[str, str]:
    request_fingerprint = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    confirmation = hmac.new(
        adk_credential.encode("utf-8"),
        f"{idempotency_key}:{request_fingerprint}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-ADK-Confirmation": confirmation,
        "Authorization": f"Bearer {adk_credential}",
    }


def _adk_bearer_headers(adk_credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {adk_credential}"}


def _response_json(response: Any, *, expected_status: int, description: str) -> dict[str, Any]:
    if response.status != expected_status:
        body = _safe_message(response.text())
        raise BrowserSmokeError(
            f"{description} returned HTTP {response.status}; expected {expected_status}. Body: {body[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise BrowserSmokeError(f"{description} did not return a JSON object.")
    return payload


def _fetch_json(
    context: Any,
    url: str,
    *,
    expected_status: int,
    timeout: float,
    description: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = context.request.get(url, headers=headers, timeout=timeout * 1000)
    return _response_json(response, expected_status=expected_status, description=description)


def _wait_for_run_status(
    context: Any,
    *,
    target: SmokeTarget,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = _fetch_json(
            context,
            f"{target.api_url}/runs/{run_id}",
            expected_status=200,
            timeout=timeout,
            description="ADK workflow run polling",
        )
        last_payload = payload
        status = str((payload.get("run") or {}).get("status") or "")
        if status not in {"queued", "running"}:
            return payload
        time.sleep(0.25)
    pending = str(((last_payload or {}).get("run") or {}).get("status") or "unknown")
    raise BrowserSmokeError(f"Timed out waiting for ADK workflow run {run_id}; last status {pending}.")


def _seed_review_required_state(
    *,
    run_id: str,
    state_root: Path | None,
    fallback_case_id: str,
    evidence_dir: Path,
) -> dict[str, str]:
    root = _effective_state_root(state_root)
    registry_path = root / "tmp" / "run-registry.json"
    review_store = root / "tmp" / "reviews"
    if not registry_path.is_file():
        raise BrowserSmokeError(
            "Full browser smoke could not seed deterministic review state because "
            f"the owned registry is unavailable at {registry_path}."
        )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    runs = registry.get("runs")
    if not isinstance(runs, list):
        raise BrowserSmokeError("Owned run registry has an invalid shape.")
    entry = next((item for item in runs if item.get("run_id") == run_id), None)
    if not isinstance(entry, dict):
        raise BrowserSmokeError(f"Owned run registry does not contain ADK run {run_id}.")
    artifact_root = Path(str(entry.get("artifact_root") or "")).expanduser().resolve()
    if not artifact_root.is_dir():
        raise BrowserSmokeError("ADK parity run artifact root is unavailable for review seeding.")
    case_id = str(entry.get("case_id") or fallback_case_id)
    review_id = f"review-{run_id}"
    packet = {
        "status": "review_required",
        "run_id": run_id,
        "case_id": case_id,
        "workspace_id": ADK_WORKSPACE_ID,
        "case_summary": "Browser smoke deterministic review-required ADK run.",
        "assigned_to": "browser-smoke-operator",
        "review_reasons": ["browser_smoke_review_required"],
        "failed_checks": ["browser_smoke_review_required"],
        "automated_result": {
            "status": "review_required",
            "summary": "The browser smoke forced a deterministic review boundary.",
        },
        "review_checklist": [
            {
                "id": "browser-smoke-review-boundary",
                "title": "Review boundary",
                "question": "Can only the Operator capability record the review decision?",
            }
        ],
        "decision_note": "Approve in the browser smoke only after ADK receives 403.",
        "artifact_links": {"run_manifest": "run_manifest.json"},
    }
    _write_json(artifact_root / "review_packet.json", packet)
    (artifact_root / "review_packet.md").write_text(
        "\n".join(
            [
                f"# Browser smoke review — {case_id}",
                "",
                f"- Run ID: `{run_id}`",
                "- Status: `review_required`",
                "- Reason: browser_smoke_review_required",
            ]
        ),
        encoding="utf-8",
    )
    manifest_path = artifact_root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    artifact_paths = dict(manifest.get("artifact_paths") or {})
    artifact_paths["run_manifest"] = "run_manifest.json"
    artifact_paths["review_packet"] = "review_packet.json"
    artifact_paths["review_packet_markdown"] = "review_packet.md"
    manifest.update(
        {
            "workflow_id": entry.get("workflow_id") or manifest.get("workflow_id") or "chainladder-basic",
            "case_id": case_id,
            "run_id": run_id,
            "artifact_paths": artifact_paths,
            "status": "review_required",
        }
    )
    provenance = entry.get("provenance")
    if isinstance(provenance, dict):
        manifest.update(provenance)
    _write_json(manifest_path, manifest)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry["status"] = "needs_review"
    entry["review_required"] = True
    entry["summary"] = "Browser smoke deterministic ADK run requires Operator review."
    entry["updated_at"] = now
    entry["errors"] = ["browser_smoke_review_required"]
    entry.setdefault("status_history", []).append(
        {
            "status": "needs_review",
            "timestamp": now,
            "summary": entry["summary"],
            "event_type": "browser_smoke.review_required",
            "provenance": provenance,
        }
    )
    _write_json(registry_path, registry)

    review_record = {
        "review_id": review_id,
        "run_id": run_id,
        "case_id": case_id,
        "status": "review_required",
        "reason_codes": ["browser_smoke_review_required"],
        "assigned_to": "browser-smoke-operator",
        "workspace_id": ADK_WORKSPACE_ID,
        "packet": packet,
        "created_at": now,
        "updated_at": now,
        "decision": None,
    }
    _write_json(review_store / review_id / "review_record.json", review_record)
    _write_json(
        evidence_dir / "review_state_seed.json",
        {
            "source": "seeded_after_real_adk_execution",
            "run_id": run_id,
            "review_id": review_id,
            "registry": "owned-local-run-registry",
        },
    )
    return {"source": "seeded_after_real_adk_execution"}


def _effective_state_root(configured: Path | None) -> Path:
    return (configured or REPO_ROOT).expanduser().resolve()


def _assert_run_parity(
    *,
    operator_run: dict[str, Any],
    adk_run: dict[str, Any],
    run_id: str,
    expected_status: str,
    correlation_id: str,
) -> None:
    operator_payload = operator_run.get("run") or {}
    adk_payload = adk_run.get("run") or {}
    if operator_payload.get("run_id") != run_id or adk_payload.get("run_id") != run_id:
        raise BrowserSmokeError("ADK/API parity run identity mismatch.")
    if operator_payload.get("status") != expected_status or adk_payload.get("status") != expected_status:
        raise BrowserSmokeError("ADK/API parity run status mismatch.")
    if operator_payload.get("source") != ADK_SOURCE or adk_payload.get("source") != ADK_SOURCE:
        raise BrowserSmokeError("ADK/API parity run source mismatch.")
    operator_provenance = operator_payload.get("provenance") or {}
    adk_provenance = adk_payload.get("provenance") or {}
    for payload in (operator_provenance, adk_provenance):
        if payload.get("correlation_id") != correlation_id:
            raise BrowserSmokeError("ADK/API parity correlation ID mismatch.")
        if payload.get("source") != ADK_SOURCE:
            raise BrowserSmokeError("ADK/API parity provenance source mismatch.")
    if "artifact_root" in adk_payload:
        raise BrowserSmokeError("ADK run detail exposed an artifact_root path.")


def _assert_review_parity(
    *,
    operator_review: dict[str, Any],
    adk_review: dict[str, Any],
    run_id: str,
    review_id: str,
    expected_status: str,
) -> None:
    operator_payload = operator_review.get("review") or {}
    adk_payload = adk_review.get("review") or {}
    for payload in (operator_payload, adk_payload):
        if payload.get("run_id") != run_id:
            raise BrowserSmokeError("ADK/API parity review run_id mismatch.")
        if payload.get("review_id") != review_id:
            raise BrowserSmokeError("ADK/API parity review_id mismatch.")
        if payload.get("status") != expected_status:
            raise BrowserSmokeError("ADK/API parity review status mismatch.")


def _assert_artifact_parity(
    *,
    operator_artifacts: dict[str, Any],
    adk_artifacts: dict[str, Any],
    required_ids: set[str],
) -> None:
    operator_ids = _artifact_ids(operator_artifacts)
    adk_ids = _artifact_ids(adk_artifacts)
    if not required_ids <= operator_ids:
        raise BrowserSmokeError("Operator artifact list is missing required logical artifacts.")
    if not required_ids <= adk_ids:
        raise BrowserSmokeError("ADK artifact list is missing required logical artifacts.")
    for artifact in adk_artifacts.get("artifacts", []) or []:
        if any(key in artifact for key in ("path", "absolute_path", "artifact_root")):
            raise BrowserSmokeError("ADK artifact metadata exposed local paths.")


def _artifact_ids(payload: dict[str, Any]) -> set[str]:
    return {
        str(item.get("artifact_id"))
        for item in payload.get("artifacts", []) or []
        if isinstance(item, dict) and item.get("artifact_id")
    }


def _capture_rendered_adk_post_run_evidence(
    adk_page: Any,
    *,
    target: SmokeTarget,
    session_id: str,
    run_id: str,
    operation_id: str,
    correlation_id: str,
    expected_status: str,
    expected_review_status: str,
    expected_artifact_ids: set[str],
    evidence_dir: Path,
    timeout: float,
) -> dict[str, Any]:
    del session_id
    ui_url = f"{target.adk_url}/"
    adk_page.goto(ui_url, wait_until="domcontentloaded", timeout=timeout * 1000)
    adk_page.wait_for_timeout(500)
    body_text = adk_page.locator("body").inner_text(timeout=timeout * 1000)
    required_values = {
        "run_id": run_id,
        "operation_id": operation_id,
        "correlation_id": correlation_id,
        "status": expected_status,
        "review_status": expected_review_status,
    }
    visible_field_checks = {
        label: expected in body_text for label, expected in required_values.items()
    }
    missing_artifacts = sorted(
        artifact_id for artifact_id in expected_artifact_ids if artifact_id not in body_text
    )
    missing_fields = sorted(label for label, visible in visible_field_checks.items() if not visible)
    rendered_fields_complete = not missing_fields and not missing_artifacts
    screenshot = evidence_dir / "adk_developer_web_post_run.png"
    adk_page.screenshot(path=str(screenshot), full_page=True)
    return {
        "checked": True,
        "source": "adk_developer_web_ui_session_ux",
        "surface": "adk_developer_web_ui",
        "ui_route": "/",
        "screenshot": _evidence_ref(screenshot, evidence_dir),
        "visible_fields": sorted(
            label for label, visible in visible_field_checks.items() if visible
        ),
        "visible_artifact_ids": sorted(
            artifact_id for artifact_id in expected_artifact_ids if artifact_id in body_text
        ),
        "rendered_fields_complete": rendered_fields_complete,
        "missing_visible_fields": missing_fields,
        "missing_visible_artifact_ids": missing_artifacts,
        "limitation": None
        if rendered_fields_complete
        else (
            "The stock ADK Developer Web UI was loaded after the run, but it did not "
            "render every AI Actuary run/review/artifact field. Protocol/API parity "
            "evidence remains in adk_developer_session_after_run.json and "
            "adk_console_api_parity.json; this screenshot is retained only as "
            "ADK UI/session UX proof, not as REST JSON."
        ),
    }


def _verify_draft_published_isolation(
    context: Any,
    *,
    target: SmokeTarget,
    adk_headers: dict[str, str],
    adk_credential: str,
    timeout: float,
) -> dict[str, Any]:
    workflow = _fetch_json(
        context,
        f"{target.api_url}/workflows/chainladder-basic",
        expected_status=200,
        timeout=timeout,
        headers=adk_headers,
        description="ADK published workflow read",
    )
    if workflow.get("workflow_id") != "chainladder-basic" or workflow.get("builtin") is not True:
        raise BrowserSmokeError("ADK published workflow catalog did not expose the built-in workflow.")
    draft_payload = {
        "workflow_id": "browser-smoke-draft-only",
        "case_id": f"browser-smoke-draft-{int(time.time())}",
        "inputs": {},
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "browser-smoke-draft-session",
        "adk_invocation_id": "browser-smoke-draft-invocation",
    }
    draft_key = f"browser-smoke-draft-{int(time.time() * 1000)}"
    draft_response = context.request.post(
        f"{target.api_url}/adk/runs",
        data=_canonical_json(draft_payload),
        headers=_adk_start_headers(
            payload=draft_payload,
            idempotency_key=draft_key,
            adk_credential=adk_credential,
        ),
        timeout=timeout * 1000,
    )
    if draft_response.status not in {400, 422}:
        raise BrowserSmokeError(
            f"Unpublished draft workflow start returned HTTP {draft_response.status}; expected 400 or 422."
        )
    return {
        "published_workflow_id": workflow.get("workflow_id"),
        "published_builtin": workflow.get("builtin"),
        "draft_only_start_status": draft_response.status,
    }


def _verify_negative_host_origin_rejection(
    context: Any,
    *,
    target: SmokeTarget,
    bootstrap_token: str | None,
    timeout: float,
) -> dict[str, Any]:
    del context
    bad_host_status = _http_status_no_cookies(
        f"{target.api_url}/console",
        headers={"Host": "example.invalid"},
        timeout=timeout,
    )
    if bad_host_status != 403:
        raise BrowserSmokeError(
            f"Bad Host check returned HTTP {bad_host_status}; expected 403."
        )
    origin_status: int | None = None
    if bootstrap_token:
        origin_status = _http_status_no_cookies(
            f"{target.api_url}/auth/operator/bootstrap",
            method="POST",
            data=_canonical_json({"bootstrap_token": bootstrap_token}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "http://example.invalid",
            },
            timeout=timeout,
        )
        if origin_status != 403:
            raise BrowserSmokeError(
                f"Bad Origin check returned HTTP {origin_status}; expected 403."
            )
    return {
        "checked": True,
        "bad_host_status": bad_host_status,
        "bad_origin_status": origin_status,
    }


def _verify_csrf_mutation_rejection(
    context: Any,
    *,
    target: SmokeTarget,
    review_id: str,
    decision_payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    missing = context.request.post(
        f"{target.api_url}/reviews/{review_id}/decision",
        data=_canonical_json(decision_payload),
        headers={
            "Content-Type": "application/json",
            "Origin": target.api_url,
        },
        timeout=timeout * 1000,
    )
    invalid = context.request.post(
        f"{target.api_url}/reviews/{review_id}/decision",
        data=_canonical_json(decision_payload),
        headers={
            "Content-Type": "application/json",
            "Origin": target.api_url,
            "X-CSRF-Token": "browser-smoke-invalid-csrf-token",
        },
        timeout=timeout * 1000,
    )
    if missing.status != 403:
        raise BrowserSmokeError(
            f"Missing CSRF review decision returned HTTP {missing.status}; expected 403."
        )
    if invalid.status != 403:
        raise BrowserSmokeError(
            f"Invalid CSRF review decision returned HTTP {invalid.status}; expected 403."
        )
    return {
        "checked": True,
        "missing_csrf_status": missing.status,
        "invalid_csrf_status": invalid.status,
    }


def _verify_rotated_credential_rejects_formerly_valid_adk(
    context: Any,
    *,
    target: SmokeTarget,
    run_id: str,
    formerly_valid_adk_credential: str,
    evidence_dir: Path,
    timeout: float,
) -> dict[str, Any]:
    del context
    status_evidence: dict[str, Any] = {
        "checked": False,
        "run_id": run_id,
    }

    def record() -> None:
        _write_json(evidence_dir / "adk_rotation_status.json", status_evidence)

    before_status = _http_status_no_cookies(
        f"{target.api_url}/runs/{run_id}",
        headers={"Authorization": f"Bearer {formerly_valid_adk_credential}"},
        timeout=timeout,
    )
    status_evidence["formerly_valid_status_before_rotation"] = before_status
    record()
    if before_status != 200:
        raise BrowserSmokeError(
            f"ADK rotation precheck returned HTTP {before_status}; expected 200."
        )
    rotated_credential = "browser-smoke-adk-credential-rotated"
    rotation_status = _http_status_no_cookies(
        f"{target.api_url}/adk/browser-smoke/rotate-credential",
        method="POST",
        data=_canonical_json({"new_credential": rotated_credential}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {formerly_valid_adk_credential}",
        },
        timeout=timeout,
    )
    status_evidence["rotation_status"] = rotation_status
    record()
    if rotation_status != 200:
        raise BrowserSmokeError(
            f"ADK rotation returned HTTP {rotation_status}; expected 200."
        )
    rejected_status = _http_status_no_cookies(
        f"{target.api_url}/runs/{run_id}",
        headers={"Authorization": f"Bearer {formerly_valid_adk_credential}"},
        timeout=timeout,
    )
    status_evidence["rotated_credential_rejected_status"] = rejected_status
    record()
    if rejected_status != 401:
        raise BrowserSmokeError(
            f"ADK rotation old-token check returned HTTP {rejected_status}; expected 401."
        )
    accepted_status = _http_status_no_cookies(
        f"{target.api_url}/runs/{run_id}",
        headers={"Authorization": f"Bearer {rotated_credential}"},
        timeout=timeout,
    )
    status_evidence["new_credential_accepted_status"] = accepted_status
    record()
    if accepted_status != 200:
        raise BrowserSmokeError(
            f"ADK rotation new-token check returned HTTP {accepted_status}; expected 200."
        )
    status_evidence.update({
        "checked": True,
    })
    record()
    return status_evidence


def _capture_adk_developer_session_evidence(
    context: Any,
    *,
    target: SmokeTarget,
    session_id: str,
    invocation_id: str,
    run_id: str,
    operation_id: str,
    correlation_id: str,
    review_id: str,
    expected_status: str,
    expected_review_status: str,
    expected_artifact_ids: set[str],
    evidence_dir: Path,
    timeout: float,
) -> dict[str, Any]:
    inspect_result = _inspect_workflow_run_through_adk_developer_web(
        context,
        target=target,
        session_id=session_id,
        invocation_id=f"{invocation_id}-inspect",
        run_id=run_id,
        timeout=timeout,
    )
    summary = inspect_result.get("summary")
    if not isinstance(summary, dict):
        raise BrowserSmokeError("ADK Developer Web inspect did not return a run summary.")
    if summary.get("run_id") != run_id:
        raise BrowserSmokeError("ADK Developer Web inspect returned the wrong run ID.")
    if summary.get("status") != expected_status:
        raise BrowserSmokeError(
            "ADK Developer Web inspect returned run status "
            f"{summary.get('status')}; expected {expected_status}."
        )
    if summary.get("review_status") != expected_review_status:
        raise BrowserSmokeError(
            "ADK Developer Web inspect returned review status "
            f"{summary.get('review_status')}; expected {expected_review_status}."
        )
    summary_artifact_ids = set(summary.get("artifact_ids") or [])
    missing_artifacts = expected_artifact_ids - summary_artifact_ids
    if missing_artifacts:
        raise BrowserSmokeError(
            "ADK Developer Web inspect omitted logical artifacts: "
            f"{', '.join(sorted(missing_artifacts))}."
        )

    session_url = (
        f"{target.adk_url}/apps/ai_actuary_developer/users/"
        f"{ADK_BROWSER_SMOKE_USER}/sessions/{session_id}"
    )
    session_response = context.request.get(session_url, timeout=timeout * 1000)
    session_payload = _response_json(
        session_response,
        expected_status=200,
        description="ADK Developer Web post-run session",
    )
    session_events = session_payload.get("events")
    if not isinstance(session_events, list) or not session_events:
        raise BrowserSmokeError("ADK Developer Web post-run session did not expose events.")
    session_start = _find_adk_start_result(session_events)
    if session_start is None or session_start.get("run_id") != run_id:
        raise BrowserSmokeError("ADK Developer Web post-run session omitted the workflow start event.")
    if session_start.get("operation_id") != operation_id:
        raise BrowserSmokeError("ADK Developer Web post-run session operation ID did not match.")
    if session_start.get("correlation_id") != correlation_id:
        raise BrowserSmokeError("ADK Developer Web post-run session correlation ID did not match.")
    if _find_adk_confirmation_request(session_events) is None:
        raise BrowserSmokeError("ADK Developer Web post-run session omitted the confirmation event.")

    trace_id = f"ai_actuary_developer:{session_id}:{invocation_id}"
    trace_evidence = _fetch_adk_trace_evidence(
        context,
        target=target,
        session_id=session_id,
        invocation_id=invocation_id,
        run_id=run_id,
        correlation_id=correlation_id,
        session_events=session_events,
        evidence_dir=evidence_dir,
        timeout=timeout,
    )
    summary_evidence = dict(summary)
    provenance = summary.get("provenance")
    if isinstance(provenance, dict):
        provenance_evidence = dict(provenance)
        adk_session_id = provenance_evidence.pop("adk_session_id", None)
        if adk_session_id is not None:
            provenance_evidence["adk_conversation"] = adk_session_id
        summary_evidence["provenance"] = provenance_evidence

    evidence = {
        "source": "adk_developer_web_protocol",
        "adk_conversation": session_id,
        "invocation_id": invocation_id,
        "run_id": run_id,
        "operation_id": operation_id,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "review_id": review_id,
        "summary": summary_evidence,
        "inspect_events": inspect_result.get("adk_events", []),
        "conversation_events": _adk_event_summary(session_events),
        "trace_evidence": trace_evidence,
    }
    _write_json(evidence_dir / "adk_developer_session_after_run.json", evidence)
    return {
        "protocol_start": "adk_developer_protocol_start.json",
        "post_run_evidence": "adk_developer_session_after_run.json",
        "adk_conversation": session_id,
        "conversation_event_count": len(session_events),
        "trace_id": trace_id,
        "trace_available": bool(trace_evidence.get("available")),
    }


def _inspect_workflow_run_through_adk_developer_web(
    context: Any,
    *,
    target: SmokeTarget,
    session_id: str,
    invocation_id: str,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    events = _run_adk_sse(
        context,
        target=target,
        user_id=ADK_BROWSER_SMOKE_USER,
        session_id=session_id,
        invocation_id=invocation_id,
        new_message=_adk_text_message(
            "Use the summarize_run tool to inspect run "
            f"{run_id} and report its status, artifacts, and review state."
        ),
        state_delta={"browser_smoke_inspect": {"run_id": run_id}},
        timeout=timeout,
        description="ADK Developer Web post-run inspect",
    )
    summary = _find_adk_summary_result(events, run_id=run_id)
    if summary is None:
        raise BrowserSmokeError("ADK Developer Web post-run inspect did not summarize the run.")
    summary["adk_events"] = _adk_event_summary(events)
    return summary


def _fetch_adk_trace_evidence(
    context: Any,
    *,
    target: SmokeTarget,
    session_id: str,
    invocation_id: str,
    run_id: str,
    correlation_id: str,
    session_events: list[dict[str, Any]] | None = None,
    evidence_dir: Path | None = None,
    timeout: float,
) -> dict[str, Any]:
    response = context.request.get(
        f"{target.adk_url}/dev/apps/ai_actuary_developer/debug/trace/session/{session_id}",
        timeout=timeout * 1000,
    )
    payload: Any = None
    if response.status == 200:
        payload = response.json()
    spans = payload.get("spans") if isinstance(payload, dict) else payload if isinstance(payload, list) else None
    session_trace_records = _trace_records(payload)
    event_trace_records = _fetch_adk_event_trace_records(
        context,
        target=target,
        session_events=session_events or [],
        timeout=timeout,
    )
    joined_span = _find_joined_adk_project_span(
        session_trace_records,
        session_id=session_id,
        invocation_id=invocation_id,
        run_id=run_id,
        correlation_id=correlation_id,
    )
    summary = _adk_debug_trace_summary(
        status=response.status,
        spans=spans,
        session_trace_records=session_trace_records,
        event_trace_records=event_trace_records,
        joined_span=joined_span,
    )
    if evidence_dir is not None:
        _write_json(evidence_dir / "adk_debug_trace_summary.json", summary)
    if response.status != 200 or joined_span is None:
        raise BrowserSmokeError(
            "real ADK trace evidence did not expose nonempty debug trace records tied "
            "to the browser-smoke session/invocation and run/correlation; ordinary "
            "session events are not accepted as trace proof."
        )
    return {
        "available": True,
        "status": response.status,
        "span_count": len(spans) if isinstance(spans, list) else 0,
        "record_count": len(session_trace_records),
        "session_trace_count": len(session_trace_records),
        "event_trace_count": len(event_trace_records),
        "session_linked": True,
        "invocation_linked": True,
        "run_or_correlation_linked": True,
        "joined_span": joined_span,
        "source": "adk_debug_trace",
    }


_TRACE_ATTRIBUTE_ALLOWLIST = {
    "gen_ai.conversation.id",
    "gcp.vertex.agent.session_id",
    "ai_actuary.adk.invocation_id",
    "ai_actuary.run_id",
    "ai_actuary.operation_id",
    "ai_actuary.correlation_id",
    "ai_actuary.workflow_id",
    "ai_actuary.case_id",
}


def _find_joined_adk_project_span(
    records: list[Any],
    *,
    session_id: str,
    invocation_id: str,
    run_id: str,
    correlation_id: str,
) -> dict[str, Any] | None:
    for record in records:
        if not isinstance(record, dict):
            continue
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            continue
        session_linked = (
            attributes.get("gen_ai.conversation.id") == session_id
            or attributes.get("gcp.vertex.agent.session_id") == session_id
        )
        invocation_linked = attributes.get("ai_actuary.adk.invocation_id") == invocation_id
        run_linked = attributes.get("ai_actuary.run_id") == run_id
        correlation_linked = attributes.get("ai_actuary.correlation_id") == correlation_id
        if session_linked and invocation_linked and run_linked and correlation_linked:
            return _safe_trace_span(record)
    return None


def _safe_trace_span(record: dict[str, Any]) -> dict[str, Any]:
    attributes = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
    safe_attributes = {
        key: str(value)
        for key, value in attributes.items()
        if key in _TRACE_ATTRIBUTE_ALLOWLIST and isinstance(value, (str, int, float, bool))
    }
    return {
        "name": str(record.get("name") or "span"),
        "trace_id": str(record.get("trace_id") or ""),
        "span_id": str(record.get("span_id") or ""),
        "attributes": safe_attributes,
    }


def _adk_debug_trace_summary(
    *,
    status: int,
    spans: Any,
    session_trace_records: list[Any],
    event_trace_records: list[dict[str, Any]],
    joined_span: dict[str, Any] | None,
) -> dict[str, Any]:
    attributes = joined_span.get("attributes", {}) if isinstance(joined_span, dict) else {}
    return {
        "endpoint_status": status,
        "span_count": len(spans) if isinstance(spans, list) else 0,
        "record_count": len(session_trace_records),
        "event_trace_count": len(event_trace_records),
        "joined_span": joined_span,
        "session_linked": bool(
            attributes.get("gen_ai.conversation.id")
            or attributes.get("gcp.vertex.agent.session_id")
        ),
        "invocation_linked": bool(attributes.get("ai_actuary.adk.invocation_id")),
        "run_linked": bool(attributes.get("ai_actuary.run_id")),
        "correlation_linked": bool(attributes.get("ai_actuary.correlation_id")),
        "source": "adk_debug_trace",
    }


def _fetch_adk_event_trace_records(
    context: Any,
    *,
    target: SmokeTarget,
    session_events: list[dict[str, Any]],
    timeout: float,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in session_events:
        event_id = event.get("id") or event.get("event_id")
        if not event_id:
            continue
        event_id = str(event_id)
        if event_id in seen:
            continue
        seen.add(event_id)
        response = context.request.get(
            f"{target.adk_url}/dev/apps/ai_actuary_developer/debug/trace/{event_id}",
            timeout=timeout * 1000,
        )
        if response.status != 200:
            continue
        payload = response.json()
        event_records = _trace_records(payload)
        for record in event_records:
            records.append({"event_id": event_id, "trace_record": record})
    return records


def _trace_records(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        for key in ("spans", "events", "traceEvents", "invocations", "records"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                return value
        return [payload] if payload else []
    if isinstance(payload, list):
        return payload
    return []


def _session_event_trace_evidence(
    session_events: list[dict[str, Any]],
    *,
    session_id: str,
    invocation_id: str,
    run_id: str,
    correlation_id: str,
    endpoint_status: int,
) -> dict[str, Any] | None:
    if not session_events:
        return None
    serialized = json.dumps(session_events, sort_keys=True, default=str)
    has_session_or_invocation = session_id in serialized or invocation_id in serialized
    has_run_or_correlation = run_id in serialized or correlation_id in serialized
    if not has_session_or_invocation or not has_run_or_correlation:
        return None
    return {
        "available": True,
        "status": endpoint_status,
        "span_count": None,
        "record_count": len(session_events),
        "session_linked": has_session_or_invocation,
        "run_or_correlation_linked": has_run_or_correlation,
        "source": "adk_session_events",
    }


def _start_workflow_run_through_adk_developer_web(
    context: Any,
    *,
    target: SmokeTarget,
    workflow_id: str,
    case_id: str,
    inputs: dict[str, Any],
    session_id: str,
    invocation_id: str,
    timeout: float,
) -> dict[str, Any]:
    start_request = {
        "workflow_id": workflow_id,
        "case_id": case_id,
        "inputs": inputs,
    }
    session_url = (
        f"{target.adk_url}/apps/ai_actuary_developer/users/"
        f"{ADK_BROWSER_SMOKE_USER}/sessions/{session_id}"
    )
    session_response = context.request.post(
        session_url,
        data=_canonical_json({}),
        headers={"Content-Type": "application/json"},
        timeout=timeout * 1000,
    )
    _response_json(
        session_response,
        expected_status=200,
        description="ADK Developer Web session create",
    )
    first_events = _run_adk_sse(
        context,
        target=target,
        user_id=ADK_BROWSER_SMOKE_USER,
        session_id=session_id,
        invocation_id=invocation_id,
        new_message=_adk_text_message(
            "Use the start_workflow_run tool to start the published workflow "
            f"{workflow_id} for case {case_id} with inputs "
            f"{json.dumps(inputs, sort_keys=True)}. If confirmation is requested, "
            "request confirmation for exactly that tool call."
        ),
        state_delta={"browser_smoke_start": start_request},
        timeout=timeout,
        description="ADK Developer Web workflow start",
    )
    start_result = _find_adk_start_result(first_events)
    if start_result is not None:
        start_result["adk_events"] = _adk_event_summary(first_events)
        return start_result
    confirmation = _find_adk_confirmation_request(first_events)
    if confirmation is None:
        raise BrowserSmokeError(
            "ADK Developer Web run did not expose a workflow start result or confirmation request."
        )
    confirmed_events = _run_adk_sse(
        context,
        target=target,
        user_id=ADK_BROWSER_SMOKE_USER,
        session_id=session_id,
        invocation_id=invocation_id,
        new_message=_adk_confirmation_message(confirmation),
        timeout=timeout,
        description="ADK Developer Web workflow confirmation",
    )
    start_result = _find_adk_start_result(confirmed_events)
    if start_result is None:
        raise BrowserSmokeError("ADK Developer Web confirmation did not start a workflow run.")
    start_result["adk_events"] = _adk_event_summary([*first_events, *confirmed_events])
    return start_result


def _run_adk_sse(
    context: Any,
    *,
    target: SmokeTarget,
    user_id: str,
    session_id: str,
    invocation_id: str,
    new_message: dict[str, Any],
    state_delta: dict[str, Any] | None = None,
    timeout: float,
    description: str,
) -> list[dict[str, Any]]:
    payload = {
        "app_name": "ai_actuary_developer",
        "user_id": user_id,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "new_message": new_message,
        "streaming": False,
        "state_delta": state_delta,
        "custom_metadata": {"browser_smoke": True},
    }
    response = context.request.post(
        f"{target.adk_url}/run_sse",
        data=_canonical_json(payload),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        timeout=timeout * 1000,
    )
    if response.status != 200:
        raise BrowserSmokeError(f"{description} returned HTTP {response.status}; expected 200.")
    events = _parse_sse_events(_response_text(response))
    if any("error" in event for event in events):
        raise BrowserSmokeError(f"{description} returned an ADK error event.")
    return events


def _adk_text_message(text: str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"text": text}]}


def _adk_confirmation_message(confirmation: dict[str, Any]) -> dict[str, Any]:
    tool_confirmation = (confirmation.get("args") or {}).get("toolConfirmation") or {}
    return {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "id": confirmation["id"],
                    "name": "adk_request_confirmation",
                    "response": {
                        "confirmed": True,
                        "payload": tool_confirmation.get("payload"),
                    },
                }
            }
        ],
    }


def _find_adk_start_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        for part in ((event.get("content") or {}).get("parts") or []):
            response = _event_function_response(part)
            if not isinstance(response, dict) or response.get("name") != "start_workflow_run":
                continue
            payload = response.get("response")
            if isinstance(payload, dict) and payload.get("run_id"):
                return dict(payload)
    return None


def _find_adk_summary_result(
    events: list[dict[str, Any]], *, run_id: str
) -> dict[str, Any] | None:
    for event in events:
        for part in ((event.get("content") or {}).get("parts") or []):
            response = _event_function_response(part)
            if not isinstance(response, dict) or response.get("name") != "summarize_run":
                continue
            payload = response.get("response")
            summary = payload.get("summary") if isinstance(payload, dict) else None
            if (
                isinstance(summary, dict)
                and summary.get("ok") is True
                and isinstance(summary.get("data"), dict)
            ):
                summary = summary["data"]
            if (
                isinstance(payload, dict)
                and payload.get("run_id") == run_id
                and isinstance(summary, dict)
                and summary.get("run_id") == run_id
            ):
                return {**payload, "summary": dict(summary)}
    return None


def _find_adk_confirmation_request(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        for part in ((event.get("content") or {}).get("parts") or []):
            function_call = _event_function_call(part)
            if (
                isinstance(function_call, dict)
                and function_call.get("name") == "adk_request_confirmation"
                and function_call.get("id")
            ):
                return function_call
    return None


def _adk_event_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for event in events:
        item = {
            "id": event.get("id"),
            "invocation_id": event.get("invocationId") or event.get("invocation_id"),
            "author": event.get("author"),
        }
        function_names: list[str] = []
        for part in ((event.get("content") or {}).get("parts") or []):
            function_call = _event_function_call(part)
            function_response = _event_function_response(part)
            if isinstance(function_call, dict) and function_call.get("name"):
                function_names.append(str(function_call["name"]))
            if isinstance(function_response, dict) and function_response.get("name"):
                function_names.append(str(function_response["name"]))
        item["functions"] = function_names
        summary.append(item)
    return summary


def _event_function_call(part: object) -> dict[str, Any] | None:
    if not isinstance(part, dict):
        return None
    function_call = part.get("functionCall") or part.get("function_call")
    return function_call if isinstance(function_call, dict) else None


def _event_function_response(part: object) -> dict[str, Any] | None:
    if not isinstance(part, dict):
        return None
    function_response = part.get("functionResponse") or part.get("function_response")
    return function_response if isinstance(function_response, dict) else None


def _parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw_text.split("\n\n"):
        data_lines = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise BrowserSmokeError("ADK Developer Web returned invalid SSE JSON.") from exc
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _response_text(response: Any) -> str:
    text_method = getattr(response, "text", None)
    if callable(text_method):
        return str(text_method())
    payload = response.json()
    return payload if isinstance(payload, str) else json.dumps(payload)


def _cleanup_ports_released(cleanup: dict[str, Any]) -> bool:
    ports = cleanup.get("ports") if isinstance(cleanup, dict) else None
    if not isinstance(ports, dict):
        return True
    for payload in ports.values():
        if isinstance(payload, dict) and payload.get("released") is False:
            return False
    return True


def _evidence_ref(path: Path, evidence_dir: Path) -> str:
    try:
        return path.resolve().relative_to(evidence_dir.resolve()).as_posix()
    except ValueError:
        return path.name


def _safe_message(message: str) -> str:
    return sanitize_text(message)


def _is_allowed_adk_dev_ui_console_error(item: dict[str, Any]) -> bool:
    location = item.get("location")
    url = location.get("url", "") if isinstance(location, dict) else ""
    return (
        item.get("surface") == "adk_developer_web"
        and item.get("type") == "error"
        and url.endswith("/dev-ui/prism-dark.css")
        and "404" in str(item.get("text", ""))
    )


def _is_allowed_adk_dev_ui_request_failure(item: dict[str, Any]) -> bool:
    return (
        item.get("surface") == "adk_developer_web"
        and str(item.get("route", item.get("url", ""))).endswith("dev-ui/prism-dark.css")
        and item.get("failure") == "net::ERR_ABORTED"
    )


def _safe_route_label(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "unparseable"
    path = parsed.path or "/"
    replacements = (
        (r"/runs/[^/]+", "/runs/{run_id}"),
        (r"/reviews/[^/]+", "/reviews/{review_id}"),
        (r"/sessions/[^/]+", "/sessions/{session_id}"),
        (r"/users/[^/]+", "/users/{user_id}"),
        (r"/debug/trace/session/[^/]+", "/debug/trace/session/{session_id}"),
    )
    route = path
    for pattern, replacement in replacements:
        route = re.sub(pattern, replacement, route)
    if parsed.query:
        route += "?{query}"
    route = route.lstrip("/") or "root"
    return f"route:{route}"


def _cleanup_local_workbench(
    workbench: subprocess.Popen[bytes] | None,
    *,
    target: SmokeTarget | None,
    disable_adk: bool,
    evidence_dir: Path,
) -> dict[str, Any]:
    cleanup: dict[str, Any] = {
        "owned_process": workbench is not None,
        "terminated": False,
        "returncode": None,
        "ports": {},
    }
    if workbench is not None:
        if workbench.poll() is None:
            workbench.terminate()
            try:
                workbench.wait(timeout=5)
            except subprocess.TimeoutExpired:
                workbench.kill()
                workbench.wait(timeout=5)
        cleanup["terminated"] = True
        cleanup["returncode"] = workbench.returncode
        _close_child_log_handles(workbench)
    if target is not None:
        cleanup["ports"]["api"] = {
            "port": target.api_port,
            "released": _wait_for_port_released(target.api_port),
        }
        cleanup["ports"]["adk"] = {
            "port": target.adk_port,
            "disabled": disable_adk,
            "released": True if disable_adk else _wait_for_port_released(target.adk_port),
        }
    if workbench is not None:
        _sanitize_child_logs(evidence_dir)
    _write_json(evidence_dir / "cleanup_evidence.json", cleanup)
    return cleanup


def _close_child_log_handles(workbench: subprocess.Popen[bytes]) -> None:
    for attribute in ("_ai_actuary_stdout_handle", "_ai_actuary_stderr_handle"):
        handle = getattr(workbench, attribute, None)
        if handle is not None and not handle.closed:
            handle.close()


def _sanitize_child_logs(evidence_dir: Path) -> None:
    for stream_name in ("stdout", "stderr"):
        raw_path = evidence_dir / f"local_workbench.{stream_name}.raw.log"
        clean_path = evidence_dir / f"local_workbench.{stream_name}.log"
        if raw_path.exists():
            _sanitize_text_file(raw_path, clean_path)
            raw_path.unlink(missing_ok=True)


def _sanitize_text_file(raw_path: Path, clean_path: Path) -> None:
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
    sanitized_lines = [sanitize_text(line.rstrip("\r\n")) for line in raw_text.splitlines()]
    clean_path.write_text("\n".join(sanitized_lines) + ("\n" if sanitized_lines else ""), encoding="utf-8")
    if raw_path.resolve() != clean_path.resolve():
        _unlink_with_retry(raw_path)


def _unlink_with_retry(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def _sanitize_trace_archive(raw_path: Path, clean_path: Path) -> None:
    if not raw_path.exists():
        return
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(raw_path, "r") as source, zipfile.ZipFile(clean_path, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            suffix = Path(info.filename).suffix.lower()
            if suffix in TEXT_EVIDENCE_SUFFIXES or _bytes_look_textual(data):
                text = data.decode("utf-8", errors="replace")
                sanitized = "\n".join(sanitize_text(line) for line in text.splitlines())
                target.writestr(info.filename, sanitized.encode("utf-8"))
            else:
                target.writestr(info.filename, data)
    raw_path.unlink(missing_ok=True)


def scan_evidence_tree(evidence_dir: Path) -> dict[str, Any]:
    root = evidence_dir.resolve()
    leaks: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        scanned += 1
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() == ".zip":
            leaks.extend(_scan_zip_for_leaks(path, rel))
            continue
        data = path.read_bytes()
        leaks.extend(_scan_bytes_for_leaks(data, rel))
    return {
        "ok": not leaks,
        "leak_count": len(leaks),
        "scanned_file_count": scanned,
        "leaks": leaks[:50],
    }


def _scan_zip_for_leaks(path: Path, rel: str) -> list[dict[str, Any]]:
    leaks: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path, "r") as archive:
            for name in archive.namelist():
                data = archive.read(name)
                leaks.extend(_scan_bytes_for_leaks(data, f"{rel}!{name}"))
    except zipfile.BadZipFile:
        leaks.append({"file": rel, "kind": "invalid_zip", "sample": "trace archive is invalid"})
    return leaks


def _scan_bytes_for_leaks(data: bytes, rel: str) -> list[dict[str, Any]]:
    leaks: list[dict[str, Any]] = []
    for kind, pattern in LEAK_PATTERNS:
        match = pattern.search(data)
        if match is not None:
            leaks.append(
                {
                    "file": rel,
                    "kind": kind,
                    "sample": _safe_message(match.group(0).decode("utf-8", errors="replace")),
                }
            )
    return leaks


def _bytes_look_textual(data: bytes) -> bool:
    if not data:
        return True
    sample = data[:4096]
    return b"\x00" not in sample and sum(byte < 9 for byte in sample) == 0


def _read_url(url: str, *, timeout: float) -> bytes:
    opener = build_opener(ProxyHandler({})).open
    with opener(Request(url, method="GET"), timeout=timeout) as response:
        data = response.read()
        if response.status != 200:
            raise HTTPError(url, response.status, response.reason, response.headers, None)
        return data


def _http_status_no_cookies(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> int:
    opener = build_opener(ProxyHandler({})).open
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with opener(request, timeout=timeout) as response:
            response.read()
            return int(response.status)
    except HTTPError as exc:
        exc.read()
        return int(exc.code)


def _normalize_loopback_origin(raw_url: str, *, purpose: str) -> str:
    try:
        parsed = urlsplit(raw_url.strip())
        parsed_port = parsed.port
    except ValueError as exc:
        raise BrowserSmokeError(f"{purpose} must be a loopback HTTP origin with a port.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed_port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BrowserSmokeError(f"{purpose} must be a loopback HTTP origin with a port.")
    return f"http://{parsed.hostname}:{parsed_port}"


def _port_from_origin(origin: str) -> int:
    parsed = urlsplit(origin)
    assert parsed.port is not None
    return parsed.port


def _unused_loopback_port(*, excluding: set[int] | None = None) -> int:
    excluded = excluding or set()
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind((LOOPBACK_HOST, 0))
            port = int(candidate.getsockname()[1])
        if port not in excluded:
            return port
    raise BrowserSmokeError("Unable to select an unused loopback port.")


def _wait_for_port_released(port: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not _port_accepts_connections(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _port_accepts_connections(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((LOOPBACK_HOST, port)) == 0


def _local_smoke_bootstrap_token() -> str:
    return "browser-smoke-bootstrap-token"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_for_runtime(payload), indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
