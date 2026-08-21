"""Validate one isolated declarative ADK Workflow Lab draft."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from reserving_workflow.adapters.adk.source_integrity import (
    assert_source_integrity_unchanged,
    capture_source_integrity,
)
from reserving_workflow.adapters.adk.workflow_lab import WorkflowLab, WorkflowLabError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft", type=Path)
    arguments = parser.parse_args()
    try:
        lab, app_name, repo_root = _lab_from_draft(arguments.draft)
        before = capture_source_integrity(repo_root) if repo_root else None
        report = lab.validate(app_name)
        if before is not None:
            after = capture_source_integrity(repo_root)
            assert_source_integrity_unchanged(before, after)
        payload = asdict(report)
        payload["ok"] = True
        payload["integrity_unchanged"] = before is not None
        print(json.dumps(payload, indent=2, sort_keys=True))
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


def _lab_from_draft(draft: Path) -> tuple[WorkflowLab, str, Path | None]:
    absolute = draft.absolute()
    if absolute.parent.name != "adk-workflow-drafts" or absolute.parent.parent.name != "tmp":
        raise WorkflowLabError(
            "draft_root_required",
            "Draft must be tmp/adk-workflow-drafts/<app>.",
            stage="preflight",
        )
    app_name = absolute.name
    repo_root = absolute.parent.parent.parent
    if (repo_root / ".git").exists():
        return WorkflowLab.for_source_checkout(repo_root), app_name, repo_root
    return WorkflowLab.for_installed_runtime(absolute.parent.parent), app_name, None


if __name__ == "__main__":
    raise SystemExit(main())
