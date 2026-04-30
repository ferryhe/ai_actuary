# AI Actuary

> A compositional actuarial-agent workspace built around **CAS Core + OpenAI Planner + Hermes Workers**.

---

## Project Positioning

This repository is a governed actuarial workflow prototype.

- **CAS Core** owns deterministic actuarial truth, governance rules, benchmark scoring, and artifact contracts.
- **OpenAI Planner** owns planning, routing, and governed orchestration.
- **Hermes Workers** own execution loops, artifact packaging, review handoff generation, and operator-facing runtime flows.

One-line model:

**OpenAI decides what to do, Hermes executes it, and CAS Core defines what is numerically correct.**

---

## Repository Layout

```text
.
├── benchmarks/
├── docs/
│   ├── architecture/
│   ├── plans/
│   └── reports/
├── prompts/
│   └── codex/
├── references/
│   └── upstream/
├── scripts/
├── src/
├── tests/
└── workflows/
```

### Key handoff docs

- `docs/architecture.md` — current three-layer architecture and runtime boundaries
- `docs/project-plan.md` — completed scope, remaining gaps, and next recommended steps
- `docs/architecture/overview.md` — short architecture summary for quick orientation
- `docs/plans/openai-hermes-composition-design.md` — full original design and phased roadmap
- `prompts/codex/step-by-step-prompts.md` — staged implementation prompt sequence

---

## Current Working Surface

### CAS Core

- `src/reserving_workflow/schemas/`
- `src/reserving_workflow/calculators/`
- `src/reserving_workflow/constitution/`
- `src/reserving_workflow/artifacts/`
- `src/reserving_workflow/evaluation/`

### OpenAI Planner

- `workflows/agent-runtimes/openai-agents/agents.py`
- `workflows/agent-runtimes/openai-agents/routing.py`
- `workflows/agent-runtimes/openai-agents/tools.py`
- `workflows/agent-runtimes/openai-agents/runner.py`

### Hermes Workers

- `workflows/agent-runtimes/hermes-worker/task_contracts.py`
- `workflows/agent-runtimes/hermes-worker/case_worker.py`
- `workflows/agent-runtimes/hermes-worker/review_worker.py`
- `workflows/agent-runtimes/hermes-worker/batch_worker.py`
- `workflows/agent-runtimes/hermes-worker/artifact_packager.py`

---

## Operator CLI Entry Points

All current operator-facing entry points are machine-readable JSON CLIs.

1. `scripts/run_governed_case.py`
2. `scripts/run_batch_benchmark.py`
3. `scripts/replay_case.py`
4. `scripts/compare_repeatability.py`
5. `scripts/list_runs.py`
6. `scripts/show_run.py`
7. `scripts/rerun_case.py`

---

## Step-by-Step Operating Guide

### A. Run one governed case

**Step 1 — Human:** prepare local environment.

```bash
cd /tmp/ai_actuary
pip install -e .
set -a && . ./.env && set +a
```

**Step 2 — Human:** start the single-case CLI.

```bash
python scripts/run_governed_case.py \
  --case-id demo-case \
  --artifact-dir ./tmp/demo-case \
  --registry-path ./tmp/run-registry.json
```

**Step 3 — Agent system:** the OpenAI planner routes the governed run and Hermes worker executes the case.

**Step 4 — Human:** inspect artifacts under `./tmp/demo-case/`, starting with `run_manifest.json`.

**Expected top-level CLI JSON fields**

- `ok` — boolean success flag; `false` means the operator/planner path failed before a usable governed result was produced
- `status` — stable single-run status: `completed`, `needs_review`, or `failed`
- `case_id`
- `run_id`
- `summary`
- `route`
- `trace`
- `worker_result`
- `final_output`
- `errors`
- `error_category`
- `review_packet` — present only when `status == "needs_review"`

**Failure semantics**

- `needs_review` means the workflow executed successfully but governance escalated the case
- `failed` means the run did not complete successfully
- worker-side invalid input failures expose structured metadata under `worker_result.worker_metadata`, including `failure_category`, `failure_stage`, and `error_type`

### B. Trigger review flow

**Step 1 — Human:** run the same workflow with a tighter review threshold.

```bash
python scripts/run_governed_case.py \
  --case-id review-case \
  --artifact-dir ./tmp/review-case \
  --review-threshold-origin-count 5
```

**Optional operator delivery outbox:**

```bash
python scripts/run_governed_case.py \
  --case-id review-case \
  --artifact-dir ./tmp/review-case \
  --review-threshold-origin-count 5 \
  --review-delivery-dir ./tmp/review-outbox
```

**Step 2 — Agent system:** the worker produces governance outputs and, when required, writes `review_packet.json` and `review_packet.md`.

**Step 3 — Human:** inspect `constitution_check.json`, then read `review_packet.md`.

### C. Run a batch benchmark comparison

**Step 1 — Human:** create `cases.json`, for example:

```json
[
  {"case_id": "batch-case-1", "sample_name": "RAA"},
  {"case_id": "batch-case-2", "sample_name": "RAA", "review_threshold_origin_count": 5}
]
```

**Step 2 — Human:** start the batch CLI.

