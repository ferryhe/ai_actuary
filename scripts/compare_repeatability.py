#!/usr/bin/env python3
"""CLI wrapper for comparing repeatability across saved run manifests."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path



def _load_replay_module():
    module_path = Path(__file__).resolve().parents[1] / "src" / "reserving_workflow" / "artifacts" / "replay.py"
    spec = importlib.util.spec_from_file_location("replay_module", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load replay module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare repeatability across saved AI Actuary run manifests.")
    parser.add_argument(
        "--manifest-path",
        action="append",
        required=True,
        help="Repeatable flag for run_manifest.json paths to compare.",
    )
    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    replay_module = _load_replay_module()
    result = replay_module.compare_repeatability(args.manifest_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
