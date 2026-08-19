# Source Data and Selected-Step Results Review Plan

## Objective

Make local source data a governed input to the actuarial tools, expose the selected run's actual step results in the operator console, and make data-validation exceptions and material period-over-period reserve movements reviewable. Preserve the current sample-driven workflow and artifact audit trail.

## Current Baseline

- The console creates a Chainladder run with `sample_name` (default `RAA`).
- The Chainladder tool accepts exactly one source today: `sample_name` or inline `triangle_rows`.
- Validation already checks required columns, finite numeric values, duplicate origin/development cells, and that `chainladder` can construct a triangle.
- The console's Primary Evidence sequence is `run_manifest`, `validated_input`, `deterministic_result`, `narrative_draft`, and `constitution_check`.
- The evidence panel lists every possible review/export artifact, including conditional artifacts that are correctly absent on a passing run.

## Scope and Non-Goals

In scope:

- A local, Git-ignored source-data folder and a CSV data-source contract.
- Tool-linked input validation and reproducible source metadata.
- A console results-review panel backed by the existing Primary Evidence artifacts.
- A recorded, human-approved exception path for eligible source-data validation failures.
- Comparison of a run with a selected, comparable prior approved run, with review escalation for configured reserve movements.
- Tests and documentation for all supported input paths.

Out of scope for the first increment:

- Direct upload to a remote service, spreadsheet parsing, database connectors, or multi-tenant storage.
- Automatic inclusion of confidential source data in Git.
- Excel or other non-CSV source formats in v1.
- Changing reserving methodology beyond the existing Chainladder tool.
- Treating a prior-period reserve as an actuarial ground-truth benchmark.

## Implementation Priorities

1. Backend contracts and reusable functions come first.
2. CLI and API surfaces must call the same source-ingestion and result-projection functions.
3. The frontend remains the current static HTML/CSS/JavaScript console; no frontend framework or build system is added.
4. The console renders backend payloads and never parses source files, computes actuarial metrics, or infers result status itself.

The two backend application boundaries are:

- `source_data`: safely list local CSV sources, validate and normalize an input, create the immutable run snapshot, and build a bounded preview/provenance payload.
- `run_results`: read Primary Evidence artifacts and return bounded, typed key-result and selected-evidence projections.

## Source Data Contract

### Local folder

`data/source/` is the local source-data root. Its `.gitignore` keeps data files out of commits; anonymized fixtures remain under `tests/fixtures/`.

### Input choices

Extend `ChainladderToolInput` so a run provides exactly one of:

1. `sample_name` — existing built-in Chainladder sample, such as `RAA`.
2. `triangle_rows` — existing inline row payload.
3. `source_file` — new CSV path relative to `data/source/`.

`source_file` must not accept an absolute path or resolve outside `data/source/`. CSV is the only new file type in v1. Column defaults remain `origin`, `development`, and `value`; `origin_column`, `development_column`, `value_column`, `index_column`, and `cumulative` apply consistently to inline and CSV data.

For backward compatibility, a request that omits all three source fields continues to default to `sample_name: RAA`. A request that explicitly supplies more than one source is rejected.

### CSV format

Each record describes one triangle cell:

```csv
origin,development,value
2022,12,100000
2022,24,150000
2023,12,120000
```

- `origin`: non-empty origin-period value.
- `development`: non-empty development-period value.
- `value`: finite number.
- `index_column`: optional grouping/index field when needed by the Chainladder library.
- `cumulative`: `true` by default; it must be explicitly set to `false` for incremental data.

### Atomic ingestion and input snapshot

The service reads an accepted CSV once, validates those bytes, computes a SHA-256 checksum, and writes an immutable normalized snapshot under the run's local artifact root. Background execution, rerun, and replay consume that snapshot rather than the original `data/source/` file.

The console input view reads a bounded preview from the snapshot: column names, row count, checksum, and the first 20 rows. The full snapshot remains available as a local artifact for an authorized human reviewer but is not embedded in `/console/state` or copied into the run registry.

### Required validation

The tool boundary, not the UI, must enforce:

