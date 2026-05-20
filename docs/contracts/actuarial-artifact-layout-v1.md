# Actuarial Artifact Layout v1

This document defines the **artifact-oriented layout** for the PR1 tool-contract surface.

## Principles

- Artifact exchange is **file-first** in v1.
- Relative artifact names are preferred inside manifests where possible.
- `run_manifest.json` is the canonical index for a run directory.
- JSON artifacts are the machine contract; markdown artifacts are operator-facing derived views.
- `Review.schema.json` describes a control-plane review record, not the standalone `review_packet.json` evidence artifact.
- HTTP or MCP transport may wrap these artifacts later, but they do not replace the artifact contract.

## Canonical single-run layout

```text
<run-root>/
├── case_input.json
├── deterministic_result.json
├── narrative_draft.json
├── constitution_check.json
├── review_packet.json              # when review is generated
├── review_packet.md                # when review is generated
├── operator_handoff.md             # when report export runs
├── reserve_summary.json            # when report export runs
├── reserve_summary.md              # when report export runs
└── run_manifest.json
```

## Canonical artifact identifiers

| Artifact ID | Filename | Producer |
|---|---|---|
| `case_input` | `case_input.json` | operator/orchestrator input |
| `deterministic_result` | `deterministic_result.json` | `chainladder-calc` |
| `narrative_draft` | `narrative_draft.json` | `narrative-draft` |
| `constitution_check` | `constitution_check.json` | `constitution-check` |
| `review_packet` | `review_packet.json` | `review-generator` |
| `review_packet_markdown` | `review_packet.md` | `review-generator` |
| `operator_handoff` | `operator_handoff.md` | `report-export` |
| `reserve_summary_json` | `reserve_summary.json` | `report-export` |
| `reserve_summary_markdown` | `reserve_summary.md` | `report-export` |
| `run_manifest` | `run_manifest.json` | artifact packager/orchestrator |

## Manifest contract

`run_manifest.json` is represented by `RunArtifactManifest` and should contain at minimum:

- `case_id`
- `run_id`
- `artifact_root`
- `artifact_paths`
- `created_by`
- `metadata`

Example:

```json
{
  "case_id": "golden-raa",
  "run_id": "golden-raa-20260520T120000Z",
  "artifact_root": ".",
  "artifact_paths": {
    "case_input": "case_input.json",
    "deterministic_result": "deterministic_result.json",
    "narrative_draft": "narrative_draft.json",
    "constitution_check": "constitution_check.json",
    "review_packet": "review_packet.json",
    "review_packet_markdown": "review_packet.md",
    "operator_handoff": "operator_handoff.md",
    "reserve_summary_json": "reserve_summary.json",
    "reserve_summary_markdown": "reserve_summary.md",
    "run_manifest": "run_manifest.json"
  },
  "created_by": "contract-fixture",
  "metadata": {
    "contract_version": "actuarial-reserving.v1"
  }
}
```

## Tool input/output expectations

### `chainladder-calc`

Inputs:
- `case_input.json`

Outputs:
- `deterministic_result.json`

### `narrative-draft`

Inputs:
- `case_input.json`
- `deterministic_result.json`

Outputs:
- `narrative_draft.json`

### `constitution-check`

Inputs:
- `case_input.json`
- `deterministic_result.json`
- `narrative_draft.json`
- optional `run_manifest.json`

Outputs:
- `constitution_check.json`

### `review-generator`

Inputs:
- `constitution_check.json`
- `deterministic_result.json`
- `narrative_draft.json`
- `run_manifest.json`

Outputs:
- `review_packet.json`
- `review_packet.md`

### `replay-run`

Inputs:
- `run_manifest.json`

Outputs:
- `replayed_result.json`

### `repeatability-check`

Inputs:
- one or more `run_manifest.json`

Outputs:
- `stability_report.json`

### `report-export`

CLI in current PR1 runtime:
- `python scripts/export_run_report.py --registry-path <run-registry.json> --run-id <run-id> --review-store-dir <review-store-dir> [--output-dir <dir>]`

Inputs:
- required `--registry-path` pointing to the run registry JSON file
- required `--run-id` identifying the recorded run
- required `--review-store-dir` for independent review records / decisions
- optional `--output-dir` override for export destinations
- resolved `run_manifest.json` loaded from the run artifact root referenced by the registry entry
- optional control-plane review record represented by `Review.schema.json`, materialized from review-store state

Outputs:
- `operator_handoff.md`
- `reserve_summary.json`
- `reserve_summary.md`

## Golden fixture intent in PR1

PR1 includes a deterministic illustrative golden run under:

`tests/fixtures/tool_contracts/golden_run/`

It is intended to:

- validate schema export assumptions
- provide stable cross-repo examples for future orchestration consumers
- prove that `narrative-draft` is a distinct artifact in the pipeline
- document the v1 artifact names without changing runtime behavior

## Non-goals

- No requirement that every current runtime path already writes every v1 artifact
- No HTTP payload contract beyond exported JSON Schemas already defined in Python
- No storage backend abstraction changes
