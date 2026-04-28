"""OpenAI planner configuration for the minimal governed workflow."""

from __future__ import annotations

from typing import Any

DEFAULT_PLANNER_CONFIG = {
    "runtime": "openai-agents",
    "use_real_api": True,
    "model": "gpt-4.1-mini",
    "workflow_name": "ai-actuary-governed-workflow",
    "tracing_disabled": False,
}


def build_openai_run_config(
    *,
    workflow_name: str | None = None,
    tracing_disabled: bool | None = None,
    trace_metadata: dict[str, Any] | None = None,
    agents_module=None,
):
    agents_sdk = agents_module or _import_agents_sdk()
    run_config_cls = getattr(agents_sdk, "RunConfig", None)
    if run_config_cls is None:
        raise RuntimeError("OpenAI Agents SDK is missing RunConfig.")
    return run_config_cls(
        workflow_name=workflow_name or DEFAULT_PLANNER_CONFIG["workflow_name"],
        tracing_disabled=DEFAULT_PLANNER_CONFIG["tracing_disabled"] if tracing_disabled is None else tracing_disabled,
        trace_metadata=trace_metadata or {},
    )


def _import_agents_sdk():
    try:
        import agents  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI Agents SDK is required for Prompt 6. Install it with `pip install openai-agents` "
            "and set OPENAI_API_KEY before running the real planner workflow."
        ) from exc
    return agents
