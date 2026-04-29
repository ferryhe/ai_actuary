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
            "worker_result": {
                "status": "completed",
                "case_id": task.case_ref,
                "run_id": f"operator-{task.case_ref}-local",
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

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["case_id"] == "operator-case"
    assert result["route"]["mode"] == "governed"
    assert result["worker_result"]["status"] == "completed"
    assert result["run_id"] == "operator-operator-case-local"
    assert result["final_output"]["case_id"] == "operator-case"
    assert result["final_output"]["deterministic_method"] == "chainladder"
    assert "review_packet" not in result


class MissingRunIdRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        return {
            "route": {"mode": "governed"},
            "worker_result": {
                "status": "completed",
                "case_id": task.case_ref,
                "summary": "worker complete",
                "artifact_paths": {},
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
                "artifact_manifest_path": None,
                "narrative_summary": "ok",
            },
        }


class SpuriousReviewPacketRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        return {
            "route": {"mode": "governed"},
            "worker_result": {
                "status": "completed",
                "case_id": task.case_ref,
                "run_id": f"operator-{task.case_ref}-local",
                "summary": "worker complete",
                "artifact_paths": {},
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
                "artifact_manifest_path": None,
                "narrative_summary": "ok",
            },
            "review_packet": {"status": "review_required"},
        }


def test_run_operator_flow_falls_back_to_deterministic_run_id_when_runner_omits_it(tmp_path):
    module = _load_module()

    result = module.run_operator_flow(
        case_id="missing-run-id",
        artifact_dir=tmp_path,
        objective="Operator flow",
        runner_module=MissingRunIdRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
    )

    assert result["status"] == "completed"
    assert result["run_id"] == "operator-missing-run-id-local"


def test_run_operator_flow_drops_review_packet_when_status_is_not_review(tmp_path):
    module = _load_module()

    result = module.run_operator_flow(
        case_id="spurious-review-packet",
        artifact_dir=tmp_path,
        objective="Operator flow",
        runner_module=SpuriousReviewPacketRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
    )

    assert result["status"] == "completed"
    assert "review_packet" not in result


class ExplodingRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        raise RuntimeError("runner unavailable")


class ReviewDeliveryRunnerModule:
    @staticmethod
    def run_openai_governed_workflow(task, *, user_prompt=None):
        packet_dir = Path(task.inputs["artifact_dir"]) / "packet"
        packet_dir.mkdir(parents=True, exist_ok=True)
        packet_json = packet_dir / "review_packet.json"
        packet_markdown = packet_dir / "review_packet.md"
        packet_json.write_text('{"status":"review_required"}', encoding="utf-8")
        packet_markdown.write_text('# Review Packet\n', encoding="utf-8")
        return {
            "route": {"mode": "governed"},
            "worker_result": {
                "status": "needs_review",
                "case_id": task.case_ref,
                "run_id": f"operator-{task.case_ref}-local",
                "summary": "review required",
                "artifact_paths": {"run_manifest": str(Path(task.inputs["artifact_dir"]) / "run_manifest.json")},
                "metrics": {},
                "review_reasons": ["threshold breach"],
                "errors": [],
                "worker_metadata": {"adapter": "local-callable"},
            },
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "needs_review",
                "deterministic_method": "chainladder",
                "cited_values": {"ibnr": 1.0},
                "review_reasons": ["threshold breach"],
                "artifact_manifest_path": str(Path(task.inputs["artifact_dir"]) / "run_manifest.json"),
                "narrative_summary": "needs review",
            },
            "review_packet": {
                "case_id": task.case_ref,
                "run_id": f"operator-{task.case_ref}-local",
                "status": "review_required",
                "case_summary": "review required",
                "deterministic_outputs": {},
                "failed_checks": ["threshold breach"],
                "draft_narrative": {},
                "artifact_links": {},
                "packet_paths": {"json": str(packet_json), "markdown": str(packet_markdown)},
            },
        }


def test_run_operator_flow_delivers_review_packet_when_outbox_is_configured(tmp_path):
    module = _load_module()

    result = module.run_operator_flow(
        case_id="review-delivery-case",
        artifact_dir=tmp_path / "artifacts",
        objective="Operator flow",
        runner_module=ReviewDeliveryRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
        review_delivery_dir=tmp_path / "outbox",
    )

    assert result["status"] == "needs_review"
    assert result["review_delivery"]["destination"] == "local_outbox"
    assert Path(result["review_delivery"]["delivered_paths"]["json"]).exists()
    assert Path(result["review_delivery"]["delivered_paths"]["markdown"]).exists()


def test_run_operator_flow_returns_structured_failure_payload_when_runner_crashes(tmp_path):
    module = _load_module()

    result = module.run_operator_flow(
        case_id="broken-case",
        artifact_dir=tmp_path,
        objective="Operator flow",
        runner_module=ExplodingRunnerModule,
        task_contracts_module=FakeTaskContractsModule,
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["case_id"] == "broken-case"
    assert result["error_category"] == "planner_runtime"
    assert result["errors"] == ["runner unavailable"]
    assert result["worker_result"]["status"] == "failed"
    assert result["worker_result"]["worker_metadata"]["failure_stage"] == "planner_runtime"


def test_workflow_source_path_raises_clear_error_when_workflow_file_is_missing(monkeypatch):
    module = _load_module()

    missing_root = ROOT / "missing-repo-root"

    class FakeResolvedPath:
        @property
        def parents(self):
            return [None, None, missing_root]

    monkeypatch.setattr(module.Path, "resolve", lambda self: FakeResolvedPath())

    try:
        module._workflow_source_path("agent-runtimes", "openai-agents", "runner.py")
    except FileNotFoundError as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected _workflow_source_path to raise FileNotFoundError")

    assert "workflows/" in message
    assert "editable mode" in message


def test_run_governed_case_script_emits_json(tmp_path):
    helper = tmp_path / "invoke_operator_script.py"
    helper.write_text(
        "import importlib.util, json, types\n"
        f"script_spec=importlib.util.spec_from_file_location('run_governed_case', {str(SCRIPT_PATH)!r})\n"
        "script=importlib.util.module_from_spec(script_spec)\n"
        "script_spec.loader.exec_module(script)\n"
        "script._load_operator_module=lambda: types.SimpleNamespace(main=lambda argv=None: {'ok': True, 'status': 'completed', 'case_id': 'cli-case', 'argv': argv or []})\n"
        "script.main(['--case-id','cli-case','--artifact-dir','./tmp/test-cli'])\n"
    )

    proc = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["case_id"] == "cli-case"
    assert "--case-id" in payload["argv"]