1. Source selection exclusivity and safe relative-path resolution, including traversal and symlink escape checks.
2. File existence, regular-file check, `.csv` extension, UTF-8/UTF-8-BOM decoding, header presence, non-empty data, and documented file/row/column limits.
3. Required configured columns, trimmed non-empty origin/development values, finite numeric values, and no duplicate cells. When `index_column` is configured, uniqueness includes index, origin, and development, and the index value must be present.
4. Comparable development values and deterministic normalized ordering; source row order is not authoritative.
5. A cumulative decrease produces a visible validation warning by default because recoveries and corrections can be legitimate. A strict mode may promote the warning to an error.
6. Successful conversion to a Chainladder triangle, with its shape, source kind, warnings, and provenance recorded in `validated_input.json` without duplicating all raw rows.

Validation failures return a stable `validation_error` and identify the affected row/column without echoing source values or the whole file. An accepted run still writes a sanitized failed `run_manifest.json` for auditability, but it writes no deterministic, narrative, governance, review, or export result artifacts.

### Validation exception review

The default remains fail closed: a source-data validation failure blocks deterministic calculation. An exception is an explicit, auditable human decision, not an automatic continuation.

For an eligible validation failure, the service may create `validation_review_packet.json` and `.md`. The packet contains the stable reason codes, source provenance, configured column mapping, warnings, and a bounded safe preview; it never contains a full confidential source file or raw error echo. A reviewer must submit an `approved`, `rejected`, or `changes_requested` decision with reviewer identity, rationale, decision timestamp, and an optional expiry. Only an active `approved` decision permits the specified run to continue, and the decision is recorded in `validation_decision.json` and `.md` under that run's artifact root.

The following failures are not eligible for override: unsafe or out-of-root paths, an unreadable or non-CSV file, missing header or required configured columns, empty input, non-finite values, duplicate triangle cells, missing required index values, or inability to construct a Chainladder triangle. These are data-integrity or boundary failures that must be corrected at source. The eligibility of any other warning or validation rule is explicitly configured and recorded in the packet.

## Snapshot, Provenance, and Manifest Contract

Add these file-backed input artifacts to `run_manifest.json`:

```json
{
  "artifact_paths": {
    "source_data_snapshot": "source_data_snapshot.csv",
    "source_provenance": "source_provenance.json",
    "validation_review_packet": "validation_review_packet.json",
    "validation_decision": "validation_decision.json"
  }
}
```

`source_provenance.json` records the source filename, SHA-256 checksum, row count, normalized columns, column mapping, cumulative flag, warnings, and snapshot artifact ID. It contains no raw rows. The manifest remains an artifact index and does not duplicate tool outputs or source data.

The validation review and decision artifact paths are present only when an eligible exception is requested. The decision records the reason codes, reviewer, rationale, timestamp, expiry, and the exact source checksum and run ID it authorizes. It cannot authorize a different source snapshot or a later rerun.

No new runtime-step mapping or `steps` array is required for this panel. The displayed sequence follows the existing Primary Evidence artifact order, keeping operator and CLI artifact views consistent.

## Local Confidential-Data Policy

This v1 demo assumes source data is confidential and local:

- `data/source/`, `.env`, and `tmp/` are ignored by Git. Sanitized test fixtures are the only data committed.
- The full source snapshot is stored only inside the selected run's local artifact root and inherits local-user filesystem access. Deleting the run artifact root removes the retained snapshot.
- The run registry stores artifact references, source filename, checksum, row count, and validation status only; it does not store raw rows or the full normalized input payload.
- `/console/state`, validation errors, event summaries, manifests, and logs must not contain raw source rows or cell values.
- The bounded console preview is read from the selected run snapshot on demand and returns at most 20 rows. It must not be included in exported operator handoff reports.
- Inline `triangle_rows` remains backward compatible but is intended for sanitized fixtures and small non-confidential inputs. The console uses `source_file` for local confidential data.

## Backend CLI and API Functions

### Source data

Shared backend functions support both transports:

- List safe relative `.csv` files beneath the configured source root without returning file contents.
- Validate one source configuration and return column mapping, row count, checksum, warnings, and a maximum 20-row preview.
- Atomically ingest and snapshot the selected CSV when a run is accepted.
- Resolve the snapshot for background execution, rerun, and replay.

CLI delivery:

- Add a source-data validation command with stable JSON output for success and `validation_error` failure.
- Allow the existing pipeline/operator CLI paths to configure a source root and consume a case input containing `source_file`.
- Extend `scripts/show_run.py` with a results mode backed by the shared `run_results` projection.

API delivery:

- Add a read-only source-data listing endpoint.
- Add a validation/preview endpoint that returns only the bounded payload.
- Extend `POST /runs` to accept `inputs.source_file` and column/cumulative settings.
- Add `GET /runs/{run_id}/results` and reuse its projection in `/console/state`.

