"""Evaluation and benchmark scoring boundary for CAS Core."""

from .case_packs import DEFAULT_CASE_PACK_ID, load_case_pack, resolve_case_definition
from .comparison import score_batch_mode_results
from .simulation import build_simulated_case_payload, simulate_claim_triangle

__all__ = [
    "DEFAULT_CASE_PACK_ID",
    "build_simulated_case_payload",
    "load_case_pack",
    "resolve_case_definition",
    "score_batch_mode_results",
    "simulate_claim_triangle",
]
