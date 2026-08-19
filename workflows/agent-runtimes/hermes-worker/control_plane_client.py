"""Hermes compatibility facade over the shared control-plane client."""

from __future__ import annotations

from reserving_workflow.adapters.control_plane import ReadOnlyControlPlaneClient
from reserving_workflow.contracts import (
    AgentExecutionPlan,
    AgentRunHandle,
)


class HermesControlPlaneClient(ReadOnlyControlPlaneClient):
    """Keep the legacy import and run helpers while sharing transport/parsing."""

    def create_run(self, plan: AgentExecutionPlan) -> AgentRunHandle:
        payload = self._request_json("POST", "/runs", json=plan.to_run_create_payload())
        return self._validate_contract(AgentRunHandle, payload)
