"""State inventory and safe cleanup helpers for local development runtimes."""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class UnsafeCleanupTargetError(ValueError):
    """A cleanup target is broad, ambiguous, or outside the owned boundary."""


@dataclass(frozen=True)
class StateTarget:
    state_id: str
    label: str
    target: Path
    category: str
    ownership: str
    retention: str
    permission_model: str
    cleanup_allowed: bool
    preserve_reason: str | None = None


def build_local_state_cleanup_plan(repo_root: str | Path) -> dict[str, Any]:
    """Return an exact dry-run inventory of local state and cleanup eligibility."""

    root = Path(repo_root).expanduser().resolve()
    specs = _local_state_specs(root)
    cleanup_targets = [_target_payload(item) for item in specs if item.cleanup_allowed]
    preserved_targets = [_target_payload(item) for item in specs if not item.cleanup_allowed]
    return {
        "ok": True,
        "mode": "dry_run",
        "repo_root_name": root.name,
        "cleanup_targets": cleanup_targets,
        "preserved_targets": preserved_targets,
        "summary": {
            "target_count": len(specs),
            "cleanup_target_count": len(cleanup_targets),
            "preserved_target_count": len(preserved_targets),
        },
    }


def execute_cleanup_plan(
    repo_root: str | Path,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Remove only developer-owned cleanup targets from the local state plan."""

    root = Path(repo_root).expanduser().resolve()
    plan = build_local_state_cleanup_plan(root)
    actions: list[dict[str, Any]] = []
    for item in plan["cleanup_targets"]:
        target = validate_cleanup_target(item["target"], repo_root=root)
        action = {
            "state_id": item["state_id"],
            "target": str(target),
            "dry_run": dry_run,
        }
        if not target.exists():
            action["status"] = "missing"
        elif dry_run:
            action["status"] = "would_remove"
        else:
            _remove_path_no_follow(target)
            action["status"] = "removed"
        actions.append(action)
    return {
        "ok": True,
        "mode": "dry_run" if dry_run else "executed",
        "actions": actions,
        "preserved_targets": plan["preserved_targets"],
    }


def validate_cleanup_target(target: str | Path, *, repo_root: str | Path) -> Path:
    """Resolve one exact cleanup target and reject broad or unresolved inputs."""

    raw = str(target)
    if raw.strip() in {"", ".", ".."}:
        raise UnsafeCleanupTargetError("cleanup_target_empty_or_broad")
    if any(marker in raw for marker in ("*", "?", "[", "]", "{", "}")):
        raise UnsafeCleanupTargetError("cleanup_target_glob_forbidden")
    if "$" in raw or "%" in raw:
        raise UnsafeCleanupTargetError("cleanup_target_variable_forbidden")

    root = Path(repo_root).expanduser().resolve()
    tmp_root = root / "tmp"
    resolved = Path(target).expanduser()
    if not resolved.is_absolute():
        resolved = root / resolved
    resolved = resolved.resolve()

    forbidden = {Path(resolved.anchor).resolve(), Path.home().resolve(), root, tmp_root}
    if resolved in forbidden:
        raise UnsafeCleanupTargetError("cleanup_target_broad")
    try:
        relative = resolved.relative_to(tmp_root)
    except ValueError as exc:
        raise UnsafeCleanupTargetError("cleanup_target_outside_owned_boundary") from exc
    explicit_top_level_targets = {
        "adk-evaluations",
        "adk-workflow-drafts",
        "adk-workflow-exports",
        "local-workbench-diagnostics",
    }
    if len(relative.parts) < 2 and relative.parts[0] not in explicit_top_level_targets:
        raise UnsafeCleanupTargetError("cleanup_target_not_specific")
    return resolved


def _local_state_specs(root: Path) -> tuple[StateTarget, ...]:
    tmp = root / "tmp"
    return (
        StateTarget(
            "adk_sessions",
            "ADK session database",
            tmp / "adk-dev" / "sessions",
            "sessions_traces",
            "developer",
            "delete_on_request",
            "0700 directories; sqlite file owned by local launcher user",
            True,
        ),
        StateTarget(
            "adk_traces",
            "ADK trace state",
            tmp / "adk-dev" / "traces",
            "sessions_traces",
            "developer",
            "delete_on_request",
            "0700 directories; local launcher user only",
            True,
        ),
        StateTarget(
            "adk_developer_artifacts",
            "ADK developer artifact scratch",
            tmp / "adk-dev" / "artifacts",
            "developer_artifacts",
            "developer",
            "delete_on_request",
            "0700 directories; generated from local developer runs",
            True,
        ),
        StateTarget(
            "workflow_drafts",
            "Workflow Lab drafts",
            tmp / "adk-workflow-drafts",
            "drafts_exports",
            "developer",
            "delete_on_request_after_review",
            "project-owned draft tree; no symlink or reparse traversal",
            True,
        ),
        StateTarget(
            "workflow_exports",
            "Workflow Lab exports",
            tmp / "adk-workflow-exports",
            "drafts_exports",
            "developer",
            "delete_on_request_after_review",
            "server-owned export tree; no symlink or reparse traversal",
            True,
        ),
        StateTarget(
            "adk_evaluations",
            "ADK evaluation and benchmark evidence",
            tmp / "adk-evaluations",
            "eval_benchmark",
            "developer",
            "default_7_days_or_delete_on_request",
            "bounded evidence store; business roots are snapshotted only",
            True,
        ),
        StateTarget(
            "workbench_diagnostics",
            "Local workbench diagnostics",
            tmp / "local-workbench-diagnostics",
            "diagnostics",
            "developer",
            "delete_on_request",
            "sanitized JSONL/log files with stable logical IDs",
            True,
        ),
        StateTarget(
            "run_registry",
            "Business run registry",
            tmp / "run-registry.json",
            "registry",
            "business",
            "preserve",
            "business audit state; cleanup never deletes it",
            False,
            "business_state",
        ),
        StateTarget(
            "business_artifacts",
            "Business run artifacts",
            tmp / "api-artifacts",
            "business_artifacts",
            "business",
            "preserve",
            "operator/API artifacts; cleanup never deletes it",
            False,
            "business_state",
        ),
        StateTarget(
            "review_store",
            "Review store",
            tmp / "reviews",
            "review_store",
            "business",
            "preserve",
            "human review records and decisions; cleanup never deletes it",
            False,
            "business_state",
        ),
        StateTarget(
            "review_delivery",
            "Review delivery outbox",
            tmp / "review-outbox",
            "business_artifacts",
            "business",
            "preserve",
            "operator-facing review delivery artifacts; cleanup never deletes it",
            False,
            "business_state",
        ),
        StateTarget(
            "batch_benchmarks",
            "Business benchmark outputs",
            tmp / "batch",
            "eval_benchmark",
            "business",
            "preserve",
            "operator-selected benchmark outputs; cleanup never deletes it",
            False,
            "business_state",
        ),
    )


def _target_payload(target: StateTarget) -> dict[str, Any]:
    resolved = target.target.resolve()
    return {
        "state_id": target.state_id,
        "label": target.label,
        "target": str(resolved),
        "category": target.category,
        "ownership": target.ownership,
        "retention": target.retention,
        "permission_model": target.permission_model,
        "cleanup_allowed": target.cleanup_allowed,
        "exists": resolved.exists(),
        **({"preserve_reason": target.preserve_reason} if target.preserve_reason else {}),
    }


def _remove_path_no_follow(path: Path) -> None:
    metadata = os.lstat(path)
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode):
        path.unlink()
        return
    if attributes & reparse_attribute:
        if stat.S_ISDIR(metadata.st_mode):
            os.rmdir(path)
        else:
            path.unlink()
        return
    if not stat.S_ISDIR(metadata.st_mode):
        _make_writable(path)
        path.unlink()
        return
    for entry in sorted(os.scandir(path), key=lambda item: item.name.casefold()):
        _remove_path_no_follow(Path(entry.path))
    _make_writable(path)
    os.rmdir(path)


def _make_writable(path: Path) -> None:
    try:
        path.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Console entry point for dry-run and explicit local-state cleanup."""

    parser = argparse.ArgumentParser(
        description=(
            "Show or execute safe cleanup for local developer-owned AI Actuary "
            "workbench state. Business registry, artifacts, reviews, and "
            "benchmark outputs are always preserved."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root whose tmp/ state should be inventoried.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Remove the exact developer-owned targets shown in the plan.",
    )
    args = parser.parse_args(argv)
    try:
        payload = execute_cleanup_plan(args.repo_root, dry_run=not args.execute)
    except UnsafeCleanupTargetError as exc:
        payload = {"ok": False, "error_code": str(exc)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


__all__ = [
    "UnsafeCleanupTargetError",
    "build_local_state_cleanup_plan",
    "execute_cleanup_plan",
    "main",
    "validate_cleanup_target",
]
