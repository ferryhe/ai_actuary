# AI Actuary

AI Actuary is a local **Agentic Actuarial Workbench** prototype. It runs deterministic actuarial reserving tools, records every run as auditable artifacts, exposes a small FastAPI control plane, and provides a lightweight operator console for creating runs, inspecting evidence, handling reviews, rerunning cases, and exporting handoff reports.

The project’s operating model is simple:

**Agents plan and explain; actuarial tools calculate; human actuaries decide; artifacts provide audit and replay evidence.**

---

## Project Positioning

- **CAS Core** owns deterministic actuarial truth, governance rules, benchmark scoring, and artifact contracts.
- **OpenAI Planner** owns planning, routing, and governed orchestration.
- **Hermes Workers** own execution loops, artifact packaging, review handoff generation, and operator-facing runtime flows.

## Current Status

The current repo state is past the original CLI proof of concept. It includes:

- deterministic Chainladder reserving through `chainladder-python`
- governed single-case execution with OpenAI planner / Hermes worker boundaries
- CLI/file-artifact tools for the actuarial pipeline
- local JSON run registry and rerun tooling
- FastAPI control plane for runs, tools, workflows, reviews, artifacts, replay, repeatability, and report export
- lightweight `/console` operator UI with run creation, event polling, artifact/review panels, review decisions, rerun, and report export actions
- tool catalog with the current builtin `chainladder` tool
- workflow catalog with bounded local sequential execution
- independent review contract and review-decision artifacts
- prototype `operator_id` / `workspace_id` ownership metadata
- bounded OpenAI planner / Hermes worker adapter seam that uses public API surfaces only
- evidence-only operator handoff export
- cross-repo `actuarial-reserving.v1` schema/fixture compatibility package

Still intentionally out of scope:

- production queue workers, websocket/SSE streaming, or durable async orchestration
- auth, SSO/RBAC, enterprise multitenancy, or production workspace administration
- object storage / database-backed audit store
- production frontend build system
- broader actuarial method catalog beyond the current `chainladder` path
- optional HTTP calculator microservice or MCP adapter without a concrete caller

---

## Repository Layout

```text
.
├── benchmarks/                         # deterministic benchmark case packs
├── docs/                               # current docs plus archive
│   ├── archive/                         # historical plans/reports/prompts
│   ├── architecture.md                  # current architecture reference
│   ├── contracts/                       # control-plane and tool contracts
│   ├── operator_handoff.md              # report-export contract
│   ├── project-introduction.html        # standalone HTML overview and usage guide
│   └── project-plan.md                  # current scope and next steps
├── scripts/                             # operator CLI wrappers
├── schemas/actuarial-reserving/v1/      # exported JSON Schemas
├── src/reserving_workflow/              # core implementation
├── tests/                               # pytest suite and golden fixtures
└── workflows/agent-runtimes/            # OpenAI planner and Hermes worker adapters
```

Read in this order when taking over the project:

1. `README.md`
2. `docs/project-introduction.html`
3. `docs/architecture.md`
4. `docs/contracts/control-plane.md`
5. `docs/project-plan.md`
6. `docs/operator_handoff.md`
7. `docs/README.md`

Archived material is under `docs/archive/` and is retained for history only.

---

## Install

Use Python 3.11+.

```bash
git clone git@github.com:ferryhe/ai_actuary.git
cd ai_actuary
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

For governed OpenAI planner paths, provide `OPENAI_API_KEY` through your local `.env` or shell. Do not commit secrets.

```bash
set -a && [ -f ./.env ] && . ./.env && set +a
```

For API-only usage, `pip install -e '.[api]'` is enough; `dev` includes API dependencies and pytest.

---

## Quick Start: Local API and Console

Start the control plane:

```bash
python -m uvicorn 'reserving_workflow.api.app:create_app' --factory --host 127.0.0.1 --port 8000
```

Check readiness:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/preflight
```

Open the operator console:

```text
http://127.0.0.1:8000/console
```

The console can create runs, poll background events, inspect artifacts, view review state, submit review decisions, rerun recorded cases, and export operator handoff reports. It is intentionally a lightweight static shell over the API, not a production dashboard stack.

---

## Quick Start: Create a Run by API

Synchronous run:

