"""Builtin deterministic benchmark case packs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reserving_workflow.operator_entrypoint import DEFAULT_REQUIRED_ARTIFACTS
from reserving_workflow.evaluation.simulation import build_simulated_case_payload


DEFAULT_CASE_PACK_ID = "deterministic-v1"


def load_case_pack(case_pack_id: str = DEFAULT_CASE_PACK_ID) -> dict[str, Any]:
    if case_pack_id != DEFAULT_CASE_PACK_ID:
        raise ValueError(f"Unknown benchmark case pack: {case_pack_id}")
    payload = json.loads(_default_case_pack_path().read_text(encoding="utf-8"))
    resolved_cases = [resolve_case_definition(case) for case in payload.get("cases", [])]
    return {
        "case_pack_id": payload.get("case_pack_id", case_pack_id),
        "description": payload.get("description"),
        "cases": resolved_cases,
    }


def resolve_case_definition(case_definition: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(case_definition)
    case_id = str(resolved["case_id"])
    if resolved.get("simulation") is not None and resolved.get("case_payload") is None:
        review_threshold_origin_count = resolved.get("review_threshold_origin_count")
        resolved["case_payload"] = build_simulated_case_payload(
            case_id=case_id,
            simulation=dict(resolved["simulation"]),
            method=str(resolved.get("method", "chainladder")),
            required_artifacts=list(DEFAULT_REQUIRED_ARTIFACTS),
            review_threshold_origin_count=review_threshold_origin_count,
        )
    return resolved


def _default_case_pack_path() -> Path:
    return Path(__file__).resolve().parents[3] / "benchmarks" / "case_packs" / "deterministic_case_pack.json"
