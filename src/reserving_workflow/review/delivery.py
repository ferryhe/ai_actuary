"""Review-packet delivery adapters kept outside planner/core logic."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def deliver_review_packet(packet: dict[str, Any], *, destination_dir: str | Path) -> dict[str, Any]:
    base_dir = Path(destination_dir).expanduser().resolve()
    case_id = _safe_path_component(packet.get("case_id"), fallback="unknown-case", field_name="case_id")
    run_id = _safe_path_component(packet.get("run_id"), fallback="unknown-run", field_name="run_id")
    target_dir = (base_dir / case_id / run_id).resolve()
    try:
        target_dir.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError(f"Resolved delivery path escapes destination_dir: {target_dir}") from exc
    target_dir.mkdir(parents=True, exist_ok=True)

    packet_paths = packet.get("packet_paths", {}) or {}
    source_json = _resolve_packet_file(packet_paths, "json")
    source_markdown = _resolve_packet_file(packet_paths, "markdown")

    delivered_json = target_dir / source_json.name
    delivered_markdown = target_dir / source_markdown.name
    shutil.copy2(source_json, delivered_json)
    shutil.copy2(source_markdown, delivered_markdown)

    return {
        "destination": "local_outbox",
        "case_id": packet.get("case_id"),
        "run_id": packet.get("run_id"),
        "destination_dir": str(target_dir),
        "delivered_paths": {
            "json": str(delivered_json),
            "markdown": str(delivered_markdown),
        },
    }


def _safe_path_component(value: Any, *, fallback: str, field_name: str) -> str:
    component = str(value or fallback)
    candidate = Path(component)
    if component in {"", ".", ".."}:
        raise ValueError(f"Invalid {field_name}: {component!r}")
    if "/" in component or "\\" in component:
        raise ValueError(f"Invalid {field_name}: {component!r}")
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise ValueError(f"Invalid {field_name}: {component!r}")
    return component


def _resolve_packet_file(packet_paths: dict[str, Any], key: str) -> Path:
    raw_path = packet_paths.get(key)
    if not raw_path:
        raise ValueError(f"Review packet is missing packet_paths.{key}")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Review packet source file does not exist: {path}")
    return path
