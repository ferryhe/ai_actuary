# AI Actuary

> A compositional actuarial-agent project built around **CAS Core + OpenAI Planner + Hermes Workers**.

---

## Project Positioning

This repository is a focused research and engineering workspace, not a generic skill-composition sandbox.

- **CAS Core** holds deterministic reserving logic, constitution rules, benchmark schemas, and artifact contracts.
- **OpenAI Planner** handles planning, routing, and governed orchestration through the OpenAI Agents SDK.
- **Hermes Workers** handle execution, packaging, operator flows, and future notification/runtime operations.

One-line model:

**OpenAI decides what to do, Hermes executes it, and CAS Core defines what is numerically correct.**

---

## Repository Layout

```text
.
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

### Key Files

- `docs/plans/openai-hermes-composition-design.md`
  - Full architecture, role boundaries, workflow design, and phased roadmap.
- `prompts/codex/step-by-step-prompts.md`
  - The implementation prompt sequence used to build the project incrementally.
- `docs/architecture/overview.md`
  - Concise architecture summary.
- `scripts/run_governed_case.py`
  - Operator-facing CLI entrypoint for a single governed case run.
- `references/upstream/cas/`
  - Proposal, architecture, and benchmark context from the CAS-side source materials.
- `references/upstream/openai-agents/`
  - OpenAI Agents SDK documentation snapshot.
- `references/upstream/hermes/`
  - Hermes documentation snapshot.

---

## What To Read First

1. `docs/plans/openai-hermes-composition-design.md`
2. `docs/architecture/overview.md`
3. `prompts/codex/step-by-step-prompts.md`
4. `docs/reports/current-workflow-report.md`
5. `references/upstream/*` when deeper context is needed

---

## Current Implementation Scope

The repository now contains a working minimum governed single-case flow.

### CAS Core

- Core schemas under `src/reserving_workflow/schemas/`
- Deterministic calculator adapter under `src/reserving_workflow/calculators/`
- Constitution engine under `src/reserving_workflow/constitution/`

### OpenAI Planner

- Real OpenAI Agents SDK governed workflow entrypoint
- Workflow manager agent definition
- Planner routing and tool wrappers
- Minimal tracing/run configuration

### Hermes Workers

- Worker task/result contracts
- Single-case worker loop
- Artifact packager
- Review packet worker

### Operator Entry

- A CLI operator can run a governed case directly from:
  - `scripts/run_governed_case.py`

---

## Current Capabilities

The current branch supports:

- deterministic reserving via the CAS `chainladder-python` boundary
- governed single-case execution through OpenAI Agents SDK
- worker-produced narrative draft stubs
- constitution checks with pass / fail / review-required states
- artifact generation for the case run
- review packet generation when review is triggered
- operator-facing CLI execution for pass and review scenarios

---

## Intentionally Out of Scope So Far

The current branch does **not** yet include:

- real Hermes CLI/API runtime integration for worker orchestration
- benchmark batch execution
- replay and repeatability hooks
- persistent artifact store beyond local files
- messaging-platform delivery of review packets

---

## Environment and Runtime Setup

### Minimum project setup

```bash
cd /tmp/ai_actuary
pip install -e .
export OPENAI_API_KEY=***
```

If you keep the key in a repository-local `.env` file:

```bash
cd /tmp/ai_actuary
set -a && . ./.env && set +a
```

### What must be configured when

- **OpenAI Agents SDK** is required from the governed planner stage onward.
- **Hermes full runtime integration** is not yet required for the current local callable worker flow.
- **Messaging / review delivery integration** becomes relevant after the review packet stage when packets need to leave the local filesystem.

---

## Operator CLI Usage

### Minimal governed pass run

```bash
cd /tmp/ai_actuary
set -a && . ./.env && set +a
python scripts/run_governed_case.py \
  --case-id demo-case \
  --artifact-dir ./tmp/demo-case
```

### Minimal review-triggered run

```bash
cd /tmp/ai_actuary
set -a && . ./.env && set +a
python scripts/run_governed_case.py \
  --case-id review-case \
  --artifact-dir ./tmp/review-case \
  --review-threshold-origin-count 5
```

### Output shape

The CLI returns JSON with the main fields below:

- `route`
- `worker_result`
- `final_output`
- `review_packet` when review is triggered

---

## Hermes CLI As Operator

Hermes itself can act as the operator for this repository.

Typical usage:

```bash
cd /tmp/ai_actuary
set -a && . ./.env && set +a
hermes chat -q "Run a governed case smoke test in this repository."
```

Hermes CLI must have:

- a working provider/model configuration for Hermes itself
- local terminal/file tool access
- access to the repository-local `OPENAI_API_KEY` when running this project workflow

Hermes global configuration paths on this machine:

- config: `/home/ec2-user/.hermes/config.yaml`
- env: `/home/ec2-user/.hermes/.env`

Project runtime env for this repository:

- `/tmp/ai_actuary/.env`

---

## Current Validation Status

The current implementation has been validated through:

- unit and integration tests in `tests/`
- real OpenAI Agents SDK governed workflow smoke runs
- operator CLI pass-path execution
- operator CLI review-path execution
- Hermes CLI acting as an operator on the repository

---

## Next Recommended Steps

1. benchmark batch runner and baseline comparison
2. replay and repeatability hooks
3. artifact-store hardening
4. Hermes/Feishu review-packet delivery integration

---

## Notes

This repository is meant to preserve a clean separation of responsibilities:

- **CAS Core** owns truth
- **OpenAI Planner** owns governed orchestration
- **Hermes Workers** own execution and operational packaging

That separation is the main architectural idea of the project.