```bash
python scripts/run_batch_benchmark.py \
  --cases-json ./cases.json \
  --artifact-root ./tmp/batch-run
```

**Step 3 — Agent system:** baseline and governed modes are executed and scored.

**Step 4 — Human:** inspect `./tmp/batch-run/comparison_report.json`.

### D. Replay one saved run

**Step 1 — Human:** point to a saved `run_manifest.json`.

```bash
python scripts/replay_case.py \
  --manifest-path ./tmp/demo-case/run_manifest.json
```

**Step 2 — Agent system:** replay loads saved artifacts and recomputes the deterministic result.

**Step 3 — Human:** compare `saved_summary` and `replayed_summary` in the JSON output.

### E. Compare repeatability across multiple runs

**Step 1 — Human:** collect two or more manifests for the same case.

```bash
python scripts/compare_repeatability.py \
  --manifest-path ./tmp/repeat-a/run_manifest.json \
  --manifest-path ./tmp/repeat-b/run_manifest.json
```

**Step 2 — Agent system:** repeatability loads each run and evaluates status plus IBNR stability.

**Step 3 — Human:** inspect `stable_ibnr`, `ibnr_values`, and `all_statuses`.

### F. Inspect the local run registry

**Step 1 — Human:** run one or more governed cases with a registry path.

```bash
python scripts/run_governed_case.py \
  --case-id registry-case \
  --artifact-dir ./tmp/registry-case \
  --registry-path ./tmp/run-registry.json
```

**Step 2 — Human:** list recorded runs.

```bash
python scripts/list_runs.py --registry-path ./tmp/run-registry.json
```

**Step 3 — Human:** inspect one run.

```bash
python scripts/show_run.py \
  --registry-path ./tmp/run-registry.json \
  --run-id operator-registry-case-local
```

**Step 4 — Human:** rerun a recorded case with optional overrides.

```bash
python scripts/rerun_case.py \
  --registry-path ./tmp/run-registry.json \
  --run-id operator-registry-case-local \
  --artifact-dir ./tmp/registry-case-rerun
```

---

## Artifact Model

A governed run writes local JSON artifacts under the chosen artifact directory.

### Standard artifacts

- `case_input.json`
- `deterministic_result.json`
- `narrative_draft.json`
- `constitution_check.json`
- `run_manifest.json`

### Review artifacts

When governance escalates the case, the run also writes:

- `review_packet.json`
- `review_packet.md`

### How to inspect artifacts

1. Read `run_manifest.json` first — it is the run-level index of produced files.
2. Read `deterministic_result.json` for reserve outputs.
3. Read `constitution_check.json` for governance status.
4. Read `review_packet.md` for the human-readable review handoff.

---

## Human Responsibilities vs Agent Responsibilities

### Human responsibilities

- prepare Python environment and secrets
- choose case IDs, artifact directories, and review thresholds
- launch CLI entry points
- inspect artifacts and make release/review decisions
- decide what changes should become commits and PRs

### Agent responsibilities

- plan and route governed execution through the OpenAI layer
- run deterministic calculation and governance checks through worker paths
- write artifacts and manifests
- generate review flow outputs when escalation is required
- produce replay, repeatability, and batch comparison outputs from artifact contracts

### Shared boundary

- humans own operational intent and approval
- agents own execution and artifact production
- CAS Core remains the numeric source of truth regardless of which agent runtime is active

---

## Environment Setup

```bash
cd /tmp/ai_actuary
pip install -e .
set -a && . ./.env && set +a
```

Minimum runtime requirements:

- Python environment with project dependencies installed
- `OPENAI_API_KEY` for governed planner runs
- repository checkout or editable install, because operator entrypoints load modules from `workflows/`

---

## Current Scope Status

### Completed

- Prompt 1-7: governed single-case workflow, review escalation path, and operator CLI
- Prompt 8: batch benchmark runner with baseline vs governed comparison
- Prompt 9: replay and repeatability helpers plus CLI wrappers
- Prompt 10: developer handoff documentation closeout

### Not Yet Implemented

- persistent artifact store beyond local filesystem
- outbound messaging/delivery of review packets
- production Hermes runtime orchestration instead of local callable worker modules
- richer actuarial methods, multi-dataset benchmark catalogs, and formal sign-off workflows
- service-layer HTTP API

### Next Recommended Steps

1. add artifact-store and retention strategy
2. add review-packet delivery adapters after packet generation
3. add HTTP/API surface only after current artifact contracts stabilize
4. expand benchmark coverage beyond the current sample-driven path
5. harden replay/repeatability into CI-grade regression workflows

---

## Validation Status

The repository has been validated through:

- unit and integration tests in `tests/`
- real OpenAI governed case smoke runs
- review-triggered governed run checks
- batch benchmark smoke runs
- replay and repeatability regression tests
- Hermes CLI acting as an operator against the repo

For the latest business-facing workflow memo, see:

- `docs/reports/current-workflow-report.md`

---

## Read This First If You Are Taking Over

1. `README.md`
2. `docs/architecture.md`
3. `docs/project-plan.md`
4. `docs/plans/openai-hermes-composition-design.md`
5. `docs/reports/current-workflow-report.md`

That reading order gives a new developer or future worker enough context to continue without rediscovering the current boundaries.
