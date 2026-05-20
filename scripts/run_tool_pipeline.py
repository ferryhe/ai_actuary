#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from reserving_workflow.tool_runner import run_pipeline
from reserving_workflow.tool_runner.runner import ToolRunnerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local artifact-backed reserving tool pipeline.")
    parser.add_argument("--pipeline", required=True, help="Path to pipeline YAML.")
    parser.add_argument("--input", required=True, help="Path to case_input.json.")
    parser.add_argument("--artifact-root", default=None, help="Optional run artifact root override.")
    parser.add_argument("--run-id", default=None, help="Optional stable run id.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_pipeline(
            repo_root=REPO_ROOT,
            pipeline_path=args.pipeline,
            input_path=args.input,
            artifact_root=args.artifact_root,
            run_id=args.run_id,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary must convert all setup failures to stable output.
        payload = _error_payload(exc)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"error[{payload['error']['category']}]: {payload['error']['message']}", file=sys.stderr)
        return 2 if payload["error"]["category"] in {"validation_error", "io_error"} else 1

    payload = result.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"pipeline_id={payload['pipeline_id']} run_id={payload['run_id']} status={payload['status']} run_status={payload['run_status']}")
        print(f"artifact_root={payload['artifact_root']}")
        for step in payload["steps"]:
            print(f"- {step['step_id']}: {step['status']}")
    return 0 if result.ok else 1


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ToolRunnerError):
        category = exc.category
        details = exc.details
    elif isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        category = "io_error"
        details = {}
    elif isinstance(exc, (ValueError, TypeError)):
        category = "validation_error"
        details = {}
    else:
        category = "runner_error"
        details = {"exception_type": type(exc).__name__}
    return {
        "ok": False,
        "status": "error",
        "error": {
            "category": category,
            "message": str(exc),
            "details": details,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
