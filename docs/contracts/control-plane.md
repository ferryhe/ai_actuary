# Control-Plane Contracts

This document freezes the bounded operator-facing control-plane contract through PR15.

## Scope

These contracts apply to the local FastAPI control plane and the lightweight operator console.

- They define stable status and event literals for run tracking.
- They define the operator-visible tool catalog shape.
- They define the operator-visible builtin workflow catalog shape.
- They define the bounded agent-adapter plan and summary shapes used to call the control plane.
- They do not change planner routing, worker execution, or deterministic dispatch behavior.
- They do not add upload flows, workflow builders, SSO/RBAC, websocket/SSE, DB, queues, or object storage.

## Run Status

`Run.status` is frozen to:

- `accepted`
- `queued`
- `running`
- `completed`
- `needs_review`
- `failed`

The local JSON registry records only these status values.

## Prototype Ownership

PR13 adds bounded prototype ownership fields for per-actuary workspaces.

`Run` may now expose:

- `operator_id`
- `workspace_id`
- `created_by`

`POST /runs` may accept the same fields. When omitted, the control plane applies the single-user fallback:

- `operator_id = "local-actuary"`
- `workspace_id = "default-workspace"`
- `created_by = operator_id`

These fields are local control-plane metadata only. They do not add auth, RBAC, SSO, enterprise multitenancy, or external identity providers.

## Run Event Type

`RunEvent.type` is frozen to:

- `run.accepted`
- `run.queued`
- `run.running`
- `run.completed`
- `run.needs_review`
- `run.failed`
- `workflow.started`
- `workflow.completed`
- `workflow.needs_review`
- `workflow.failed`
- `workflow.step.started`
- `workflow.step.running`
- `workflow.step.completed`
- `workflow.step.needs_review`
- `workflow.step.failed`

Current API payloads also keep the legacy `event_type` field for compatibility. It mirrors `type`.

## ArtifactRef

`ArtifactRef` is the stable operator-facing artifact pointer:

- `artifact_id`
- `path`
- `label`
- `present`

Artifact lists are derived from `run_manifest.json`. The manifest remains the source of truth for artifact paths.

## Review Contract

`Review.status` is frozen to:

- `not_available`
- `not_required`
- `review_required`
- `review_decided`

`ReviewDecision.decision` is frozen to:

- `approved`
- `rejected`
- `changes_requested`

`Review` is an independent governance object. Review decisions do not mutate `Run.status`.

PR13 also keeps review ownership lightweight:

- `assigned_to` remains a prototype field only
- local review records may also expose `workspace_id`
- default review assignment may derive from `created_by` when a review record is materialized for an owned run

The local control plane now exposes:

- `GET /reviews`
- `GET /reviews/{review_id}`
- `GET /runs/{run_id}/review`
- `POST /reviews/{review_id}/decision`
- `POST /runs/{run_id}/report-export`

Decision submission persists an independent review record under the local review store and, when a run artifact root exists, writes deterministic `review_decision.json` and `review_decision.md` artifacts under that run root. These decision artifacts may be added to `run_manifest.json` as artifact refs, but the run terminal status remains execution-only.

`POST /reviews/{review_id}/decision` is hardened to local deterministic semantics:

- invalid decision values are rejected with HTTP 400
- the first accepted decision moves `Review.status` to `review_decided`
- reposting the same decision payload is idempotent and preserves the original decision timestamp
- reposting a different payload for an already-decided review returns HTTP 409
- review decision artifacts are exposed through review detail/list surfaces when the run artifact root exists

## Tool Catalog

PR8 adds a bounded tool catalog and local registry.

- `GET /tools` returns catalog summaries.
- `GET /tools/{tool_id}` returns full metadata and schema.
- The built-in catalog currently contains `chainladder`.

PR9 keeps `GET /tools` and `GET /tools/{tool_id}` as the discovery surfaces and upgrades `POST /runs` to accept a tool-backed invocation.

