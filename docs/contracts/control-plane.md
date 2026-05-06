# Control-Plane Contracts

This document freezes the bounded operator-facing control-plane contract as of PR9.

## Scope

These contracts apply to the local FastAPI control plane and the lightweight operator console.

- They define stable status and event literals for run tracking.
- They define the operator-visible tool catalog shape.
- They do not change planner routing, worker execution, or deterministic dispatch behavior.
- They do not add upload flows, workflow builders, human review systems, auth, websocket/SSE, DB, or object storage.

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

`ReviewDecision` is frozen to:

- `not_required`
- `pending`
- `approved`
- `rejected`

The current control plane exposes review packet presence and packet metadata. It does not yet implement a persistent human review decision workflow.

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

## Rerun Semantics

`POST /runs/{run_id}/rerun` is frozen to the following semantics:

- rerun always creates a distinct new `run_id`
- the source registry entry is preserved unchanged
- recorded `operator_params` are reused as the rerun base
- only `artifact_dir` and `review_delivery_dir` are overrideable through the current rerun request contract

The control-plane contract exposes these semantics through `RerunSemantics`.
