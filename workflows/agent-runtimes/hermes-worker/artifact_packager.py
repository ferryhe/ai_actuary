"""Minimal artifact packaging helpers for Hermes worker local adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from reserving_workflow.artifacts.storage import resolve_artifact_path, resolve_artifact_root, write_json_artifact
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
    root = resolve_artifact_root(artifact_dir)

    artifact_names: list[str] = []
    for name in [*(required_artifacts or []), *DEFAULT_ARTIFACTS]:
        if name not in artifact_names:
            artifact_names.append(name)

    artifact_paths = {name: str(resolve_artifact_path(root, f"{name}.json")) for name in artifact_names}
    return RunArtifactManifest(
        case_id=case_id,
        run_id=run_id,
        artifact_root=str(root),
        artifact_paths=artifact_paths,
        created_by=created_by,
        metadata=metadata or {},
    )


def write_artifacts(manifest: RunArtifactManifest, artifacts: Mapping[str, Any]) -> RunArtifactManifest:
    artifact_root = resolve_artifact_root(manifest.artifact_root)
    for artifact_name, payload in artifacts.items():
        target_path = manifest.artifact_paths.get(artifact_name)
        if target_path is None:
            target_path = str(resolve_artifact_path(artifact_root, f"{artifact_name}.json"))
            manifest.artifact_paths[artifact_name] = target_path
        write_json_artifact(target_path, payload)

    write_json_artifact(manifest.artifact_paths["run_manifest"], manifest)
    return manifest
