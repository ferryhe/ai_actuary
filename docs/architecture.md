# AI Actuary Architecture

## Overview

This repository uses a three-layer composition model:

1. **CAS Core** — deterministic actuarial truth, governance rules, artifact contracts, and benchmark comparison logic.
2. **OpenAI Planner** — workflow planning, route selection, and governed orchestration through the OpenAI Agents SDK.
3. **Hermes Workers** — execution loops, artifact packaging, review packet generation, and operator-facing runtime paths.

The key boundary is stable:

- numeric truth must come from CAS Core
- planning and summarization can come from OpenAI
- execution and operational packaging belong to Hermes workers

The system is intentionally contract-first: worker outputs, review packets, manifests, and benchmark reports are represented as local artifacts rather than hidden in transient model state.

---

## Layer Breakdown

### CAS Core

Primary files:

- `src/reserving_workflow/schemas/core.py`
- `src/reserving_workflow/calculators/chainladder_adapter.py`
- `src/reserving_workflow/constitution/engine.py`
- `src/reserving_workflow/artifacts/replay.py`
- `src/reserving_workflow/evaluation/comparison.py`

Responsibilities:

- define the shared artifact contract and case schemas
- compute deterministic reserve outputs
- evaluate constitution and review rules
- replay saved runs from `run_manifest.json`
- compare repeatability and benchmark outputs

### OpenAI Planner

Primary files:

- `workflows/agent-runtimes/openai-agents/agents.py`
- `workflows/agent-runtimes/openai-agents/routing.py`
- `workflows/agent-runtimes/openai-agents/tools.py`
- `workflows/agent-runtimes/openai-agents/runner.py`

Responsibilities:

- decide route for a governed run
- invoke the worker tool boundary
- assemble final governed output
- keep the planner separate from deterministic numeric truth

### Hermes Workers

Primary files:

- `workflows/agent-runtimes/hermes-worker/task_contracts.py`
- `workflows/agent-runtimes/hermes-worker/case_worker.py`
- `workflows/agent-runtimes/hermes-worker/review_worker.py`
- `workflows/agent-runtimes/hermes-worker/batch_worker.py`
- `workflows/agent-runtimes/hermes-worker/artifact_packager.py`

Responsibilities:

- execute single-case tasks
- package artifacts under a stable local directory
- emit `run_manifest.json` for audit and replay
- generate review flow outputs when a case needs escalation
- support batch execution through a worker-style boundary

---

## Runtime Paths

### Single-case governed path

1. **Human:** choose case ID, artifact directory, and optional review threshold.
2. **Human:** start `scripts/run_governed_case.py`.
3. **OpenAI Planner:** routes the governed run.
4. **Hermes Worker:** executes deterministic calculation and governance checks.
5. **Hermes Worker:** writes artifacts to disk.
6. **Human:** inspects outputs and decides whether review or follow-up is needed.

When `--registry-path` is configured, the operator path also records a minimal local task lifecycle (`queued -> running -> completed|needs_review|failed`) in a JSON run registry. This registry is intentionally separate from the artifact contract: it indexes runs and their current state, while `run_manifest.json` and related artifacts remain the audit evidence.

The current operator CLI is intentionally contract-first. For single-case governed runs, the top-level JSON response now exposes a stable envelope:

- `ok`
- `status` (`completed`, `needs_review`, `failed`)
- `case_id`
- `run_id`
- `summary`
- `route`
- `trace`
- `worker_result`
- `final_output`
- `errors`
- `error_category`
- optional `review_packet`

This keeps operator-facing automation on a stable response surface even when planner/runtime failures occur before a usable business result exists.

### Batch benchmark path

1. **Human:** prepares a list of cases.
2. **Human:** starts `scripts/run_batch_benchmark.py`.
3. **Batch runner / workers:** execute `baseline_prompt` and `governed_workflow`.
4. **CAS Core:** scores comparison outputs.
5. **Human:** reads `comparison_report.json` and judges quality.

### Replay path

1. **Human:** points to one saved `run_manifest.json`.
2. **Human:** starts `scripts/replay_case.py`.
3. **Replay helper:** reloads the saved case and prior deterministic result.
4. **CAS Core:** recomputes the reserve output.
5. **Human:** compares saved and replayed summaries.

### Repeatability path

1. **Human:** passes multiple manifests for one logical case.
2. **Human:** starts `scripts/compare_repeatability.py`.
3. **Repeatability helper:** loads each run and compares statuses plus IBNR values.
4. **Human:** checks whether the result is complete and stable enough for trust.

