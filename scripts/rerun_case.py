#!/usr/bin/env python3
"""Rerun one recorded governed case from the local JSON registry."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def _load_operator_module():
    module_path = Path(__file__).resolve().parents[1] / "src" / "reserving_workflow" / "operator_entrypoint.py"
    spec = importlib.util.spec_from_file_location("operator_entrypoint", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load operator module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rerun one governed AI Actuary case from the local run registry.")
    parser.add_argument("--registry-path", required=True, help="Path to the local run registry JSON file.")
    parser.add_argument("--run-id", required=True, help="Run id to rerun.")
    parser.add_argument("--artifact-dir", default=None, help="Optional override artifact directory for the rerun.")
    parser.add_argument("--review-delivery-dir", default=None, help="Optional override outbox directory for review packet delivery.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    operator = _load_operator_module()
    result = operator.rerun_from_registry(
        args.run_id,
        registry_path=args.registry_path,
        artifact_dir=args.artifact_dir,
        review_delivery_dir=args.review_delivery_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
