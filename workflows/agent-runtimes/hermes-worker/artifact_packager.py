"""Minimal artifact packaging helpers for Hermes worker local adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from reserving_workflow.schemas import RunArtifactManifest

DEFAULT_ARTIFACTS = (
    "case_input",
    "deterministic_result",
    "narrative_draft",
    "constitution_check",
    "run_manifest",
)


def build_run_artifact_manifest(
    *,
    case_id: str,
    run_id: str,
    artifact_dir: str | Path,
    required_artifacts: list[str] | None = None,
    created_by: str = "local-case-worker",
    metadata: dict[str, Any] | None = None,
) -> RunArtifactManifest:
    root = Path(artifact_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    artifact_names: list[str] = []
    for name in [*(required_artifacts or []), *DEFAULT_ARTIFACTS]:
        if name not in artifact_names:
            artifact_names.append(name)

    artifact_paths = {name: str(root / f"{name}.json") for name in artifact_names}
    return RunArtifactManifest(
        case_id=case_id,
        run_id=run_id,
        artifact_paths=artifact_paths,
        created_by=created_by,
        metadata=metadata or {},
    )


def write_artifacts(manifest: RunArtifactManifest, artifacts: Mapping[str, Any]) -> RunArtifactManifest:
    for artifact_name, payload in artifacts.items():
        target_path = manifest.artifact_paths.get(artifact_name)
        if target_path is None:
            target_path = str(Path(next(iter(manifest.artifact_paths.values()))).resolve().parent / f"{artifact_name}.json")
            manifest.artifact_paths[artifact_name] = target_path
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_to_serializable(payload), indent=2, sort_keys=True), encoding="utf-8")

    run_manifest_path = Path(manifest.artifact_paths["run_manifest"])
    run_manifest_path.write_text(json.dumps(_to_serializable(manifest), indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _to_serializable(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, dict):
        return {str(key): _to_serializable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_to_serializable(item) for item in payload]
    return payload
