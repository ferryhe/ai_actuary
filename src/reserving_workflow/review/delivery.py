"""Review-packet delivery adapters kept outside planner/core logic."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def deliver_review_packet(packet: dict[str, Any], *, destination_dir: str | Path) -> dict[str, Any]:
    base_dir = Path(destination_dir).expanduser().resolve()
    target_dir = base_dir / str(packet.get("case_id") or "unknown-case") / str(packet.get("run_id") or "unknown-run")
    target_dir.mkdir(parents=True, exist_ok=True)

    packet_paths = packet.get("packet_paths", {}) or {}
    source_json = Path(packet_paths["json"]).expanduser().resolve()
    source_markdown = Path(packet_paths["markdown"]).expanduser().resolve()

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
