from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

from reserving_workflow.storage.interfaces import ArtifactStore, ReviewStore, RunStore
from reserving_workflow.storage.local import (
    LocalArtifactStore,
    LocalReviewStore,
    LocalRunStore,
    ReviewRecordReadError,
)
from reserving_workflow.storage.safe_json import PinnedJsonRoot, SafeJsonReadError


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("junction creation is unavailable")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


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
        created_by="actuary-a",
        operator_id="actuary-a",
        workspace_id="workspace-a",
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
    assert entry["created_by"] == "actuary-a"
    assert entry["operator_id"] == "actuary-a"
    assert entry["workspace_id"] == "workspace-a"
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


def test_local_run_store_persists_custom_workflow_history_payloads(tmp_path):
    store = LocalRunStore(tmp_path / "run-registry.json")

    store.create_run(
        task_id="operator-workflow-case",
        case_id="workflow-case",
        run_id="workflow-run",
        status="queued",
        summary="queued",
        operator_params={"case_id": "workflow-case", "workflow_id": "chainladder-basic"},
    )
    store.update_run_status(
        run_id="workflow-run",
        task_id="operator-workflow-case",
        case_id="workflow-case",
        status="running",
        summary="running workflow step",
        operator_params={"case_id": "workflow-case", "workflow_id": "chainladder-basic"},
        event_type="workflow.step.running",
        event_payload={"workflow_id": "chainladder-basic", "step_id": "chainladder"},
    )

    entry = store.get_run("workflow-run")

    assert entry["operator_params"]["workflow_id"] == "chainladder-basic"
    assert entry["status_history"][1]["event_type"] == "workflow.step.running"
    assert entry["status_history"][1]["payload"] == {
        "workflow_id": "chainladder-basic",
        "step_id": "chainladder",
    }


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
        status="review_required",
        reason_codes=["origin_count_below_threshold"],
        assigned_to="actuary-001",
        workspace_id="workspace-001",
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
    review_decision_markdown = (review_dir / "review_decision.md").read_text(encoding="utf-8")

    assert isinstance(store, ReviewStore)
    assert created["status"] == "review_required"
    assert created["assigned_to"] == "actuary-001"
    assert created["workspace_id"] == "workspace-001"
    assert created["created_at"] == created["updated_at"]
    assert decided["decision"] == "approved"
    assert decided["decided_at"] == loaded["updated_at"]
    assert loaded["decision"]["decision"] == "approved"
    assert loaded["status"] == "review_decided"
    assert review_record["run_id"] == "run-001"
    assert review_record["assigned_to"] == "actuary-001"
    assert review_record["workspace_id"] == "workspace-001"
    assert review_record["packet"] == {"status": "review_required"}
    assert review_decision["comment"] == "Looks good."
    assert review_decision["decided_by"] == "actuary-001"
    assert "Review Decision" in review_decision_markdown
    assert store.get_review_for_run("run-001")["review_id"] == "review-001"
    assert store.list_reviews()[0]["review_id"] == "review-001"


def test_local_review_store_rejects_duplicate_review_id_without_overwriting_decision(tmp_path):
    store = LocalReviewStore(tmp_path / "reviews")
    store.create_review(review_id="review-001", run_id="run-001", case_id="case-001", status="review_required")
    store.submit_decision(review_id="review-001", decision="approved", comment="Approved")

    with pytest.raises(ValueError, match="Review id already exists"):
        store.create_review(review_id="review-001", run_id="run-002", case_id="case-002", status="review_required")

    loaded = store.get_review("review-001")
    decision_path = tmp_path / "reviews" / "review-001" / "review_decision.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert loaded["run_id"] == "run-001"
    assert loaded["decision"]["decision"] == "approved"
    assert decision["decision"] == "approved"


def test_local_review_store_rejects_decision_for_missing_review_without_creating_directory(tmp_path):
    root = tmp_path / "reviews"
    store = LocalReviewStore(root)

    with pytest.raises(ValueError, match="Review id not found"):
        store.submit_decision(review_id="missing-review", decision="approved")

    assert not (root / "missing-review").exists()


def test_local_review_store_rejects_nested_review_ids(tmp_path):
    store = LocalReviewStore(tmp_path / "reviews")

    with pytest.raises(ValueError, match="review_id must be a single safe path component"):
        store.create_review(review_id="nested/review", run_id="run-001", case_id="case-001", status="review_required")

    with pytest.raises(ValueError, match="review_id must be a single safe path component"):
        store.submit_decision(review_id="nested/review", decision="approved")


def test_local_review_store_rejects_symlinked_record_without_reading_target(tmp_path):
    root = tmp_path / "reviews"
    review_dir = root / "review-001"
    review_dir.mkdir(parents=True)
    outside = tmp_path / "outside-review.json"
    outside.write_text('{"review_id":"foreign-review"}', encoding="utf-8")
    try:
        (review_dir / "review_record.json").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    store = LocalReviewStore(root)

    with pytest.raises(ReviewRecordReadError) as exc_info:
        store.get_review("review-001")

    assert str(exc_info.value) == "Stored review record could not be read safely."
    assert str(outside) not in str(exc_info.value)


