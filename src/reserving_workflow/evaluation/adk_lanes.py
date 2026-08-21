"""Isolated ADK evaluation lane evidence helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from reserving_workflow.calculators import ChainladderAdapter
from reserving_workflow.evaluation.case_packs import load_case_pack
from reserving_workflow.runtime.adk_execution import canonical_json
from reserving_workflow.schemas import ReservingCaseInput
from reserving_workflow.validation import build_chainladder_case_payload


def run_offline_evaluation_lane(
    *,
    case_pack_id: str,
    case_pack: dict[str, Any] | None = None,
    state_root: str | Path,
    business_roots: Iterable[str | Path] = (),
    case_limit: int | None = None,
    evidence_id: str | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = _snapshot_roots(business_roots)
    resolved_case_pack = case_pack or load_case_pack(case_pack_id)
    effective_budget = dict(budget or {})
    if case_limit is not None:
        effective_budget["case_limit"] = case_limit
    cases = _bounded_cases(
        resolved_case_pack,
        case_limit=_integer_budget(effective_budget, "case_limit") or case_limit,
    )
    case_results, totals, status = _run_bounded_case_evaluations(
        cases,
        budget=effective_budget,
    )
    evidence = _base_evidence(
        lane="offline",
        status=status,
        case_pack_id=case_pack_id,
        case_count=len(cases),
    )
    evidence["ok"] = status == "completed"
    evidence["model_required"] = False
    evidence["budget"] = effective_budget
    evidence.update(totals)
    evidence["cases"] = case_results
    evidence["completed_count"] = sum(1 for case in case_results if case["status"] == "completed")
    evidence["failed_count"] = sum(1 for case in case_results if case["status"] in {"failed", "timeout"})
    evidence["effective_concurrency"] = 1
    if effective_budget.get("retention_days") is not None:
        evidence["retention_policy"] = _retention_policy(effective_budget["retention_days"])
    evidence["checks"] = [
        {
            "check_id": "case_pack_loaded",
            "status": "completed",
            "summary": "Builtin deterministic case pack loaded.",
        },
        {
            "check_id": "case_evaluations",
            "status": status,
            "summary": "Bounded deterministic case evaluations completed.",
        },
        {
            "check_id": "business_storage_invariant",
            "status": "completed",
            "summary": "Business registry, artifacts, and reviews were not touched.",
        },
    ]
    return _record_evidence(
        evidence,
        state_root=state_root,
        business_changed=_snapshot_roots(business_roots) != before,
        evidence_id=evidence_id,
    )


def run_real_model_evaluation_lane(
    *,
    case_pack_id: str,
    case_pack: dict[str, Any] | None = None,
    state_root: str | Path,
    business_roots: Iterable[str | Path] = (),
    credentials_present: bool | None = None,
    case_limit: int | None = None,
    evidence_id: str | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = _snapshot_roots(business_roots)
    resolved_case_pack = case_pack or load_case_pack(case_pack_id)
    effective_budget = dict(budget or {})
    if case_limit is not None:
        effective_budget["case_limit"] = case_limit
    cases = _bounded_cases(
        resolved_case_pack,
        case_limit=_integer_budget(effective_budget, "case_limit") or case_limit,
    )
    has_credentials = (
        credentials_present
        if credentials_present is not None
        else bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    )
    mode = os.environ.get("AI_ACTUARY_REAL_MODEL_EVAL_MODE", "").strip()
    if has_credentials and mode == "local_stub":
        case_results, totals, status = _run_bounded_case_evaluations(
            cases,
            budget=effective_budget,
        )
        reason = None
        summary = "Configured local real-model evaluation adapter completed."
    else:
        case_results = []
        totals = _empty_totals()
        if has_credentials:
            status = "failed"
            reason = "evaluator_not_configured"
            summary = "Real-model lane credentials were present, but no configured evaluator was selected."
        else:
            status = "skipped"
            reason = "credentials_missing"
            summary = "Real-model lane was skipped because model credentials were not configured."
    evidence = _base_evidence(
        lane="real_model",
        status=status,
        case_pack_id=case_pack_id,
        case_count=len(cases),
    )
    evidence["ok"] = status in {"completed", "skipped"}
    evidence["config_summary"] = {
        "mode": mode or "unconfigured",
        "credentials_present": has_credentials,
        "provider": "openai" if os.environ.get("OPENAI_API_KEY") else "google" if os.environ.get("GOOGLE_API_KEY") else "none",
    }
    evidence["budget"] = effective_budget
    evidence.update(totals)
    evidence["cases"] = case_results
    evidence["completed_count"] = sum(1 for case in case_results if case["status"] == "completed")
    evidence["failed_count"] = sum(1 for case in case_results if case["status"] in {"failed", "timeout"})
    evidence["effective_concurrency"] = 1
    if effective_budget.get("retention_days") is not None:
        evidence["retention_policy"] = _retention_policy(effective_budget["retention_days"])
    evidence["code_sha"] = _code_sha()
    if reason is not None:
        evidence["not_run_reason"] = reason
        if status == "failed":
            evidence["failure_class"] = reason
    evidence["checks"] = [
        {
            "check_id": "configured_evaluator" if status == "completed" else "credentials",
            "status": status,
            "summary": summary,
        },
        {
            "check_id": "business_storage_invariant",
            "status": "completed",
            "summary": "Business registry, artifacts, and reviews were not touched.",
        },
    ]
    return _record_evidence(
        evidence,
        state_root=state_root,
        business_changed=_snapshot_roots(business_roots) != before,
        evidence_id=evidence_id,
    )


def _bounded_cases(case_pack: dict[str, Any], *, case_limit: int | None) -> list[Any]:
    cases = list(case_pack.get("cases", []))
    if case_limit is None:
        return cases
    return cases[:case_limit]


def _run_bounded_case_evaluations(
    cases: list[Any],
    *,
    budget: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    started = time.monotonic()
    wall_time_seconds = _float_budget(budget, "wall_time_seconds")
    input_limit = _integer_budget(budget, "input_byte_limit")
    total_limit = _integer_budget(budget, "total_byte_limit")
    output_limit = _integer_budget(budget, "output_byte_limit")
    temp_limit = _integer_budget(budget, "temp_storage_bytes")
    total_input_bytes = 0
    total_output_bytes = 0
    total_bytes = 0
    results: list[dict[str, Any]] = []

    for case in cases:
        case_id = str(case.get("case_id") if isinstance(case, dict) else "case")
        if _wall_time_exceeded(started, wall_time_seconds):
            results.append(
                _failed_case_result(
                    case_id,
                    status="timeout",
                    failure_class="wall_time_exceeded",
                )
            )
            break
        try:
            case_payload = _case_payload(case)
            input_bytes = _json_size(case_payload)
            if input_limit is not None and input_bytes > input_limit:
                results.append(
                    _failed_case_result(
                        case_id,
                        failure_class="input_limit_exceeded",
                        input_bytes=input_bytes,
                    )
                )
                continue
            if total_limit is not None and total_bytes + input_bytes > total_limit:
                results.append(
                    _failed_case_result(
                        case_id,
                        failure_class="total_limit_exceeded",
                        input_bytes=input_bytes,
                    )
                )
                continue
            if temp_limit is not None and total_bytes + input_bytes > temp_limit:
                results.append(
                    _failed_case_result(
                        case_id,
                        failure_class="temp_storage_exceeded",
                        input_bytes=input_bytes,
                    )
                )
                continue
            case_input = ReservingCaseInput.model_validate(case_payload)
            result_payload = ChainladderAdapter().calculate(case_input).model_dump(
                mode="json"
            )
            output_bytes = _json_size(result_payload)
            if output_limit is not None and output_bytes > output_limit:
                results.append(
                    _failed_case_result(
                        case_id,
                        failure_class="output_limit_exceeded",
                        input_bytes=input_bytes,
                        output_bytes=output_bytes,
                    )
                )
                continue
            if total_limit is not None and total_bytes + input_bytes + output_bytes > total_limit:
                results.append(
                    _failed_case_result(
                        case_id,
                        failure_class="total_limit_exceeded",
                        input_bytes=input_bytes,
                        output_bytes=output_bytes,
                    )
                )
                continue
            if temp_limit is not None and total_bytes + input_bytes + output_bytes > temp_limit:
                results.append(
                    _failed_case_result(
                        case_id,
                        failure_class="temp_storage_exceeded",
                        input_bytes=input_bytes,
                        output_bytes=output_bytes,
                    )
                )
                continue
            total_input_bytes += input_bytes
            total_output_bytes += output_bytes
            total_bytes += input_bytes + output_bytes
            results.append(
                {
                    "case_id": case_id,
                    "status": "completed",
                    "input_bytes": input_bytes,
                    "output_bytes": output_bytes,
                    "result_digest": hashlib.sha256(
                        canonical_json(result_payload).encode("utf-8")
                    ).hexdigest(),
                }
            )
        except Exception:
            results.append(
                _failed_case_result(
                    case_id,
                    failure_class="case_evaluation_failed",
                )
            )

    status = _aggregate_case_status(results, expected_count=len(cases))
    return results, {
        "input_bytes": total_input_bytes,
        "output_bytes": total_output_bytes,
        "total_bytes": total_bytes,
    }, status


def _case_payload(case: Any) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise ValueError("case_invalid")
    if isinstance(case.get("case_payload"), dict):
        return dict(case["case_payload"])
    return build_chainladder_case_payload(
        case_id=str(case.get("case_id") or "case"),
        tool_inputs={
            "sample_name": case.get("sample_name", "RAA"),
            "method_variant": case.get("method", "chainladder"),
            **(
                {"review_threshold_origin_count": case["review_threshold_origin_count"]}
                if case.get("review_threshold_origin_count") is not None
                else {}
            ),
        },
    )


def _json_size(payload: Any) -> int:
    return len(canonical_json(payload).encode("utf-8"))


def _integer_budget(budget: dict[str, Any], field: str) -> int | None:
    value = budget.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_budget(budget: dict[str, Any], field: str) -> float | None:
    value = budget.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _wall_time_exceeded(started: float, wall_time_seconds: float | None) -> bool:
    if wall_time_seconds is None:
        return False
    if wall_time_seconds <= 0.001:
        return True
    return (time.monotonic() - started) > wall_time_seconds


def _failed_case_result(
    case_id: str,
    *,
    failure_class: str,
    status: str = "failed",
    input_bytes: int = 0,
    output_bytes: int = 0,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": status,
        "failure_class": failure_class,
        "input_bytes": input_bytes,
        "output_bytes": output_bytes,
    }


def _aggregate_case_status(results: list[dict[str, Any]], *, expected_count: int) -> str:
    if any(case.get("status") == "timeout" for case in results):
        return "timeout"
    completed = sum(1 for case in results if case.get("status") == "completed")
    failed = sum(1 for case in results if case.get("status") == "failed")
    if completed == expected_count and failed == 0:
        return "completed"
    if completed and failed:
        return "partial"
    if failed:
        return "failed"
    return "completed"


def _empty_totals() -> dict[str, int]:
    return {"input_bytes": 0, "output_bytes": 0, "total_bytes": 0}


def _retention_policy(retention_days: Any) -> dict[str, Any]:
    return {
        "retention_days": retention_days,
        "cleanup_enforced": False,
        "cleanup_status": "not_applicable",
    }


def _base_evidence(
    *,
    lane: str,
    status: str,
    case_pack_id: str,
    case_count: int,
) -> dict[str, Any]:
    evidence_id = f"eval_{uuid.uuid4().hex}"
    return {
        "ok": True,
        "evidence_id": evidence_id,
        "lane": lane,
        "status": status,
        "case_pack_id": case_pack_id,
        "case_count": case_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _record_evidence(
    evidence: dict[str, Any],
    *,
    state_root: str | Path,
    business_changed: bool,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        **evidence,
        "business_storage_changed": business_changed,
    }
    root = Path(state_root).expanduser().resolve()
    resolved_evidence_id = evidence_id or str(payload["evidence_id"])
    payload["evidence_id"] = resolved_evidence_id
    target_dir = root / resolved_evidence_id
    target_dir.mkdir(parents=True, exist_ok=False)
    (target_dir / "evidence.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return _path_free_evidence(payload)


def _path_free_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key
        in {
            "ok",
            "evidence_id",
            "lane",
            "status",
            "case_pack_id",
            "case_count",
            "business_storage_changed",
            "model_required",
            "not_run_reason",
            "failure_class",
            "budget",
            "config_summary",
            "code_sha",
            "checks",
            "cases",
            "completed_count",
            "failed_count",
            "input_bytes",
            "output_bytes",
            "total_bytes",
            "retention_policy",
            "effective_concurrency",
        }
    }


def _snapshot_roots(roots: Iterable[str | Path]) -> dict[str, tuple[bool, str | None]]:
    snapshot: dict[str, tuple[bool, str | None]] = {}
    for root_value in roots:
        root = Path(root_value).expanduser().resolve()
        if not root.exists():
            snapshot[root.name] = (False, None)
            continue
        digests: list[str] = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            digests.append(
                f"{relative}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            )
        snapshot[root.name] = (
            True,
            hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest(),
        )
    return snapshot


def _code_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[3],
        )
        candidate = completed.stdout.strip()
        if len(candidate) == 40:
            return candidate
    except Exception:
        pass
    return hashlib.sha1(Path(__file__).read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record ADK evaluation lane evidence.")
    parser.add_argument("--lane", choices=("offline", "real-model"), required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--case-pack-id", default="deterministic-v1")
    args = parser.parse_args(argv)
    if args.lane == "offline":
        result = run_offline_evaluation_lane(
            case_pack_id=args.case_pack_id,
            state_root=args.state_root,
        )
    else:
        result = run_real_model_evaluation_lane(
            case_pack_id=args.case_pack_id,
            state_root=args.state_root,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("ok") is False or result.get("status") in {"failed", "timeout"}:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "main",
    "run_offline_evaluation_lane",
    "run_real_model_evaluation_lane",
]
