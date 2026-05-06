# AI Actuary Project Plan Status

## Goal

Build a governed actuarial-agent workbench where:

- **CAS Core** remains the actuarial source of truth
- **OpenAI Planner** plans and routes bounded work
- **Hermes Workers** execute operator-facing loops and package artifacts
- **Control-plane contracts** expose runs, events, tools, workflows, reviews, reports, and reruns through stable text-first surfaces
- **Artifacts and review decisions** keep every operator action inspectable and replayable

One-line operating model:

**Agents can plan, explain, and route; actuarial tools calculate; human actuaries decide; artifacts provide audit evidence.**

---

## Current Stage

The repository is now a local **Agentic Actuarial Workbench** prototype through PR15.

It is no longer only a CLI proof of concept. It now has:

- governed single-case execution
- batch benchmark comparison
- replay and repeatability helpers
- local run registry and rerun tooling
- FastAPI control plane
- lightweight operator console
- tool catalog with `chainladder` as the first registered actuarial tool
- bounded workflow catalog and sequential workflow execution
- independent review contract and review decision artifacts
- prototype per-actuary workspace ownership metadata
- OpenAI planner / Hermes worker adapter seam over public APIs
- evidence-only report export and operator handoff artifacts

It is still intentionally local-first and prototype-grade. It does **not** yet claim to be a production queue, storage service, auth system, enterprise console, or full actuarial platform.

---

## Completed

### Prompt 1-3: repository and design foundation

- repository workspace bootstrapped
- upstream context materials imported
- core project positioning and staged design documented

### Prompt 4: Hermes worker baseline

- Hermes worker task/result contracts added
- single-case worker loop implemented
- artifact packaging baseline implemented

### Prompt 5-7: OpenAI planner and governed single-case path

- OpenAI planner runner skeleton added
- real OpenAI Agents SDK governed path wired
- review flow and review packet generation implemented
- operator-facing single-case CLI added via `scripts/run_governed_case.py`

### Prompt 8: batch benchmark runner

- batch benchmark runner implemented
- baseline vs governed comparison report implemented
- operator-facing batch CLI added via `scripts/run_batch_benchmark.py`

### Prompt 9: replay and repeatability

- replay helper implemented from `run_manifest.json`
- repeatability helper implemented across multiple manifests
- operator-facing replay CLI added via `scripts/replay_case.py`
- operator-facing repeatability CLI added via `scripts/compare_repeatability.py`
- artifact-path portability and incomplete-IBNR handling hardened

### Prompt 10: developer and operator handoff docs

- README rewritten as a developer/operator handoff document
- `docs/architecture.md` added as the current architecture reference
- `docs/project-plan.md` added as the current status and handoff plan
- architecture overview updated to match actual implemented scope
- step-by-step operator guidance and role split added to final docs

### PR1-PR3: stable local operator foundation

- stable single-run operator response contract
- structured failure semantics
- artifact storage boundary and local review delivery outbox
- local JSON run registry
- list/show/rerun scripts for recorded runs

### PR4: FastAPI control plane skeleton

- local FastAPI control-plane wrapper added
- API routes aligned with run/event/artifact/review/rerun semantics
- routes remain wrappers over existing operator, registry, artifact, replay, repeatability, and batch helpers
- TestClient coverage added for API contracts

### PR5: lightweight operator console shell

- `GET /console` serves a static, offline-friendly console shell
- `GET /console/state` exposes console-shaped state
- run queue, timeline, artifact panel, review panel, and action panel added
- console remains a thin view over existing control-plane contracts

### PR6: bounded background execution

- `POST /runs` supports `background=true`
- background mode returns accepted state immediately
- local FastAPI background tasks reuse the existing operator path
- `GET /runs/{run_id}/events` exposes polling-friendly lifecycle events

### PR7: actionable console

