"""Paths and command construction for the local dual-interface workbench."""

from __future__ import annotations

import os
import re
import stat
import csv
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_CONTROL_PLANE_PORT = 8000
DEFAULT_ADK_PORT = 8001
ADK_DEVELOPER_LOGO_TEXT = (
    "AI Actuary Developer (DEV) | Console: http://127.0.0.1:8000/console"
)
ADK_DEVELOPER_LOGO_DATA_URL = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='10' "
    "fill='%232457c5'/%3E%3C/svg%3E"
)


@dataclass(frozen=True)
class LocalWorkbenchConfig:
    """Fixed-loopback configuration for the developer-only workbench."""

    repo_root: Path
    agents_dir: Path
    state_root: Path
    session_database: Path
    artifact_directory: Path
    diagnostics_log: Path
    control_plane_port: int = DEFAULT_CONTROL_PLANE_PORT
    adk_port: int = DEFAULT_ADK_PORT

    @classmethod
    def from_repo_root(
        cls,
        repo_root: Path,
        *,
        control_plane_port: int = DEFAULT_CONTROL_PLANE_PORT,
        adk_port: int = DEFAULT_ADK_PORT,
    ) -> "LocalWorkbenchConfig":
        root = repo_root.resolve()
        state_root = root / "tmp" / "adk-dev"
        return cls(
            repo_root=root,
            agents_dir=root / "developer_workflows",
            state_root=state_root,
            session_database=state_root / "sessions" / "sessions.db",
            artifact_directory=state_root / "artifacts",
            diagnostics_log=root / "tmp" / "local-workbench-diagnostics" / "launcher.jsonl",
            control_plane_port=control_plane_port,
            adk_port=adk_port,
        )

    def prepare_state_directories(self) -> None:
        for directory in self.state_directories():
            _secure_directory(directory)
        for sensitive_file in (
            self.session_database,
            self.diagnostics_log,
            self.repo_root / "tmp" / "run-registry.json",
        ):
            if sensitive_file.exists():
                secure_sensitive_file(sensitive_file)

    def state_directories(self) -> tuple[Path, ...]:
        tmp = self.repo_root / "tmp"
        return (
            tmp,
            self.state_root,
            self.session_database.parent,
            self.artifact_directory,
            self.state_root / "traces",
            tmp / "adk-workflow-drafts",
            tmp / "adk-workflow-exports",
            tmp / "adk-evaluations",
            self.diagnostics_log.parent,
            tmp / "api-artifacts",
            tmp / "reviews",
            tmp / "review-outbox",
            tmp / "batch",
        )


def _secure_directory(path: Path) -> dict[str, object]:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _set_windows_private_acl(path, tree=True)
        metadata = _windows_privacy_metadata(path)
        if not metadata["private"]:
            raise PermissionError(f"Local workbench state directory is not private: {path}")
        return metadata
    try:
        path.chmod(stat.S_IRWXU)
    except OSError as exc:
        raise PermissionError(f"Unable to secure local workbench state directory: {path}") from exc
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"Local workbench state directory is not private: {path}")
    return {"private": True, "mode": oct(mode), "unsafe_principals": []}


def secure_sensitive_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"private": False, "missing": True, "unsafe_principals": []}
    if os.name == "nt":
        _set_windows_private_acl(path, tree=False)
        metadata = _windows_privacy_metadata(path)
        if not metadata["private"]:
            raise PermissionError(f"Local workbench sensitive file is not private: {path}")
        try:
            with path.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise PermissionError(f"Local workbench sensitive file is not writable by the owner: {path}") from exc
        return metadata
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise PermissionError(f"Unable to secure local workbench sensitive file: {path}") from exc
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"Local workbench sensitive file is not private: {path}")
    return {"private": True, "mode": oct(mode), "unsafe_principals": []}


def _set_windows_private_acl(path: Path, *, tree: bool) -> None:
    identity = _current_windows_identity()
    sid = identity["sid"]
    rights = "(OI)(CI)F" if tree else "F"
    allowed = _allowed_windows_principals(identity)
    targets = [path]
    if tree and path.is_dir():
        targets.extend(sorted(path.rglob("*"), key=lambda item: len(item.parts)))
    for target in targets:
        target_rights = "(OI)(CI)F" if target.is_dir() else "F"
        _run_icacls(
            [
                str(target),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:{target_rights}",
                f"*S-1-5-18:{target_rights}",
                f"*S-1-5-32-544:{target_rights}",
            ],
            "set owner-private ACL",
        )
        _remove_windows_unsafe_acl_entries(target, allowed_principals=allowed)
        _run_icacls(
            [
                str(target),
                "/grant:r",
                f"*{sid}:{target_rights}",
                f"*S-1-5-18:{target_rights}",
                f"*S-1-5-32-544:{target_rights}",
            ],
            "restore owner-private ACL allowlist",
        )


