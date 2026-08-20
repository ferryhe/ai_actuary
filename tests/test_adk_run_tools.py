from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from developer_workflows.ai_actuary_developer import tools
from reserving_workflow.runtime.adk_execution import summarize_adk_inputs


class FakeConfirmationContext:
    def __init__(self):
        self.invocation_id = "invocation-1"
        self.session = SimpleNamespace(id="session-1")
        self.state = {}
        self.tool_confirmation = None
        self.requests = []

    def request_confirmation(self, **kwargs):
        self.requests.append(kwargs)


class FakeExecutionClient:
    calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass

    def start_workflow_run(self, **kwargs):
        self.calls.append(kwargs)
        return {"run_id": "adk-run-1", "case_id": kwargs["case_id"], "status": "accepted", "operation_id": "op-1"}


def test_exact_tool_surface_retains_twelve_reads_and_adds_four_execution_tools():
    assert len(tools.READ_TOOL_NAMES) == 12
    assert tools.EXECUTION_TOOL_NAMES == (
        "start_workflow_run", "wait_run", "get_run_status", "summarize_run"
    )
    assert len(set(tools.READ_TOOL_NAMES + tools.EXECUTION_TOOL_NAMES)) == 16


def test_start_confirmation_has_zero_control_plane_side_effect_before_approval():
    FakeExecutionClient.calls.clear()
    context = FakeConfirmationContext()
    with tools.use_execution_client_factory(FakeExecutionClient):
        pending = tools.start_workflow_run(
            workflow_id="chainladder-basic", case_id="case-1",
            inputs={"sample_name": "RAA"}, tool_context=context,
        )
        assert pending["status"] == "confirmation_required"
        assert context.requests[0]["payload"]["workspace_id"] == "adk-development"
        assert context.requests[0]["payload"]["expected_artifact_types"]
        assert FakeExecutionClient.calls == []

        confirmation_payload = context.requests[0]["payload"]
        context.tool_confirmation = SimpleNamespace(
            confirmed=False, payload=confirmation_payload
        )
        rejected = tools.start_workflow_run(
            workflow_id="chainladder-basic", case_id="case-1",
            inputs={"sample_name": "RAA"}, tool_context=context,
        )
        assert rejected["status"] == "rejected"
        assert FakeExecutionClient.calls == []

        context.tool_confirmation = None
        assert tools.start_workflow_run(
            workflow_id="chainladder-basic", case_id="case-1",
            inputs={"sample_name": "RAA"}, tool_context=context,
        )["status"] == "confirmation_required"
        confirmation_payload = context.requests[-1]["payload"]
        context.tool_confirmation = SimpleNamespace(
            confirmed=True, payload=confirmation_payload
        )
        accepted = tools.start_workflow_run(
            workflow_id="chainladder-basic", case_id="case-1",
            inputs={"sample_name": "RAA"}, tool_context=context,
        )
        assert accepted["ok"] is True
        assert len(FakeExecutionClient.calls) == 1
        first_key = FakeExecutionClient.calls[0]["idempotency_key"]
        retried = tools.start_workflow_run(
            workflow_id="chainladder-basic", case_id="case-1",
            inputs={"sample_name": "RAA"}, tool_context=context,
        )
        assert retried["ok"] is True
        assert FakeExecutionClient.calls[-1]["idempotency_key"] == first_key


@pytest.mark.parametrize(
    ("workflow_id", "case_id", "inputs"),
    (
        ("chainladder-basic", "changed-case", {"sample_name": "RAA"}),
        ("chainladder-validated", "case-1", {"sample_name": "RAA"}),
        ("chainladder-basic", "case-1", {"sample_name": "RAA", "nested": {"x": 2}}),
    ),
)
def test_confirmed_start_rejects_exact_request_mutation_without_network_call(
    workflow_id, case_id, inputs
):
    FakeExecutionClient.calls.clear()
    context = FakeConfirmationContext()
    with tools.use_execution_client_factory(FakeExecutionClient):
        pending = tools.start_workflow_run(
            workflow_id="chainladder-basic",
            case_id="case-1",
            inputs={"sample_name": "RAA", "nested": {"x": 1}},
            tool_context=context,
        )
        assert pending["status"] == "confirmation_required"
        context.tool_confirmation = SimpleNamespace(
            confirmed=True, payload=context.requests[0]["payload"]
        )
        rejected = tools.start_workflow_run(
            workflow_id=workflow_id,
            case_id=case_id,
            inputs=inputs,
            tool_context=context,
        )
    assert rejected["error"]["code"] == "confirmation_context_mismatch"
    assert FakeExecutionClient.calls == []


