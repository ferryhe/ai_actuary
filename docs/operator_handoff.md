# Operator Handoff

PR15 adds a bounded report export surface for operator handoff.

## Scope

The export is built from existing deterministic artifacts, `run_manifest.json`, local run-registry metadata, review packets, and independent review decisions.

The export writes:

- `operator_handoff.md`
- `reserve_summary.json`
- `reserve_summary.md`

These files are generated from recorded evidence only. They do not create new actuarial values, and they do not reclassify execution status into review status.

## Inputs

The export path reads:

- `run_manifest.json`
- `deterministic_result.json` when present
- `review_packet.json` when present
- review decisions from the local review store and run-root decision artifacts
- local run-registry metadata for ownership, timestamps, and execution status

## Boundaries

- Review decisions remain independent governance records.
- Run execution status remains `completed`, `needs_review`, or `failed`.
- Missing reserve metrics are marked as missing; the export must do not fabricate or infer missing numeric facts.
- The export does not add PDF, BI dashboard, queue, or workflow expansion.

## Operator Surfaces

- CLI: `scripts/export_run_report.py`
- API: `POST /runs/{run_id}/report-export`
- Console: action panel button for report export

## Output Shape

`reserve_summary.json` carries:

- deterministic reserve values that were actually present
- missing metric names for absent values
- source information pointing back to deterministic artifacts

`operator_handoff.md` carries:

- source `run_id` and `case_id`
- execution status and independent review status
- reserve summary values and explicit missing markers
- source artifact and review decision references
