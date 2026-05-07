from __future__ import annotations

import pytest

from reserving_workflow.schemas import ReservingCaseInput
from reserving_workflow.validation import (
    ReservingValidationError,
    build_chainladder_case_payload,
    validate_chainladder_case,
)


def test_build_chainladder_case_payload_normalizes_case_id_whitespace():
    payload = build_chainladder_case_payload(
        case_id="  trimmed-case  ",
        tool_inputs={"sample_name": "RAA", "method_variant": "chainladder"},
    )

    assert payload["case_id"] == "trimmed-case"
    assert ReservingCaseInput.model_validate(payload).case_id == "trimmed-case"


def test_validate_chainladder_case_uses_stable_triangle_construction_error_message():
    case = ReservingCaseInput(
        case_id="bad-triangle",
        metadata={
            "triangle_rows": [
                {"origin": "not-a-year", "development": 12, "value": 100.0},
            ],
        },
        run_config={"method": "chainladder"},
    )

    with pytest.raises(
        ReservingValidationError,
        match=r"^metadata\.triangle_rows could not be converted into a chainladder Triangle\.$",
    ) as exc_info:
        validate_chainladder_case(case)

    assert exc_info.value.__cause__ is not None
