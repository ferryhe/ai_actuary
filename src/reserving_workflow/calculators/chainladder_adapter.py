"""Adapter boundary for CAS chainladder-python.

This module treats `chainladder-python` as an external actuarial tool.
It does not reimplement reserving algorithms; it only normalizes inputs
and maps outputs into this project's deterministic result contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from pydantic import ValidationError

try:
    import chainladder as cl
except ImportError as exc:  # pragma: no cover - exercised by environment setup, not tests
    raise RuntimeError(
        "chainladder-python is required for ChainladderAdapter. "
        "Install the 'chainladder' package before using this adapter."
    ) from exc

from reserving_workflow.schemas import DeterministicReserveResult, ReservingCaseInput
from reserving_workflow.validation import (
    ReservingValidationError,
    build_chainladder_validation_summary,
    validate_chainladder_case,
)


class ChainladderAdapterError(ValueError):
    """Raised when case input cannot be translated into a chainladder run."""


@dataclass(frozen=True)
class _TriangleSource:
    triangle: Any
    source_description: str
    input_kind: str
    method: str


class ChainladderAdapter:
    """Minimal deterministic calculator boundary backed by chainladder-python."""

    _METHODS = {
        "chainladder": cl.Chainladder,
        "mack_chainladder": cl.MackChainladder,
    }

    def calculate(self, case_input: ReservingCaseInput) -> DeterministicReserveResult:
        try:
            source = self._build_triangle_source(case_input)
        except ReservingValidationError as exc:
            raise ChainladderAdapterError(str(exc)) from exc
        method_name = source.method
        estimator_cls = self._METHODS.get(method_name)
        if estimator_cls is None:
            supported = ", ".join(sorted(self._METHODS))
            raise ChainladderAdapterError(
                f"Unsupported method '{method_name}'. Supported methods: {supported}."
            )

        model = estimator_cls().fit(source.triangle)
        latest_diagonal = self._triangle_total(source.triangle.latest_diagonal)
        ultimate_total = self._triangle_total(model.ultimate_)
        ibnr_total = self._triangle_total(model.ibnr_)

        return DeterministicReserveResult(
            case_id=case_input.case_id,
            method=method_name,
            reserve_summary={
                "latest_diagonal": latest_diagonal,
                "ultimate": ultimate_total,
                "ibnr": ibnr_total,
            },
            diagnostics={
                "origin_count": len(source.triangle.origin),
                "development_count": len(source.triangle.development),
                "valuation_date": str(source.triangle.valuation_date),
                "is_cumulative": bool(source.triangle.is_cumulative),
                "input_validation": build_chainladder_validation_summary(case_input, source),
            },
            metadata={
                "backend": "chainladder-python",
                "source": source.source_description,
                "triangle_shape": list(source.triangle.shape),
            },
        )

    def _build_triangle_source(self, case_input: ReservingCaseInput) -> _TriangleSource:
        validated = validate_chainladder_case(case_input)
        return _TriangleSource(
            triangle=validated.triangle,
            source_description=validated.source_description,
            input_kind=validated.source_kind,
            method=validated.method,
        )

    @staticmethod
    def _triangle_total(triangle_obj: Any) -> float:
        frame = triangle_obj.to_frame().fillna(0)
        return float(frame.sum(numeric_only=True).iloc[0])


def calculate_deterministic_reserve(case_payload: dict[str, Any]) -> DeterministicReserveResult:
    """Convenience function for callers that hold raw payload dictionaries."""
    try:
        case_input = ReservingCaseInput.model_validate(case_payload)
    except ValidationError as exc:
        raise ChainladderAdapterError("Invalid reserving case payload.") from exc
    return ChainladderAdapter().calculate(case_input)
