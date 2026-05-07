"""Deterministic claim and triangle simulation helpers for benchmark cases."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


DEFAULT_CDF_PATTERN = ("0.55", "0.78", "0.9", "0.97", "1.0")
_SIX_DP = Decimal("0.000001")


def simulate_claim_triangle(simulation: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic claim stream and cumulative triangle rows."""

    simulation_id = str(simulation.get("simulation_id") or "deterministic-simulation")
    origin_year_start = int(simulation.get("origin_year_start", 2018))
    origin_count = int(simulation.get("origin_count", 5))
    development_count = int(simulation.get("development_count", 5))
    base_ultimate = _decimal(simulation.get("base_ultimate", 1000.0))
    origin_increment = _decimal(simulation.get("origin_increment", 100.0))
    curvature = _decimal(simulation.get("curvature", 0.0))
    claim_count_base = max(int(simulation.get("claim_count_base", 2)), 1)
    cdf_pattern = tuple(simulation.get("cdf_pattern") or DEFAULT_CDF_PATTERN)

    if origin_count <= 0:
        raise ValueError("simulation.origin_count must be positive")
    if development_count <= 0:
        raise ValueError("simulation.development_count must be positive")
    if len(cdf_pattern) != development_count:
        raise ValueError("simulation.cdf_pattern length must equal development_count")

    cdf_decimals = [_decimal(value) for value in cdf_pattern]
    previous = Decimal("0")
    for index, value in enumerate(cdf_decimals):
        if value < previous:
            raise ValueError("simulation.cdf_pattern must be non-decreasing")
        previous = value
        if index == len(cdf_decimals) - 1 and value != Decimal("1.0"):
            raise ValueError("simulation.cdf_pattern must end at 1.0")

    claim_records: list[dict[str, Any]] = []
    triangle_rows: list[dict[str, Any]] = []

    for origin_index in range(origin_count):
        origin_year = origin_year_start + origin_index
        observed_development_count = development_count - origin_index
        if observed_development_count <= 0:
            break

        ultimate = base_ultimate + (origin_increment * origin_index) + (curvature * (origin_index**2))
        prior_cumulative = Decimal("0")
        for development_index in range(observed_development_count):
            cumulative = _quantize(ultimate * cdf_decimals[development_index])
            incremental = _quantize(cumulative - prior_cumulative)
            prior_cumulative = cumulative
            development_year = origin_year + development_index
            claim_count = claim_count_base + ((origin_index + development_index) % 2)
            weights = [Decimal(weight) for weight in range(1, claim_count + 1)]
            weight_total = sum(weights)
            remaining = incremental
            for claim_number, weight in enumerate(weights, start=1):
                if claim_number == claim_count:
                    paid_amount = remaining
                else:
                    paid_amount = _quantize(incremental * weight / weight_total)
                    remaining = _quantize(remaining - paid_amount)
                claim_records.append(
                    {
                        "claim_id": f"{simulation_id}-OY{origin_year}-DY{development_year}-C{claim_number:02d}",
                        "origin": origin_year,
                        "development": development_year,
                        "paid_incremental": _float(paid_amount),
                    }
                )
            triangle_rows.append(
                {
                    "origin": origin_year,
                    "development": development_year,
                    "paid": _float(cumulative),
                }
            )

    return {
        "simulation_id": simulation_id,
        "parameters": {
            "origin_year_start": origin_year_start,
            "origin_count": origin_count,
            "development_count": development_count,
            "base_ultimate": _float(base_ultimate),
            "origin_increment": _float(origin_increment),
            "curvature": _float(curvature),
            "claim_count_base": claim_count_base,
            "cdf_pattern": [_float(value) for value in cdf_decimals],
        },
        "claim_records": claim_records,
        "triangle_rows": triangle_rows,
    }


def build_simulated_case_payload(
    *,
    case_id: str,
    simulation: dict[str, Any],
    method: str = "chainladder",
    required_artifacts: list[str] | None = None,
    review_threshold_origin_count: int | None = None,
    metadata_overrides: dict[str, Any] | None = None,
    run_config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    simulated = simulate_claim_triangle(simulation)
    metadata = {
        "triangle_rows": simulated["triangle_rows"],
        "origin_column": "origin",
        "development_column": "development",
        "value_column": "paid",
        "cumulative": True,
        "simulation": simulated,
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)

    run_config = {
        "method": method,
        "required_artifacts": list(required_artifacts or []),
    }
    if review_threshold_origin_count is not None:
        run_config["review_thresholds"] = {"origin_count": review_threshold_origin_count}
    if run_config_overrides:
        run_config.update(run_config_overrides)

    return {
        "case_id": case_id,
        "metadata": metadata,
        "run_config": run_config,
    }


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_SIX_DP, rounding=ROUND_HALF_UP)


def _float(value: Decimal) -> float:
    return float(_quantize(value))
