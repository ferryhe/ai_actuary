"""Adapter boundary for CAS chainladder-python.

This module treats `chainladder-python` as an external actuarial tool.
It does not reimplement reserving algorithms; it only normalizes inputs
and maps outputs into this project's deterministic result contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from pydantic import ValidationError

try:
    import chainladder as cl
except ImportError as exc:  # pragma: no cover - exercised by environment setup, not tests
    raise RuntimeError(
        "chainladder-python is required for ChainladderAdapter. "
        "Install the 'chainladder' package before using this adapter."
    ) from exc

from reserving_workflow.schemas import DeterministicReserveResult, ReservingCaseInput


class ChainladderAdapterError(ValueError):
    """Raised when case input cannot be translated into a chainladder run."""


@dataclass(frozen=True)
class _TriangleSource:
    triangle: Any
    source_description: str


class ChainladderAdapter:
    """Minimal deterministic calculator boundary backed by chainladder-python."""

    _METHODS = {
        "chainladder": cl.Chainladder,
        "mack_chainladder": cl.MackChainladder,
    }

    def calculate(self, case_input: ReservingCaseInput) -> DeterministicReserveResult:
        source = self._build_triangle_source(case_input)
        method_name = str(case_input.run_config.get("method", "chainladder")).lower()
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
            },
            metadata={
                "backend": "chainladder-python",
                "source": source.source_description,
                "triangle_shape": list(source.triangle.shape),
            },
        )

    def _build_triangle_source(self, case_input: ReservingCaseInput) -> _TriangleSource:
        metadata = case_input.metadata or {}
        sample_name = metadata.get("chainladder_sample") or metadata.get("sample_name")
        if sample_name:
            return _TriangleSource(
                triangle=cl.load_sample(str(sample_name)),
                source_description=f"sample:{sample_name}",
            )

        triangle_rows = metadata.get("triangle_rows")
        if triangle_rows:
            return _TriangleSource(
                triangle=self._triangle_from_rows(triangle_rows, metadata),
                source_description="rows",
            )

        raise ChainladderAdapterError(
            "Case input must provide metadata.chainladder_sample or metadata.triangle_rows."
        )

    def _triangle_from_rows(self, triangle_rows: Any, metadata: dict[str, Any]):
        if not isinstance(triangle_rows, list) or not triangle_rows:
            raise ChainladderAdapterError("metadata.triangle_rows must be a non-empty list of row dicts.")
        try:
            frame = pd.DataFrame(triangle_rows)
        except ValueError as exc:
            raise ChainladderAdapterError("metadata.triangle_rows could not be converted into a DataFrame.") from exc

        origin_col = metadata.get("origin_column", "origin")
        development_col = metadata.get("development_column", "development")
        value_col = metadata.get("value_column", "value")
        missing_cols = [col for col in (origin_col, development_col, value_col) if col not in frame.columns]
        if missing_cols:
            raise ChainladderAdapterError(
                "triangle_rows is missing required columns: " + ", ".join(missing_cols)
            )

        cumulative = bool(metadata.get("cumulative", True))
        index_col = metadata.get("index_column")
        triangle_kwargs = {
            "data": frame,
            "origin": origin_col,
            "development": development_col,
            "columns": value_col,
            "cumulative": cumulative,
        }
        if index_col:
            triangle_kwargs["index"] = index_col
        return cl.Triangle(**triangle_kwargs)

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
