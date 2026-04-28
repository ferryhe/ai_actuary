from reserving_workflow.constitution import evaluate_case_constitution
from reserving_workflow.schemas import (
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    RunArtifactManifest,
)


def test_constitution_pass_case():
    case = ReservingCaseInput(
        case_id="case-pass",
        metadata={"chainladder_sample": "RAA"},
        run_config={
            "required_artifacts": ["deterministic_result", "run_manifest"],
            "review_thresholds": {"ratio": 2.0},
        },
    )
    result = DeterministicReserveResult(
        case_id="case-pass",
        method="chainladder",
        reserve_summary={"ibnr": 42.0, "ultimate": 100.0},
        diagnostics={"ratio": 1.2},
    )
    draft = NarrativeDraft(
        case_id="case-pass",
        summary="Looks stable.",
        key_points=["No anomaly noted"],
        cited_values={"ibnr": 42.0},
    )
    manifest = RunArtifactManifest(
        case_id="case-pass",
        run_id="run-001",
        artifact_paths={"deterministic_result": "a.json", "run_manifest": "m.json"},
    )

    check = evaluate_case_constitution(case, result, draft, manifest)

    assert check.status == "pass"
    assert check.hard_constraints == []
    assert check.review_triggers == []


def test_constitution_hard_fail_on_numeric_mismatch_and_missing_input():
    case = ReservingCaseInput(case_id="case-fail")
    result = DeterministicReserveResult(
        case_id="case-fail",
        method="chainladder",
        reserve_summary={"ibnr": 42.0},
        diagnostics={},
    )
    draft = NarrativeDraft(
        case_id="case-fail",
        summary="Mismatch.",
        cited_values={"ibnr": 41.0, "unsupported": 5.0},
    )

    check = evaluate_case_constitution(case, result, draft)

    assert check.status == "fail"
    assert any(item.startswith("required_input_missing") for item in check.hard_constraints)
    assert any(item.startswith("numeric_mismatch:ibnr") for item in check.hard_constraints)
    assert any(item.startswith("unsupported_numeric_claim:unsupported") for item in check.hard_constraints)


def test_constitution_review_required_on_threshold_and_artifact_gap():
    case = ReservingCaseInput(
        case_id="case-review",
        metadata={"triangle_rows": [{"origin": 1981, "development": 1981, "paid": 100.0}]},
        run_config={
            "review_thresholds": {"ratio": 1.0},
            "required_artifacts": ["deterministic_result", "constitution_check"],
        },
    )
    result = DeterministicReserveResult(
        case_id="case-review",
        method="chainladder",
        reserve_summary={"ibnr": 10.0},
        diagnostics={"ratio": 1.4},
    )
    draft = NarrativeDraft(
        case_id="case-review",
        summary="Threshold crossed.",
        cited_values={"ibnr": 10.0},
    )
    manifest = RunArtifactManifest(
        case_id="case-review",
        run_id="run-002",
        artifact_paths={"deterministic_result": "a.json"},
    )

    check = evaluate_case_constitution(case, result, draft, manifest)

    assert check.status == "review_required"
    assert any(item.startswith("diagnostic_threshold:ratio") for item in check.review_triggers)
    assert any(item.startswith("artifact_incomplete:") for item in check.review_triggers)
