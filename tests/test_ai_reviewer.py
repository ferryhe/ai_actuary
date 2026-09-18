"""Regression tests for the AI reviewer slot and the model-tool artifact layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from reserving_workflow.model_tools import runner
from reserving_workflow.review import ai_reviewer

REVIEW_ENV_VARS = (
    "AI_ACTUARY_AI_REVIEW_ENABLED",
    "AI_ACTUARY_REVIEW_MODEL",
    "AI_ACTUARY_REVIEW_BASE_URL",
    "AI_ACTUARY_REVIEW_API_KEY",
    "AI_ACTUARY_REVIEW_MAX_TOKENS",
    "AI_ACTUARY_REVIEW_TIMEOUT_SECONDS",
)

# Shape deviations a provider can return that are not "a bad number" but simply
# a reply that does not match the contract.
MALFORMED_REPLIES = {
    "severity_out_of_enum": {
        "summary": "looks risky",
        "focus_points": [{"title": "t", "severity": "critical"}],
    },
    "focus_points_is_dict": {"summary": "looks risky", "focus_points": {"title": "t"}},
    "focus_point_is_scalar": {"summary": "looks risky", "focus_points": ["nope"]},
    "evidence_is_string": {
        "summary": "looks risky",
        "focus_points": [{"title": "t", "evidence": "e"}],
    },
}


@pytest.fixture
def review_enabled(monkeypatch):
    for name in REVIEW_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_ACTUARY_AI_REVIEW_ENABLED", "1")
    monkeypatch.setenv("AI_ACTUARY_REVIEW_API_KEY", "test-key")


@pytest.fixture
def offline_models(monkeypatch):
    """Keep the suite off the network: no real call from either model slot."""

    monkeypatch.setenv("AI_ACTUARY_NARRATIVE_ENABLED", "0")
    monkeypatch.setenv("AI_ACTUARY_AI_REVIEW_ENABLED", "0")


def test_valid_model_reply_stays_ok(tmp_path, review_enabled, monkeypatch):
    monkeypatch.setattr(
        ai_reviewer,
        "chat_json",
        lambda *args, **kwargs: (
            {
                "summary": "check the tail",
                "focus_points": [
                    {"title": "t", "severity": "high", "rationale": "r", "evidence": ["e"]}
                ],
                "suggested_actions": ["verify"],
            },
            None,
        ),
    )

    result = ai_reviewer.generate_ai_review({"case_id": "c1"}, output_dir=tmp_path)

    assert result["status"] == "ok"
    assert result["focus_points"][0]["severity"] == "high"


@pytest.mark.parametrize(
    "payload", list(MALFORMED_REPLIES.values()), ids=list(MALFORMED_REPLIES)
)
def test_malformed_model_reply_degrades_to_failed(tmp_path, review_enabled, monkeypatch, payload):
    monkeypatch.setattr(ai_reviewer, "chat_json", lambda *args, **kwargs: (payload, None))

    result = ai_reviewer.generate_ai_review({"case_id": "c1"}, output_dir=tmp_path)

    assert result["status"] == "failed"
    assert "malformed_model_reply" in result["error"]


@pytest.mark.parametrize(
    "payload", list(MALFORMED_REPLIES.values()), ids=list(MALFORMED_REPLIES)
)
def test_review_packet_survives_malformed_model_reply(
    tmp_path, review_enabled, monkeypatch, payload
):
    """A malformed advisory reply must not fail the run (model-tools path)."""

    monkeypatch.setattr(ai_reviewer, "chat_json", lambda *args, **kwargs: (payload, None))

    packet = runner.build_experience_study_review_packet(
        case_id="c1",
        run_id="r1",
        constitution_check={"case_id": "c1", "status": "review_required", "review_triggers": ["t"]},
        deterministic_result={"case_id": "c1", "diagnostics": {"max_ae_ratio": 9.0}},
        narrative_draft={"case_id": "c1", "summary": "s", "key_points": []},
        run_manifest={"case_id": "c1", "run_id": "r1", "artifact_paths": {}},
        artifact_root=tmp_path,
        summary="case summary",
    )

    assert packet["ai_review"]["status"] == "failed"
    assert "error" in packet["ai_review"]


def test_model_tool_artifact_dir_is_not_nested_twice(tmp_path, offline_models):
    """`artifact_dir` is already per-run; the run id must not be appended twice."""

    run_dir = tmp_path / "c1" / "run-1"

    result = runner.run_minimax_experience_study(
        case_id="c1",
        inputs={},
        artifact_dir=str(run_dir),
        registry_path=str(tmp_path / "registry.jsonl"),
        run_id="run-1",
        created_by="tester",
        operator_id="tester",
        workspace_id="test",
    )

    manifest_path = Path(result["final_output"]["artifact_manifest_path"])
    assert manifest_path.exists()
    assert manifest_path.parent.resolve() == run_dir.resolve()
