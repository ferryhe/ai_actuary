from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BATCH_RUNNER_PATH = REPO_ROOT / "benchmarks" / "runners" / "batch_runner.py"
COMPARISON_PATH = REPO_ROOT / "src" / "reserving_workflow" / "evaluation" / "comparison.py"
CASE_PACKS_PATH = REPO_ROOT / "src" / "reserving_workflow" / "evaluation" / "case_packs.py"
SIMULATION_PATH = REPO_ROOT / "src" / "reserving_workflow" / "evaluation" / "simulation.py"
REPORT_EXPORT_PATH = REPO_ROOT / "src" / "reserving_workflow" / "reports" / "export.py"
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
    case_worker_module = _load_module("batch_runner_case_worker", HERMES_WORKER_DIR / "case_worker.py")

    def fake_governed_runner(task, *, user_prompt=None):
        worker_result = case_worker_module.run_case_worker(task)
        return {
            "route": {"mode": "governed"},
            "worker_result": worker_result.model_dump(mode="json"),
            "final_output": {
                "case_id": worker_result.case_id,
                "run_id": worker_result.run_id,
                "worker_status": worker_result.status,
                "deterministic_method": "chainladder",
                "cited_values": dict(worker_result.deterministic_result.get("reserve_summary", {})),
                "review_reasons": list(worker_result.review_reasons),
                "artifact_manifest_path": worker_result.artifact_paths["run_manifest"],
                "narrative_summary": f"governed summary for {worker_result.case_id}",
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
    assert Path(saved_report["resolved_case_pack_path"]).exists()
    assert Path(saved_report["batch_manifest_path"]).exists()
    assert Path(saved_report["registry_path"]).exists()


def test_run_batch_benchmark_records_failed_mode_and_still_writes_report(tmp_path):
    batch_runner = _load_module("batch_runner_module_fail", BATCH_RUNNER_PATH)

    def failing_governed_runner(task, *, user_prompt=None):
        raise RuntimeError("governed runner unavailable")

    report = batch_runner.run_batch_benchmark(
        cases=[{"case_id": "batch-case-1", "sample_name": "RAA"}],
        artifact_root=tmp_path,
        governed_runner=failing_governed_runner,
    )

    assert Path(report["comparison_report_path"]).exists()
    governed_result = report["mode_results"]["governed_workflow"][0]
    assert governed_result["status"] == "failed"
    assert "RuntimeError" in governed_result["errors"][0]
    assert report["mode_artifact_manifests"]["governed_workflow"] == []


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


def test_run_batch_worker_returns_failed_worker_result_on_batch_error(tmp_path):
    task_contracts = _load_module("batch_task_contracts_fail", HERMES_WORKER_DIR / "task_contracts.py")
    batch_worker = _load_module("batch_worker_module_fail", HERMES_WORKER_DIR / "batch_worker.py")

    task = task_contracts.WorkerTask(
        task_id="batch-task-err-001",
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
            "FailingBatchRunnerModule",
            (),
            {"run_batch_benchmark": staticmethod(lambda **kwargs: (_ for _ in ()).throw(RuntimeError("batch runner exploded")))},
        ),
    )

    assert result.status == "failed"
    assert "batch runner exploded" in result.errors[0]
    assert result.artifact_paths["artifact_root"].endswith(str(tmp_path))


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


def test_simulate_claim_triangle_returns_stable_rows_and_claims():
    simulation = _load_module("simulation_module", SIMULATION_PATH)

    payload = simulation.simulate_claim_triangle(
        {
            "simulation_id": "stable-v1",
            "origin_year_start": 2020,
            "origin_count": 3,
            "development_count": 3,
            "base_ultimate": 1000.0,
            "origin_increment": 100.0,
            "curvature": 10.0,
            "claim_count_base": 2,
            "cdf_pattern": [0.5, 0.8, 1.0],
        }
    )

    assert payload["simulation_id"] == "stable-v1"
    assert payload["triangle_rows"] == [
        {"origin": 2020, "development": 2020, "paid": 500.0},
        {"origin": 2020, "development": 2021, "paid": 800.0},
        {"origin": 2020, "development": 2022, "paid": 1000.0},
        {"origin": 2021, "development": 2021, "paid": 555.0},
        {"origin": 2021, "development": 2022, "paid": 888.0},
        {"origin": 2022, "development": 2022, "paid": 620.0},
    ]
    assert payload["claim_records"][0]["claim_id"] == "stable-v1-OY2020-DY2020-C01"
    last_cell_total = sum(
        record["paid_incremental"]
        for record in payload["claim_records"]
        if record["origin"] == 2022 and record["development"] == 2022
    )
    assert last_cell_total == 620.0


def test_load_case_pack_resolves_deterministic_simulations():
    case_packs = _load_module("case_packs_module", CASE_PACKS_PATH)

    pack = case_packs.load_case_pack()

    assert pack["case_pack_id"] == "deterministic-v1"
    assert len(pack["cases"]) == 3
    simulated_case = next(case for case in pack["cases"] if case["case_id"] == "simulated-steady-growth")
    assert simulated_case["case_payload"]["metadata"]["simulation"]["simulation_id"] == "steady-growth-v1"
    assert simulated_case["case_payload"]["run_config"]["required_artifacts"]
    assert simulated_case["case_payload"]["metadata"]["triangle_rows"][0]["origin"] == 2018


def test_batch_benchmark_generated_run_can_be_replayed_and_exported(tmp_path):
    batch_runner = _load_module("batch_runner_replay_export", BATCH_RUNNER_PATH)
    case_packs = _load_module("case_packs_replay_export", CASE_PACKS_PATH)
    replay_module = _load_module("replay_module_benchmark", REPO_ROOT / "src" / "reserving_workflow" / "artifacts" / "replay.py")
    report_export = _load_module("report_export_benchmark", REPORT_EXPORT_PATH)
    case_worker_module = _load_module("case_worker_benchmark", HERMES_WORKER_DIR / "case_worker.py")

    def fake_governed_runner(task, *, user_prompt=None):
        worker_result = case_worker_module.run_case_worker(task)
        return {
            "route": {"mode": "governed"},
            "worker_result": worker_result.model_dump(mode="json"),
            "final_output": {
                "case_id": worker_result.case_id,
                "run_id": worker_result.run_id,
                "worker_status": worker_result.status,
                "cited_values": dict(worker_result.deterministic_result.get("reserve_summary", {})),
                "review_reasons": list(worker_result.review_reasons),
                "artifact_manifest_path": worker_result.artifact_paths["run_manifest"],
            },
        }

    pack = case_packs.load_case_pack()
    report = batch_runner.run_batch_benchmark(
        cases=pack["cases"][:1],
        artifact_root=tmp_path / "batch",
        governed_runner=fake_governed_runner,
        case_pack_id=pack["case_pack_id"],
    )

    governed_result = report["mode_results"]["governed_workflow"][0]
    replay = replay_module.replay_case_from_manifest(governed_result["artifact_manifest_path"])
    export = report_export.export_run_report(
        registry_path=report["registry_path"],
        run_id=governed_result["run_id"],
        review_store_root=tmp_path / "reviews",
    )

    assert replay["matches_saved_result"] is True
    assert replay["saved_summary"] == replay["replayed_summary"]
    assert export["run"]["run_id"] == governed_result["run_id"]
    assert Path(export["exports"]["operator_handoff_markdown"]).exists()
    assert Path(export["exports"]["reserve_summary_json"]).exists()


def test_run_batch_benchmark_script_supports_builtin_case_pack(tmp_path):
    script_path = REPO_ROOT / "scripts" / "run_batch_benchmark.py"
    helper = tmp_path / "invoke_batch_pack_script.py"
    helper.write_text(
        "import importlib.util, types\n"
        f"script_spec=importlib.util.spec_from_file_location('run_batch_benchmark', {str(script_path)!r})\n"
        "script=importlib.util.module_from_spec(script_spec)\n"
        "script_spec.loader.exec_module(script)\n"
        "script._load_batch_runner_module=lambda: types.SimpleNamespace(run_batch_benchmark=lambda **kwargs: {'ok': True, 'case_count': len(kwargs['cases']), 'case_pack_id': kwargs['case_pack_id']})\n"
        "script.main(['--case-pack', 'deterministic-v1', '--artifact-root', './tmp/batch-cli'])\n"
    )

    proc = __import__('subprocess').run([__import__('sys').executable, str(helper)], capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["case_count"] == 3
    assert payload["case_pack_id"] == "deterministic-v1"
