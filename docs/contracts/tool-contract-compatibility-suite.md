# Tool contract compatibility suite

This repository is the Python source of truth for `actuarial-reserving.v1` tool contracts. The cross-repo compatibility suite pins the schema, fixture, artifact ID, and canonical pipeline surface that `ai_interface` consumes without reimplementing actuarial calculations in TypeScript.

## Portable compatibility manifest

`tests/fixtures/tool_contracts/actuarial_reserving_v1_compat_manifest.json` is a portable checksum manifest for downstream consumers. It contains:

- the contract version (`actuarial-reserving.v1`);
- all seven public tool IDs;
- required golden-run artifact IDs;
- SHA-256 and byte-size entries for exported JSON Schemas;
- SHA-256 and byte-size entries for golden fixture artifacts;
- the golden run's manifest-relative artifact path map;
- the canonical local pipeline fixture and its ordered tool IDs/output artifacts.

Regenerate it after an intentional contract fixture/schema change:

```bash
PYTHONPATH=src python scripts/export_contract_schemas.py
PYTHONPATH=src python scripts/export_contract_compat_manifest.py
PYTHONPATH=src python -m pytest tests/test_contract_schema_export.py tests/test_tool_contract_compat_manifest.py -q
```

The manifest intentionally uses repo-relative paths only, so `ai_interface` can copy the fixture package into its own tests and compare checksums/required IDs without depending on this checkout at runtime.

## What must not drift silently

A compatibility test should fail if any of these change without an explicit contract decision:

1. public tool IDs (`chainladder-calc`, `narrative-draft`, `constitution-check`, `review-generator`, `replay-run`, `repeatability-check`, `report-export`);
2. required golden-run artifact IDs (for example `deterministic_result`, `narrative_draft`, `constitution_check`, `operator_handoff`);
3. exported schema file set for `actuarial-reserving.v1`;
4. canonical pipeline step order or output artifact names;
5. fixture checksums.

## Bumping to v2

When a breaking change is required, do not mutate `actuarial-reserving.v1` in place. Instead:

1. add a new schema directory, for example `schemas/actuarial-reserving/v2/`;
2. add a new golden fixture package and compatibility manifest with `contractVersion: actuarial-reserving.v2`;
3. keep the v1 manifest/tests until downstream consumers have migrated;
4. update `ai_interface` to consume the v2 fixture package in its own PR;
5. document the migration notes and any artifact ID/schema changes in the PR body.

Non-breaking additions may extend v1 only when existing v1 fields, required artifact IDs, and fixture meanings stay backward compatible.
