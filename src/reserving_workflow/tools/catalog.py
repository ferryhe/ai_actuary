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
            "method": "chainladder",
            "background": True,
        },
        input_schema={
            "type": "object",
            "title": "ChainladderRunConfig",
            "properties": {
                "case_id": {
                    "type": "string",
                    "description": "Logical case identifier used for the operator run.",
                },
                "sample_name": {
                    "type": "string",
                    "default": "RAA",
                    "description": "chainladder sample name passed through to the worker case payload.",
                },
                "method": {
                    "type": "string",
                    "const": "chainladder",
                    "default": "chainladder",
                    "description": "Existing deterministic method identifier preserved for dispatch.",
                },
                "review_threshold_origin_count": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional review threshold override for intentional escalation tests.",
                },
                "background": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether the API should accept and run the case in background mode.",
                },
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
    )