```bash
curl -X POST http://127.0.0.1:8000/runs \
  -H 'content-type: application/json' \
  -d '{
    "case_id": "demo-case",
    "tool_id": "chainladder",
    "inputs": {
      "sample_name": "RAA",
      "method_variant": "chainladder"
    }
  }'
```

Background run:

```bash
curl -X POST http://127.0.0.1:8000/runs \
  -H 'content-type: application/json' \
  -d '{
    "case_id": "demo-bg",
    "tool_id": "chainladder",
    "inputs": {
      "sample_name": "RAA",
      "method_variant": "chainladder"
    },
    "background": true
  }'
```

Poll events:

```bash
curl http://127.0.0.1:8000/runs/<run_id>/events
```

Inspect artifacts and review state:

```bash
curl http://127.0.0.1:8000/runs/<run_id>/artifacts
curl http://127.0.0.1:8000/runs/<run_id>/review
```

Export operator handoff:

```bash
curl -X POST http://127.0.0.1:8000/runs/<run_id>/report-export
```

---

## Key API Routes

```text
GET  /health
GET  /health/preflight
GET  /console
GET  /console/state
GET  /tools
GET  /tools/{tool_id}
GET  /workflows
GET  /workflows/{workflow_id}
POST /runs
GET  /runs
GET  /runs/{run_id}
GET  /runs/{run_id}/events
POST /runs/{run_id}/rerun
GET  /runs/{run_id}/artifacts
GET  /runs/{run_id}/review-packet
GET  /runs/{run_id}/review
POST /runs/{run_id}/report-export
GET  /reviews
GET  /reviews/{review_id}
POST /reviews/{review_id}/decision
POST /replay
POST /repeatability
POST /benchmarks/batch
```

Current stable run statuses:

```text
accepted, queued, running, completed, needs_review, failed
```

Review decisions are separate from run status:

```text
approved, rejected, changes_requested
```

---

## Step-by-Step Operating Guide

### 1. Governed single-case run

```bash
python scripts/run_governed_case.py \
  --case-id demo-case \
  --artifact-dir ./tmp/demo-case \
  --registry-path ./tmp/run-registry.json
```

Inspect the run directory starting with `run_manifest.json`.

### 2. Force a review-required run

```bash
python scripts/run_governed_case.py \
  --case-id review-case \
  --artifact-dir ./tmp/review-case \
  --registry-path ./tmp/run-registry.json \
  --review-threshold-origin-count 5
```

When governance escalates, inspect `review_packet.json` and `review_packet.md`.

### 3. List, show, and rerun recorded runs

```bash
python scripts/list_runs.py --registry-path ./tmp/run-registry.json
python scripts/show_run.py --registry-path ./tmp/run-registry.json --run-id <run_id>
python scripts/rerun_case.py \
  --registry-path ./tmp/run-registry.json \
  --run-id <run_id> \
  --artifact-dir ./tmp/rerun-case
```

A rerun always creates a distinct new run and preserves the source run.

### 4. Export operator handoff report

```bash
python scripts/export_run_report.py \
  --registry-path ./tmp/run-registry.json \
  --run-id <run_id> \
  --review-store-dir ./tmp/reviews
```

Outputs are written from recorded evidence only:

- `operator_handoff.md`
- `reserve_summary.json`
- `reserve_summary.md`

Missing reserve facts remain explicit missing values; the exporter must not fabricate numeric results.

### 5. Batch benchmark

```bash
python scripts/run_batch_benchmark.py \
  --case-pack deterministic-v1 \
  --artifact-root ./tmp/batch-run \
  --registry-path ./tmp/batch-run/run-registry.json
```

Inspect:

- `comparison_report.json`
- `batch_manifest.json`
- per-run `run_manifest.json` files

### 6. Replay and repeatability

```bash
python scripts/replay_case.py \
  --manifest-path ./tmp/demo-case/run_manifest.json

python scripts/compare_repeatability.py \
  --manifest-path ./tmp/repeat-a/run_manifest.json \
  --manifest-path ./tmp/repeat-b/run_manifest.json
```

---

## CLI Tool Contract Surface

The v1 file-artifact tool layer is the stable bridge for orchestrators such as `ai_interface`:

