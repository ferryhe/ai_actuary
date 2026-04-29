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

## Responsibility Split

### Human
- prepares environment and secrets
- launches the desired workflow CLI
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