def test_local_review_store_keeps_root_lexical_and_rejects_linked_root(tmp_path):
    outside = tmp_path / "outside-reviews"
    record = outside / "review-001" / "review_record.json"
    record.parent.mkdir(parents=True)
    sentinel = "OUTSIDE-REVIEW-SENTINEL"
    record.write_text(
        json.dumps({"review_id": "review-001", "run_id": sentinel}),
        encoding="utf-8",
    )
    root = tmp_path / "reviews"
    _make_directory_link(root, outside)
    store = LocalReviewStore(root)

    assert store.root == Path(os.path.abspath(os.path.expanduser(str(root))))
    with pytest.raises(ReviewRecordReadError) as get_error:
        store.get_review("review-001")
    with pytest.raises(ReviewRecordReadError) as list_error:
        store.list_reviews()

    assert sentinel not in str(get_error.value)
    assert sentinel not in str(list_error.value)


def test_pinned_root_enumerates_and_reads_the_same_directory_after_root_rename(tmp_path):
    root = tmp_path / "reviews"
    original_record = root / "review-original" / "review_record.json"
    original_record.parent.mkdir(parents=True)
    original_record.write_text(
        '{"review_id":"review-original","run_id":"run-original"}',
        encoding="utf-8",
    )
    replacement = tmp_path / "replacement"
    replacement_record = replacement / "review-replacement" / "review_record.json"
    replacement_record.parent.mkdir(parents=True)
    replacement_record.write_text(
        '{"review_id":"review-replacement","run_id":"run-replacement"}',
        encoding="utf-8",
    )
    parked = tmp_path / "parked"

    with PinnedJsonRoot(root, namespace="review_record", allow_nested=True) as pinned:
        root.rename(parked)
        replacement.rename(root)
        try:
            assert pinned.list_directories(max_entries=10) == ["review-original"]
            assert pinned.read_bounded_json_object(
                "review-original/review_record.json"
            )["run_id"] == "run-original"
        finally:
            root.rename(replacement)
            parked.rename(root)


def test_pinned_root_directory_enumeration_skips_linked_children(tmp_path):
    root = tmp_path / "reviews"
    root.mkdir()
    safe = root / "review-safe"
    safe.mkdir()
    outside = tmp_path / "outside-review"
    outside.mkdir()
    _make_directory_link(root / "review-linked", outside)

    with PinnedJsonRoot(root, namespace="review_record", allow_nested=True) as pinned:
        assert pinned.list_directories(max_entries=10) == ["review-safe"]


def test_pinned_root_directory_enumeration_has_a_total_entry_limit(tmp_path):
    root = tmp_path / "reviews"
    root.mkdir()
    for index in range(4):
        (root / f"entry-{index}").mkdir()

    with PinnedJsonRoot(root, namespace="review_record", allow_nested=True) as pinned:
        with pytest.raises(SafeJsonReadError) as exc_info:
            pinned.list_directories(max_entries=3)

    assert exc_info.value.code == "review_record_entry_limit_exceeded"


def test_local_review_list_keeps_enumeration_and_records_on_one_pinned_root(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "reviews"
    original = root / "review-original" / "review_record.json"
    original.parent.mkdir(parents=True)
    original.write_text(
        '{"review_id":"review-original","run_id":"run-original"}',
        encoding="utf-8",
    )
    replacement = tmp_path / "replacement"
    replacement_record = replacement / "review-original" / "review_record.json"
    replacement_record.parent.mkdir(parents=True)
    replacement_record.write_text(
        '{"review_id":"review-original","run_id":"run-replacement"}',
        encoding="utf-8",
    )
    parked = tmp_path / "parked"
    original_list = PinnedJsonRoot.list_directories
    swapped = False

    def list_then_swap(pinned, *, max_entries=1_000, namespace=None):
        nonlocal swapped
        names = original_list(
            pinned,
            max_entries=max_entries,
            namespace=namespace,
        )
        root.rename(parked)
        replacement.rename(root)
        swapped = True
        return names

    monkeypatch.setattr(PinnedJsonRoot, "list_directories", list_then_swap)
    try:
        reviews = LocalReviewStore(root).list_reviews()
    finally:
        if swapped:
            root.rename(replacement)
            parked.rename(root)

    assert swapped is True
    assert reviews[0]["run_id"] == "run-original"


def test_local_review_list_rejects_more_than_the_bounded_entry_limit(tmp_path):
    root = tmp_path / "reviews"
    root.mkdir()
    for index in range(1_001):
        (root / f"entry-{index}").touch()

    with pytest.raises(ReviewRecordReadError):
        LocalReviewStore(root).list_reviews()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="fd accounting is unavailable")
