"""Explicit validation helpers for reserving tool inputs and case payloads."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping

import pandas as pd

try:
    import chainladder as cl
except ImportError as exc:  # pragma: no cover - exercised by environment setup, not tests
    raise RuntimeError(
        "chainladder-python is required for reserving validation helpers. "
        "Install the 'chainladder' package before using this module."
    ) from exc

from reserving_workflow.schemas import ReservingCaseInput


SUPPORTED_CHAINLADDER_METHODS = frozenset({"chainladder", "mack_chainladder"})


class ReservingValidationError(ValueError):
    """Raised when tool inputs or case payloads fail deterministic validation."""


@dataclass(frozen=True)
class ValidatedChainladderSource:
    triangle: Any
    source_description: str
    source_kind: str
    method: str


def build_chainladder_case_payload(
    *,
    case_id: str,
    tool_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_case_id = str(case_id).strip()
    metadata: dict[str, Any] = {}
    if tool_inputs.get("triangle_rows") is not None:
        metadata["triangle_rows"] = list(tool_inputs["triangle_rows"])
        metadata["origin_column"] = tool_inputs.get("origin_column", "origin")
        metadata["development_column"] = tool_inputs.get("development_column", "development")
        metadata["value_column"] = tool_inputs.get("value_column", "value")
        metadata["cumulative"] = bool(tool_inputs.get("cumulative", True))
        if tool_inputs.get("index_column") is not None:
            metadata["index_column"] = tool_inputs["index_column"]
    else:
        metadata["chainladder_sample"] = tool_inputs.get("sample_name", "RAA")

    run_config: dict[str, Any] = {
        "method": tool_inputs.get("method_variant", "chainladder"),
    }
    review_threshold = tool_inputs.get("review_threshold_origin_count")
    if review_threshold is not None:
        run_config["review_thresholds"] = {"origin_count": review_threshold}
    return {
        "case_id": normalized_case_id,
        "metadata": metadata,
        "run_config": run_config,
    }


def build_chainladder_case_input(
    *,
    case_id: str,
    tool_inputs: Mapping[str, Any],
) -> ReservingCaseInput:
    return ReservingCaseInput.model_validate(
        build_chainladder_case_payload(case_id=case_id, tool_inputs=tool_inputs)
    )


def validate_chainladder_case(case_input: ReservingCaseInput) -> ValidatedChainladderSource:
    method_name = _normalized_method_name(case_input)
    metadata = case_input.metadata or {}
    sample_name = _normalized_optional_string(
        metadata.get("chainladder_sample") or metadata.get("sample_name")
    )
    triangle_rows = metadata.get("triangle_rows")

    if sample_name is not None and triangle_rows is not None:
        raise ReservingValidationError(
            "Case input must provide exactly one triangle source: metadata.chainladder_sample or metadata.triangle_rows."
        )
    if sample_name is not None:
        try:
            triangle = cl.load_sample(sample_name)
        except Exception as exc:
            raise ReservingValidationError(
                f"Unknown chainladder sample {sample_name!r}. Provide a valid sample name or metadata.triangle_rows."
            ) from exc
        return ValidatedChainladderSource(
            triangle=triangle,
            source_description=f"sample:{sample_name}",
            source_kind="sample",
            method=method_name,
        )
    if triangle_rows is not None:
        triangle = _triangle_from_rows(triangle_rows, metadata)
        return ValidatedChainladderSource(
            triangle=triangle,
            source_description="rows",
            source_kind="triangle_rows",
            method=method_name,
        )
    raise ReservingValidationError(
        "Case input must provide metadata.chainladder_sample or metadata.triangle_rows."
    )


def build_chainladder_validation_summary(
    case_input: ReservingCaseInput,
    validated_source: ValidatedChainladderSource,
) -> dict[str, Any]:
    triangle = validated_source.triangle
    metadata = case_input.metadata or {}
    source_kind = getattr(validated_source, "source_kind", None)
    if source_kind is None:
        source_kind = getattr(validated_source, "input_kind")
    summary: dict[str, Any] = {
        "case_id": case_input.case_id,
        "status": "validated",
        "tool_id": "chainladder",
        "method": validated_source.method,
        "source_kind": source_kind,
        "source": validated_source.source_description,
        "diagnostics": {
            "origin_count": len(triangle.origin),
            "development_count": len(triangle.development),
            "valuation_date": str(triangle.valuation_date),
            "is_cumulative": bool(triangle.is_cumulative),
            "triangle_shape": list(triangle.shape),
        },
    }
    if source_kind == "sample":
        summary["normalized_input"] = {
            "sample_name": metadata.get("chainladder_sample") or metadata.get("sample_name"),
        }
    else:
        summary["normalized_input"] = {
            "triangle_rows": len(metadata.get("triangle_rows", []) or []),
            "origin_column": metadata.get("origin_column", "origin"),
            "development_column": metadata.get("development_column", "development"),
            "value_column": metadata.get("value_column", "value"),
            "cumulative": bool(metadata.get("cumulative", True)),
            "index_column": metadata.get("index_column"),
        }
    return summary


def _normalized_method_name(case_input: ReservingCaseInput) -> str:
    method_name = str(case_input.run_config.get("method", "chainladder")).strip().lower()
    if method_name not in SUPPORTED_CHAINLADDER_METHODS:
        supported = ", ".join(sorted(SUPPORTED_CHAINLADDER_METHODS))
        raise ReservingValidationError(
            f"Unsupported method '{method_name}'. Supported methods: {supported}."
        )
    return method_name


def _normalized_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        raise ReservingValidationError("Triangle source names must not be empty strings.")
    return normalized


def _triangle_from_rows(triangle_rows: Any, metadata: Mapping[str, Any]):
    if not isinstance(triangle_rows, list) or not triangle_rows:
        raise ReservingValidationError("metadata.triangle_rows must be a non-empty list of row objects.")
    for index, row in enumerate(triangle_rows):
        if not isinstance(row, dict):
            raise ReservingValidationError(f"metadata.triangle_rows[{index}] must be an object.")

    origin_col = _required_column_name(metadata.get("origin_column", "origin"), "origin_column")
    development_col = _required_column_name(
        metadata.get("development_column", "development"),
        "development_column",
    )
    value_col = _required_column_name(metadata.get("value_column", "value"), "value_column")

    duplicates: set[tuple[Any, Any]] = set()
    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(triangle_rows):
        missing_cols = [col for col in (origin_col, development_col, value_col) if col not in row]
        if missing_cols:
            raise ReservingValidationError(
                f"metadata.triangle_rows[{index}] is missing required columns: {', '.join(missing_cols)}"
            )
        if row[origin_col] is None:
            raise ReservingValidationError(
                f"metadata.triangle_rows[{index}].{origin_col} must not be null."
            )
        if row[development_col] is None:
            raise ReservingValidationError(
                f"metadata.triangle_rows[{index}].{development_col} must not be null."
            )
        numeric_value = _coerce_numeric(
            row[value_col],
            field_path=f"metadata.triangle_rows[{index}].{value_col}",
        )
        key = (row[origin_col], row[development_col])
        if key in duplicates:
            raise ReservingValidationError(
                f"metadata.triangle_rows contains a duplicate origin/development combination: {key!r}"
            )
        duplicates.add(key)
        normalized_row = dict(row)
        normalized_row[value_col] = numeric_value
        normalized_rows.append(normalized_row)

    try:
        frame = pd.DataFrame(normalized_rows)
    except ValueError as exc:
        raise ReservingValidationError(
            "metadata.triangle_rows could not be converted into a DataFrame."
        ) from exc

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
    try:
        return cl.Triangle(**triangle_kwargs)
    except Exception as exc:
        raise ReservingValidationError(
            "metadata.triangle_rows could not be converted into a chainladder Triangle."
        ) from exc


def _required_column_name(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ReservingValidationError(f"metadata.{field_name} must not be empty.")
    return normalized


def _coerce_numeric(value: Any, *, field_path: str) -> float:
    if isinstance(value, bool):
        raise ReservingValidationError(f"{field_path} must be a finite number.")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ReservingValidationError(f"{field_path} must be a finite number.") from exc
    if not isfinite(numeric_value):
        raise ReservingValidationError(f"{field_path} must be a finite number.")
    return numeric_value
