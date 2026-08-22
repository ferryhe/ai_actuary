# AI Actuary Architecture Overview

AI Actuary is a local Agentic Actuarial Workbench prototype built around three durable layers:

1. **CAS Core** — deterministic actuarial truth, schemas, constitution rules, replay helpers, and benchmark comparison logic.
2. **OpenAI Planner** — bounded planning, routing, and explanation through agent-facing contracts.
3. **Hermes Workers** — execution loops, artifact packaging, review generation, and operator-facing runtime paths.

The central boundary remains unchanged: numeric reserve truth comes from deterministic tools, not from the language model.

## Current Operator Surfaces

- CLI single-case governed run: `scripts/run_governed_case.py`
- CLI batch benchmark: `scripts/run_batch_benchmark.py`
- CLI replay: `scripts/replay_case.py`
- CLI repeatability: `scripts/compare_repeatability.py`
- CLI report export: `scripts/export_run_report.py`
- CLI registry operations: `scripts/list_runs.py`, `scripts/show_run.py`, `scripts/rerun_case.py`
- Local FastAPI control plane: `GET /tools`, `GET /workflows`, `POST /runs`, `GET /runs/{run_id}/events`, review routes, replay/repeatability routes, and report export routes
- Lightweight console: `GET /console` and `GET /console/state`

## Current Product Shape

The repo now supports a local operator loop:

1. discover tools and workflows;
2. create a governed run from CLI, API, or console;
3. poll lifecycle events;
4. inspect artifacts and governance outputs;
5. materialize or review review-required runs;
6. submit independent review decisions;
7. rerun recorded cases without overwriting the source run;
8. export evidence-only operator handoff reports.

This is still a local prototype. It is not a production queue, SaaS console, enterprise auth system, or durable audit warehouse.

## Control Plane / Console Boundary

The FastAPI control plane is a transport wrapper over existing operator, registry, artifact, replay, repeatability, batch, review, workflow, and report helpers. It should not duplicate business logic.

The console is a static, offline-friendly operator shell over the same API. It renders run queue, timeline, artifact evidence, review inbox/decision form, rerun, and report-export actions without introducing a frontend build system.

## Tool and Artifact Boundary

The current builtin actuarial tool is `chainladder`.

The v1 file-artifact pipeline is:

```text
case_input.json
  -> chainladder-calc -> deterministic_result.json
  -> narrative-draft -> narrative_draft.json
  -> constitution-check -> constitution_check.json
  -> review-generator when review is required -> review_packet.json/md
  -> report-export -> operator_handoff.md + reserve_summary.*
```

Side tools:

```text
run_manifest.json -> replay-run -> replayed_result.json
[run_manifest.json, ...] -> repeatability-check -> stability_report.json
```

Future actuarial methods should plug into the same registry, schema, artifact, and workflow pattern rather than adding bespoke UI-only logic.

## Review and Report Boundary

Run execution status is separate from review decisions.

- Run status: `accepted`, `queued`, `running`, `completed`, `needs_review`, `failed`
- Review decision: `approved`, `rejected`, `changes_requested`

Report export is evidence-only. It reads registry data, manifests, deterministic artifacts, review packets, and review decisions; it does not fabricate missing reserve values.

## Human / Agent Split

Human actuary:

- defines objectives and assumptions;
- inspects evidence;
- decides approval, rejection, or change requests;
- signs off on business use.

Agent/system:

- plans bounded tool/workflow calls;
- calls public CLI/API surfaces;
- executes deterministic tools and governance checks;
- writes artifacts and manifests;
- summarizes evidence without inventing facts.

For full detail, see `docs/architecture.md`, `docs/adk-local-workbench.md`, `docs/contracts/control-plane.md`, and the historical `docs/archive/project-plan.md`.
