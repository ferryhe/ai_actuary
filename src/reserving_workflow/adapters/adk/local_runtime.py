"""Paths and command construction for the local dual-interface workbench."""

from __future__ import annotations

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
            control_plane_port=control_plane_port,
            adk_port=adk_port,
        )

    def prepare_state_directories(self) -> None:
        self.session_database.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_directory.mkdir(parents=True, exist_ok=True)


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
        ADK_DEVELOPER_LOGO_TEXT,
        "--logo-image-url",
        ADK_DEVELOPER_LOGO_DATA_URL,
        "--session_service_uri",
        session_uri,
        "--artifact_service_uri",
        artifact_uri,
        str(config.agents_dir.resolve()),
    ]
