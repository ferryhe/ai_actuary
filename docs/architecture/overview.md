# AI Actuary Combined Architecture

This repo follows a three-layer composition model:

1. **CAS Core** — deterministic actuarial truth, constitution rules, benchmark schemas, artifacts.
2. **OpenAI Planner** — workflow planning, routing, guardrails, orchestration.
3. **Hermes Workers** — execution, packaging, notifications, recurring operations, process memory.

## Current Skeleton Scope

This branch only establishes the minimum project skeleton needed for later implementation.

The deterministic calculator boundary now explicitly targets **CAS official `chainladder-python`** as the underlying reserving tool, instead of reimplementing reserve methods inside this repo.

### CAS Core
- `src/reserving_workflow/schemas/core.py`
- `src/reserving_workflow/calculators/`
- `src/reserving_workflow/artifacts/`
- `src/reserving_workflow/constitution/`
- `src/reserving_workflow/review/`
- `src/reserving_workflow/evaluation/`

### OpenAI Planner
- `workflows/agent-runtimes/openai-agents/agents.py`
- `workflows/agent-runtimes/openai-agents/tools.py`
- `workflows/agent-runtimes/openai-agents/runner.py`
- `workflows/agent-runtimes/openai-agents/routing.py`
- `workflows/agent-runtimes/openai-agents/config.py`

### Hermes Workers
- `workflows/agent-runtimes/hermes-worker/task_contracts.py`
- `workflows/agent-runtimes/hermes-worker/case_worker.py`
- `workflows/agent-runtimes/hermes-worker/batch_worker.py`
- `workflows/agent-runtimes/hermes-worker/review_worker.py`
- `workflows/agent-runtimes/hermes-worker/artifact_packager.py`

## Intentionally Out of Scope for This Step

- real Hermes CLI/API integration
- benchmark execution logic
- artifact persistence workflow

The OpenAI planner side now includes a **minimal real OpenAI Agents SDK governed workflow entry point** for single-case execution, but it still keeps Hermes on the local callable adapter and does not yet cover review packets, batch workflows, or production runtime operations.

See `docs/plans/openai-hermes-composition-design.md` for the full design and phased project plan.
