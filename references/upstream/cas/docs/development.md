# Development Guide

## Branch

Current primary branch: `main`

## Development Priorities

### Final Submission Cleanup

- keep final proposal and final deck files separate from history files,
- keep README and plan documents aligned with the final CAS calendar,
- avoid generating new PPTX or nonfinal PDFs unless they are explicitly needed.

### June 12 to June 30, 2026

- finalize schemas,
- draft Constitution v1,
- define workflow interfaces and artifact formats,
- prepare initial OpenClaw and Hermes adapter notes,
- add benchmark fixtures.

### July 2026

- scaffold deterministic adapters,
- wire workflow orchestration,
- implement hard checks and review gates,
- add baseline runners,
- start hosted model API and local LLM setup.

### August 2026

- run main experiments and ablations,
- collect expert review notes,
- prepare interim reporting materials by August 28,
- refine model and runtime test assumptions.

### September to October 2, 2026

- complete robustness checks,
- package benchmark, workflow, and reproducibility assets,
- finalize report, executive summary, and deck materials.

## Repository Rules

- Keep proposal scope stable unless a documented change is approved.
- Do not mix deterministic reserving logic with narrative generation logic.
- Keep agent runtimes replaceable; OpenClaw and Hermes are examples, not hard dependencies.
- Keep hosted model API and local LLM settings configurable and separate from actuarial logic.
- Prefer explicit schemas and typed boundaries for workflow inputs and outputs.
- Record major architecture decisions under `docs/adr/`.

## Suggested Workflow

1. Write or update the relevant spec first.
2. Add or modify code in `src/`.
3. Add tests in `tests/`.
4. Update documentation when behavior or structure changes.

## Testing Expectations

- unit tests for schema validation and deterministic adapters,
- integration tests for workflow stage handoffs,
- fixture based tests for constitutional checks and escalation rules.
- smoke tests for hosted API and local LLM routes using synthetic or public data only.

## Documentation Expectations

Before adding major implementation areas, document:

- purpose,
- inputs and outputs,
- assumptions,
- unresolved risks.

## Open Questions to Resolve Early

- exact integration pattern for OpenClaw assets in the repo,
- exact integration pattern for Hermes assets in the repo,
- whether a stronger agent runtime should be tested if it becomes available during the project,
- hosted model API selection and usage limits,
- local LLM serving pattern for company style environments,
- workstation, GPU, OpenClaw server, and Hermes server requirements,
- minimum portable adapter contract shared by both runtimes,
- chainladder adapter interface and local execution model,
- benchmark storage format and case versioning,
- audit artifact format for experiments and reviewer inspection.