### Result projection

The results payload is based on persisted Primary Evidence and contains:

- `highlights`: run/governance status, method, latest diagonal, ultimate, IBNR, and review-required state when available.
- `source`: sample or source filename, row count, checksum, shape, cumulative flag, and warnings.
- `evidence`: the ordered Primary Evidence items, availability state, bounded display fields, and safe artifact references.
- `review`: review status and reason codes, without duplicating the review decision contract.

Missing values are represented as unavailable; the backend does not recompute them from other artifacts.

### Prior approved run comparison

After deterministic calculation, an operator may select a prior approved run as a comparison baseline. The system accepts the comparison only when the current and prior runs have the same configured portfolio/case mapping, reserving method, cumulative basis, and valuation convention. It compares `latest_diagonal`, `ultimate`, and `ibnr` using both absolute and percentage deltas.

Thresholds are configured per metric and may include an absolute amount, a percentage, or both. A breach creates the review reason `reserve_movement_threshold:<metric>` and generates `prior_run_comparison.json` and `.md`, which include current and prior values, deltas, thresholds, baseline run ID, source/provenance changes, and the agent-generated narrative. The ordinary review packet links this comparison so a human can approve, reject, or request changes after inspecting the evidence.

This is a period-over-period monitoring control, not an actuarial accuracy benchmark: a prior approved reserve is a review baseline, not a known ultimate or a replacement for benchmark cases with expected results. If no comparable approved baseline is selected, the result projection marks the comparison as unavailable; it does not fabricate a delta or automatically fail the calculation.

## Review Panel and Key Result Highlights

Enhance the existing Review Panel instead of adding a separate frontend application. The top of the panel shows compact key-result highlights for the selected run:

- Governance/run status.
- Deterministic method.
- Latest diagonal.
- Ultimate.
- IBNR.
- Review-required indicator, validation warnings, validation-exception decision, and prior-run comparison status.

Highlights are displayed whenever their Primary Evidence exists, including passing runs that do not require a review decision. Values come only from the backend `run_results` projection.

Below the highlights, add a Primary Evidence selector. Its sequence requires no additional persisted mapping:

- `run_manifest` — run identity, status, source provenance, and available artifacts.
- `validated_input` — data shape, column mapping, warnings, and the bounded source preview.
- `deterministic_result` — method, latest diagonal, ultimate, IBNR, and diagnostics.
- `narrative_draft` — narrative summary, cited values, and key points.
- `constitution_check` — governance status, hard constraints, review triggers, and guidance.
- `prior_run_comparison` — selected prior run, metrics, deltas, thresholds, and comparison outcome when configured.

The panel lists only Primary Evidence artifacts available for the selected run, in the existing artifact order. Each view uses a bounded allowlist of fields supplied by the backend; it never renders arbitrary JSON or raw logs inline.

- For a failed step, show the sanitized status/error recorded for that Primary Evidence item.
- Review-packet and decision controls appear only when the run requires review.
- A validation-exception packet and decision appear before deterministic results only when an eligible validation failure is submitted for review; calculation controls remain unavailable until approved.
- A movement-triggered review presents the comparison alongside the deterministic results and review packet; the operator can open the linked prior-run evidence without exposing its raw source rows.
- Exported handoff and reserve-summary artifacts appear once the report-export action has completed.
- Replace the current undifferentiated evidence-gap wording with `not produced`, `not applicable`, or `missing unexpectedly`, based on run status and artifact conditions.

The result and evidence views are read-only. Existing review-decision controls remain below them and are enabled only when a review record exists.

## Multi-PR Delivery Plan

All implementation PRs target `main`. Create each branch from the latest `main` after the preceding PR merges; do not use a long-lived stacked branch. The current `feature/source-data-results-review` branch contains the planning artifacts only.

### PR 1 — Shared CSV ingestion and provenance contract

- **Branch:** `feat/source-data-ingestion`
- **Target:** establish the shared backend `source_data` boundary without changing the console.
- **Work to do:**
- Add typed source configuration, provenance, preview, warning, and validation-error contracts.
- Add typed validation-review packet and decision contracts, including explicit non-overridable validation reason codes.
  - Implement safe source-root resolution, CSV decoding/parsing, limits, column validation, deterministic sorting, index-aware uniqueness, checksum, atomic snapshot, and redaction.
