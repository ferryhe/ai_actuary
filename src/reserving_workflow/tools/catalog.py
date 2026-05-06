"""Builtin tool catalog and local registry helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolCatalogEntry(BaseModel):
    """Metadata and schema for one operator-visible tool."""

    tool_id: str
    method: str
    title: str
    description: str
    builtin: bool = True
    tags: list[str] = Field(default_factory=list)
    console_defaults: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        payload = self.model_dump()
        payload.pop("input_schema", None)
        return payload


class ToolRegistry:
    """Local in-memory registry for operator-visible tools."""

    def __init__(self, entries: list[ToolCatalogEntry] | None = None):
        sorted_entries = sorted(entries or [], key=lambda item: item.tool_id)
        self._entries: dict[str, ToolCatalogEntry] = {}
        for entry in sorted_entries:
            if entry.tool_id in self._entries:
                raise ValueError(f"Duplicate tool id in registry: {entry.tool_id}")
            self._entries[entry.tool_id] = entry

    def list_tools(self) -> list[ToolCatalogEntry]:
        return list(self._entries.values())

    def list_tool_summaries(self) -> list[dict[str, Any]]:
        return [entry.summary() for entry in self.list_tools()]

    def get_tool(self, tool_id: str) -> ToolCatalogEntry:
        try:
            return self._entries[tool_id]
        except KeyError as exc:
            raise ValueError(f"Tool id not found in registry: {tool_id}") from exc


def build_builtin_tool_registry() -> ToolRegistry:
    return ToolRegistry(entries=[_builtin_chainladder_tool()])


def _builtin_chainladder_tool() -> ToolCatalogEntry:
    return ToolCatalogEntry(
        tool_id="chainladder",
        method="chainladder",
        title="Chainladder",
        description="Deterministic chainladder reserving over the existing governed run path.",
        tags=["builtin", "deterministic", "reserving"],
        console_defaults={
            "sample_name": "RAA",
            "method_variant": "chainladder",
            "background": True,
        },
        input_schema={
            "type": "object",
            "title": "ChainladderToolInput",
            "properties": {
                "sample_name": {
                    "type": "string",
                    "default": "RAA",
                    "description": "Built-in chainladder sample name. Provide this or triangle_rows, but not both.",
                },
                "triangle_rows": {
                    "type": "array",
                    "description": "Explicit triangle rows. Provide this or sample_name, but not both.",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "origin_column": {
                    "type": "string",
                    "default": "origin",
                    "description": "Column name used for origin periods when triangle_rows is provided.",
                },
                "development_column": {
                    "type": "string",
                    "default": "development",
                    "description": "Column name used for development periods when triangle_rows is provided.",
                },
                "value_column": {
                    "type": "string",
                    "default": "value",
                    "description": "Numeric value column used when triangle_rows is provided.",
                },
                "cumulative": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether triangle_rows are cumulative values.",
                },
                "index_column": {
                    "type": "string",
                    "description": "Optional index column passed through to chainladder Triangle construction.",
                },
                "method_variant": {
                    "type": "string",
                    "const": "chainladder",
                    "default": "chainladder",
                    "description": "Stable deterministic method variant used after tool-backed input normalization.",
                },
                "review_threshold_origin_count": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional review threshold override for intentional escalation tests.",
                },
                "method": {
                    "type": "string",
                    "const": "chainladder",
                    "default": "chainladder",
                    "description": "Legacy input alias accepted for compatibility and normalized into method_variant.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
