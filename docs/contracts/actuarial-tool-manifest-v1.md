# Actuarial Tool Manifest v1

This document defines the current **v1 CLI/file-artifact tool surface** for actuarial reserving decomposition.

## Scope and intent

- This manifest is the reviewable **contract surface**, not the implementation surface.
- **CLI + file artifacts are first-class in v1.**
- **HTTP and MCP adapters are optional later layers**, not prerequisites for the initial tool boundary.
- **Pydantic models and control-plane contracts are the source of truth.**
- Versioned JSON Schema exports under `schemas/actuarial-reserving/v1/` are the machine-readable contract consumed by TypeScript/orchestration layers.
- `narrative-draft` is a **first-class tool**. It is not folded into `chainladder-calc`.

## Versioning

- Contract version: `actuarial-reserving.v1`
- Runtime family: `python`
- Execution style: `cli`
- Artifact transport: local files, JSON payloads, markdown reports where declared

## Implementation status

- The contract surface is documented and exported under `schemas/actuarial-reserving/v1/`.
- Runnable Python module entrypoints now live under `reserving_workflow.tools_cli` for all seven tools in this manifest.
- Cross-repo compatibility is pinned by `tests/fixtures/tool_contracts/actuarial_reserving_v1_compat_manifest.json`.
- `python scripts/export_run_report.py` remains available as a compatibility wrapper for the legacy report-export CLI.

## Review contract boundary in v1

- `schemas/actuarial-reserving/v1/Review.schema.json` exports the existing **control-plane review record** contract.
- `review_packet.json` is a separate **run evidence packet artifact** emitted by `review-generator` when review is required.
- In v1, `review_packet.json` is **not declared to validate against** `Review.schema.json`.
- The control-plane `Review` object may embed packet content under its `packet` field, but that does not make the packet artifact itself a `Review` object.
- A dedicated `ReviewPacket` schema can be added later if the evidence packet needs its own strict machine contract.

## Canonical pipeline

```text
case_input.json
  -> chainladder-calc -> deterministic_result.json
  -> narrative-draft -> narrative_draft.json
  -> constitution-check -> constitution_check.json
  -> review-generator when review is required -> review_packet.json + review_packet.md
  -> report-export -> operator_handoff.md + reserve_summary.*
```

Side pipelines:

```text
run_manifest.json -> replay-run -> replayed_result.json
[run_manifest.json, ...] -> repeatability-check -> stability_report.json
```

## Common execution contract

All tools in this manifest follow these v1 assumptions:

- Execution kind: `cli`
- Input handoff: JSON files and explicit artifact paths
- Output handoff: JSON files, markdown files, exit code, and stdout/stderr logs
- Error contract: `tool_error.v1`
- Artifact root authority: `run_manifest.json` and declared relative artifact names

Common error semantics:

- exit code `0`: successful execution, declared outputs should exist
- non-zero exit code: execution failed, caller should inspect structured error JSON when present plus stderr/logs
- input validation failures must be distinguishable from execution failures in machine-readable output when runtime entrypoints are added later

## Tool definitions

### 1. `chainladder-calc`

```yaml
toolId: chainladder-calc
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.chainladder_calc
inputs:
  case_input:
    schemaRef: schemas/actuarial-reserving/v1/ReservingCaseInput.schema.json
    artifact: case_input.json
outputs:
  deterministic_result:
    schemaRef: schemas/actuarial-reserving/v1/DeterministicReserveResult.schema.json
    artifact: deterministic_result.json
errors:
  format: tool_error.v1
```

### 2. `narrative-draft`

`narrative-draft` is explicitly first-class because governance and review need a stable narrative artifact independent of deterministic numeric execution.

```yaml
toolId: narrative-draft
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.narrative_draft
inputs:
  case_input:
    schemaRef: schemas/actuarial-reserving/v1/ReservingCaseInput.schema.json
    artifact: case_input.json
  deterministic_result:
    schemaRef: schemas/actuarial-reserving/v1/DeterministicReserveResult.schema.json
    artifact: deterministic_result.json
outputs:
  narrative_draft:
    schemaRef: schemas/actuarial-reserving/v1/NarrativeDraft.schema.json
    artifact: narrative_draft.json
errors:
  format: tool_error.v1
```

### 3. `constitution-check`

