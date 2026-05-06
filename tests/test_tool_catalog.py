from __future__ import annotations

import pytest

from reserving_workflow.tools import ToolCatalogEntry, ToolRegistry, build_builtin_tool_registry


def test_builtin_tool_registry_lists_chainladder_summary():
    registry = build_builtin_tool_registry()

    tools = registry.list_tool_summaries()

    assert len(tools) == 1
    assert tools[0]["tool_id"] == "chainladder"
    assert tools[0]["method"] == "chainladder"
    assert "input_schema" not in tools[0]


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
    assert tool.input_schema["required"] == ["case_id"]
    assert tool.input_schema["properties"]["method"]["const"] == "chainladder"
