"""Public interfaces supplied to the C4 code-generation benchmark."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import polars as pl


@dataclass(frozen=True)
class ExperienceInput:
    """In-memory row boundary constructed and supplied by the benchmark harness."""

    population_id: str
    period: str
    rows: pl.LazyFrame


@dataclass(frozen=True)
class GroupingRequest:
    """Ordered dimensions used for an authoritative ratio-of-sums result."""

    dimensions: tuple[str, ...]


@dataclass(frozen=True)
class MetricDefinition:
    actual_field: str
    expected_field: str
    unit: str
    mortality_improvement: bool


COUNT_METRICS = (
    MetricDefinition("Death_Count", "ExpDth_VBT2015_Cnt", "deaths", False),
    MetricDefinition("Death_Count", "ExpDth_VBT2015wMI_Cnt", "deaths", True),
)
AMOUNT_METRICS = (
    MetricDefinition("Death_Claim_Amount", "ExpDth_VBT2015_Amt", "USD", False),
    MetricDefinition("Death_Claim_Amount", "ExpDth_VBT2015wMI_Amt", "USD", True),
)


def decimal_text(value: Decimal) -> str:
    """The publication layer owns rounding; calculation outputs retain exact decimals."""

    return format(value, "f")
