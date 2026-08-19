"""Authoritative grouped actual-to-expected calculations.

Implements ratio-of-sums A/E for every requested group across four
bases: count/amount times with/without mortality improvement. Zero
expected denominators return a null ratio with a stable reason code.
Additive measures are preserved as exact decimals. Ratios and
interval bounds are produced at full precision; the publication
layer applies any rounding per the documented ``rounding_rule``.

95% Poisson intervals are attached to count results; amount
uncertainty is reported as unavailable because no reconciled
amount-moment interface is supplied to this benchmark.

Cell-level A/E ratios are never averaged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .interfaces import (
    AMOUNT_METRICS,
    COUNT_METRICS,
    ExperienceInput,
    GroupingRequest,
    MetricDefinition,
    decimal_text,
)
from .uncertainty import (
    POISSON_BASIS,
    amount_uncertainty_unavailable,
    count_poisson_interval,
)


__all__ = [
    "ActualToExpectedResult",
    "ZERO_DENOMINATOR_REASON",
    "compute_grouped_actual_to_expected",
]


# Stable reason code returned alongside null ratios for groups whose
# expected total is exactly zero.
ZERO_DENOMINATOR_REASON = "zero_expected_denominator"

# Documented publication boundary. The calculation module retains full
# precision; the publication layer is expected to round displayed
# ratios and interval bounds to this many decimal places.
_ROUNDING_DECIMALS = 6
_ROUNDING_RULE_TEMPLATE = (
    "calculation outputs retain full precision; the publication layer "
    "rounds displayed ratios and interval bounds to {n} decimal places"
)

# A group is marked "credible" when it carries at least this many
# contributing rows. The threshold is documented in the result
# metadata for downstream consumers.
_HIGH_CREDIBILITY_ROW_THRESHOLD = 5


@dataclass(frozen=True)
class ActualToExpectedResult:
    """A single ratio-of-sums result for one group and one metric base.

    The result is immutable and carries every piece of metadata
    required to audit the calculation: population, period, grouping
    dimensions, actual and expected fields, the unit, the MI flag,
    the rounding rule, the credibility flag, and a deterministic
    evidence ID.
    """

    ratio_id: str
    population_id: str
    period: str
    grouping_dimensions: tuple[str, ...]
    group_values: tuple[tuple[str, str], ...]
    metric_kind: str
    actual_field: str
    expected_field: str
    actual_total: str
    expected_total: str
    ratio: str | None
    unit: str
    mortality_improvement: bool
    zero_denominator: bool
    reason_code: str | None
    rounding_rule: str
    credibility_flag: str
    row_count: int
    actual_count_int: int
    lower_ci: str | None
    upper_ci: str | None
    confidence_level: float | None
    uncertainty_basis: str
    evidence_id: str


def _stable_id(prefix: str, *parts: object) -> str:
    """Return a stable, content-addressed identifier for a result."""
    payload = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _non_negative_decimal(value: object, *, field: str) -> Decimal:
    """Return one exact input value while enforcing the calculation domain."""

    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{field} must be finite")
    if decimal_value < 0:
        raise ValueError(f"{field} must be non-negative")
    if field == "Death_Count" and decimal_value != decimal_value.to_integral_value():
        raise ValueError("Death_Count must be an integer")
    return decimal_value


def _group_value(value: object, *, field: str) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        raise ValueError(f"{field} must be a non-empty scalar")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty scalar")
    return text


def _credibility(row_count: int) -> str:
    if row_count >= _HIGH_CREDIBILITY_ROW_THRESHOLD:
        return "credible"
    return "low_credibility"


def _build_result(
    *,
    experience_input: ExperienceInput,
    grouping: GroupingRequest,
    metric: MetricDefinition,
    metric_kind: str,
    actual_total: Decimal,
    expected_total: Decimal,
    row_count: int,
    group_values: tuple[tuple[str, str], ...],
) -> ActualToExpectedResult:
    is_count = metric_kind == "count"
    actual_int = int(actual_total) if is_count else 0

    zero_denominator = expected_total == Decimal("0")
    if zero_denominator:
        ratio_decimal: Decimal | None = None
        reason_code: str | None = ZERO_DENOMINATOR_REASON
    else:
        ratio_decimal = actual_total / expected_total
        reason_code = None

    if is_count:
        interval = count_poisson_interval(
            actual_int, expected_total, confidence=0.95
        )
        if zero_denominator:
            lower_ci: str | None = None
            upper_ci: str | None = None
        else:
            lower_ci = decimal_text(
                Decimal(str(interval.lower_count)) / expected_total
            )
            upper_ci = decimal_text(
                Decimal(str(interval.upper_count)) / expected_total
            )
        basis = POISSON_BASIS
        confidence_level: float | None = 0.95
    else:
        unavailable = amount_uncertainty_unavailable()
        lower_ci = None
        upper_ci = None
        basis = unavailable.basis
        confidence_level = None

    sorted_group_values = [(k, v) for k, v in group_values]
    ratio_id = _stable_id(
        "ratio",
        experience_input.population_id,
        experience_input.period,
        list(grouping.dimensions),
        metric.actual_field,
        metric.expected_field,
        sorted_group_values,
    )
    evidence_id = _stable_id(
        "evidence",
        experience_input.population_id,
        experience_input.period,
        list(grouping.dimensions),
        metric.actual_field,
        metric.expected_field,
        decimal_text(actual_total),
        decimal_text(expected_total),
        decimal_text(ratio_decimal) if ratio_decimal is not None else None,
        sorted_group_values,
    )

    return ActualToExpectedResult(
        ratio_id=ratio_id,
        population_id=experience_input.population_id,
        period=experience_input.period,
        grouping_dimensions=grouping.dimensions,
        group_values=group_values,
        metric_kind=metric_kind,
        actual_field=metric.actual_field,
        expected_field=metric.expected_field,
        actual_total=decimal_text(actual_total),
        expected_total=decimal_text(expected_total),
        ratio=decimal_text(ratio_decimal) if ratio_decimal is not None else None,
        unit=metric.unit,
        mortality_improvement=metric.mortality_improvement,
        zero_denominator=zero_denominator,
        reason_code=reason_code,
        rounding_rule=_ROUNDING_RULE_TEMPLATE.format(n=_ROUNDING_DECIMALS),
        credibility_flag=_credibility(row_count),
        row_count=row_count,
        actual_count_int=actual_int,
        lower_ci=lower_ci,
        upper_ci=upper_ci,
        confidence_level=confidence_level,
        uncertainty_basis=basis,
        evidence_id=evidence_id,
    )


def compute_grouped_actual_to_expected(
    experience_input: ExperienceInput,
    grouping: GroupingRequest,
) -> list[ActualToExpectedResult]:
    """Compute all four ratio-of-sums bases for every requested group.

    For each group produced by ``grouping.dimensions`` (or the total
    population when the dimensions are empty), this returns four
    ``ActualToExpectedResult`` rows: count, count-with-MI, amount,
    and amount-with-MI. Sum-of-ratios (cell-level averaging) is never
    produced.
    """
    rows = experience_input.rows
    schema = rows.collect_schema().names()
    group_cols = list(grouping.dimensions)
    if any(not column.strip() for column in group_cols) or len(set(group_cols)) != len(
        group_cols
    ):
        raise ValueError("grouping dimensions must be unique, non-empty column names")

    numeric_cols: list[str] = []
    for metric in (*COUNT_METRICS, *AMOUNT_METRICS):
        for field in (metric.actual_field, metric.expected_field):
            if field not in numeric_cols:
                numeric_cols.append(field)
    required_columns = set(numeric_cols) | set(group_cols)
    missing = required_columns - set(schema)
    if missing:
        raise KeyError(
            "Required columns missing from ExperienceInput: "
            f"{sorted(missing)}"
        )

    selected_columns = list(dict.fromkeys([*group_cols, *numeric_cols]))
    frame = rows.select(selected_columns).collect()
    if frame.height == 0:
        raise ValueError("ExperienceInput rows must not be empty")

    grouped_totals: dict[
        tuple[tuple[str, str], ...], dict[str, Decimal | int]
    ] = {}
    for row in frame.iter_rows(named=True):
        group_values = tuple(
            (column, _group_value(row[column], field=column)) for column in group_cols
        )
        totals = grouped_totals.setdefault(
            group_values,
            {"_row_count": 0, **{column: Decimal("0") for column in numeric_cols}},
        )
        totals["_row_count"] = int(totals["_row_count"]) + 1
        for column in numeric_cols:
            totals[column] = Decimal(totals[column]) + _non_negative_decimal(
                row[column], field=column
            )

    results: list[ActualToExpectedResult] = []
    metric_kinds = [
        *((metric, "count") for metric in COUNT_METRICS),
        *((metric, "amount") for metric in AMOUNT_METRICS),
    ]
    for group_values in sorted(grouped_totals):
        totals = grouped_totals[group_values]
        row_count = int(totals["_row_count"])
        for metric, kind in metric_kinds:
            actual_total = Decimal(totals[metric.actual_field])
            expected_total = Decimal(totals[metric.expected_field])
            results.append(
                _build_result(
                    experience_input=experience_input,
                    grouping=grouping,
                    metric=metric,
                    metric_kind=kind,
                    actual_total=actual_total,
                    expected_total=expected_total,
                    row_count=row_count,
                    group_values=group_values,
                )
            )
    return results
