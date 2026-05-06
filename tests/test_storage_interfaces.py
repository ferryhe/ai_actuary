from __future__ import annotations

import json
from pathlib import Path

import pytest

from reserving_workflow.storage.interfaces import ArtifactStore, ReviewStore, RunStore
from reserving_workflow.storage.local import LocalArtifactStore, LocalReviewStore, LocalRunStore


def test_local_run_store_tracks_history_and_lists_latest_first(tmp_path):
    store = LocalRunStore(tmp_path / "run-registry.json")

    created = store.create_run(
        task_id="operator-case-a",
        case_id="case-a",
        run_id="run-a",
        status="queued",
        artifact_root=str(tmp_path / "artifacts-a"),
        summary="queued",
        operator_params={"case_id": "case-a"},
    )
    store.append_event(run_id="run-a", status="running", summary="running")
    updated = store.update_run_status(
        run_id="run-a",
        task_id="operator-case-a",
        case_id=None,
        status="completed",
        summary="done",
        review_required=False,
    )
    store.create_run(
        task_id="operator-case-b",
        case_id="case-b",
        run_id="run-b",
        status="accepted",
        summary="accepted",
    )

    runs = store.list_runs()
    entry = store.get_run("run-a")

    assert isinstance(store, RunStore)
    assert created["status"] == "queued"
    assert updated["status"] == "completed"
    assert entry["case_id"] == "case-a"
    assert [item["status"] for item in entry["status_history"]] == ["queued", "running", "completed"]
    assert [item["run_id"] for item in runs] == ["run-b", "run-a"]


def test_local_run_store_rejects_duplicate_create(tmp_path):
    store = LocalRunStore(tmp_path / "run-registry.json")
    store.create_run(task_id="operator-case-a", case_id="case-a", run_id="run-a", status="queued")

    with pytest.raises(ValueError, match="Run id already exists"):
        store.create_run(task_id="operator-case-a-duplicate", case_id="case-b", run_id="run-a", status="queued")

    entry = store.get_run("run-a")
    assert entry["task_id"] == "operator-case-a"
    assert entry["case_id"] == "case-a"
    assert [item["status"] for item in entry["status_history"]] == ["queued"]


def test_local_artifact_store_writes_reads_and_lists_artifacts(tmp_path):
    store = LocalArtifactStore()
    root = tmp_path / "artifacts"

    json_path = store.write_artifact(root=root, relative_path="deterministic_result.json", payload={"ibnr": 12.0})
    text_path = store.write_artifact(
        root=root,
        relative_path="review/review_packet.md",
        payload="# Review Packet\n",
        format="text",
    )

    artifacts = store.list_artifacts(root)

    assert isinstance(store, ArtifactStore)
    assert json_path == root.resolve() / "deterministic_result.json"
    assert text_path == root.resolve() / "review" / "review_packet.md"
    assert store.read_artifact(json_path) == {"ibnr": 12.0}
    assert store.read_artifact(text_path, format="text") == "# Review Packet\n"
    assert artifacts == ["deterministic_result.json", "review/review_packet.md"]


def test_local_review_store_creates_and_updates_artifact_backed_reviews(tmp_path):
    store = LocalReviewStore(tmp_path / "reviews")

    created = store.create_review(
        review_id="review-001",
        run_id="run-001",
        case_id="case-001",
        status="pending",
        reason_codes=["origin_count_below_threshold"],
        packet={"status": "review_required"},
    )
    decided = store.submit_decision(
        review_id="review-001",
        decision="approved",
        comment="Looks good.",
        decided_by="actuary-001",
    )
    loaded = store.get_review("review-001")

    review_dir = tmp_path / "reviews" / "review-001"
    review_record = json.loads((review_dir / "review_record.json").read_text(encoding="utf-8"))
    review_decision = json.loads((review_dir / "review_decision.json").read_text(encoding="utf-8"))

    assert isinstance(store, ReviewStore)
    assert created["status"] == "pending"
    assert decided["decision"] == "approved"
    assert loaded["decision"]["decision"] == "approved"
    assert review_record["run_id"] == "run-001"
    assert review_record["packet"] == {"status": "review_required"}
    assert review_decision["comment"] == "Looks good."
    assert review_decision["decided_by"] == "actuary-001"


def test_local_review_store_rejects_duplicate_review_id_without_overwriting_decision(tmp_path):
    store = LocalReviewStore(tmp_path / "reviews")
    store.create_review(review_id="review-001", run_id="run-001", case_id="case-001", status="pending")
    store.submit_decision(review_id="review-001", decision="approved", comment="Approved")

    with pytest.raises(ValueError, match="Review id already exists"):
        store.create_review(review_id="review-001", run_id="run-002", case_id="case-002", status="pending")

    loaded = store.get_review("review-001")
    decision_path = tmp_path / "reviews" / "review-001" / "review_decision.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert loaded["run_id"] == "run-001"
    assert loaded["decision"]["decision"] == "approved"
    assert decision["decision"] == "approved"


def test_local_review_store_rejects_decision_for_missing_review(tmp_path):
    store = LocalReviewStore(tmp_path / "reviews")

    with pytest.raises(ValueError, match="Review id not found"):
        store.submit_decision(review_id="missing-review", decision="approved")
