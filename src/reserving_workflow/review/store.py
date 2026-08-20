"""Review-store helpers for local control-plane APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reserving_workflow.artifacts.storage import read_json_artifact, write_json_artifact
from reserving_workflow.contracts.control_plane import Review, ReviewDecisionArtifact
from reserving_workflow.storage.local import (
    LocalArtifactStore,
    LocalReviewStore,
    ReviewNotFoundError,
    resolve_artifact_path,
    resolve_artifact_root,
)


class ReviewIdentityMismatchError(ValueError):
    """A persisted review or packet conflicts with authoritative run identity."""

    code = "review_identity_mismatch"
    message = "Stored review identity conflicts with the registered run."

    def __init__(self) -> None:
        super().__init__(self.message)


def ensure_review_record(
    *,
    review_store: LocalReviewStore,
    run_entry: dict[str, Any],
    review_packet: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not _run_needs_review(run_entry, review_packet):
        return review_store.get_review_for_run(str(run_entry.get("run_id"))) if run_entry.get("run_id") else None

    existing = review_store.get_review_for_run(str(run_entry.get("run_id")))
    if existing is not None:
        return existing

    packet_payload = review_packet or {}
    review_id = build_review_id(str(run_entry.get("run_id")))
    reason_codes = _extract_reason_codes(run_entry, packet_payload)
    return review_store.create_review(
        review_id=review_id,
        run_id=str(run_entry.get("run_id")),
        case_id=str(run_entry.get("case_id") or "unknown-case"),
        status="review_required",
        reason_codes=reason_codes,
        assigned_to=_review_assignee(run_entry, packet_payload),
        workspace_id=_run_workspace_id(run_entry),
        packet=packet_payload or None,
    )


def build_review_snapshot(
    *,
    review_store: LocalReviewStore,
    run_entry: dict[str, Any],
    review_packet_result: dict[str, Any] | None = None,
    review_store_root: str | Path | None = None,
    decision_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable review view without creating a persistent record."""

    review_packet_result = review_packet_result or {}
    run_id = str(run_entry.get("run_id"))
    expected_review_id = build_review_id(run_id)
    try:
        record = review_store.get_review(expected_review_id)
    except ReviewNotFoundError:
        record = review_store.get_review_for_run(run_id)
    if record is not None:
        record = bind_review_record_identity(record, run_entry=run_entry)
        packet = record.get("packet")
        if packet is None and review_packet_result.get("present"):
            packet = review_packet_result.get("packet")
        validate_review_packet_identity(packet, run_entry=run_entry)
        return build_review_contract(
            record,
            review_packet_result=review_packet_result,
            review_store_root=review_store_root,
            decision_artifacts=decision_artifacts,
        )

    packet = review_packet_result.get("packet") if review_packet_result.get("present") else None
    validate_review_packet_identity(packet, run_entry=run_entry)
    needs_review = _run_needs_review(run_entry, packet)
    status = "review_required" if needs_review else "not_required"
    packet_payload = packet if isinstance(packet, dict) else {}
    return Review(
        status=status,
        review_id=build_review_id(run_id) if needs_review else None,
        run_id=run_id,
        case_id=run_entry.get("case_id"),
        workspace_id=_run_workspace_id(run_entry),
        review_required=needs_review,
        reason_codes=_extract_reason_codes(run_entry, packet_payload) if needs_review else [],
        assigned_to=_review_assignee(run_entry, packet_payload) if needs_review else None,
        packet=packet if isinstance(packet, dict) else None,
        json_path=review_packet_result.get("json_path"),
        markdown_path=review_packet_result.get("markdown_path"),
        review_delivery=run_entry.get("review_delivery"),
    ).model_dump(exclude_none=True)