- console create-run form calls `POST /runs`
- background lifecycle polling works through `/runs/{run_id}/events`
- rerun action calls `POST /runs/{run_id}/rerun`
- console remains text-contract-first without a frontend build system

### PR8: foundation contracts and tool catalog

- control-plane contract frozen in `docs/contracts/control-plane.md`
- tool catalog added with `chainladder` as the first registered tool
- `GET /tools` and `GET /tools/{tool_id}` added
- console tool selector loads from the catalog with safe fallback behavior

### PR9: tool-backed run dispatch and input contracts

- `POST /runs` accepts `tool_id` plus `inputs`
- legacy top-level `method` remains as compatibility alias
- stable `tool_id` is separated from `inputs.method_variant`
- invalid tool/method variants are rejected at the boundary
- `validated_input.json` is written into run artifacts and preserved for reruns

### PR10: store boundary and local adapters

- `RunStore`, `ArtifactStore`, and `ReviewStore` interfaces added
- local JSON/filesystem adapters implemented
- compatibility wrappers preserve existing CLI/API behavior
- duplicate create and missing record cases hardened through focused tests

### PR11: workflow templates and sequential execution

- workflow schema and builtin workflow catalog added
- `GET /workflows` and `GET /workflows/{workflow_id}` added
- `POST /runs` can accept `workflow_id`
- bounded sequential workflow execution emits workflow and step events
- execution remains local `inline` / `local_background`; no production queue or DAG builder was added

### PR12: independent review contract and console inbox

- review is modeled as an independent governance object
- review decisions do not mutate `Run.status`
- `GET /reviews`, `GET /reviews/{review_id}`, `GET /runs/{run_id}/review`, and `POST /reviews/{review_id}/decision` added
- decision artifacts `review_decision.json` and `review_decision.md` are written from recorded review actions
- console Review Inbox and decision form added

### PR13: per-actuary workspace and ownership prototype

- runs can carry `operator_id`, `workspace_id`, and `created_by`
- local single-user fallback remains `local-actuary` / `default-workspace`
- console filters can narrow by operator and workspace
- review assignment remains a prototype metadata field, not RBAC

### PR14: agent planner and Hermes worker adapter seam

- bounded `AgentExecutionPlan`, `AgentRunHandle`, and `AgentRunSummary` contracts added
- OpenAI planner adapter produces request plans only
- Hermes control-plane client starts/polls/reads runs through public HTTP surfaces
- adapters do not write deterministic results, review decisions, or artifact-store records directly

### PR15: report export and operator handoff

- `scripts/export_run_report.py` added
- `POST /runs/{run_id}/report-export` added
- console action panel can trigger handoff export
- export writes `operator_handoff.md`, `reserve_summary.json`, and `reserve_summary.md`
- report output is evidence-only and never fabricates missing reserve values

---

## Current Usable Product Slice

A local operator or future agent worker can now do all of the following from this repository:

1. inspect available tools through `GET /tools`
2. inspect available workflow templates through `GET /workflows`
3. run one governed actuarial case through CLI or `POST /runs`
4. start a background run and poll `/runs/{run_id}/events`
5. use the console to create runs, inspect timeline/artifacts/reviews, rerun, and export reports
6. intentionally trigger review flow and inspect `review_packet.md`
7. list reviews and submit independent review decisions
8. run a batch benchmark comparison
9. inspect local artifacts through `run_manifest.json`
10. replay a saved deterministic run from artifacts
11. compare repeatability across multiple runs of the same case
12. rerun a recorded case while preserving the source run
13. export evidence-only operator handoff reports from recorded runs
14. call the control plane through the bounded OpenAI/Hermes agent adapter seam

---

## Current Important Contracts

### Run status

Execution status remains separate from review decisions.

Allowed run statuses:

- `accepted`
- `queued`
- `running`
- `completed`
- `needs_review`
- `failed`

Allowed review decisions:

- `approved`
- `rejected`
- `changes_requested`