def test_pinned_directory_enumeration_closes_posix_descriptors(tmp_path):
    root = tmp_path / "reviews"
    (root / "review-001").mkdir(parents=True)
    before = len(tuple(Path("/proc/self/fd").iterdir()))

    for _ in range(50):
        with PinnedJsonRoot(root, namespace="review_record") as pinned:
            assert pinned.list_directories() == ["review-001"]

    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows handle accounting is unavailable")
def test_pinned_directory_enumeration_closes_windows_handles(tmp_path):
    import ctypes
    from ctypes import wintypes

    root = tmp_path / "reviews"
    (root / "review-001").mkdir(parents=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count():
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        )
        return int(count.value)

    before = handle_count()
    for _ in range(50):
        with PinnedJsonRoot(root, namespace="review_record") as pinned:
            assert pinned.list_directories() == ["review-001"]

    assert handle_count() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_local_review_store_rejects_fifo_without_blocking(tmp_path):
    root = tmp_path / "reviews"
    review_dir = root / "review-001"
    review_dir.mkdir(parents=True)
    os.mkfifo(review_dir / "review_record.json")
    store = LocalReviewStore(root)

    started = time.monotonic()
    with pytest.raises(ReviewRecordReadError):
        store.get_review("review-001")

    assert time.monotonic() - started < 2


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="POSIX socket fixture is unavailable",
)
def test_local_review_store_rejects_socket_record_without_blocking(tmp_path):
    root = tmp_path / "reviews"
    review_dir = root / "review-001"
    review_dir.mkdir(parents=True)
    target = review_dir / "review_record.json"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(target))
        started = time.monotonic()
        with pytest.raises(ReviewRecordReadError):
            LocalReviewStore(root).get_review("review-001")
        assert time.monotonic() - started < 2
    finally:
        listener.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX device fixture is unavailable")
def test_local_review_store_rejects_device_record_without_blocking(tmp_path):
    root = tmp_path / "reviews"
    review_dir = root / "review-001"
    review_dir.mkdir(parents=True)
    target = review_dir / "review_record.json"
    try:
        os.mknod(target, stat.S_IFCHR | 0o600, os.makedev(1, 3))
    except (AttributeError, OSError) as exc:
        pytest.skip(f"device creation is unavailable: {exc}")
    store = LocalReviewStore(root)

    started = time.monotonic()
    with pytest.raises(ReviewRecordReadError):
        store.get_review("review-001")

    assert time.monotonic() - started < 2


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="fd accounting is unavailable")
def test_local_review_store_closes_descriptors_on_unsafe_record(tmp_path):
    root = tmp_path / "reviews"
    review_dir = root / "review-001"
    review_dir.mkdir(parents=True)
    (review_dir / "review_record.json").write_text("{broken", encoding="utf-8")
    store = LocalReviewStore(root)
    before = len(tuple(Path("/proc/self/fd").iterdir()))

    for _ in range(50):
        with pytest.raises(ReviewRecordReadError):
            store.get_review("review-001")

    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


def test_local_review_store_list_uses_safe_record_reader(tmp_path):
    root = tmp_path / "reviews"
    safe = root / "review-safe"
    unsafe = root / "review-unsafe"
    safe.mkdir(parents=True)
    unsafe.mkdir()
    (safe / "review_record.json").write_text(
        '{"review_id":"review-safe","run_id":"run-safe"}',
        encoding="utf-8",
    )
    (unsafe / "review_record.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ReviewRecordReadError) as exc_info:
        LocalReviewStore(root).list_reviews()

    assert str(exc_info.value) == "Stored review record could not be read safely."


@pytest.mark.skipif(os.name != "nt", reason="Windows junction fixture is unavailable")
def test_local_review_store_rejects_junctioned_review_directory(tmp_path):
    root = tmp_path / "reviews"
    root.mkdir()
    outside = tmp_path / "outside-review"
    outside.mkdir()
    (outside / "review_record.json").write_text(
        '{"review_id":"foreign-review"}',
        encoding="utf-8",
    )
    junction = root / "review-001"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        with pytest.raises(ReviewRecordReadError):
            LocalReviewStore(root).get_review("review-001")
    finally:
        os.rmdir(junction)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle accounting is unavailable")
def test_local_review_store_closes_windows_handles_on_error(tmp_path):
    import ctypes
    from ctypes import wintypes

    root = tmp_path / "reviews"
    review_dir = root / "review-001"
    review_dir.mkdir(parents=True)
    (review_dir / "review_record.json").write_text("{broken", encoding="utf-8")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count():
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        )
        return int(count.value)

    store = LocalReviewStore(root)
    before = handle_count()
    for _ in range(50):
        with pytest.raises(ReviewRecordReadError):
            store.get_review("review-001")

    assert handle_count() == before
