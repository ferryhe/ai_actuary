from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "src" / "reserving_workflow" / "runtime" / "run_registry.py"
OPERATOR_PATH = ROOT / "src" / "reserving_workflow" / "operator_entrypoint.py"
LIST_RUNS_SCRIPT = ROOT / "scripts" / "list_runs.py"
SHOW_RUN_SCRIPT = ROOT / "scripts" / "show_run.py"
RERUN_CASE_SCRIPT = ROOT / "scripts" / "rerun_case.py"


class FakeWorkerTask:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeTaskContractsModule:
    WorkerTask = FakeWorkerTask


class FakeRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        return {
            "route": {"mode": "governed"},
            "worker_result": {
                "status": "completed",
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "summary": "worker complete",
                "artifact_paths": {"run_manifest": str(Path(task.inputs["artifact_dir"]) / "run_manifest.json")},
                "metrics": {},
                "review_reasons": [],
                "errors": [],
                "worker_metadata": {"adapter": "local-callable"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "completed",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 1.0},
                "review_reasons": [],
                "artifact_manifest_path": str(Path(task.inputs["artifact_dir"]) / "run_manifest.json"),
                "narrative_summary": "ok",
            },
        }


class FakeOperatorModule:
    @staticmethod
    def rerun_from_registry(run_id, *, registry_path, artifact_dir=None, review_delivery_dir=None):
        return {
            "ok": True,
            "status": "completed",
            "case_id": "registry-case",
            "run_id": run_id,
            "registry_path": registry_path,
            "artifact_dir": artifact_dir,
            "review_delivery_dir": review_delivery_dir,
        }


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_registry_records_status_history(tmp_path):
    registry = _load_module("run_registry", REGISTRY_PATH)
    registry_path = tmp_path / "run-registry.json"

    registry.record_run_event(
        registry_path=registry_path,
        task_id="operator-case-a",
        case_id="case-a",
        run_id="operator-case-a-local",
        status="queued",
        artifact_root=str(tmp_path / "case-a"),
        summary="queued",
        operator_params={"case_id": "case-a"},
    )
    registry.record_run_event(
        registry_path=registry_path,
        task_id="operator-case-a",
        case_id="case-a",
        run_id="operator-case-a-local",
        status="running",
        artifact_root=str(tmp_path / "case-a"),
        summary="running",
        operator_params={"case_id": "case-a"},
    )
    registry.record_run_event(
        registry_path=registry_path,
        task_id="operator-case-a",
        case_id="case-a",
        run_id="operator-case-a-local",
        status="completed",
        artifact_root=str(tmp_path / "case-a"),
        summary="done",
        operator_params={"case_id": "case-a"},
    )

    entry = registry.get_run(registry_path, "operator-case-a-local")

    assert entry["status"] == "completed"
    assert [item["status"] for item in entry["status_history"]] == ["queued", "running", "completed"]
    assert entry["operator_params"]["case_id"] == "case-a"


def test_run_registry_serializes_review_delivery_on_creation(tmp_path):
    registry = _load_module("run_registry_serialization", REGISTRY_PATH)
    registry_path = tmp_path / "run-registry.json"
    delivery_path = tmp_path / "outbox" / "packet.json"

    entry = registry.record_run_event(
        registry_path=registry_path,
        task_id="operator-case-serializable",
        case_id="case-serializable",
        run_id="operator-case-serializable-local",
        status="completed",
        artifact_root=str(tmp_path / "case-serializable"),
        summary="done",
        review_delivery={"json": delivery_path},
    )

    assert entry["review_delivery"]["json"] == str(delivery_path)
    persisted = json.loads(registry_path.read_text(encoding="utf-8"))
    assert persisted["runs"][0]["review_delivery"]["json"] == str(delivery_path)


def test_run_registry_preserves_case_id_when_update_event_omits_it(tmp_path):
    registry = _load_module("run_registry_case_id", REGISTRY_PATH)
    registry_path = tmp_path / "run-registry.json"

    registry.record_run_event(
        registry_path=registry_path,
        task_id="operator-case-preserve",
        case_id="case-preserve",
        run_id="operator-case-preserve-local",
        status="queued",
        artifact_root=str(tmp_path / "case-preserve"),
        summary="queued",
    )
    registry.record_run_event(
        registry_path=registry_path,
        task_id="operator-case-preserve",
        case_id=None,
        run_id="operator-case-preserve-local",
        status="running",
        artifact_root=str(tmp_path / "case-preserve"),
        summary="running",
    )

    entry = registry.get_run(registry_path, "operator-case-preserve-local")
    assert entry["case_id"] == "case-preserve"


