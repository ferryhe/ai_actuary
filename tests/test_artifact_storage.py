from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STORAGE_PATH = REPO_ROOT / "src" / "reserving_workflow" / "artifacts" / "storage.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storage_helpers_write_and_read_artifacts(tmp_path):
    storage = _load_module("artifact_storage", STORAGE_PATH)

    artifact_root = storage.resolve_artifact_root(tmp_path / "artifacts")
    json_path = storage.resolve_artifact_path(artifact_root, "deterministic_result.json")
    markdown_path = storage.resolve_artifact_path(artifact_root, "review_packet.md")

    storage.write_json_artifact(json_path, {"ibnr": 12.0, "status": "ok"})
    storage.write_text_artifact(markdown_path, "# Review Packet\n")

    assert json_path.exists()
    assert markdown_path.exists()
    assert storage.read_json_artifact(json_path)["ibnr"] == 12.0
    assert markdown_path.read_text(encoding="utf-8") == "# Review Packet\n"