- Add `source_data_snapshot`, `source_provenance`, and conditional validation-review/decision artifact handling.
  - Preserve the omitted-source default of `RAA` and existing inline-row behavior.
  - Add sanitized fixtures and focused unit/contract tests.
- **Delivery:** a reusable backend service that can ingest the same CSV deterministically, blocks invalid data by default, and permits only recorded eligible exceptions while leaving existing API, CLI, and sample runs compatible.

### PR 2 — CLI and API source-data integration

- **Branch:** `feat/source-data-cli-api`
- **Target:** expose the PR 1 backend through supported CLI and API functions.
- **Work to do:**
  - Add CLI source-root configuration and a JSON source-validation command.
  - Support `source_file` in operator and tool-pipeline case inputs.
- Add API settings plus source-listing and validation/preview endpoints.
- Add endpoints to retrieve a validation-review packet and submit one immutable validation decision for an eligible failed run.
  - Extend `POST /runs` for file-backed inputs.
  - Make background execution, rerun, replay, and CLI execution consume the immutable snapshot.
  - Keep raw rows out of the registry, logs, errors, manifests, and API state.
- **Delivery:** the same CSV produces the same checksum, validation outcome, and deterministic result through CLI and API; an exception decision applies only to its recorded checksum and run; mutating the original after acceptance cannot change the run or rerun.

### PR 3 — Primary Evidence results API and CLI

- **Branch:** `feat/run-results-projection`
- **Target:** provide one bounded backend contract for result review before changing presentation.
- **Work to do:**
- Implement the shared `run_results` projection over existing Primary Evidence.
- Add key-result highlights for status, method, latest diagonal, ultimate, IBNR, review state, and warnings.
- Add a prior-approved-run comparison contract with compatibility checks, per-metric absolute/percentage thresholds, stable movement reason codes, and bounded comparison artifacts.
  - Add `GET /runs/{run_id}/results` and include the same payload in `/console/state`.
  - Extend `scripts/show_run.py` with a JSON results mode.
  - Implement `not produced`, `not applicable`, and `missing unexpectedly` using explicit producer/run-status conditions and backward-compatible behavior for older manifests.
  - Test completed, failed, review-required, reviewed, exported, and legacy runs.
- **Delivery:** CLI and API return equivalent result and comparison payloads without recalculation, arbitrary JSON rendering, or confidential row leakage; prior-period values are clearly presented as a review baseline rather than actuarial truth.

### PR 4 — Simple console source controls and Review Panel

- **Branch:** `feat/console-source-results-review`
- **Target:** render the PR 2 and PR 3 backend contracts in the existing console style.
- **Work to do:**
  - Add sample-versus-local-CSV selection, source file selector, column mapping, cumulative flag, and validation feedback to the create-run form.
- Add key-result highlight cards to the existing Review Panel.
- Add the Primary Evidence selector and bounded detail views.
- Add validation-exception and prior-run comparison views using the bounded backend contracts.
  - Keep review decisions and report-export actions in their current workflow.
  - Add API HTML/state tests and manually verify the main interactions in a browser.
- **Delivery:** an operator can select local CSV data, validate it, create a run, review key actuarial results and each Primary Evidence item, and complete review/export actions without a frontend build system.

## Program Completion Criteria

- Default `RAA`, inline rows, and existing manifests remain backward compatible.
- CSV-only file input works consistently through CLI and API.
- Accepted runs are reproducible from their immutable local snapshots.
- Invalid data cannot run without a recorded eligible validation exception; non-overridable data-integrity failures always remain blocked.
- No raw confidential rows appear in registry records, logs, errors, manifest metadata, `/console/state`, or exported reports.
- CLI and API result projections are contract-equivalent.
- A material prior-period movement creates reviewable comparison evidence with the selected baseline, configured thresholds, and explicit unavailable state when no comparable baseline exists.
- The Review Panel shows key highlights and every available Primary Evidence result for the selected run.

## Resolved Decisions

1. The source-data folder contains only local confidential files; only sanitized test fixtures may be committed.
2. CSV-only is sufficient for v1.
3. Each run retains a full immutable normalized snapshot locally. The console shows only a bounded preview for human review.
4. Results are shown for every available Primary Evidence item, using its existing artifact ID and order rather than a new runtime-step mapping.
5. Failed validation is blocked by default; only explicitly eligible failures can proceed under a recorded, checksum-bound human exception decision.
6. Prior approved runs provide a period-over-period review baseline only; they are not an actuarial accuracy benchmark.
