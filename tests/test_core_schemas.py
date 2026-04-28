from reserving_workflow.schemas import (
    ConstitutionCheckResult,
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    ReviewDecision,
    RunArtifactManifest,
)


def test_core_schema_creation_and_serialization():
    case = ReservingCaseInput(
        case_id="case-001",
        triangles={"paid": [100.0, 120.0]},
        metadata={"lob": "auto"},
        run_config={"mode": "governed"},
    )
    result = DeterministicReserveResult(
        case_id="case-001",
        method="mock",
        reserve_summary={"ibnr": 42.0},
        diagnostics={"ratio": 1.2},
    )
    draft = NarrativeDraft(
        case_id="case-001",
        summary="Reserve looks stable.",
        key_points=["No severe anomaly detected"],
        cited_values={"ibnr": 42.0},
    )
    check = ConstitutionCheckResult(
        case_id="case-001",
        status="pass",
    )
    review = ReviewDecision(
        case_id="case-001",
        status="not_required",
    )
    manifest = RunArtifactManifest(
        case_id="case-001",
        run_id="run-001",
        artifact_paths={"result": "artifacts/result.json"},
    )

    assert case.model_dump()["metadata"]["lob"] == "auto"
    assert result.model_dump()["reserve_summary"]["ibnr"] == 42.0
    assert draft.model_dump()["cited_values"]["ibnr"] == 42.0
    assert check.model_dump()["status"] == "pass"
    assert review.model_dump()["status"] == "not_required"
    assert manifest.model_dump()["artifact_paths"]["result"] == "artifacts/result.json"
