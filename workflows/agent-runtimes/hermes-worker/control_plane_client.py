"""Hermes compatibility facade over the shared control-plane client."""

from __future__ import annotations

import time

from reserving_workflow.adapters.control_plane import ReadOnlyControlPlaneClient
from reserving_workflow.contracts import (
    AgentExecutionPlan,
    AgentRunHandle,
    AgentRunSummary,
    is_terminal_run_status,
)


class HermesControlPlaneClient(ReadOnlyControlPlaneClient):
    """Keep the legacy import and run helpers while sharing transport/parsing."""

    def create_run(self, plan: AgentExecutionPlan) -> AgentRunHandle:
        payload = self._request_json("POST", "/runs", json=plan.to_run_create_payload())
        return self._validate_contract(AgentRunHandle, payload)

    def wait_for_terminal_run(
        self,
        run_id: str,
        *,
        poll_interval_seconds: float = 0.0,
        max_polls: int = 20,
    ) -> AgentRunSummary:
        if max_polls < 1:
            raise ValueError("max_polls must be at least 1")
        for attempt in range(max_polls):
            summary = self.summarize_run(run_id)
            if summary.terminal:
                return summary
            if poll_interval_seconds > 0 and attempt + 1 < max_polls:
                time.sleep(poll_interval_seconds)
        return summary

    def summarize_run(self, run_id: str) -> AgentRunSummary:
        run = self.get_run(run_id)
        events = self.get_run_events(run_id)
        artifacts = self.get_run_artifacts(run_id)
        review = self.get_run_review(run_id)
        return AgentRunSummary(
            run_id=run.run_id,
            case_id=run.case_id,
            status=run.status,
            summary=run.summary,
            terminal=is_terminal_run_status(run.status),
            event_count=len(events),
            last_event_type=(events[-1].type if events else None),
            artifact_ids=[artifact.artifact_id for artifact in artifacts],
            review_status=review.status,
            review_required=bool(run.review_required or review.review_required),
        )
