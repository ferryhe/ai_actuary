"""Rollback evidence helpers for installed-wheel drills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reserving_workflow.runtime.redaction import sanitize_for_runtime


def build_rollback_summary(
    *,
    baseline_commit: str,
    baseline_wheel: dict[str, Any],
    candidate_wheel: dict[str, Any],
    restored_wheel: dict[str, Any],
    install_steps: list[dict[str, Any]],
    stage_proofs: dict[str, dict[str, Any]],
    business_state_checksums: dict[str, str],
    backup_restore: dict[str, Any],
    resource_audits: dict[str, Any],
    schema_compatibility: dict[str, Any],
) -> dict[str, Any]:
    """Build path-free evidence tying rollback stages to exact artifacts."""

    preserved = (
        business_state_checksums.get("before_candidate")
        == business_state_checksums.get("after_candidate")
        == business_state_checksums.get("after_rollback")
    )
    summary = {
        "ok": preserved and not bool(schema_compatibility.get("fail_closed")),
        "baseline_commit": baseline_commit,
        "baseline_wheel": _wheel_identity(baseline_wheel),
        "candidate_wheel": _wheel_identity(candidate_wheel),
        "restored_wheel": _wheel_identity(restored_wheel),
        "install_steps": [_install_step(step) for step in install_steps],
        "stage_proofs": {
            stage: _stage_proof(proof) for stage, proof in sorted(stage_proofs.items())
        },
        "business_state": {
            "checksums": dict(business_state_checksums),
            "preserved": preserved,
        },
        "backup_restore": sanitize_for_runtime(backup_restore),
        "resource_audits": sanitize_for_runtime(resource_audits),
        "schema_compatibility": sanitize_for_runtime(schema_compatibility),
    }
    return sanitize_for_runtime(summary)


def _wheel_identity(payload: dict[str, Any]) -> dict[str, Any]:
    path = payload.get("path")
    name = Path(path).name if path is not None else None
    return {
        "wheel_artifact": name,
        "wheel_ref": f"wheel:{name}" if name else None,
        "sha256": payload.get("sha256"),
    }


def _install_step(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": payload.get("stage"),
        "command_label": payload.get("command_label") or _command_label(payload.get("command")),
        "exit_code": payload.get("exit_code"),
    }


def _stage_proof(payload: dict[str, Any]) -> dict[str, Any]:
    proof = {
        "version": payload.get("version"),
        "distribution": payload.get("distribution") or "ai-actuary",
        "import_location": "site-packages"
        if "site-packages" in str(payload.get("import_path", "")).replace("\\", "/")
        else "unknown",
        "resources_ok": payload.get("resources_ok"),
    }
    for key in (
        "dependencies_complete",
        "entry_points",
        "distribution_metadata",
        "business_core_read",
        "resource_audit",
    ):
        if key in payload:
            proof[key] = payload.get(key)
    return sanitize_for_runtime(proof)


def _command_label(command: Any) -> str:
    if command is None:
        return "not-recorded"
    text = str(command)
    if "pip install" in text:
        return "pip install wheel"
    return "command"


__all__ = ["build_rollback_summary"]
