"""Create a deterministic immutable Workflow Lab candidate and patch."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from validate_adk_workflow import _lab_from_draft

from reserving_workflow.adapters.adk.source_integrity import (
    assert_source_integrity_unchanged,
    capture_source_integrity,
)
from reserving_workflow.adapters.adk.workflow_lab import WorkflowLabError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="also require the candidate patch to pass git apply --check",
    )
    arguments = parser.parse_args()
    try:
        lab, app_name, repo_root = _lab_from_draft(arguments.draft)
        if repo_root is None:
            raise WorkflowLabError(
                "source_checkout_required",
                "Deterministic Git diff export is unavailable in installed mode.",
                stage="export",
            )
        before = capture_source_integrity(repo_root)
        receipt = lab.export(app_name)
        try:
            with receipt:
                if arguments.check:
                    _check_patch_applies(
                        repo_root, receipt.export_dir / "candidate.patch"
                    )
                after = capture_source_integrity(repo_root)
                assert_source_integrity_unchanged(before, after)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "check": bool(arguments.check),
                            "integrity_unchanged": True,
                            "export_id": receipt.export_id,
                            "export_dir": str(receipt.export_dir),
                            "bundle_digest": receipt.bundle_digest,
                            "candidate_digest": receipt.candidate_digest,
                            "patch_digest": receipt.patch_digest,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
        except BaseExceptionGroup:
            raise
        except BaseException as failure:
            if isinstance(failure, WorkflowLabError):
                raise
            if not isinstance(failure, Exception):
                raise
            raise WorkflowLabError(
                "post_export_integrity_failed",
                f"Post-export processing failed: {type(failure).__name__}.",
                stage="integrity",
            ) from failure
        return 0
    except WorkflowLabError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": exc.code,
                    "stage": exc.stage,
                    "completed_stages": exc.completed_stages,
                    "message": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


def _check_patch_applies(repo_root: Path, patch: Path) -> None:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", str(patch)],
            cwd=repo_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkflowLabError(
            "patch_check_failed",
            f"Candidate patch applicability check failed: {type(exc).__name__}.",
            stage="export",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise WorkflowLabError(
            "patch_check_failed",
            f"Candidate patch does not apply cleanly: {detail}",
            stage="export",
        )


if __name__ == "__main__":
    raise SystemExit(main())
