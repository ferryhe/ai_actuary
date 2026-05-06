# Control-Plane Contracts

This document freezes the bounded operator-facing control-plane contract added in PR8.

## Scope

These contracts apply to the local FastAPI control plane and the lightweight operator console.

- They define stable status and event literals for run tracking.
- They define the operator-visible tool catalog shape.
- They do not change planner routing, worker execution, or deterministic dispatch behavior.
- They do not add PR9 tool-backed workflow execution, queueing, auth, websocket/SSE, DB, or object storage.

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

The console create-run form loads its selector from this catalog, but still posts the existing `method` field to `POST /runs`.

That means the catalog is a control-plane discovery surface only in PR8. It is not a new execution router.

## Rerun Semantics

`POST /runs/{run_id}/rerun` is frozen to the following semantics:

- rerun always creates a distinct new `run_id`
- the source registry entry is preserved unchanged
- recorded `operator_params` are reused as the rerun base
- only `artifact_dir` and `review_delivery_dir` are overrideable through the current rerun request contract

The control-plane contract exposes these semantics through `RerunSemantics`.
