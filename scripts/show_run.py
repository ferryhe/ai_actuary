#!/usr/bin/env python3
"""Show one recorded AI Actuary run from the local JSON registry."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def _load_registry_module():
    module_path = Path(__file__).resolve().parents[1] / "src" / "reserving_workflow" / "runtime" / "run_registry.py"
    spec = importlib.util.spec_from_file_location("run_registry", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load run registry module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show one AI Actuary run from a local JSON registry.")
    parser.add_argument("--registry-path", required=True, help="Path to the local run registry JSON file.")
    parser.add_argument("--run-id", required=True, help="Run id to inspect.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    registry = _load_registry_module()
    run = registry.get_run(args.registry_path, args.run_id)
    print(json.dumps(run, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