def test_stale_confirmation_from_another_invocation_has_zero_network_calls():
    FakeExecutionClient.calls.clear()
    first = FakeConfirmationContext()
    with tools.use_execution_client_factory(FakeExecutionClient):
        tools.start_workflow_run(
            "chainladder-basic", "case-1", {"sample_name": "RAA"}, first
        )
        stale_payload = first.requests[0]["payload"]
        second = FakeConfirmationContext()
        second.invocation_id = "invocation-2"
        second.state = first.state
        second.tool_confirmation = SimpleNamespace(
            confirmed=True, payload=stale_payload
        )
        rejected = tools.start_workflow_run(
            "chainladder-basic", "case-1", {"sample_name": "RAA"}, second
        )
    assert rejected["error"]["code"] == "confirmation_context_mismatch"
    assert FakeExecutionClient.calls == []


def test_confirmation_payload_is_bounded_summary_not_raw_inputs():
    context = FakeConfirmationContext()
    raw_value = "sensitive-raw-value"
    result = tools.start_workflow_run(
        "chainladder-basic",
        "case-1",
        {"nested": {"value": raw_value}, "rows": [[1, 2], [3, 4]]},
        context,
    )
    assert result["status"] == "confirmation_required"
    payload = context.requests[0]["payload"]
    assert raw_value not in str(payload)
    assert "bounded_input_summary" in payload
    assert len(str(payload)) < 2_000


def test_maximum_key_summary_has_fixed_byte_and_key_caps_with_omission_digest():
    inputs = {
        f"key-{index:04d}-" + "x" * 100: f"secret-value-{index}"
        for index in range(400)
    }

    summary = summarize_adk_inputs(inputs)
    encoded = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert len(encoded) <= 2_048
    assert len(summary["top_level_shapes"]) <= 8
    assert summary["omitted_top_level_key_count"] == 392
    assert len(summary["input_digest"]) == 64
    assert "secret-value" not in encoded.decode("utf-8")


def test_wait_run_distinguishes_poll_timeout_from_business_status():
    class WaitingClient(FakeExecutionClient):
        def wait_run(self, **kwargs):
            return {"wait_outcome": "poll_timeout", "run_status": "running", "run_id": kwargs["run_id"]}

    with tools.use_execution_client_factory(WaitingClient):
        result = tools.wait_run("adk-run-1", timeout_seconds=0.1, poll_interval_seconds=0.05)
    assert result["ok"] is True
    assert result["data"]["wait_outcome"] == "poll_timeout"
    assert result["data"]["run_status"] == "running"


def test_status_and_summary_preserve_server_persisted_provenance_and_recovery_state():
    provenance = {
        "source": "adk-developer",
        "correlation_id": "corr-1",
    }

    class StatusClient(FakeExecutionClient):
        def get_run_status(self, run_id):
            return {
                "run_id": run_id,
                "case_id": "case-1",
                "status": "failed",
                "source": "adk-developer",
                "provenance": provenance,
                "recovery_state": "stale",
            }

        def summarize_run(self, run_id):
            return SimpleNamespace(
                model_dump=lambda **kwargs: {
                    "run_id": run_id,
                    "case_id": "case-1",
                    "status": "failed",
                }
            )

    with tools.use_execution_client_factory(StatusClient):
        status = tools.get_run_status("adk-run-1")
        summary = tools.summarize_run("adk-run-1")

    assert status["data"]["provenance"] == provenance
    assert status["data"]["recovery_state"] == "stale"
    assert summary["data"]["provenance"] == provenance
    assert summary["data"]["recovery_state"] == "stale"