def bind_review_record_identity(
    record: dict[str, Any],
    *,
    run_entry: dict[str, Any],
) -> dict[str, Any]:
    """Bind present persisted identities and fill legacy omissions in memory."""

    run_id = str(run_entry.get("run_id"))
    expected: dict[str, str | None] = {
        "review_id": build_review_id(run_id),
        "run_id": run_id,
        "case_id": (
            str(run_entry["case_id"])
            if run_entry.get("case_id") is not None
            else None
        ),
        "workspace_id": _run_workspace_id(run_entry),
    }
    bound = dict(record)
    for field, expected_value in expected.items():
        if expected_value is None:
            continue
        if field in record and record[field] is not None:
            if str(record[field]) != expected_value:
                raise ReviewIdentityMismatchError()
        else:
            bound[field] = expected_value
    validate_review_packet_identity(bound.get("packet"), run_entry=run_entry)
    _validate_review_decision_identity(bound.get("decision"), run_entry=run_entry)
    return bound


def validate_review_packet_identity(
    packet: Any,
    *,
    run_entry: dict[str, Any],
) -> None:
    if not isinstance(packet, dict):
        return
    expected = {
        "run_id": run_entry.get("run_id"),
        "case_id": run_entry.get("case_id"),
        "workspace_id": _run_workspace_id(run_entry),
    }
    for field, expected_value in expected.items():
        if field not in packet or expected_value is None:
            continue
        if packet[field] is None or str(packet[field]) != str(expected_value):
            raise ReviewIdentityMismatchError()


def _validate_review_decision_identity(
    decision: Any,
    *,
    run_entry: dict[str, Any],
) -> None:
    if not isinstance(decision, dict):
        return
    run_id = str(run_entry.get("run_id"))
    expected = {
        "review_id": build_review_id(run_id),
        "run_id": run_id,
    }
    for field, expected_value in expected.items():
        if field not in decision:
            continue
        if decision[field] is None or str(decision[field]) != expected_value:
            raise ReviewIdentityMismatchError()


