#!/usr/bin/env python3
"""Import a verified benchmark component candidate into AI Actuary review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reserving_workflow.review.benchmark_import import (  # noqa: E402
    BenchmarkImportError,
    import_benchmark_review,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify and import a ready_for_human_review benchmark result into "
            "the AI Actuary review queue."
        )
    )
    parser.add_argument("--manifest", required=True, help="sandbox_evaluation_manifest.json path")
    parser.add_argument("--registry-path", default=str(ROOT / "tmp" / "run-registry.json"))
    parser.add_argument("--artifact-root", default=str(ROOT / "tmp" / "api-artifacts"))
    parser.add_argument("--review-store", default=str(ROOT / "tmp" / "reviews"))
    parser.add_argument("--operator-id", default="local-actuary")
    parser.add_argument("--workspace-id", default="default-workspace")
    parser.add_argument("--case-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = import_benchmark_review(
            manifest_path=args.manifest,
            registry_path=args.registry_path,
            artifact_root=args.artifact_root,
            review_store_root=args.review_store,
            operator_id=args.operator_id,
            workspace_id=args.workspace_id,
            case_id=args.case_id,
        )
    except BenchmarkImportError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