```yaml
toolId: constitution-check
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.constitution_check
inputs:
  case_input:
    schemaRef: schemas/actuarial-reserving/v1/ReservingCaseInput.schema.json
    artifact: case_input.json
  deterministic_result:
    schemaRef: schemas/actuarial-reserving/v1/DeterministicReserveResult.schema.json
    artifact: deterministic_result.json
  narrative_draft:
    schemaRef: schemas/actuarial-reserving/v1/NarrativeDraft.schema.json
    artifact: narrative_draft.json
  run_manifest:
    schemaRef: schemas/actuarial-reserving/v1/RunArtifactManifest.schema.json
    artifact: run_manifest.json
    required: false
outputs:
  constitution_check:
    schemaRef: schemas/actuarial-reserving/v1/ConstitutionCheckResult.schema.json
    artifact: constitution_check.json
errors:
  format: tool_error.v1
```

### 4. `review-generator`

```yaml
toolId: review-generator
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.review_generator
inputs:
  constitution_check:
    schemaRef: schemas/actuarial-reserving/v1/ConstitutionCheckResult.schema.json
    artifact: constitution_check.json
  deterministic_result:
    schemaRef: schemas/actuarial-reserving/v1/DeterministicReserveResult.schema.json
    artifact: deterministic_result.json
  narrative_draft:
    schemaRef: schemas/actuarial-reserving/v1/NarrativeDraft.schema.json
    artifact: narrative_draft.json
  run_manifest:
    schemaRef: schemas/actuarial-reserving/v1/RunArtifactManifest.schema.json
    artifact: run_manifest.json
outputs:
  review_packet:
    artifact: review_packet.json
  review_packet_markdown:
    artifact: review_packet.md
errors:
  format: tool_error.v1
```

### 5. `replay-run`

```yaml
toolId: replay-run
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.replay_run
inputs:
  run_manifest:
    schemaRef: schemas/actuarial-reserving/v1/RunArtifactManifest.schema.json
    artifact: run_manifest.json
outputs:
  replayed_result:
    schemaRef: schemas/actuarial-reserving/v1/DeterministicReserveResult.schema.json
    artifact: replayed_result.json
errors:
  format: tool_error.v1
```

### 6. `repeatability-check`

```yaml
toolId: repeatability-check
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.repeatability_check
inputs:
  run_manifests:
    schemaRef: schemas/actuarial-reserving/v1/RunArtifactManifest.schema.json
    artifact: run_manifest.json
    cardinality: many
outputs:
  stability_report:
    artifact: stability_report.json
errors:
  format: tool_error.v1
```

### 7. `report-export`

```yaml
toolId: report-export
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.report_export
inputs:
  registry_path:
    type: local_json_file_path
    required: true
    cli_flag: --registry-path
    description: Path to the run registry JSON file used to resolve the run entry.
  run_id:
    type: string
    required: true
    cli_flag: --run-id
    description: Run identifier to export from the registry.
  review_store_dir:
    type: local_directory_path
    required: true
    cli_flag: --review-store-dir
    default: ./tmp/reviews
    description: Local review-store directory containing review records and decisions.
  output_dir:
    type: local_directory_path
    required: false
    cli_flag: --output-dir
    description: Optional export destination; defaults to the run artifact root resolved from the registry entry.
  resolved_run_manifest:
    schemaRef: schemas/actuarial-reserving/v1/RunArtifactManifest.schema.json
    required: true
    provenance: derived_from_registry_entry.artifact_root
    description: `report-export` loads `run_manifest.json` from the resolved run artifact root; callers do not pass the manifest as a direct CLI argument in v1.
  resolved_review_record:
    schemaRef: schemas/actuarial-reserving/v1/Review.schema.json
    required: false
    provenance: derived_from_review_store_plus_run_entry
    description: Control-plane review record materialized from review-store state and run evidence when present.
outputs:
  operator_handoff:
    artifact: operator_handoff.md
  reserve_summary_json:
    artifact: reserve_summary.json
  reserve_summary_markdown:
    artifact: reserve_summary.md
errors:
  format: tool_error.v1
```

## Source-of-truth schemas

PR1 exports versioned JSON Schema for these existing Pydantic/control-plane models:

- `ReservingCaseInput`
- `DeterministicReserveResult`
- `NarrativeDraft`
- `ConstitutionCheckResult`
- `RunArtifactManifest`
- `ToolInvocation`
- `Workflow`
- `Run`
- `RunEvent`
- `Review`

These schemas are exported by `scripts/export_contract_schemas.py` into `schemas/actuarial-reserving/v1/`.

## Non-goals for v1

- No runtime entrypoint changes
- No CLI implementation changes beyond schema export
- No FastAPI/HTTP contract expansion for these tools
- No MCP tool adapter
- No `ai_interface` integration changes
