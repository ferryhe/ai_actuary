from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_WORKER_DIR = REPO_ROOT / "workflows" / "agent-runtimes" / "hermes-worker"
ARTIFACT_PACKAGER_PATH = HERMES_WORKER_DIR / "artifact_packager.py"
REPLAY_PATH = REPO_ROOT / "src" / "reserving_workflow" / "artifacts" / "replay.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _make_run_case_task(*, task_id: str, case_id: str, artifact_dir: Path, review_thresholds: dict | None = None):
    task_contracts = _load_module("replay_task_contracts", HERMES_WORKER_DIR / "task_contracts.py")
    return task_contracts.WorkerTask(
        task_id=task_id,
        task_kind="run_case",
        case_ref=case_id,
        objective="Replay utility test",
        inputs={
            "artifact_dir": str(artifact_dir),
            "case_payload": {
                "case_id": case_id,
                "metadata": {"chainladder_sample": "RAA"},
                "run_config": {
                    "method": "chainladder",
                    "required_artifacts": [
                        "case_input",
                        "deterministic_result",
                        "narrative_draft",
                        "constitution_check",
                        "run_manifest",
                    ],
                    **({"review_thresholds": review_thresholds} if review_thresholds else {}),
                },
            },
        },
    )



def test_build_run_artifact_manifest_records_artifact_root(tmp_path):
    artifact_packager = _load_module("artifact_packager_replay", ARTIFACT_PACKAGER_PATH)

    manifest = artifact_packager.build_run_artifact_manifest(
        case_id="manifest-case",
        run_id="run-001",
        artifact_dir=tmp_path,
        required_artifacts=["deterministic_result"],
    )

    assert manifest.artifact_root == str(tmp_path.resolve())
    assert manifest.artifact_paths["run_manifest"].endswith("run_manifest.json")



def test_replay_case_from_manifest_matches_saved_outputs(tmp_path):
    case_worker = _load_module("replay_case_worker", HERMES_WORKER_DIR / "case_worker.py")
    replay_module = _load_module("replay_module", REPLAY_PATH)

    task = _make_run_case_task(
        task_id="replay-case-001",
        case_id="replay-case",
        artifact_dir=tmp_path / "replay-case",
    )
    result = case_worker.run_case_worker(task)

    replay = replay_module.replay_case_from_manifest(result.artifact_paths["run_manifest"])

    assert replay["case_id"] == "replay-case"
    assert replay["saved_summary"]["ibnr"] == replay["replayed_summary"]["ibnr"]
    assert replay["matches_saved_result"] is True
    assert replay["saved_constitution_status"] == "pass"



def test_compare_repeatability_summarizes_multiple_manifests(tmp_path):
    case_worker = _load_module("repeatability_case_worker", HERMES_WORKER_DIR / "case_worker.py")
    replay_module = _load_module("repeatability_module", REPLAY_PATH)

    run_one = case_worker.run_case_worker(
        _make_run_case_task(
            task_id="repeat-run-001",
            case_id="repeat-case",
            artifact_dir=tmp_path / "repeat-case-one",
        )
    )
    run_two = case_worker.run_case_worker(
        _make_run_case_task(
            task_id="repeat-run-002",
            case_id="repeat-case",
            artifact_dir=tmp_path / "repeat-case-two",
        )
    )

    summary = replay_module.compare_repeatability(
        [run_one.artifact_paths["run_manifest"], run_two.artifact_paths["run_manifest"]]
    )

    assert summary["case_id"] == "repeat-case"
    assert summary["run_count"] == 2
    assert summary["all_statuses"] == ["completed", "completed"]
    assert summary["stable_ibnr"] is True
    assert summary["ibnr_values"] == [52135.228261210155, 52135.228261210155]
    assert len(summary["runs"]) == 2



