from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BATCH_RUNNER_PATH = REPO_ROOT / "benchmarks" / "runners" / "batch_runner.py"
COMPARISON_PATH = REPO_ROOT / "src" / "reserving_workflow" / "evaluation" / "comparison.py"
HERMES_WORKER_DIR = REPO_ROOT / "workflows" / "agent-runtimes" / "hermes-worker"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_score_batch_mode_results_counts_statuses_and_numeric_deltas():
    comparison = _load_module("comparison_module", COMPARISON_PATH)

    scored = comparison.score_batch_mode_results(
        {
            "baseline_prompt": [
                {
                    "case_id": "case-1",
                    "status": "completed",
                    "reserve_summary": {"ibnr": 10.0, "ultimate": 110.0},
                },
                {
                    "case_id": "case-2",
                    "status": "needs_review",
                    "reserve_summary": {"ibnr": 20.0, "ultimate": 120.0},
                },
            ],
            "governed_workflow": [
                {
                    "case_id": "case-1",
                    "status": "completed",
                    "reserve_summary": {"ibnr": 11.0, "ultimate": 110.0},
                },
                {
                    "case_id": "case-2",
                    "status": "needs_review",
                    "reserve_summary": {"ibnr": 20.0, "ultimate": 120.0},
                },
            ],
        }
    )

    assert scored["mode_summaries"]["baseline_prompt"]["case_count"] == 2
    assert scored["mode_summaries"]["baseline_prompt"]["status_counts"]["needs_review"] == 1
    assert scored["case_comparisons"]["case-1"]["ibnr_delta"] == 1.0
    assert scored["case_comparisons"]["case-2"]["ibnr_delta"] == 0.0


def test_run_batch_benchmark_generates_comparison_report(tmp_path):
    batch_runner = _load_module("batch_runner_module", BATCH_RUNNER_PATH)

    def fake_governed_runner(task, *, user_prompt=None):
        return {
            "route": {"mode": "governed"},
            "worker_result": {
                "case_id": task.case_ref,
                "status": "completed",
                "review_reasons": [],
                "artifact_paths": {"run_manifest": str(Path(task.inputs["artifact_dir"]) / "run_manifest.json")},
                "deterministic_result": {
                    "reserve_summary": {"latest_diagonal": 160987.0, "ultimate": 213122.22826121017, "ibnr": 52135.228261210155},
                },
            },
            "final_output": {
                "case_id": task.case_ref,
                "worker_status": "completed",
                "deterministic_method": "chainladder",
                "cited_values": {"latest_diagonal": 160987.0, "ultimate": 213122.22826121017, "ibnr": 52135.228261210155},
                "review_reasons": [],
                "artifact_manifest_path": str(Path(task.inputs["artifact_dir"]) / "run_manifest.json"),
                "narrative_summary": f"governed summary for {task.case_ref}",
            },
        }

    report = batch_runner.run_batch_benchmark(
        cases=[
            {"case_id": "batch-case-1", "sample_name": "RAA"},
            {"case_id": "batch-case-2", "sample_name": "RAA", "review_threshold_origin_count": 5},
        ],
        artifact_root=tmp_path,
        governed_runner=fake_governed_runner,
    )

    assert report["case_count"] == 2
    assert sorted(report["modes"]) == ["baseline_prompt", "governed_workflow"]
    assert Path(report["comparison_report_path"]).exists()

    saved_report = json.loads(Path(report["comparison_report_path"]).read_text(encoding="utf-8"))
    assert saved_report["mode_summaries"]["baseline_prompt"]["case_count"] == 2
    assert saved_report["mode_summaries"]["governed_workflow"]["case_count"] == 2
    assert "batch-case-1" in saved_report["case_comparisons"]
    assert Path(saved_report["mode_artifact_manifests"]["baseline_prompt"][0]).exists()
    assert Path(saved_report["mode_artifact_manifests"]["governed_workflow"][0]).parent.parent.name == "governed_workflow"


def test_run_batch_worker_returns_completed_summary(tmp_path):
    task_contracts = _load_module("batch_task_contracts", HERMES_WORKER_DIR / "task_contracts.py")
    batch_worker = _load_module("batch_worker_module", HERMES_WORKER_DIR / "batch_worker.py")

    task = task_contracts.WorkerTask(
        task_id="batch-task-001",
        task_kind="run_batch",
        objective="Run benchmark batch",
        inputs={
            "artifact_root": str(tmp_path),
            "cases": [{"case_id": "batch-case-1", "sample_name": "RAA"}],
        },
    )

    result = batch_worker.run_batch_worker(
        task,
        batch_runner_module=type(
            "FakeBatchRunnerModule",
            (),
            {
                "run_batch_benchmark": staticmethod(
                    lambda **kwargs: {
                        "case_count": 1,
                        "modes": ["baseline_prompt", "governed_workflow"],
                        "comparison_report_path": str(tmp_path / "comparison_report.json"),
                        "mode_artifact_manifests": {"baseline_prompt": [], "governed_workflow": []},
                    }
                )
            },
        ),
    )

    assert result.status == "completed"
    assert result.task_kind == "run_batch"
    assert result.metrics["case_count"] == 1
    assert result.artifact_paths["comparison_report"].endswith("comparison_report.json")


def test_run_batch_benchmark_script_emits_json(tmp_path):
    script_path = REPO_ROOT / "scripts" / "run_batch_benchmark.py"
    helper = tmp_path / "invoke_batch_script.py"
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([{"case_id": "batch-case-1", "sample_name": "RAA"}]), encoding="utf-8")
    helper.write_text(
        "import importlib.util, types\n"
        f"script_spec=importlib.util.spec_from_file_location('run_batch_benchmark', {str(script_path)!r})\n"
        "script=importlib.util.module_from_spec(script_spec)\n"
        "script_spec.loader.exec_module(script)\n"
        "script._load_batch_runner_module=lambda: types.SimpleNamespace(run_batch_benchmark=lambda **kwargs: {'ok': True, 'case_count': 1, 'artifact_root': kwargs['artifact_root']})\n"
        f"script.main(['--cases-json', {str(cases_path)!r}, '--artifact-root', './tmp/batch-cli'])\n"
    )

    proc = __import__('subprocess').run([__import__('sys').executable, str(helper)], capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["case_count"] == 1
