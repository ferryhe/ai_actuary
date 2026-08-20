from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
ARCHITECTURE_PATH = REPO_ROOT / "docs" / "architecture.md"
PROJECT_PLAN_PATH = REPO_ROOT / "docs" / "project-plan.md"
OPERATOR_HANDOFF_PATH = REPO_ROOT / "docs" / "operator_handoff.md"
CREDENTIAL_TRANSPORT_ADR_PATH = (
    REPO_ROOT
    / "docs"
    / "architecture"
    / "adr-0003-local-capability-credential-transport.md"
)
CONTROL_PLANE_CONTRACT_PATH = REPO_ROOT / "docs" / "contracts" / "control-plane.md"
CAS_REFERENCE_README_PATH = REPO_ROOT / "references" / "upstream" / "cas" / "README.md"
CAS_PROPOSAL_DIR = REPO_ROOT / "references" / "upstream" / "cas" / "Proposal"
CAS_UPSTREAM_PROJECT_PLAN = REPO_ROOT / "references" / "upstream" / "cas" / "docs" / "project-plan.md"
CAS_UPSTREAM_DEVELOPMENT = REPO_ROOT / "references" / "upstream" / "cas" / "docs" / "development.md"
CAS_UPSTREAM_ADR = REPO_ROOT / "references" / "upstream" / "cas" / "docs" / "adr" / "0001-repo-scope.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")



def test_prompt10_handoff_docs_exist() -> None:
    assert README_PATH.exists()
    assert ARCHITECTURE_PATH.exists()
    assert PROJECT_PLAN_PATH.exists()
    assert OPERATOR_HANDOFF_PATH.exists()



def test_readme_covers_operator_entrypoints_review_flow_and_role_split() -> None:
    readme = _read(README_PATH)

    for expected in [
        "Calculation Core",
        "OpenAI Planner",
        "Hermes Workers",
        "scripts/run_governed_case.py",
        "scripts/run_batch_benchmark.py",
        "scripts/replay_case.py",
        "scripts/compare_repeatability.py",
        "scripts/export_run_report.py",
        "review_packet.md",
        "run_manifest.json",
        "operator_handoff.md",
        "Step-by-Step Operating Guide",
        "Human Responsibilities vs Agent Responsibilities",
    ]:
        assert expected in readme


def test_readme_quick_start_uses_the_supported_operator_session_workflow() -> None:
    readme = _read(README_PATH)
    quick_start = readme.split(
        "## Quick Start: Create a Governed Operator Run", 1
    )[1].split("\n---", 1)[0]
    adr = _read(CREDENTIAL_TRANSPORT_ADR_PATH)

    for expected in (
        "Request launcher handoff",
        "Create Governed Run",
        "Create run",
        "Run Queue",
        "Export handoff report",
        "CSRF",
        "Origin",
        "ADR 0003",
    ):
        assert expected in quick_start
    assert "curl -X POST http://127.0.0.1:8000/runs" not in quick_start
    assert "Console GET requests only return the static shell" in adr
    assert "launcher's terminal prompt" in adr


def test_control_plane_contract_has_no_credentialless_enforcement_mode() -> None:
    contract = _read(CONTROL_PLANE_CONTRACT_PATH)
    normalized = " ".join(contract.split())

    assert "capability enforcement is disabled" not in normalized.lower()
    assert (
        "Deployable, embedded, and test callers all configure capability credentials"
        in normalized
    )
    assert "Caller-supplied identity fields may only narrow" in normalized
    assert "omit `source` use `operator-console` semantics" in normalized
    assert "omit `workspace_id` use `default-workspace`" in normalized
    assert "offline/mock identity inputs" not in normalized
    assert "these headers never grant identity or authority" in normalized



def test_architecture_doc_covers_three_layers_artifacts_and_role_split() -> None:
    architecture = _read(ARCHITECTURE_PATH)

    for expected in [
        "CAS Core",
        "OpenAI Planner",
        "Hermes Workers",
        "Artifact Contract",
        "operator handoff",
        "review flow",
        "Replay path",
        "Repeatability path",
        "Human responsibilities",
        "Agent responsibilities",
    ]:
        assert expected in architecture


def test_operator_handoff_doc_covers_export_artifacts_and_boundaries() -> None:
    handoff = _read(OPERATOR_HANDOFF_PATH)

    for expected in [
        "operator_handoff.md",
        "reserve_summary.json",
        "reserve_summary.md",
        "review decisions",
        "deterministic artifacts",
        "do not fabricate",
        "/runs/{run_id}/report-export",
        "scripts/export_run_report.py",
    ]:
        assert expected in handoff



def test_project_plan_doc_lists_completed_remaining_next_steps_and_handoff_steps() -> None:
    project_plan = _read(PROJECT_PLAN_PATH)

    for expected in [
        "Completed",
        "Not Yet Implemented",
        "Next Recommended Steps",
        "Step-by-Step Handoff Guide",
        "Human steps",
        "Agent steps",
        "Prompt 8",
        "Prompt 9",
        "Prompt 10",
    ]:
        assert expected in project_plan



def test_cas_application_materials_removed_from_repo_snapshot() -> None:
    assert not CAS_PROPOSAL_DIR.exists()
    assert not CAS_UPSTREAM_PROJECT_PLAN.exists()
    assert not CAS_UPSTREAM_DEVELOPMENT.exists()
    assert not CAS_UPSTREAM_ADR.exists()

    cas_reference_readme = _read(CAS_REFERENCE_README_PATH)
    assert "Application and submission materials were intentionally removed" in cas_reference_readme
