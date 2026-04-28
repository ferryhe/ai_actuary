from __future__ import annotations

import pytest

from reserving_workflow.calculators import ChainladderAdapter, ChainladderAdapterError
from reserving_workflow.schemas import ReservingCaseInput


def test_chainladder_adapter_with_official_sample():
    case = ReservingCaseInput(
        case_id="raa-sample",
        metadata={"chainladder_sample": "RAA"},
        run_config={"method": "chainladder"},
    )

    result = ChainladderAdapter().calculate(case)

    assert result.case_id == "raa-sample"
    assert result.method == "chainladder"
    assert result.metadata["backend"] == "chainladder-python"
    assert result.metadata["source"] == "sample:RAA"
    assert result.reserve_summary["ultimate"] >= result.reserve_summary["latest_diagonal"]
    assert result.reserve_summary["ibnr"] >= 0
    assert result.diagnostics["origin_count"] > 0


def test_chainladder_adapter_with_triangle_rows():
    rows = [
        {"origin": 1981, "development": 1981, "paid": 100.0},
        {"origin": 1981, "development": 1982, "paid": 150.0},
        {"origin": 1981, "development": 1983, "paid": 180.0},
        {"origin": 1982, "development": 1982, "paid": 120.0},
        {"origin": 1982, "development": 1983, "paid": 170.0},
        {"origin": 1983, "development": 1983, "paid": 130.0},
    ]
    case = ReservingCaseInput(
        case_id="rows-case",
        metadata={
            "triangle_rows": rows,
            "origin_column": "origin",
            "development_column": "development",
            "value_column": "paid",
            "cumulative": True,
        },
        run_config={"method": "chainladder"},
    )

    result = ChainladderAdapter().calculate(case)

    assert result.case_id == "rows-case"
    assert result.reserve_summary["latest_diagonal"] == pytest.approx(480.0)
    assert result.reserve_summary["ultimate"] > result.reserve_summary["latest_diagonal"]
    assert result.metadata["source"] == "rows"


def test_chainladder_adapter_rejects_missing_triangle_source():
    case = ReservingCaseInput(case_id="bad-case")

    with pytest.raises(ChainladderAdapterError, match="chainladder_sample or metadata.triangle_rows"):
        ChainladderAdapter().calculate(case)


def test_chainladder_adapter_rejects_unsupported_method():
    case = ReservingCaseInput(
        case_id="bad-method",
        metadata={"chainladder_sample": "RAA"},
        run_config={"method": "bornhuetter_ferguson"},
    )

    with pytest.raises(ChainladderAdapterError, match="Unsupported method"):
        ChainladderAdapter().calculate(case)


def test_chainladder_adapter_rejects_missing_required_row_columns():
    case = ReservingCaseInput(
        case_id="bad-rows",
        metadata={
            "triangle_rows": [{"origin": 1981, "development": 1981, "paid": 100.0}],
            "value_column": "reported",
        },
    )

    with pytest.raises(ChainladderAdapterError, match="missing required columns"):
        ChainladderAdapter().calculate(case)
