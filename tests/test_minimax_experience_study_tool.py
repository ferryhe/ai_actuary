from __future__ import annotations

import pytest
from pydantic import ValidationError

from reserving_workflow.model_tools import (
    MINIMAX_EXPERIENCE_STUDY_TOOL_ID,
    ExperienceStudyToolInput,
    execute_minimax_experience_study,
)


def test_minimax_experience_study_runs_the_shared_synthetic_sample():
    results = execute_minimax_experience_study(ExperienceStudyToolInput())

    assert len(results) == 8
    term_results = {
        (item["metric_kind"], item["mortality_improvement"]): item
        for item in results
        if item["group_values"] == (("product", "Term"),)
    }
    assert term_results[("count", False)]["ratio"] == "1.5"
    assert term_results[("count", True)]["ratio"] == "1.5"
    assert term_results[("amount", False)]["ratio"] == "1.25"
    assert term_results[("amount", True)]["ratio"] == "1.2"
    assert all(
        item["reason_code"] == "zero_expected_denominator"
        for item in results
        if item["group_values"] == (("product", "Whole"),)
    )


def test_minimax_tool_id_is_stable_for_cross_model_comparisons():
    assert MINIMAX_EXPERIENCE_STUDY_TOOL_ID == "minimax_experience_study_tool"


def test_shared_experience_input_rejects_non_numeric_comparison_rows():
    with pytest.raises(ValidationError, match="Death_Count must be numeric"):
        ExperienceStudyToolInput.model_validate(
            {
                "dimensions": ["product"],
                "rows": [
                    {
                        "Death_Count": "not-a-number",
                        "Death_Claim_Amount": "1",
                        "ExpDth_VBT2015_Cnt": "1",
                        "ExpDth_VBT2015wMI_Cnt": "1",
                        "ExpDth_VBT2015_Amt": "1",
                        "ExpDth_VBT2015wMI_Amt": "1",
                        "product": "Term",
                    }
                ],
            }
        )


@pytest.mark.parametrize("death_count", ["-1", "1.5"])
def test_shared_experience_input_rejects_invalid_death_counts(death_count: str):
    with pytest.raises(ValidationError, match="Death_Count must be"):
        ExperienceStudyToolInput.model_validate(
            {
                "rows": [
                    {
                        "Death_Count": death_count,
                        "Death_Claim_Amount": "1",
                        "ExpDth_VBT2015_Cnt": "1",
                        "ExpDth_VBT2015wMI_Cnt": "1",
                        "ExpDth_VBT2015_Amt": "1",
                        "ExpDth_VBT2015wMI_Amt": "1",
                        "product": "Term",
                    }
                ]
            }
        )


def test_large_amount_totals_do_not_overflow_decimal128():
    row = {
        "Death_Count": "1",
        "Death_Claim_Amount": "1000000000",
        "ExpDth_VBT2015_Cnt": "1",
        "ExpDth_VBT2015wMI_Cnt": "1",
        "ExpDth_VBT2015_Amt": "1000000000",
        "ExpDth_VBT2015wMI_Amt": "1000000000",
        "product": "Term",
    }

    results = execute_minimax_experience_study(
        ExperienceStudyToolInput(rows=[dict(row) for _ in range(100)])
    )

    amount_result = next(
        item
        for item in results
        if item["metric_kind"] == "amount" and not item["mortality_improvement"]
    )
    assert amount_result["actual_total"] == "100000000000"
    assert amount_result["expected_total"] == "100000000000"
    assert amount_result["ratio"] == "1"
