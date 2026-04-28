#!/usr/bin/env python3
"""CLI wrapper for replaying a saved case run from a run manifest."""

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
    parser = argparse.ArgumentParser(description="Replay one saved AI Actuary case from a run manifest.")
    parser.add_argument("--manifest-path", required=True, help="Path to the saved run_manifest.json file.")
    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    replay_module = _load_replay_module()
    result = replay_module.replay_case_from_manifest(args.manifest_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
