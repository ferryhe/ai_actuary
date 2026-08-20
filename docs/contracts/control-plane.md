# Control-Plane Contracts

This document freezes the bounded operator-facing control-plane contract for the current local prototype.

## Local Phase-3 capability boundary

The local launcher configures two independent principals. `operator-console`
uses a one-time, short-TTL browser bootstrap that becomes an HttpOnly
server-side session with CSRF and exact Host/Origin checks.
`adk-developer` uses only `Authorization: Bearer` from its launcher-owned
process environment. Caller identity/workspace/source fields are filters, not
authority. Run, event, artifact, projection, review, report, and review-ID
objects are checked against the persisted principal workspace/source; an
out-of-scope real ID is the same `404 object_not_found` as an absent ID.

An anonymous Console GET never issues a session. The browser generates a
memory-only private claim and shows a short-lived handoff ID; the operator
pastes that non-secret ID into the launcher terminal. The launcher approves it
with the bootstrap held outside the ADK environment, and only the matching
browser claim receives the session. Bootstrap and capability secrets are never
placed in browser JavaScript, URLs, browser storage, static HTML, or logs.

All live FastAPI method/path pairs are checked at startup against the
declarative capability matrix. Health and the static Console shell are the only
anonymous reads. FastAPI docs, ReDoc, and OpenAPI are disabled. The frozen
transport decision is [ADR 0003](../architecture/adr-0003-local-capability-credential-transport.md).

ADK workflow starts use `POST /adk/runs`, require the `adk-developer`
capability, a stable opaque `Idempotency-Key`, and a confirmation binding over
the canonical request. The server forces `source=adk-developer`,
`workspace_id=adk-development`, a server-managed artifact subtree, and
creation-time provenance. Only the two published built-in Chainladder workflows
are accepted; caller paths, workspace/source, draft digests, and correlation IDs
are rejected.

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

The local JSON registry records only these status values. An incomplete ADK run
found after restart is persisted as `failed` with `recovery_state=stale`; no
success is inferred.

## Prototype Ownership

The control plane includes bounded prototype ownership fields for per-actuary workspaces.

`Run` may now expose:

- `operator_id`
- `workspace_id`
- `created_by`

Legacy `POST /runs` payloads may contain the same fields, but an authenticated
principal overrides them. Deployable, embedded, and test callers all configure
capability credentials and use an authenticated principal; there is no
credentialless enforcement mode. Caller-supplied identity fields may only narrow
authorized results and never grant authority.

Legacy persisted records that omit `source` use `operator-console` semantics.
Records that also omit `workspace_id` use `default-workspace`; their historical
metadata remains readable under that trusted Operator scope.

These fields are local control-plane metadata only and never grant authority.
The local capability boundary is not SSO, enterprise RBAC/multitenancy, or an
external identity provider.

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

## Console Artifact Evidence Panel

`GET /console/state` keeps the existing `artifact_panel` keys for backward compatibility:

- `present`
- `artifact_root`
- `artifact_manifest`
- `artifact_paths`
- `artifacts`

The console exposes structured evidence fields so it can render a readable evidence panel without changing the underlying runtime contract:

- `status` — `ok`, `manifest_missing`, `manifest_unreadable`, or `no_run_selected`
- `primary_artifact_refs`
- `review_artifact_refs`
- `decision_artifact_refs`
- `evidence_items`
- `missing_expected_artifacts`
- `freshness`

The evidence panel is a thin derived view over `run_manifest.json`, known artifact filenames, and existing review/report files. It does not invent new artifact sources.

Safe path behavior for the new evidence fields is frozen to:

- keep legacy `artifact_manifest`, `artifact_paths`, and `artifacts` behavior unchanged for compatibility
- expose new evidence refs through safe relative refs such as `run_manifest.json` or `step_validate/run_manifest.json`
- avoid leaking new absolute local filesystem paths in the added console evidence fields when a relative ref or basename is sufficient

## Result Projection

`GET /runs/{run_id}/results` and the `result_panel` in `GET /console/state` expose the same controlled projection. Experience Study projections contain only the registered tool/model/method metadata, population and period, result count, whitelisted result fields, narrative summary, key points, and path-free safety errors. Missing source fields are rendered as `unavailable`; the projection never recalculates or guesses actuarial values.

The projection reads only JSON artifacts named in the selected run's `run_manifest.json`. Absolute paths, `..` traversal, symlink escapes, oversized JSON files, and excessive result arrays are rejected. Paths are resolved against the registry-backed run artifact root, and neither successful projections nor error payloads expose local filesystem paths. Chainladder and other tools without a registered projection return `not_available` without changing their existing Console evidence behavior.

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

Review ownership remains lightweight:

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

The control plane includes a bounded tool catalog and local registry.

- `GET /tools` returns catalog summaries.
- `GET /tools/{tool_id}` returns full metadata and schema.
- The built-in catalog currently contains `chainladder`.

`GET /tools` and `GET /tools/{tool_id}` are the discovery surfaces, and `POST /runs` accepts a tool-backed invocation.

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

The control plane includes a bounded builtin workflow catalog.

- `GET /workflows` returns workflow summaries.
- `GET /workflows/{workflow_id}` returns one workflow definition with ordered steps.
- `POST /runs` now also accepts `workflow_id`.

The initial builtin workflow catalog contains:

- `chainladder-basic`

Workflow-backed runs keep the existing run lifecycle and execution modes (`inline` and local FastAPI background tasks only). They add workflow-level and step-level events into the same run timeline and write a top-level `run_manifest.json` that references workflow summary artifacts and per-step manifests.

## Agent Adapter Contract

The control plane includes a bounded agent-facing adapter contract for planner and Hermes runtime wrappers.

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
- `GET /runs/{run_id}/results`
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

The current local storage behavior sits behind explicit interfaces:

- `RunStore` backs the JSON registry and remains an operational index only
- `ArtifactStore` backs filesystem artifacts and remains the evidence source for manifests and derived artifact refs
- `ReviewStore` is a local artifact-backed adapter for persistent review records and decisions
- the console may lazily materialize a review record from an existing `needs_review` run plus review packet

This boundary still does not add DB, object storage, queues, or auth.

## Report Export

The control plane includes a bounded operator handoff export surface.

- `POST /runs/{run_id}/report-export` builds report artifacts from recorded evidence only
- export inputs are limited to run registry data, `run_manifest.json`, deterministic artifacts, review packets, and independent review decisions
- export outputs are bounded to `operator_handoff.md`, `reserve_summary.json`, and `reserve_summary.md`
- missing reserve facts must remain explicit missing values rather than fabricated content

## Filtered List Surfaces

The bounded local API may filter list-style ownership views through static request metadata:

- `GET /runs?operator_id=...&workspace_id=...`
- `GET /reviews?operator_id=...&workspace_id=...`
- `GET /console/state?operator_id=...&workspace_id=...`

The console may also send `x-operator-id` and `x-workspace-id` as optional
narrowing filters under its authenticated principal; these headers never grant
identity or authority.
