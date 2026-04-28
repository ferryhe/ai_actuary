#!/usr/bin/env python3
"""CLI wrapper for the operator-facing governed workflow entrypoint."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_operator_module():
    module_path = Path(__file__).resolve().parents[1] / "src" / "reserving_workflow" / "operator_entrypoint.py"
    spec = importlib.util.spec_from_file_location("operator_entrypoint", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    operator_module = _load_operator_module()
    result = operator_module.main(argv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