```text
case_input.json
  -> chainladder-calc -> deterministic_result.json
  -> narrative-draft -> narrative_draft.json
  -> constitution-check -> constitution_check.json
  -> review-generator when review is required -> review_packet.json + review_packet.md
  -> report-export -> operator_handoff.md + reserve_summary.*
```

Side tools:

```text
run_manifest.json -> replay-run -> replayed_result.json
[run_manifest.json, ...] -> repeatability-check -> stability_report.json
```

Runnable module entry points:

```text
python -m reserving_workflow.tools_cli.chainladder_calc
python -m reserving_workflow.tools_cli.narrative_draft
python -m reserving_workflow.tools_cli.constitution_check
python -m reserving_workflow.tools_cli.review_generator
python -m reserving_workflow.tools_cli.replay_run
python -m reserving_workflow.tools_cli.repeatability_check
python -m reserving_workflow.tools_cli.report_export
```

Schema and fixture compatibility are pinned under:

- `schemas/actuarial-reserving/v1/`
- `tests/fixtures/tool_contracts/`
- `docs/contracts/tool-contract-compatibility-suite.md`

Regenerate schema/compatibility manifests only for intentional contract changes:

```bash
PYTHONPATH=src python scripts/export_contract_schemas.py
PYTHONPATH=src python scripts/export_contract_compat_manifest.py
PYTHONPATH=src python -m pytest tests/test_contract_schema_export.py tests/test_tool_contract_compat_manifest.py -q
```

---

## Artifact Model

A governed run writes local evidence under the chosen artifact directory. Common files include:

```text
case_input.json
validated_input.json
deterministic_result.json
narrative_draft.json
constitution_check.json
review_packet.json              # when review is generated
review_packet.md                # when review is generated
review_decision.json            # when a review decision is submitted
review_decision.md              # when a review decision is submitted
operator_handoff.md             # when report export runs
reserve_summary.json            # when report export runs
reserve_summary.md              # when report export runs
run_manifest.json
```

Read `run_manifest.json` first. It is the run-level index. Registry files are operational indexes; artifacts are the audit evidence.

---

## Development and Verification

Run the full suite:

```bash
python -m pytest tests -q
```

Smoke API route availability:

```bash
python - <<'PY'
from fastapi.testclient import TestClient
from reserving_workflow.api.app import create_app
client = TestClient(create_app())
assert client.get('/health').json()['ok'] is True
paths = client.get('/openapi.json').json()['paths']
required = [
    '/console', '/console/state', '/tools', '/tools/{tool_id}',
    '/workflows', '/workflows/{workflow_id}', '/runs', '/runs/{run_id}',
    '/runs/{run_id}/events', '/runs/{run_id}/rerun',
    '/runs/{run_id}/artifacts', '/runs/{run_id}/review-packet',
    '/runs/{run_id}/review', '/runs/{run_id}/report-export',
    '/reviews', '/reviews/{review_id}', '/reviews/{review_id}/decision',
    '/replay', '/repeatability', '/benchmarks/batch',
]
missing = [p for p in required if p not in paths]
assert not missing, missing
print('api_smoke_ok', len(paths))
PY
```

For console changes, also start uvicorn and click through `/console` in a browser. TestClient alone is not enough for UI/JavaScript regressions.

---

## Human Responsibilities vs Agent Responsibilities

Human actuary:

- chooses case, objective, and acceptable operating assumptions
- reviews deterministic outputs and governance packets
- submits approval / rejection / changes-requested decisions
- signs off on business use of results

Agent / system:

- converts requests into bounded tool or workflow plans
- calls public API/CLI surfaces instead of modifying internal files directly
- runs deterministic tools and governance checks
- writes artifacts and manifests
- summarizes evidence without fabricating missing facts

---

## Next Recommended Work

1. Define artifact retention and persistence beyond local files.
2. Add the next actuarial tool behind the same `ToolRegistry` and artifact-contract pattern.
3. Expand deterministic benchmark coverage and CI-grade replay/repeatability/report-export checks.
4. Add outbound delivery adapters for review packets and handoff reports.
5. Plan production queue/storage/auth/observability as separate, narrow PRs.

Do not add a dedicated HTTP calculator adapter or MCP adapter until there is a concrete caller and deployment need.
