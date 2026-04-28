"""Routing skeleton for offline planner execution."""

from dataclasses import asdict, dataclass
from typing import Any, Literal

ROUTE_MODES = ["baseline", "governed", "review_only"]


@dataclass(frozen=True)
class RouteDecision:
    mode: Literal["baseline", "governed", "review_only"]
    worker_action: str
    review_required: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def route_case_task(task: Any) -> RouteDecision:
    inputs = getattr(task, "inputs", {}) or {}
    requested_mode = str(inputs.get("mode", "governed")).lower()

    if requested_mode == "baseline":
        return RouteDecision(
            mode="baseline",
            worker_action="run_case_worker",
            review_required=False,
            reason="Task explicitly requested baseline routing.",
        )
    if requested_mode == "review_only":
        return RouteDecision(
            mode="review_only",
            worker_action="run_case_worker",
            review_required=True,
            reason="Task explicitly requested review-only routing.",
        )
    return RouteDecision(
        mode="governed",
        worker_action="run_case_worker",
        review_required=False,
        reason="Default governed route keeps planner and worker decoupled.",
    )