### FastAPI control-plane path

The API layer in `reserving_workflow.api.app` is a transport/control-plane wrapper over the existing CLI-grade contracts. It does not replace the planner, worker, artifact, or registry layers.

PR4 routes are intentionally aligned to the future Symphony-style operator console shape:

- `GET /console` — serve a lightweight operator console shell without a frontend build system
- `GET /console/state` — return console-ready run cards, selected-run detail, timeline, artifact, review, and action panels
- `POST /runs` — start a governed single-case run through the existing operator entrypoint
- `GET /runs` — list registry-backed run summaries
- `GET /runs/{run_id}` — return registry detail, derived run events, artifact manifest, and review metadata
- `POST /runs/{run_id}/rerun` — rerun a recorded run through the existing registry/operator path
- `GET /runs/{run_id}/artifacts` — expose artifact manifest and artifact paths for an artifact panel
- `GET /runs/{run_id}/review-packet` — expose review packet metadata for a review panel
- `POST /replay` — wrap the existing replay helper
- `POST /repeatability` — wrap the existing repeatability helper
- `POST /benchmarks/batch` — wrap the existing batch benchmark runner

Derived events are mapped from registry `status_history` into `run.queued`, `run.running`, `run.completed`, `run.needs_review`, or `run.failed` event types. The PR5 console shell reuses those events and the existing artifact/review/rerun endpoints to present a thin operator-facing workspace. It remains a shell: no background queue, streaming transport, authentication layer, or separate business runtime is introduced.

---

## Artifact Contract

The artifact contract is the shared boundary between layers.

### Core files

- `run_manifest.json` — artifact index, run identity, and artifact root
- `case_input.json` — replayable case payload
- `deterministic_result.json` — numeric reserve result
- `constitution_check.json` — governance decision record
- `narrative_draft.json` — planner/worker-facing narrative draft stub

### Review flow files

- `review_packet.json`
- `review_packet.md`

### Review delivery path

When the operator configures `--review-delivery-dir`, the runtime copies generated review packets into a local outbox directory outside planner/core logic. This keeps review delivery as a post-packet adapter step rather than a planner concern.

The current concrete delivery target is:
- `local_outbox` — copies `review_packet.json` and `review_packet.md` into `<outbox>/<case_id>/<run_id>/`

### Batch file

- `comparison_report.json`

### Runtime state files

When local task-state tracking is enabled, the operator runtime also writes a JSON run registry chosen by `--registry-path`. It stores lightweight run indexing metadata such as:
- `task_id`
- `case_id`
- `run_id`
- `status`
- `created_at` / `updated_at`
- `artifact_root`
- `summary`
- `operator_params`
- `status_history`

The registry is not a replacement for manifests; it is a lightweight operational index layered on top of the artifact contract.

The system currently treats the local filesystem as the artifact store. That is a deliberate prototype constraint, not the final intended deployment architecture.

---

## Human Responsibilities vs Agent Responsibilities

### Human responsibilities

- define the operational objective
- provide environment, secrets, and run inputs
- launch the correct CLI path
- inspect review flow and benchmark outputs
- make approval, escalation, and code-change decisions

### Agent responsibilities

- orchestrate governed runs
- execute deterministic plus governance workflows through the worker boundary
- write artifact contract outputs
- produce replay and repeatability summaries
- keep runtime behavior aligned with the shared contract

### Boundary rules

1. do not move numeric truth into the planner
2. do not couple review delivery to planner logic
3. preserve the artifact contract when adding new runtimes
4. keep major workflows executable from simple CLIs
5. keep replay and repeatability usable outside the original working directory

---

## What Is Implemented Now

- governed single-case orchestration through the OpenAI planner path
- local Hermes-style worker execution
- review flow escalation and packet generation
- batch comparison between baseline and governed modes
- replay from saved manifests
- repeatability checks across multiple manifests
- FastAPI control-plane routes for runs, reruns, artifacts, review packets, replay, repeatability, and batch benchmarks
- lightweight operator console shell plus `/console/state` panel payload
- Symphony-style derived run events from the registry status history

## What Is Not Implemented Yet

- external artifact store or retention service
- production Hermes queue/runtime orchestration
- outbound messaging delivery of review packets beyond local outbox
- production operator web console with authentication, streaming updates, and multi-user state
- background execution, streaming event transport, and multi-user access control
- broader actuarial methods and richer benchmark suites
