# AI Actuary

AI Actuary is a local **Agentic Actuarial Workbench** prototype. It runs deterministic actuarial reserving tools, records every run as auditable artifacts, exposes a small FastAPI control plane, and provides a lightweight operator console for creating runs, inspecting evidence, handling reviews, rerunning cases, and exporting handoff reports.

The project’s operating model is simple:

**Agents plan and explain; actuarial tools calculate; human actuaries decide; artifacts provide audit and replay evidence.**

---

## Project Positioning

- **Calculation Core** owns deterministic actuarial truth, governance rules, benchmark scoring, and artifact contracts.
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
- tool catalog with builtin `chainladder` and `minimax_experience_study_tool` implementations
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
- the remaining model-specific experience-study tools and their cross-model comparison report
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
│   └── adk-local-workbench.md           # active local workbench/package/rollback guide
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
5. `docs/adk-local-workbench.md`
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

The deployable control-plane factory fails closed without all three local
capability secrets. Start it through the launcher described below so those
secrets are generated and injected into the two child environments without
being exposed in the browser:

```bash
pip install -e '.[dev,adk-dev]'
python scripts/run_local_workbench.py
```

For API-only local work in an environment without Google ADK:

```bash
pip install -e '.[dev]'
python scripts/run_local_workbench.py --disable-adk --smoke
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

## Local Dual-Interface Developer Workbench

The optional Google ADK Developer Web is a local development surface. It is
not installed by the default `dev` extra and is not part of the control-plane
runtime. Use Python 3.11 and install the independently pinned ADK extra:

```bash
pip install -e '.[dev,adk-dev]'
adk --version  # pinned: 2.7.1
python scripts/run_local_workbench.py
```

The launcher binds both development interfaces to loopback only and supports
stable public port flags:

```bash
python scripts/run_local_workbench.py --api-port 8123 --adk-port 8124
```

It generates independent in-memory `operator-console` and `adk-developer`
capability credentials. The browser receives only a short-lived server-side
session; the ADK client receives only its Bearer credential through the
launcher-owned child environment:

- Operator Console: `http://127.0.0.1:8000/console`
- ADK Developer Web: `http://127.0.0.1:8001`

Those are defaults only. Launcher readiness messages, diagnostics, ADK logo
text, and the Operator Console developer link use the actual configured ports.

To unlock the Console, choose **Request launcher handoff** in the browser and
paste the displayed handoff ID into the launcher's terminal prompt. The browser
and launcher exchange their private one-time values only in loopback JSON
bodies; no capability secret is placed in a URL, browser storage, static HTML,
or launcher output.

The Developer Web header is labeled `AI Actuary Developer (DEV)` and displays
the Operator Console return path using the actual configured API port.

Use `python scripts/run_local_workbench.py --smoke` to start both processes,
verify their health and discovery routes, and stop them. Use
`python scripts/browser_smoke_local_workbench.py --disable-adk` for a real
Playwright Chromium smoke of the API-only console when ADK is not installed.
The launcher refuses occupied ports, reports a missing ADK extra clearly, and
cleans up child processes on startup failure, child failure, Ctrl-C, or
SIGTERM.

ADK session and artifact state is explicitly isolated under ignored local
paths: `tmp/adk-dev/sessions/sessions.db` and `tmp/adk-dev/artifacts/`. It does
not write runtime state into `developer_workflows/` or any published workflow
directory. Developer cleanup is available through `ai-actuary-cleanup` and
preserves business registry, artifact, review, and benchmark state.

For installed-wheel usage, package resource checks, browser smoke evidence,
and cleanup target rules, see [ADK local workbench](docs/adk-local-workbench.md).

