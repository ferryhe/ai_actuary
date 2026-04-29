from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
ARCHITECTURE_PATH = REPO_ROOT / "docs" / "architecture.md"
PROJECT_PLAN_PATH = REPO_ROOT / "docs" / "project-plan.md"
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



def test_readme_covers_operator_entrypoints_review_flow_and_role_split() -> None:
    readme = _read(README_PATH)

    for expected in [
        "CAS Core",
        "OpenAI Planner",
        "Hermes Workers",
        "scripts/run_governed_case.py",
        "scripts/run_batch_benchmark.py",
        "scripts/replay_case.py",
        "scripts/compare_repeatability.py",
        "review_packet.md",
        "run_manifest.json",
        "Step-by-Step Operating Guide",
        "Human Responsibilities vs Agent Responsibilities",
    ]:
        assert expected in readme



def test_architecture_doc_covers_three_layers_artifacts_and_role_split() -> None:
    architecture = _read(ARCHITECTURE_PATH)

    for expected in [
        "CAS Core",
        "OpenAI Planner",
        "Hermes Workers",
        "Artifact Contract",
        "review flow",
        "Replay path",
        "Repeatability path",
        "Human responsibilities",
        "Agent responsibilities",
    ]:
        assert expected in architecture



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
