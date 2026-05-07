#!/usr/bin/env python3
"""CLI wrapper for running the Prompt 8 batch benchmark comparison flow."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from reserving_workflow.evaluation import DEFAULT_CASE_PACK_ID, load_case_pack


def _load_batch_runner_module():
    module_path = Path(__file__).resolve().parents[1] / "benchmarks" / "runners" / "batch_runner.py"
    spec = importlib.util.spec_from_file_location("batch_runner", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load batch runner module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Prompt 8 benchmark batch comparison.")
    case_source = parser.add_mutually_exclusive_group(required=True)
    case_source.add_argument("--cases-json", help="Path to a JSON file containing a list of benchmark case payloads.")
    case_source.add_argument(
        "--case-pack",
        choices=[DEFAULT_CASE_PACK_ID],
        help="Builtin deterministic benchmark case pack id.",
    )
    parser.add_argument("--artifact-root", required=True, help="Directory where batch artifacts and comparison report will be written.")
    parser.add_argument("--registry-path", default=None, help="Optional run registry path for benchmark-generated runs.")
    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    if args.cases_json:
        cases = json.loads(Path(args.cases_json).read_text(encoding="utf-8"))
        case_pack_id = None
    else:
        case_pack = load_case_pack(args.case_pack)
        cases = case_pack["cases"]
        case_pack_id = case_pack["case_pack_id"]
    runner_module = _load_batch_runner_module()
    result = runner_module.run_batch_benchmark(
        cases=cases,
        artifact_root=args.artifact_root,
        registry_path=args.registry_path,
        case_pack_id=case_pack_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
