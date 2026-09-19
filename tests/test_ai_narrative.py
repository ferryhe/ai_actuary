from __future__ import annotations

import os

import pytest

from reserving_workflow import ai_narrative
from reserving_workflow.ai_narrative import (
    NarrativeDraftWriter,
    draft_narrative_dict,
    evidence_numbers,
    narrative_llm_settings,
    narrative_writer_from_env,
    numbers_supported,
)
from reserving_workflow.narrative import build_narrative_draft, build_narrative_draft_with_meta
from reserving_workflow.schemas import DeterministicReserveResult, ReservingCaseInput

ENV_VARS = (
    "AI_ACTUARY_NARRATIVE_ENABLED",
    "AI_ACTUARY_NARRATIVE_MODEL",
    "AI_ACTUARY_NARRATIVE_BASE_URL",
    "AI_ACTUARY_NARRATIVE_API_KEY",
    "AI_ACTUARY_NARRATIVE_MAX_TOKENS",
    "AI_ACTUARY_NARRATIVE_TIMEOUT_SECONDS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return monkeypatch


def _case_input() -> ReservingCaseInput:
    return ReservingCaseInput(case_id="narrative-case", triangles={"paid": [1.0, 2.0]})


def _deterministic_result() -> DeterministicReserveResult:
    return DeterministicReserveResult(
        case_id="narrative-case",
        method="chainladder",
        reserve_summary={"latest_diagonal": 100.0, "ultimate": 250.0, "ibnr": 150.0},
        diagnostics={"origin_count": 7, "development_count": 4},
    )


def _writer(**overrides) -> NarrativeDraftWriter:
    settings = {
        "enabled": True,
        "model": "test-model",
        "base_url": None,
        "api_key": "test-key",
        "max_tokens": 800,
        "timeout": 5.0,
    }
    settings.update(overrides)
    return NarrativeDraftWriter(settings)


def test_narrative_slot_is_enabled_by_default(clean_env):
    settings = narrative_llm_settings()
    assert settings["enabled"] is True
    assert narrative_writer_from_env().enabled is True


def test_narrative_slot_can_be_disabled(clean_env):
    clean_env.setenv("AI_ACTUARY_NARRATIVE_ENABLED", "0")
    settings = narrative_llm_settings()
    assert settings["enabled"] is False
    assert narrative_writer_from_env().enabled is False


def test_narrative_slot_reads_provider_settings(clean_env):
    clean_env.setenv("AI_ACTUARY_NARRATIVE_ENABLED", "1")
    clean_env.setenv("AI_ACTUARY_NARRATIVE_MODEL", "deepseek-flash")
    clean_env.setenv("AI_ACTUARY_NARRATIVE_BASE_URL", "https://api.deepseek.com/v1")
    clean_env.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    clean_env.setenv("AI_ACTUARY_NARRATIVE_MAX_TOKENS", "900")

    settings = narrative_llm_settings()
    assert settings["enabled"] is True
    assert settings["model"] == "deepseek-flash"
    assert settings["max_tokens"] == 900
    # one `.env` key can serve planner, reviewer, and narrative
    assert settings["api_key"] == "deepseek-key"


def test_disabled_writer_falls_back_to_template():
    writer = _writer(enabled=False)
    result = writer.draft_text(
        case_id="narrative-case",
        method="chainladder",
        evidence={"reserve_summary": {}},
        fallback_summary="template summary",
        fallback_key_points=["point"],
    )
    assert result["summary"] == "template summary"
    assert result["meta"]["source"] == "template"
    assert result["meta"]["reason"] == "disabled"


def test_missing_api_key_falls_back_to_template():
    writer = _writer(api_key="")
    result = writer.draft_text(
        case_id="narrative-case",
        method="chainladder",
        evidence={},
        fallback_summary="template summary",
    )
    assert result["summary"] == "template summary"
    assert result["meta"]["reason"] == "missing_api_key"


def test_model_error_falls_back_to_template(monkeypatch):
    def _boom(**kwargs):
        return None, "boom: upstream unavailable"

    monkeypatch.setattr(ai_narrative, "chat_json", _boom)
    result = _writer().draft_text(
        case_id="narrative-case",
        method="chainladder",
        evidence={},
        fallback_summary="template summary",
    )
    assert result["summary"] == "template summary"
    assert result["meta"]["source"] == "template"
    assert result["meta"]["reason"] == "model_error"
    assert result["meta"]["error"] == "boom: upstream unavailable"


def test_supported_numbers_pass_and_unsupported_numbers_are_rejected():
    allowed = evidence_numbers({"ultimate": 250.0, "ibnr": 150.0}, case_id="narrative-case")
    assert numbers_supported(["ultimate is 250.0"], allowed) is True
    assert numbers_supported(["ultimate is 9999.0"], allowed) is False
    # years are not treated as claims
    assert numbers_supported(["reviewed in 2026"], allowed) is True
    # case-id digits are allowed
    assert evidence_numbers({}, case_id="case-42") == {0.0, 1.0, 100.0, 42.0}


def test_integer_token_cannot_match_rounded_evidence():
    # 42.5 rounded to "42" is a different claim, so the guard must reject it.
    assert numbers_supported(["ratio is 42"], evidence_numbers({"ratio": 42.5})) is False
    assert numbers_supported(["ratio is 42.5"], evidence_numbers({"ratio": 42.5})) is True
    # integer evidence is still quotable as an integer
    assert numbers_supported(["count is 42"], evidence_numbers({"count": 42.0})) is True


def test_model_draft_is_used_when_numbers_are_supported(monkeypatch):
    captured: dict = {}

    def _fake_chat_json(**kwargs):
        captured.update(kwargs)
        return (
            {
                "summary": "Chainladder reserving produced ultimate 250.0 and ibnr 150.0.",
                "key_points": ["Seven origin periods were developed."],
            },
            None,
        )

    monkeypatch.setattr(ai_narrative, "chat_json", _fake_chat_json)
    result = _writer().draft_text(
        case_id="narrative-case",
        method="chainladder",
        evidence={"reserve_summary": {"ultimate": 250.0, "ibnr": 150.0}, "diagnostics": {"origin_count": 7}},
        fallback_summary="template summary",
        fallback_key_points=["template point"],
    )
    assert result["meta"]["source"] == "model"
    assert result["summary"].startswith("Chainladder reserving")
    assert result["key_points"] == ["Seven origin periods were developed."]
    assert captured["settings"]["model"] == "test-model"


def test_model_draft_is_rejected_when_a_number_is_invented(monkeypatch):
    monkeypatch.setattr(
        ai_narrative,
        "chat_json",
        lambda **kwargs: ({"summary": "IBNR is 9999.0", "key_points": []}, None),
    )
    result = _writer().draft_text(
        case_id="narrative-case",
        method="chainladder",
        evidence={"reserve_summary": {"ibnr": 150.0}},
        fallback_summary="template summary",
    )
    assert result["summary"] == "template summary"
    assert result["meta"]["source"] == "template"
    assert result["meta"]["reason"] == "numeric_guard_rejected"


def test_build_narrative_draft_keeps_cited_values_deterministic(monkeypatch):
    monkeypatch.setattr(
        ai_narrative,
        "chat_json",
        lambda **kwargs: ({"summary": "Rewritten wording only.", "key_points": ["A point."]}, None),
    )
    case_input = _case_input()
    deterministic_result = _deterministic_result()
    draft, meta = build_narrative_draft_with_meta(
        case_input, deterministic_result, narrative_writer=_writer()
    )
    assert meta["source"] == "model"
    assert draft.summary == "Rewritten wording only."
    assert draft.cited_values == deterministic_result.reserve_summary

    plain = build_narrative_draft(case_input, deterministic_result)
    assert plain.summary.startswith("Deterministic chainladder run completed")
    assert plain.cited_values == deterministic_result.reserve_summary


def test_build_narrative_draft_without_writer_stays_templated():
    draft, meta = build_narrative_draft_with_meta(_case_input(), _deterministic_result())
    assert meta["source"] == "template"
    assert "ultimate=250.0" in draft.summary


def test_draft_narrative_dict_annotates_model_source(monkeypatch):
    monkeypatch.setattr(
        ai_narrative,
        "chat_json",
        lambda **kwargs: ({"summary": "Eight grouped results.", "key_points": ["point"]}, None),
    )
    narrative = {
        "case_id": "ae-case",
        "status": "completed",
        "summary": "template summary",
        "key_points": ["template point"],
    }
    drafted = draft_narrative_dict(
        narrative,
        case_id="ae-case",
        method="grouped_actual_to_expected",
        evidence={"diagnostics": {"result_count": 8}},
        writer=_writer(),
    )
    assert drafted["summary"] == "Eight grouped results."
    assert drafted["narrative_source"] == "model"
    assert drafted["narrative_model"] == "test-model"
    assert drafted["status"] == "completed"


def test_draft_narrative_dict_keeps_template_when_disabled():
    narrative = {"case_id": "ae-case", "summary": "template summary", "key_points": ["p"]}
    drafted = draft_narrative_dict(
        narrative,
        case_id="ae-case",
        method="grouped_actual_to_expected",
        evidence={},
        writer=_writer(enabled=False),
    )
    assert drafted == narrative
