#!/usr/bin/env python3
"""Export an auditable operator handoff report for one recorded run."""

from __future__ import annotations

import argparse
import json

from reserving_workflow.reports import export_run_report


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export an operator handoff report from a recorded run.")
    parser.add_argument("--registry-path", required=True, help="Path to the local run registry JSON file.")
    parser.add_argument("--run-id", required=True, help="Run id to export.")
    parser.add_argument("--review-store-dir", default="./tmp/reviews", help="Path to the local review store directory.")
    parser.add_argument("--output-dir", default=None, help="Optional directory for exported markdown/json files.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    payload = export_run_report(
        registry_path=args.registry_path,
        run_id=args.run_id,
        review_store_root=args.review_store_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
