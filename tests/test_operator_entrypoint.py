from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "reserving_workflow" / "operator_entrypoint.py"
SCRIPT_PATH = ROOT / "scripts" / "run_governed_case.py"


class FakeWorkerTask:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeTaskContractsModule:
    WorkerTask = FakeWorkerTask


class FakeRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        return {
            "stage": "collect",
            "route": {"mode": "governed"},
            "worker_result": {"status": "completed"},
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "completed",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 1.0},
                "review_reasons": [],
                "artifact_manifest_path": str(Path(task.inputs["artifact_dir"]) / "run_manifest.json"),
                "narrative_summary": "ok",
            },
            "prompt": user_prompt or "default-prompt",
        }


class FakeOperatorModule:
    @staticmethod
    def main(argv=None):
        return {
            "ok": True,
            "argv": argv or [],
        }


def _load_module():
    spec = importlib.util.spec_from_file_location("operator_entrypoint", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_operator_task_creates_run_case_task(tmp_path):
    module = _load_module()

    task = module.build_operator_task(
        case_id="operator-case",
        artifact_dir=tmp_path,
        objective="Operator flow",
        sample_name="RAA",
        task_contracts_module=FakeTaskContractsModule,
    )

    assert task.task_kind == "run_case"
    assert task.case_ref == "operator-case"
    assert task.inputs["case_payload"]["metadata"]["chainladder_sample"] == "RAA"
    assert "run_manifest" in task.inputs["case_payload"]["run_config"]["required_artifacts"]



def test_run_operator_flow_returns_governed_result(tmp_path):
    module = _load_module()

    result = module.run_operator_flow(
        case_id="operator-case",
        artifact_dir=tmp_path,
        objective="Operator flow",
        runner_module=FakeRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
        user_prompt="use tool",
    )

    assert result["route"]["mode"] == "governed"
    assert result["final_output"]["case_id"] == "operator-case"
    assert result["final_output"]["deterministic_method"] == "chainladder"



def test_run_governed_case_script_emits_json(tmp_path):
    helper = tmp_path / "invoke_operator_script.py"
    helper.write_text(
        "import importlib.util, json, types\n"
        f"script_spec=importlib.util.spec_from_file_location('run_governed_case', {str(SCRIPT_PATH)!r})\n"
        "script=importlib.util.module_from_spec(script_spec)\n"
        "script_spec.loader.exec_module(script)\n"
        "script._load_operator_module=lambda: types.SimpleNamespace(main=lambda argv=None: {'ok': True, 'argv': argv or []})\n"
        "script.main(['--case-id','cli-case','--artifact-dir','./tmp/test-cli'])\n"
    )

    proc = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert "--case-id" in payload["argv"]
