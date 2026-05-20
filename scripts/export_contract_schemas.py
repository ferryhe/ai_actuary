#!/usr/bin/env python3
"""Export versioned JSON Schema contracts for actuarial reserving models."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from reserving_workflow.contracts.control_plane import Review, Run, RunEvent, ToolInvocation, Workflow
from reserving_workflow.schemas.core import (
    ConstitutionCheckResult,
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    RunArtifactManifest,
)

MODELS: tuple[type, ...] = (
    ReservingCaseInput,
    DeterministicReserveResult,
    NarrativeDraft,
    ConstitutionCheckResult,
    RunArtifactManifest,
    ToolInvocation,
    Workflow,
    Run,
    RunEvent,
    Review,
)

OUTPUT_DIR = REPO_ROOT / "schemas" / "actuarial-reserving" / "v1"
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _schema_filename(model: type) -> str:
    return f"{model.__name__}.schema.json"


def _build_schema(model: type) -> dict:
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = SCHEMA_DRAFT
    return schema


def export_models(models: Iterable[type] = MODELS, output_dir: Path = OUTPUT_DIR) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in models:
        path = output_dir / _schema_filename(model)
        payload = _build_schema(model)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    written = export_models()
    summary = {
        "ok": True,
        "output_dir": str(OUTPUT_DIR),
        "files": [path.name for path in written],
        "count": len(written),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