The code-first `ai_actuary_developer` agent retains Phase 2's 12 bounded,
path-free read tools and adds exactly four execution tools:
`start_workflow_run`, `wait_run`, `get_run_status`, and `summarize_run`.
Confirmed starts are restricted to the published `chainladder-basic` and
`chainladder-validated` catalog workflows and are forced into the isolated
`adk-development` workspace. The agent cannot call a direct actuarial tool,
make review decisions, export reports, rerun, replay, benchmark, or access
host paths. Its model is fixed to Gemini `gemini-2.5-flash`. Importing the agent and opening
Developer Web do not require credentials; chatting with it requires a Gemini
Developer API credential, for example local `GOOGLE_API_KEY` with
`GOOGLE_GENAI_USE_VERTEXAI=FALSE`. Do not commit credentials.

Compatibility is pinned to `google-adk==2.7.1` on Python 3.11. The code-first
app loads through ADK app-info and build-graph endpoints, and the local
workbench keeps ADK sessions, traces, artifacts, and diagnostics under ignored
launcher-owned state directories with owner-private permissions/ACLs. Visual
Builder / Agent Config YAML is not generated for this code-first agent (the
`/builder` view remains empty); draft validation/export uses the project-owned
Workflow Lab described below. ADK trace/evaluation surfaces are treated as
developer evidence: trace/correlation IDs are preserved for ADK-created runs,
while benchmark/evaluation artifacts stay in cleanup-eligible developer state
unless a business workflow explicitly exports them. Upgrade ADK only in a
dedicated compatibility PR.

This is local capability authentication, not production SSO/RBAC. The workbench
is not externally hosted, CORS-enabled, or a production deployment. Credential
transport and route classification are frozen in
`docs/architecture/adr-0003-local-capability-credential-transport.md`.

---

## ADK Workflow Lab (Phase 4)

The Workflow Lab validates isolated declarative ADK 2.7.1 drafts and exports a
deterministic candidate snapshot plus reviewable no-index patch:

```bash
python scripts/validate_adk_workflow.py tmp/adk-workflow-drafts/<app>
python scripts/export_adk_workflow_diff.py tmp/adk-workflow-drafts/<app> --check
```

Native ADK Visual Builder is intentionally not exposed: its 2.7.1 save path can
write the app root and its built-in assistant can write/delete arbitrary
project files. The project-owned fallback accepts only YAML under the ignored
draft root, runs handle-pinned preflight, safe YAML, executable-reference,
frozen schema, project policy, and offline model-free contract checks in that
order, then writes a new immutable server-owned export. It cannot edit
published workflows, choose an output path, mutate Git/index/catalog state, or
publish automatically.

Published declarative resources are package data and work from an installed
wheel through `importlib.resources`; Git diff export is explicitly unavailable
outside source-checkout mode. See [ADK Workflow Lab](docs/adk-workflow-lab.md)
and [ADR 0004](docs/architecture/adr-0004-adk-workflow-lab-builder-fallback.md).

---

## Quick Start: Create a Governed Operator Run

Start the dual-interface workbench with `python scripts/run_local_workbench.py`,
then open `http://127.0.0.1:8000/console`. The Console initially remains locked:

1. Choose **Request launcher handoff** in the browser.
2. Paste the displayed handoff ID into the launcher's terminal prompt. The ID
   is not a credential; the launcher keeps its private bootstrap out of the
   browser and ADK child.
3. Wait for the Console to reload with its short-lived Operator session.

In **Create Governed Run**, enter a `case_id`, keep `sample_name` set to `RAA`
for the bundled example, choose the required tool, and select **Create run**.
Background execution is enabled by default. Select the run from **Run Queue**
to follow its Timeline and inspect the result, artifact, and review panels.

For a completed eligible run, select **Export handoff report** in the Action
Panel. The Console supplies its server-side session, CSRF token, and exact
Origin automatically. Raw unauthenticated API mutations are intentionally not
supported; programmatic clients must implement the body-bootstrap/session,
CSRF, Host, and Origin contract frozen in ADR 0003.

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
GET  /runs/{run_id}/results
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

An ADK run found persisted but incomplete after control-plane restart is marked
`failed` with `recovery_state=stale`; the server never infers a terminal success
from missing in-memory work.

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

Verify the fail-closed credential transport and exhaustive route policy:

```bash
python -m pytest tests/test_control_plane_capabilities.py -q
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
