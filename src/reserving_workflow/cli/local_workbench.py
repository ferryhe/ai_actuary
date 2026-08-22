"""Installed console entry point for the local AI Actuary workbench."""

from __future__ import annotations

import argparse
import csv
import importlib.resources as resources
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from reserving_workflow.adapters.adk.local_runtime import (
    DEFAULT_ADK_PORT,
    DEFAULT_CONTROL_PLANE_PORT,
    LocalWorkbenchConfig,
)
from reserving_workflow.cli.workbench_launcher import LauncherError, run_workbench

INSTALL_HINT = 'pip install "ai-actuary[api,adk-dev]"'
SUPPORTED_ADK_VERSION = "2.7.1"


class WorkbenchCliError(RuntimeError):
    """Expected local workbench startup failure."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Start, verify, and stop.")
    parser.add_argument("--smoke-timeout", type=float, default=30.0)
    parser.add_argument(
        "--api-port",
        dest="control_plane_port",
        type=int,
        default=DEFAULT_CONTROL_PLANE_PORT,
        help="Operator Console/API loopback port.",
    )
    parser.add_argument("--control-plane-port", type=int, default=DEFAULT_CONTROL_PLANE_PORT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--adk-port",
        type=int,
        default=DEFAULT_ADK_PORT,
        help="ADK Developer Web loopback port.",
    )
    parser.add_argument(
        "--disable-adk",
        action="store_true",
        help="Start only the local API and Operator Console.",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.cwd() / "tmp" / "installed-workbench",
        help="Local developer state root for installed-wheel smoke runs.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return _run_installed_workbench(args)
    except (WorkbenchCliError, LauncherError) as exc:
        print(f"Local workbench failed: {exc}", file=sys.stderr)
        return 1


def _run_installed_workbench(args: argparse.Namespace) -> int:
    with _packaged_agents_dir() as agents_dir:
        config = _build_config(
            state_root=args.state_root,
            agents_dir=agents_dir,
            control_plane_port=args.control_plane_port,
            adk_port=args.adk_port,
        )
        return run_workbench(
            config,
            smoke=args.smoke,
            smoke_timeout=args.smoke_timeout,
            disable_adk=args.disable_adk,
        )


def _build_config(
    *,
    state_root: Path,
    agents_dir: Path,
    control_plane_port: int,
    adk_port: int,
) -> LocalWorkbenchConfig:
    root = state_root.expanduser().resolve()
    return LocalWorkbenchConfig(
        repo_root=root,
        agents_dir=agents_dir.resolve(),
        state_root=root / "adk-dev",
        session_database=root / "adk-dev" / "sessions" / "sessions.db",
        artifact_directory=root / "adk-dev" / "artifacts",
        diagnostics_log=root / "local-workbench-diagnostics" / "launcher.jsonl",
        control_plane_port=control_plane_port,
        adk_port=adk_port,
    )


@contextmanager
def _packaged_agents_dir() -> Iterator[Path]:
    try:
        agent_resource = resources.files("developer_workflows")
    except ModuleNotFoundError as exc:
        raise WorkbenchCliError("Packaged ADK developer workflow resources are missing.") from exc
    with resources.as_file(agent_resource) as source_path:
        with TemporaryDirectory(prefix="ai-actuary-adk-app-") as temp_root:
            materialized = Path(temp_root) / "developer_workflows"
            shutil.copytree(source_path, materialized)
            _make_tree_read_only(materialized)
            _validate_tree_read_only(materialized)
            try:
                yield materialized
            finally:
                _make_tree_writable(materialized)


def _make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.chmod(stat.S_IREAD | stat.S_IEXEC)
        else:
            path.chmod(stat.S_IREAD)
    root.chmod(stat.S_IREAD | stat.S_IEXEC)
    if os.name == "nt":
        _deny_current_user_tree_mutations(root)


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    if os.name == "nt":
        _remove_current_user_tree_mutation_deny(root)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        path.chmod(stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
    root.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)


def _validate_tree_read_only(root: Path) -> None:
    if not root.is_dir():
        raise WorkbenchCliError("Packaged ADK developer workflow materialization failed.")
    writable_files = [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and (path.stat().st_mode & stat.S_IWRITE)
    ]
    if writable_files:
        raise WorkbenchCliError("Packaged ADK developer workflow files are writable.")
    if os.name == "nt":
        blocked = set(_prove_tree_effectively_read_only(root))
        expected = {"create", "replace", "rename", "delete"}
        if blocked != expected:
            raise WorkbenchCliError("Packaged ADK developer workflow tree is effectively writable.")


def _prove_tree_effectively_read_only(root: Path) -> list[str]:
    blocked: list[str] = []
    create_probe = root / "__ai_actuary_write_probe__.tmp"
    if _mutation_is_blocked(lambda: create_probe.write_text("probe", encoding="utf-8")):
        blocked.append("create")
    else:
        create_probe.unlink(missing_ok=True)

    existing_file = next((path for path in root.rglob("*") if path.is_file()), None)
    if existing_file is None:
        raise WorkbenchCliError("Packaged ADK developer workflow tree contains no files.")
    original_bytes = existing_file.read_bytes()
    if _mutation_is_blocked(lambda: existing_file.write_text("probe", encoding="utf-8")):
        blocked.append("replace")
    else:
        existing_file.write_bytes(original_bytes)

    rename_target = existing_file.with_name(f"{existing_file.name}.rename-probe")
    if _mutation_is_blocked(lambda: existing_file.rename(rename_target)):
        blocked.append("rename")
    else:
        rename_target.rename(existing_file)

    if _mutation_is_blocked(lambda: existing_file.unlink()):
        blocked.append("delete")
    else:
        existing_file.write_bytes(original_bytes)
    return blocked


def _mutation_is_blocked(action) -> bool:
    try:
        action()
    except (OSError, PermissionError):
        return True
    return False


def _deny_current_user_tree_mutations(root: Path) -> None:
    sid = _current_windows_user_sid()
    _run_icacls(
        [str(root), "/deny", f"*{sid}:(OI)(CI)(WD,AD,DC,DE)", "/T", "/C"],
        "harden packaged ADK workflow tree",
    )


def _remove_current_user_tree_mutation_deny(root: Path) -> None:
    sid = _current_windows_user_sid()
    _run_icacls(
        [str(root), "/remove:d", f"*{sid}", "/T", "/C"],
        "restore packaged ADK workflow tree cleanup permissions",
    )


def _current_windows_user_sid() -> str:
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorkbenchCliError("Unable to identify the current Windows user for ACL hardening.") from exc
    rows = list(csv.reader(completed.stdout.splitlines()))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-"):
        raise WorkbenchCliError("Unable to parse the current Windows user SID for ACL hardening.")
    return rows[0][1]


def _run_icacls(arguments: list[str], action: str) -> None:
    try:
        completed = subprocess.run(
            ["icacls", *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WorkbenchCliError(f"Unable to {action}: icacls is unavailable.") from exc
    if completed.returncode != 0:
        raise WorkbenchCliError(f"Unable to {action}: Windows ACL update failed.")


__all__ = ["main", "parse_args"]


if __name__ == "__main__":
    raise SystemExit(main())
