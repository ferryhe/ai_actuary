# AI Actuary Architecture Overview

This repository currently operates as a three-layer governed workflow prototype:

1. **CAS Core** — deterministic actuarial truth, constitution rules, replay helpers, and benchmark comparison logic.
2. **OpenAI Planner** — governed routing and orchestration through the OpenAI Agents SDK.
3. **Hermes Workers** — task execution, artifact packaging, review flow generation, batch worker behavior, and operator-facing runtime entry.

## Current Operator Paths

- single-case governed CLI: `scripts/run_governed_case.py`
- batch benchmark CLI: `scripts/run_batch_benchmark.py`
- replay CLI: `scripts/replay_case.py`
- repeatability CLI: `scripts/compare_repeatability.py`
- local FastAPI control plane and lightweight console: `GET /console`, `POST /runs`, `GET /runs/{run_id}/events`, and `POST /runs/{run_id}/rerun`

## Local Control Plane / Console Boundary

The FastAPI control plane is a transport wrapper over the existing operator, registry, artifact, replay, repeatability, and batch helpers. The lightweight console follows a Symphony-style workspace shape: run queue, timeline, artifact panel, review panel, and action panel.

PR7 makes that shell minimally operational without changing the runtime boundary:

- Create governed runs from the console using the same `RunCreateRequest` JSON contract as `POST /runs`.
- Poll background lifecycle events through `GET /runs/{run_id}/events` instead of adding websocket/SSE or a production queue.
- Trigger reruns through the existing `POST /runs/{run_id}/rerun` API instead of duplicating rerun logic in the page.
- Keep the console text-contract first: `case_id`, `sample_name`, `method`, `background`, and optional `review_threshold_origin_count` are explicit API-facing fields.

The current actuarial calculation method remains `chainladder`. Future actuarial tools should attach to the same composable `method`/case-input contract so an agent can operate them through JSON/YAML/text instructions, not through UI-only special cases.

## Responsibility Split

### Human
- prepares environment and secrets
- launches the desired workflow CLI or local console action
- reads artifacts and review outputs
- decides approvals, follow-up, and code changes

### Agent
- routes and executes workflows
- writes artifact outputs and manifests
- generates review flow, replay, and comparison results
- keeps runtime behavior aligned with the contract boundary

## Current Boundaries

- CAS Core still owns numeric reserve truth.
- OpenAI planner still owns planning and orchestration only.
- Hermes workers still own execution plus local artifact production.
- Review flow exists today and produces `review_packet.json` and `review_packet.md`.
- Replay and repeatability exist today and are driven by `run_manifest.json`.

For fuller detail, see `docs/architecture.md` and `docs/project-plan.md`.
