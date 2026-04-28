"""Planner-side agent definitions for the offline runner skeleton.

This remains SDK-agnostic for Prompt 5; real OpenAI Agents wiring lands later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PlannerAgentRole:
    name: str
    responsibility: str
    outputs: tuple[str, ...]


PLANNER_AGENT_ROLES = [
    PlannerAgentRole(
        name="workflow_manager",
        responsibility="Own intake, route selection, and final collection of worker results.",
        outputs=("workflow_request", "planner_summary"),
    ),
    PlannerAgentRole(
        name="triage_router",
        responsibility="Decide baseline / governed / review-only mode without directly executing workers.",
        outputs=("route_decision",),
    ),
    PlannerAgentRole(
        name="review_router",
        responsibility="Escalate worker outcomes that require human review.",
        outputs=("review_signal",),
    ),
    PlannerAgentRole(
        name="narrative_planner",
        responsibility="Reserve space for future narrative instructions while leaving numeric truth to workers.",
        outputs=("narrative_guidance",),
    ),
]


def get_planner_agent_configs() -> list[dict[str, object]]:
    return [asdict(role) for role in PLANNER_AGENT_ROLES]
