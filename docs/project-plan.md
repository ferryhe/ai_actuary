# AI Actuary Project Plan Status

## Goal

Build a governed actuarial-agent workflow where:

- **CAS Core** is the actuarial source of truth
- **OpenAI Planner** is the planning and orchestration layer
- **Hermes Workers** are the execution and operational layer
- **artifacts and contracts** keep the system inspectable and replayable

---

## Completed

### Prompt 1-3

- repository workspace bootstrapped
- upstream context materials imported
- core project positioning and staged design documented

### Prompt 4

- Hermes worker task/result contracts added
- single-case worker loop implemented
- artifact packaging baseline implemented

### Prompt 5-7

- OpenAI planner runner skeleton added
- real OpenAI Agents SDK governed path wired
- review flow and review packet generation implemented
- operator-facing single-case CLI added via `scripts/run_governed_case.py`

### Prompt 8

- batch benchmark runner implemented
- baseline vs governed comparison report implemented
- operator-facing batch CLI added via `scripts/run_batch_benchmark.py`

### Prompt 9

- replay helper implemented from `run_manifest.json`
- repeatability helper implemented across multiple manifests
- operator-facing replay CLI added via `scripts/replay_case.py`
- operator-facing repeatability CLI added via `scripts/compare_repeatability.py`
- artifact-path portability and incomplete-IBNR handling hardened

### Prompt 10

- README rewritten as a developer/operator handoff document
- `docs/architecture.md` added as the current architecture reference
- `docs/project-plan.md` added as the current status and handoff plan
- architecture overview updated to match actual implemented scope
- step-by-step operator guidance and role split added to final docs

---

## Not Yet Implemented

### Infrastructure gaps

- no persistent artifact store beyond local files
- no queue-backed or service-backed Hermes runtime
- no outbound notification or messaging delivery for review packets
- no HTTP API surface

### Product gaps

- no richer actuarial method catalog beyond the current deterministic path
- no expanded benchmark suite beyond the present sample-driven cases
- no formal reviewer decision workflow after packet generation
- no final memorandum/sign-off automation layer

### Hardening gaps

- replay and repeatability are local-CLI capable but not yet integrated into CI or scheduled regression runs
- artifact retention and naming conventions are still prototype-grade
- planner/runtime observability is minimal

---

## Step-by-Step Handoff Guide

### Human steps

1. install dependencies and load `.env`
2. choose one workflow path: single-case, batch, replay, or repeatability
3. launch the corresponding CLI script
4. inspect produced artifacts
5. decide whether the run passes operational review or needs follow-up development

### Agent steps

1. route the requested workflow
2. execute deterministic and governance logic through worker boundaries
3. write artifact outputs, including `run_manifest.json`
4. generate review flow, replay, or comparison outputs as required
5. return machine-readable JSON to the operator

### Recommended execution order for a new developer

1. run one governed case
2. trigger one review case and inspect `review_packet.md`
3. run one batch benchmark comparison
4. replay one manifest
5. compare repeatability across two manifests
6. only then start changing code

---

## Current Usable Product Slice

A new developer can currently do all of the following from this repository:

1. run one governed actuarial case
2. intentionally trigger review flow and inspect `review_packet.md`
3. run a batch benchmark comparison
4. inspect local artifacts through `run_manifest.json`
5. replay a saved deterministic run from artifacts
6. compare repeatability across multiple runs of the same case

---

## Next Recommended Steps

1. **Artifact store hardening**
   - define artifact retention strategy
   - abstract local filesystem assumptions behind a storage boundary
   - preserve compatibility with current `run_manifest.json`

2. **Review delivery adapters**
   - add post-packet delivery step
   - keep delivery outside the planner core
   - support at least one concrete operator destination

3. **HTTP/API surface after contract freeze**
   - expose single-case, batch, replay, and repeatability workflows only after current contracts stabilize
   - keep CLI and HTTP outputs aligned on the same response contracts

4. **Benchmark and actuarial expansion**
   - add more case catalogs
   - add more deterministic methods where needed
   - turn comparison outputs into a stable evaluation harness

5. **Operational hardening**
   - strengthen tracing/logging
   - add scheduled repeatability runs
   - add CI-friendly regression checks around replay and batch comparison artifacts

---

## Handoff Guidance

If you are picking up this repo next, read in this order:

1. `README.md`
2. `docs/architecture.md`
3. `docs/plans/openai-hermes-composition-design.md`
4. `prompts/codex/step-by-step-prompts.md`
5. `docs/reports/current-workflow-report.md`

Then verify the repo with:

```bash
python -m pytest tests -q
```

After that, choose the next change as a single-scope PR rather than reopening multiple architectural fronts at once.
