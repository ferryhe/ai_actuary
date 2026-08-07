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