### Tool invocation

The current builtin actuarial tool is:

```text
tool_id = "chainladder"
inputs.method_variant = "chainladder"
```

The legacy top-level `method` alias is preserved for compatibility, but new code should prefer `tool_id` plus `inputs`.

### Workflow invocation

The current builtin workflow catalog is discoverable through:

```text
GET /workflows
GET /workflows/{workflow_id}
```

Workflow-backed execution is intentionally sequential and local-only.

### Store boundary

The current stores remain local:

- `RunStore` over local JSON registry
- `ArtifactStore` over filesystem artifacts and manifests
- `ReviewStore` over local review records and decision artifacts

These are boundaries, not production storage claims.

### Agent boundary

Agent adapters may use public HTTP surfaces only:

- `POST /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/review`

They must not write deterministic results, review decisions, artifact files, or registry internals directly.

---

## Not Yet Implemented

### Infrastructure gaps

- persistent artifact store beyond local filesystem
- artifact retention, archival, and cleanup policy
- production queue worker or external service-backed execution runtime
- streaming event bus, websocket/SSE updates, or durable async orchestration
- production observability, tracing, and operator alerting

### Product gaps

- richer actuarial method catalog beyond the current `chainladder` path
- broader benchmark datasets and case catalogs
- formal sign-off workflow beyond independent local review decisions
- outbound messaging/delivery adapters for review packets and handoff reports
- production-grade web console frontend

### Governance and deployment gaps

- authentication, SSO, RBAC, and enterprise multitenancy
- production workspace administration
- durable audit store separate from local files
- CI-grade scheduled replay/repeatability and report-export regression checks
- documented deployment topology for a shared team server

---

## Next Recommended Steps

1. **Artifact persistence and retention**
   - define retention policy
   - preserve current manifest compatibility
   - keep artifacts as audit evidence rather than transient UI state

2. **Actuarial tool catalog expansion**
   - add the next deterministic actuarial tool behind the same `ToolRegistry` contract
   - avoid bespoke routes or UI-only tool wiring
   - add focused input/output schema tests for each new tool

3. **Benchmark and regression hardening**
   - expand benchmark case coverage
   - run replay/repeatability as scheduled or CI-grade checks
   - include report export in regression validation

4. **Outbound delivery adapters**
   - add post-packet and post-report delivery surfaces
   - keep delivery outside planner/core calculation logic
   - start with one concrete local or messaging destination

5. **Productionization planning**
   - design storage, queue, auth, and observability as separate PRs
   - keep each PR a single product slice
   - preserve the current run/event/artifact/review/report contracts as compatibility boundaries

---

## Step-by-Step Handoff Guide

### Human steps

1. install dependencies and load `.env` if running governed OpenAI paths
2. choose one workflow path: single-case, workflow-backed run, batch, replay, repeatability, review decision, rerun, or report export
3. launch the corresponding CLI, API call, or console action
4. inspect produced artifacts, starting with `run_manifest.json`
5. decide whether the run passes operational review or needs follow-up development

### Agent steps

1. create a bounded `AgentExecutionPlan` when agent planning is needed
2. call the public control-plane API instead of internal files/functions
3. poll events and read artifacts/review state through the API
4. summarize outputs without fabricating missing deterministic facts
5. hand off decisions to a human actuary or independent review object

### Recommended execution order for a new developer

1. run `python -m pytest tests -q`
2. start FastAPI locally and inspect `/console`
3. run one governed case through CLI or API
4. trigger one review case and inspect `review_packet.md`
5. submit one local review decision
6. export one operator handoff report
7. run one batch benchmark comparison
8. replay one manifest and compare repeatability across two manifests
9. only then start changing code

---

## Validation Command

```bash
python -m pytest tests -q
```

Current verified local state after PR15:

```text
135 passed
```

After that, choose the next change as a single-scope PR rather than reopening multiple architectural fronts at once.
