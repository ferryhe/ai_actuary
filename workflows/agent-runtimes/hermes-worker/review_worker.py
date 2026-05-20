"""Review packet generation for Hermes worker flows."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reserving_workflow.review.generator import build_review_packet as _build_review_packet

SUPPORTED_TASK = "build_review_packet"


def build_review_packet(worker_result: Any, *, output_dir: str | Path | None = None) -> dict[str, Any]:
    return _build_review_packet(worker_result, output_dir=output_dir)
