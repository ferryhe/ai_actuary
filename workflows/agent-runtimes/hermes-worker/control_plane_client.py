"""Hermes-side control-plane client that uses only public HTTP endpoints."""

from __future__ import annotations

import time
from typing import Any

import httpx

from reserving_workflow.contracts import (
    AgentExecutionPlan,
    AgentRunHandle,
    AgentRunSummary,
    ArtifactRef,
    Review,
    Run,
    RunEvent,
    is_terminal_run_status,
)


class HermesControlPlaneClient:
    """Thin client for the bounded FastAPI control-plane contract."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HermesControlPlaneClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def create_run(self, plan: AgentExecutionPlan) -> AgentRunHandle:
        payload = self._request_json("POST", "/runs", json=plan.to_run_create_payload())
        return AgentRunHandle.model_validate(
            {
                "run_id": payload["run_id"],
                "case_id": payload["case_id"],
                "status": payload["status"],
                "summary": payload.get("summary"),
                "execution_mode": payload.get("execution_mode"),
            }
        )

    def get_run(self, run_id: str) -> Run:
        payload = self._request_json("GET", f"/runs/{run_id}")
        return Run.model_validate(payload["run"])

    def get_run_events(self, run_id: str) -> list[RunEvent]:
        payload = self._request_json("GET", f"/runs/{run_id}/events")
        return [RunEvent.model_validate(item) for item in payload.get("events", [])]

    def get_run_artifacts(self, run_id: str) -> list[ArtifactRef]:
        payload = self._request_json("GET", f"/runs/{run_id}/artifacts")
        return [ArtifactRef.model_validate(item) for item in payload.get("artifacts", [])]

    def get_run_review(self, run_id: str) -> Review:
        payload = self._request_json("GET", f"/runs/{run_id}/review")
        return Review.model_validate(payload["review"])

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

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()
