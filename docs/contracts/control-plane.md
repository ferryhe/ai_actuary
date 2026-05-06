# Control-Plane Contracts

This document freezes the bounded operator-facing control-plane contract as of PR12.

## Scope

These contracts apply to the local FastAPI control plane and the lightweight operator console.

- They define stable status and event literals for run tracking.
- They define the operator-visible tool catalog shape.
- They define the operator-visible builtin workflow catalog shape.
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
- `workflow.failed`
- `workflow.step.started`
- `workflow.step.running`
- `workflow.step.completed`
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

The local control plane now exposes:

- `GET /reviews`
- `GET /reviews/{review_id}`
- `GET /runs/{run_id}/review`
- `POST /reviews/{review_id}/decision`

Decision submission persists an independent review record under the local review store and, when a run artifact root exists, writes deterministic `review_decision.json` and `review_decision.md` artifacts under that run root. These decision artifacts may be added to `run_manifest.json` as artifact refs, but the run terminal status remains execution-only.

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