def test_run_operator_flow_writes_completed_entry_to_registry(tmp_path):
    operator = _load_module("operator_registry", OPERATOR_PATH)
    registry_path = tmp_path / "run-registry.json"

    result = operator.run_operator_flow(
        case_id="registry-case",
        artifact_dir=tmp_path / "artifacts",
        objective="registry test",
        runner_module=FakeRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
        registry_path=registry_path,
    )

    registry = _load_module("run_registry_for_operator", REGISTRY_PATH)
    entry = registry.get_run(registry_path, result["run_id"])

    assert result["status"] == "completed"
    assert entry["status"] == "completed"
    assert entry["case_id"] == "registry-case"
    assert entry["artifact_root"] == str((tmp_path / "artifacts").resolve())
    assert [item["status"] for item in entry["status_history"]] == ["queued", "running", "completed"]


class ExplodingRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        raise RuntimeError("runner unavailable")


def test_run_operator_flow_writes_failed_entry_to_registry(tmp_path):
    operator = _load_module("operator_registry_failure", OPERATOR_PATH)
    registry_path = tmp_path / "run-registry.json"

    result = operator.run_operator_flow(
        case_id="broken-registry-case",
        artifact_dir=tmp_path / "artifacts",
        objective="registry failure test",
        runner_module=ExplodingRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
        registry_path=registry_path,
    )

    registry = _load_module("run_registry_failure_lookup", REGISTRY_PATH)
    entry = registry.get_run(registry_path, result["run_id"])

    assert result["status"] == "failed"
    assert entry["status"] == "failed"
    assert entry["error_category"] == "planner_runtime"
    assert [item["status"] for item in entry["status_history"]] == ["queued", "running", "failed"]


def test_repeated_case_runs_get_distinct_registry_entries(tmp_path):
    operator = _load_module("operator_registry_distinct_runs", OPERATOR_PATH)
    registry_path = tmp_path / "run-registry.json"

    first = operator.run_operator_flow(
        case_id="same-case",
        artifact_dir=tmp_path / "artifacts-1",
        objective="first run",
        runner_module=FakeRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
        registry_path=registry_path,
    )
    second = operator.run_operator_flow(
        case_id="same-case",
        artifact_dir=tmp_path / "artifacts-2",
        objective="second run",
        runner_module=FakeRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
        registry_path=registry_path,
    )

    registry = _load_module("run_registry_distinct_runs_lookup", REGISTRY_PATH)
    runs = registry.list_runs(registry_path)

    assert first["run_id"] != second["run_id"]
    assert len(runs) == 2
    assert {item["run_id"] for item in runs} == {first["run_id"], second["run_id"]}


def test_list_and_show_run_scripts_emit_json(tmp_path):
    registry = _load_module("run_registry_scripts", REGISTRY_PATH)
    registry_path = tmp_path / "run-registry.json"
    registry.record_run_event(
        registry_path=registry_path,
        task_id="operator-case-b",
        case_id="case-b",
        run_id="operator-case-b-local",
        status="needs_review",
        artifact_root=str(tmp_path / "case-b"),
        summary="needs review",
        operator_params={"case_id": "case-b"},
    )

    list_proc = subprocess.run(
        [sys.executable, str(LIST_RUNS_SCRIPT), "--registry-path", str(registry_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    show_proc = subprocess.run(
        [sys.executable, str(SHOW_RUN_SCRIPT), "--registry-path", str(registry_path), "--run-id", "operator-case-b-local"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert list_proc.returncode == 0
    assert show_proc.returncode == 0
    list_payload = json.loads(list_proc.stdout)
    show_payload = json.loads(show_proc.stdout)
    assert list_payload["run_count"] == 1
    assert list_payload["runs"][0]["run_id"] == "operator-case-b-local"
    assert show_payload["run_id"] == "operator-case-b-local"
    assert show_payload["status"] == "needs_review"


def test_rerun_case_script_emits_json(tmp_path):
    helper = tmp_path / "invoke_rerun_script.py"
    helper.write_text(
        "import importlib.util, types\n"
        f"spec=importlib.util.spec_from_file_location('rerun_case', {str(RERUN_CASE_SCRIPT)!r})\n"
        "mod=importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod._load_operator_module=lambda: types.SimpleNamespace(rerun_from_registry=lambda run_id, registry_path, artifact_dir=None, review_delivery_dir=None: {'ok': True, 'status': 'completed', 'run_id': run_id, 'registry_path': registry_path, 'artifact_dir': artifact_dir, 'review_delivery_dir': review_delivery_dir})\n"
        "mod.main(['--registry-path','./tmp/run-registry.json','--run-id','operator-case-c-local','--artifact-dir','./tmp/rerun'])\n"
    )

    proc = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["run_id"] == "operator-case-c-local"
    assert payload["artifact_dir"] == "./tmp/rerun"