def test_replay_case_from_manifest_resolves_relative_artifact_paths(tmp_path):
    case_worker = _load_module("replay_relative_case_worker", HERMES_WORKER_DIR / "case_worker.py")

    task = _make_run_case_task(
        task_id="replay-relative-001",
        case_id="replay-relative-case",
        artifact_dir=tmp_path / "replay-relative-case",
    )
    result = case_worker.run_case_worker(task)
    manifest_path = Path(result.artifact_paths["run_manifest"])
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_root = Path(manifest_payload["artifact_root"])
    manifest_payload["artifact_paths"] = {
        name: str(Path(path).resolve().relative_to(artifact_root)) for name, path in manifest_payload["artifact_paths"].items()
    }
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "replay_case.py"), "--manifest-path", str(manifest_path)],
        capture_output=True,
        text=True,
        check=False,
        cwd="/tmp",
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["case_id"] == "replay-relative-case"
    assert payload["matches_saved_result"] is True



def test_compare_repeatability_marks_missing_ibnr_as_unstable(tmp_path):
    case_worker = _load_module("repeatability_missing_ibnr_worker", HERMES_WORKER_DIR / "case_worker.py")
    replay_module = _load_module("repeatability_missing_ibnr_module", REPLAY_PATH)

    run_one = case_worker.run_case_worker(
        _make_run_case_task(
            task_id="repeat-missing-001",
            case_id="repeat-missing-case",
            artifact_dir=tmp_path / "repeat-missing-one",
        )
    )
    run_two = case_worker.run_case_worker(
        _make_run_case_task(
            task_id="repeat-missing-002",
            case_id="repeat-missing-case",
            artifact_dir=tmp_path / "repeat-missing-two",
        )
    )

    deterministic_path = Path(run_two.artifact_paths["deterministic_result"])
    deterministic_payload = json.loads(deterministic_path.read_text(encoding="utf-8"))
    deterministic_payload["reserve_summary"].pop("ibnr", None)
    deterministic_path.write_text(json.dumps(deterministic_payload, indent=2, sort_keys=True), encoding="utf-8")

    summary = replay_module.compare_repeatability(
        [run_one.artifact_paths["run_manifest"], run_two.artifact_paths["run_manifest"]]
    )

    assert summary["stable_ibnr"] is False
    assert summary["ibnr_values"] == [52135.228261210155, None]



def test_replay_case_script_emits_json(tmp_path):
    case_worker = _load_module("replay_case_worker_cli", HERMES_WORKER_DIR / "case_worker.py")
    script_path = REPO_ROOT / "scripts" / "replay_case.py"

    task = _make_run_case_task(
        task_id="replay-script-001",
        case_id="replay-script-case",
        artifact_dir=tmp_path / "replay-script-case",
    )
    result = case_worker.run_case_worker(task)

    proc = subprocess.run(
        [sys.executable, str(script_path), "--manifest-path", result.artifact_paths["run_manifest"]],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["case_id"] == "replay-script-case"
    assert payload["matches_saved_result"] is True


def test_compare_repeatability_script_emits_json(tmp_path):
    case_worker = _load_module("repeatability_case_worker_cli", HERMES_WORKER_DIR / "case_worker.py")
    script_path = REPO_ROOT / "scripts" / "compare_repeatability.py"

    run_one = case_worker.run_case_worker(
        _make_run_case_task(
            task_id="repeat-script-001",
            case_id="repeat-script-case",
            artifact_dir=tmp_path / "repeat-script-one",
        )
    )
    run_two = case_worker.run_case_worker(
        _make_run_case_task(
            task_id="repeat-script-002",
            case_id="repeat-script-case",
            artifact_dir=tmp_path / "repeat-script-two",
        )
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--manifest-path",
            run_one.artifact_paths["run_manifest"],
            "--manifest-path",
            run_two.artifact_paths["run_manifest"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["case_id"] == "repeat-script-case"
    assert payload["run_count"] == 2
    assert payload["stable_ibnr"] is True