`RunCreateRequest` now accepts:

- `tool_id`
- `inputs`
- legacy top-level `method` as an alias

For the built-in `chainladder` tool, the normalized validated shape is:

- `tool_id = "chainladder"`
- `inputs.method_variant = "chainladder"`

Unknown `tool_id` values are rejected with HTTP 400.

The console now posts the tool-backed request shape while preserving the legacy `method` alias in the payload for compatibility.

Each created run also writes `validated_input.json`, and `run_manifest.json` must carry a `validated_input` artifact reference.

## Workflow Catalog

PR11 adds a bounded builtin workflow catalog.

- `GET /workflows` returns workflow summaries.
- `GET /workflows/{workflow_id}` returns one workflow definition with ordered steps.
- `POST /runs` now also accepts `workflow_id`.

The initial builtin workflow catalog contains:

- `chainladder-basic`

Workflow-backed runs keep the existing run lifecycle and execution modes (`inline` and local FastAPI background tasks only). They add workflow-level and step-level events into the same run timeline and write a top-level `run_manifest.json` that references workflow summary artifacts and per-step manifests.

## Agent Adapter Contract

PR14 adds a bounded agent-facing adapter contract for planner and Hermes runtime wrappers.

`AgentExecutionPlan` is frozen to:

- `case_id`
- `objective`
- `inputs`
- exactly one of `tool_id` or `workflow_id`
- optional `user_prompt`
- optional `operator_id`
- optional `workspace_id`
- optional `created_by`
- `background`

`AgentExecutionPlan` is a request-planning contract only. It does not contain deterministic results, review decisions, or direct artifact paths to write.

`AgentRunHandle` is the bounded create-run response shape consumed by agent adapters:

- `run_id`
- `case_id`
- `status`
- `summary`
- optional `execution_mode`

`AgentRunSummary` is the bounded polling/read-model shape:

- `run_id`
- `case_id`
- `status`
- `summary`
- `terminal`
- `event_count`
- `last_event_type`
- `artifact_ids`
- `review_status`
- `review_required`

Hermes/OpenAI/Codex adapters must use public HTTP surfaces only:

- `POST /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/review`

The adapter contract does not authorize direct writes to the artifact store, review store, or deterministic result files.

## Rerun Semantics

`POST /runs/{run_id}/rerun` is frozen to the following semantics:

- rerun always creates a distinct new `run_id`
- the source registry entry is preserved unchanged
- recorded `operator_params` are reused as the rerun base
- only `artifact_dir` and `review_delivery_dir` are overrideable through the current rerun request contract

The control-plane contract exposes these semantics through `RerunSemantics`.

## Store Boundary

PR10 keeps the current local storage behavior but moves it behind explicit interfaces:

- `RunStore` backs the JSON registry and remains an operational index only
- `ArtifactStore` backs filesystem artifacts and remains the evidence source for manifests and derived artifact refs
- `ReviewStore` is a local artifact-backed adapter for persistent review records and decisions
- the console may lazily materialize a review record from an existing `needs_review` run plus review packet

This boundary still does not add DB, object storage, queues, or auth.

## Report Export

PR15 adds a bounded operator handoff export surface.

- `POST /runs/{run_id}/report-export` builds report artifacts from recorded evidence only
- export inputs are limited to run registry data, `run_manifest.json`, deterministic artifacts, review packets, and independent review decisions
- export outputs are bounded to `operator_handoff.md`, `reserve_summary.json`, and `reserve_summary.md`
- missing reserve facts must remain explicit missing values rather than fabricated content

## Filtered List Surfaces

The bounded local API may filter list-style ownership views through static request metadata:

- `GET /runs?operator_id=...&workspace_id=...`
- `GET /reviews?operator_id=...&workspace_id=...`
- `GET /console/state?operator_id=...&workspace_id=...`

The console may also use prototype `x-operator-id` and `x-workspace-id` headers as offline/mock identity inputs.
