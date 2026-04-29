from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


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

    storage.write_json_artifact(json_path, {"ibnr": 12.0, "status": "ok", "message": "中文"})
    storage.write_text_artifact(markdown_path, "# Review Packet\n")

    assert json_path.exists()
    assert markdown_path.exists()
    assert storage.read_json_artifact(json_path)["ibnr"] == 12.0
    assert '"message": "中文"' in json_path.read_text(encoding="utf-8")
    assert markdown_path.read_text(encoding="utf-8") == "# Review Packet\n"


def test_resolve_artifact_path_rejects_absolute_and_escape_paths(tmp_path):
    storage = _load_module("artifact_storage_path_guard", STORAGE_PATH)
    artifact_root = storage.resolve_artifact_root(tmp_path / "artifacts")

    with pytest.raises(ValueError):
        storage.resolve_artifact_path(artifact_root, "/tmp/evil.json")

    with pytest.raises(ValueError):
        storage.resolve_artifact_path(artifact_root, "../evil.json")


def test_read_json_artifact_rejects_non_object_payloads(tmp_path):
    storage = _load_module("artifact_storage_non_object", STORAGE_PATH)
    target = tmp_path / "array.json"
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(ValueError):
        storage.read_json_artifact(target)
