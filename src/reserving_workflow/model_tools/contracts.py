"""Shared contracts for model-specific experience-study tool comparisons."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EXPERIENCE_NUMERIC_FIELDS = (
    "Death_Count",
    "Death_Claim_Amount",
    "ExpDth_VBT2015_Cnt",
    "ExpDth_VBT2015wMI_Cnt",
    "ExpDth_VBT2015_Amt",
    "ExpDth_VBT2015wMI_Amt",
)
MINIMAX_EXPERIENCE_STUDY_TOOL_ID = "minimax_experience_study_tool"


class ExperienceStudyToolInput(BaseModel):
    """Common C4 input boundary shared by every model implementation."""

    model_config = ConfigDict(extra="forbid")

    population_id: str = "Total"
    period: str = "2018-2019"
    sample_name: Literal["ae_small"] | None = None
    rows: list[dict[str, Any]] | None = None
    dimensions: list[str] = Field(default_factory=lambda: ["product"])

    @model_validator(mode="after")
    def _validate_source_and_rows(self) -> "ExperienceStudyToolInput":
        self.population_id = self.population_id.strip()
        self.period = self.period.strip()
        self.dimensions = [value.strip() for value in self.dimensions]
        if not self.population_id or not self.period:
            raise ValueError("population_id and period must be non-empty")
        if any(not value for value in self.dimensions) or len(set(self.dimensions)) != len(
            self.dimensions
        ):
            raise ValueError("dimensions must contain unique, non-empty column names")
        if self.sample_name is None and self.rows is None:
            self.sample_name = "ae_small"
        if self.sample_name is not None and self.rows is not None:
            raise ValueError("Provide exactly one of sample_name or rows.")
        if self.rows is not None:
            if not self.rows:
                raise ValueError("rows must not be empty")
            normalized_rows: list[dict[str, Any]] = []
            for row_number, row in enumerate(self.rows, start=1):
                missing = [
                    field
                    for field in (*EXPERIENCE_NUMERIC_FIELDS, *self.dimensions)
                    if field not in row
                ]
                if missing:
                    raise ValueError(f"row {row_number} is missing required columns: {missing}")
                normalized_row = dict(row)
                for field in EXPERIENCE_NUMERIC_FIELDS:
                    try:
                        value = Decimal(str(row[field]))
                    except (InvalidOperation, ValueError) as exc:
                        raise ValueError(
                            f"row {row_number} column {field} must be numeric"
                        ) from exc
                    if not value.is_finite():
                        raise ValueError(f"row {row_number} column {field} must be finite")
                    if value < 0:
                        raise ValueError(
                            f"row {row_number} column {field} must be non-negative"
                        )
                    if field == "Death_Count" and value != value.to_integral_value():
                        raise ValueError(
                            f"row {row_number} column Death_Count must be an integer"
                        )
                    normalized_row[field] = format(value, "f")
                for field in self.dimensions:
                    raw_value = normalized_row[field]
                    if raw_value is None or isinstance(raw_value, (dict, list, tuple, set)):
                        raise ValueError(
                            f"row {row_number} column {field} must be a non-empty scalar"
                        )
                    value = str(raw_value).strip()
                    if not value:
                        raise ValueError(
                            f"row {row_number} column {field} must be a non-empty scalar"
                        )
                    normalized_row[field] = value
                normalized_rows.append(normalized_row)
            self.rows = normalized_rows
        return self
