from __future__ import annotations

import pytest

from reserving_workflow.tools import ToolCatalogEntry, ToolRegistry, build_builtin_tool_registry


def test_builtin_tool_registry_lists_chainladder_and_minimax_summaries():
    registry = build_builtin_tool_registry()

    tools = registry.list_tool_summaries()

    assert len(tools) == 2
    assert tools[0]["tool_id"] == "chainladder"
    assert tools[0]["method"] == "chainladder"
    assert "input_schema" not in tools[0]
    assert tools[1]["tool_id"] == "minimax_experience_study_tool"
    assert tools[1]["method"] == "minimax_experience_study_tool"


def test_tool_registry_rejects_duplicate_tool_ids():
    first = ToolCatalogEntry(
        tool_id="duplicate",
        method="chainladder",
        title="First",
        description="First duplicate entry.",
    )
    second = ToolCatalogEntry(
        tool_id="duplicate",
        method="chainladder",
        title="Second",
        description="Second duplicate entry.",
    )

    with pytest.raises(ValueError, match="Duplicate tool id"):
        ToolRegistry(entries=[first, second])


def test_builtin_tool_registry_returns_chainladder_schema():
    registry = build_builtin_tool_registry()

    tool = registry.get_tool("chainladder")

    assert tool.console_defaults["sample_name"] == "RAA"
    assert tool.console_defaults["method_variant"] == "chainladder"
    assert tool.input_schema["required"] == []
    assert "triangle_rows" in tool.input_schema["properties"]
    assert tool.input_schema["properties"]["method_variant"]["const"] == "chainladder"
    assert tool.input_schema["properties"]["method"]["const"] == "chainladder"


def test_builtin_tool_registry_returns_minimax_experience_schema():
    registry = build_builtin_tool_registry()

    tool = registry.get_tool("minimax_experience_study_tool")

    assert tool.console_defaults["sample_name"] == "ae_small"
    assert tool.console_defaults["dimensions"] == ["product"]
    assert "rows" in tool.input_schema["properties"]
    assert tool.tags == ["builtin", "deterministic", "experience-study", "minimax", "model-comparison"]