def _windows_privacy_metadata(path: Path) -> dict[str, object]:
    identity = _current_windows_identity()
    allowed = _allowed_windows_principals(identity)
    completed = _run_icacls([str(path)], "read ACL", capture=True)
    return _windows_privacy_metadata_from_text(
        completed.stdout,
        path=path,
        allowed_principals=allowed,
    )


def _windows_privacy_metadata_from_text(
    acl_text: str,
    *,
    path: Path,
    allowed_principals: set[str],
) -> dict[str, object]:
    unsafe_principals: list[str] = []
    unsafe_principal_names: list[str] = []
    observed_principals: list[str] = []
    for line in acl_text.splitlines():
        principal = _principal_from_icacls_line(line, path=path)
        if principal is None:
            continue
        normalized = principal.upper()
        observed_principals.append(principal)
        if normalized not in allowed_principals:
            unsafe_principals.append(line.strip())
            unsafe_principal_names.append(principal)
    return {
        "private": not unsafe_principals,
        "unsafe_principals": unsafe_principals,
        "unsafe_principal_names": unsafe_principal_names,
        "observed_principals": observed_principals,
        "platform": "windows",
    }


def _principal_from_icacls_line(line: str, *, path: Path) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("Successfully processed") or stripped.startswith("Failed processing"):
        return None
    path_text = str(path)
    rest = stripped
    if rest.upper().startswith(path_text.upper()):
        rest = rest[len(path_text):].strip()
    elif re.match(r"^[A-Za-z]:[\\/]", rest):
        parts = rest.split(None, 1)
        rest = parts[1] if len(parts) == 2 else ""
    if ":" not in rest:
        return None
    principal = rest.split(":", 1)[0].strip()
    return principal or None


def _allowed_windows_principals(identity: dict[str, str]) -> set[str]:
    return {
        identity["name"].upper(),
        identity["sid"].upper(),
        f"*{identity['sid']}".upper(),
        "NT AUTHORITY\\SYSTEM",
        "SYSTEM",
        "S-1-5-18",
        "*S-1-5-18",
        "BUILTIN\\ADMINISTRATORS",
        "ADMINISTRATORS",
        "S-1-5-32-544",
        "*S-1-5-32-544",
    }


def _remove_windows_unsafe_acl_entries(path: Path, *, allowed_principals: set[str]) -> None:
    completed = _run_icacls([str(path)], "read ACL", capture=True)
    metadata = _windows_privacy_metadata_from_text(
        completed.stdout,
        path=path,
        allowed_principals=allowed_principals,
    )
    for principal in metadata.get("unsafe_principal_names", []):
        if isinstance(principal, str) and principal.strip():
            _run_icacls([str(path), "/remove", principal], "remove non-private ACL entry")


def _current_windows_user_sid() -> str:
    return _current_windows_identity()["sid"]


def _current_windows_identity() -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PermissionError("Unable to identify current Windows user for state ACLs.") from exc
    rows = list(csv.reader(completed.stdout.splitlines()))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-"):
        raise PermissionError("Unable to parse current Windows user SID for state ACLs.")
    return {"name": rows[0][0], "sid": rows[0][1]}


def _run_icacls(
    arguments: list[str],
    action: str,
    *,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["icacls", *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PermissionError(f"Unable to {action}: icacls is unavailable.") from exc
    if completed.returncode != 0:
        raise PermissionError(f"Unable to {action}: Windows ACL command failed.")
    if capture:
        return completed
    return completed


def adk_developer_logo_text(config: LocalWorkbenchConfig) -> str:
    return (
        "AI Actuary Developer (DEV) | Console: "
        f"http://{LOOPBACK_HOST}:{config.control_plane_port}/console"
    )


def build_control_plane_command(
    config: LocalWorkbenchConfig,
    *,
    python_executable: str = sys.executable,
) -> list[str]:
    return [
        python_executable,
        "-m",
        "uvicorn",
        "reserving_workflow.api.app:create_app",
        "--factory",
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(config.control_plane_port),
    ]


def build_adk_command(
    config: LocalWorkbenchConfig,
    *,
    adk_executable: str = "adk",
) -> list[str]:
    session_path = config.session_database.resolve().as_posix()
    session_uri = f"sqlite:///{session_path}"
    artifact_uri = config.artifact_directory.resolve().as_uri()
    return [
        adk_executable,
        "web",
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(config.adk_port),
        "--no-reload",
        "--logo-text",
        adk_developer_logo_text(config),
        "--logo-image-url",
        ADK_DEVELOPER_LOGO_DATA_URL,
        "--session_service_uri",
        session_uri,
        "--artifact_service_uri",
        artifact_uri,
        str(config.agents_dir.resolve()),
    ]