def build_review_contract(
    record: dict[str, Any] | None,
    *,
    review_packet_result: dict[str, Any] | None = None,
    review_store_root: str | Path | None = None,
    decision_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if record is None:
        return Review(status="not_available", review_required=False).model_dump()

    review_packet_result = review_packet_result or {}
    packet_payload = record.get("packet")
    if packet_payload is None and review_packet_result.get("present"):
        packet_payload = review_packet_result.get("packet")

    decision_payload = record.get("decision")
    artifacts = decision_artifacts if decision_artifacts is not None else _decision_artifacts(
        record,
        review_store_root=review_store_root,
    )
    if isinstance(decision_payload, dict):
        decision_payload = {**decision_payload, "artifacts": artifacts}

    return Review(
        review_id=str(record.get("review_id")),
        run_id=str(record.get("run_id")),
        case_id=str(record.get("case_id")),
        workspace_id=record.get("workspace_id"),
        status=str(record.get("status") or "review_required"),
        review_required=True,
        decision=decision_payload,
        reason_codes=list(record.get("reason_codes", []) or []),
        assigned_to=record.get("assigned_to"),
        packet=packet_payload,
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
        record_path=str(_review_record_path(review_store_root, record.get("review_id"))) if review_store_root else None,
        json_path=review_packet_result.get("json_path"),
        markdown_path=review_packet_result.get("markdown_path"),
        review_delivery=record.get("review_delivery"),
    ).model_dump(exclude_none=True)


def write_run_review_decision_artifacts(
    *,
    run_entry: dict[str, Any],
    decision_record: dict[str, Any],
) -> list[dict[str, Any]]:
    artifact_root = run_entry.get("artifact_root")
    if not artifact_root:
        return []

    store = LocalArtifactStore()
    root = resolve_artifact_root(artifact_root)
    decision_json_path = store.write_artifact(
        root=root,
        relative_path="review_decision.json",
        payload=decision_record,
        format="json",
    )
    decision_md_path = store.write_artifact(
        root=root,
        relative_path="review_decision.md",
        payload=_render_run_decision_markdown(run_entry, decision_record),
        format="text",
    )

    manifest_path = root / "run_manifest.json"
    if manifest_path.exists():
        manifest = read_json_artifact(manifest_path)
    else:
        manifest = {
            "case_id": run_entry.get("case_id"),
            "run_id": run_entry.get("run_id"),
            "artifact_root": str(root),
            "artifact_paths": {},
        }
    artifact_paths = dict(manifest.get("artifact_paths", {}) or {})
    artifact_paths["review_decision"] = str(decision_json_path)
    artifact_paths["review_decision_markdown"] = str(decision_md_path)
    manifest["artifact_paths"] = artifact_paths
    write_json_artifact(resolve_artifact_path(root, "run_manifest.json"), manifest)

    return [
        ReviewDecisionArtifact(
            artifact_id="review_decision",
            path=str(decision_json_path),
            label="review decision",
            present=decision_json_path.exists(),
        ).model_dump(),
        ReviewDecisionArtifact(
            artifact_id="review_decision_markdown",
            path=str(decision_md_path),
            label="review decision markdown",
            present=decision_md_path.exists(),
        ).model_dump(),
    ]


def build_review_id(run_id: str) -> str:
    return f"review-{run_id}"


def _decision_artifacts(record: dict[str, Any], *, review_store_root: str | Path | None) -> list[dict[str, Any]]:
    if not review_store_root or not record.get("decision"):
        return []
    review_id = record.get("review_id")
    review_root = Path(review_store_root).expanduser().resolve() / str(review_id)
    return [
        ReviewDecisionArtifact(
            artifact_id="review_decision",
            path=str(review_root / "review_decision.json"),
            label="review decision",
            present=(review_root / "review_decision.json").exists(),
        ).model_dump(),
        ReviewDecisionArtifact(
            artifact_id="review_decision_markdown",
            path=str(review_root / "review_decision.md"),
            label="review decision markdown",
            present=(review_root / "review_decision.md").exists(),
        ).model_dump(),
    ]


def _extract_reason_codes(run_entry: dict[str, Any], review_packet: dict[str, Any]) -> list[str]:
    failed_checks = review_packet.get("failed_checks")
    if isinstance(failed_checks, list):
        return [str(item) for item in failed_checks]
    review_reasons = review_packet.get("review_reasons")
    if isinstance(review_reasons, list):
        return [str(item) for item in review_reasons]
    if isinstance(run_entry.get("errors"), list) and run_entry.get("status") == "needs_review":
        return [str(item) for item in run_entry["errors"]]
    return []


def _run_needs_review(run_entry: dict[str, Any], review_packet: dict[str, Any] | None) -> bool:
    if bool(run_entry.get("review_required")) or run_entry.get("status") == "needs_review":
        return True
    return bool(review_packet)


def _review_assignee(run_entry: dict[str, Any], review_packet: dict[str, Any]) -> str | None:
    assigned_to = review_packet.get("assigned_to")
    if assigned_to is not None:
        return str(assigned_to)
    created_by = run_entry.get("created_by")
    if created_by is not None:
        return str(created_by)
    operator_id = run_entry.get("operator_id")
    if operator_id is not None:
        return str(operator_id)
    return None


def _run_workspace_id(run_entry: dict[str, Any]) -> str | None:
    workspace_id = run_entry.get("workspace_id")
    if workspace_id is None:
        return None
    return str(workspace_id)


def _review_record_path(review_store_root: str | Path, review_id: Any) -> Path:
    return Path(review_store_root).expanduser().resolve() / str(review_id) / "review_record.json"


def _render_run_decision_markdown(run_entry: dict[str, Any], decision_record: dict[str, Any]) -> str:
    lines = [
        "# Run Review Decision",
        "",
        f"- case_id: {run_entry.get('case_id')}",
        f"- run_id: {run_entry.get('run_id')}",
        f"- decision: {decision_record.get('decision')}",
        f"- decided_by: {decision_record.get('decided_by') or 'unknown'}",
        f"- decided_at: {decision_record.get('decided_at')}",
    ]
    if decision_record.get("follow_up_run_id"):
        lines.append(f"- follow_up_run_id: {decision_record['follow_up_run_id']}")
    if decision_record.get("comment"):
        lines.extend(["", "## Comment", "", str(decision_record["comment"])])
    return "\n".join(lines) + "\n"
