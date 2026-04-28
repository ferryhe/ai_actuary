# CAS Agent Governed Reserving Workflow Repository

This repository turns the proposal in [Proposal/CAS Research Proposal_ Agent-Governed Constitutional AI for Specialized P&C Reserving Workflows.md](Proposal/CAS%20Research%20Proposal_%20Agent-Governed%20Constitutional%20AI%20for%20Specialized%20P%26C%20Reserving%20Workflows.md) into an implementation ready project workspace.

The research goal is to build and evaluate a governed reserving workflow where:

- deterministic reserving calculations stay outside the LLM,
- an agent runtime, such as OpenClaw or Hermes, orchestrates the workflow and review gates through a portable adapter,
- an actuarial constitution enforces hard checks and escalation rules,
- benchmark cases support repeatable comparison across adaptation strategies.

## Current Submission Package

The final submission materials are:

- [CAS Research Proposal FINAL.pdf](Proposal/CAS%20Research%20Proposal%20FINAL.pdf)
- [CAS Proposal Deck FINAL.pdf](Proposal/CAS%20Proposal%20Deck%20FINAL.pdf)
- [Proposal source markdown](Proposal/CAS%20Research%20Proposal_%20Agent-Governed%20Constitutional%20AI%20for%20Specialized%20P%26C%20Reserving%20Workflows.md)
- [Proposal source HTML](Proposal/CAS%20Research%20Proposal_%20Agent-Governed%20Constitutional%20AI%20for%20Specialized%20P%26C%20Reserving%20Workflows.html)
- [Deck source HTML](Proposal/CAS%20Agent-Governed%20Constitutional%20AI%20Proposal%20Deck.html)

CAS calendar alignment:

- Proposal deadline: Monday, April 27, 2026.
- Researcher notification: Friday, June 12, 2026.
- Interim progress report and executive summary: Friday, August 28, 2026.
- Final paper and deliverables: Friday, October 2, 2026.

## Scope

This repo is structured for a research prototype, not a production system. It is designed to support five workstreams:

- benchmark design for synthetic reserving cases,
- agent workflow orchestration across example runtimes,
- hosted API and local LLM trial setup,
- constitutional checks and human review gates,
- evaluation, reporting, and reproducibility.

The model and runtime scope includes both common hosted model APIs and local LLM deployment patterns that may fit company environments. Local testing may require workstation or GPU access, local model serving, and OpenClaw or Hermes server resources. These costs are part of the proposal's agent workflow trials and token/API usage budget line.

## Repository Layout

```text
.
|-- Proposal/
|-- benchmarks/
|   |-- cases/
|   `-- rubrics/
|-- data/
|   `-- synthetic/
|-- docs/
|   |-- adr/
|   |-- architecture.md
|   |-- development.md
|   `-- project-plan.md
|-- notebooks/
|-- scripts/
|-- src/
|   `-- reserving_workflow/
|-- tests/
`-- workflows/
    `-- agent-runtimes/
```

## Key Documents

- [docs/project-plan.md](docs/project-plan.md): phased project plan from repo bootstrap through final deliverables.
- [docs/architecture.md](docs/architecture.md): target system architecture and workflow boundaries.
- [docs/development.md](docs/development.md): development workflow, repo conventions, and near term priorities.
- [docs/adr/0001-repo-scope.md](docs/adr/0001-repo-scope.md): baseline architectural decision for this repository.

## Delivery Plan

The implementation plan follows the CAS calendar:

1. June 12 to June 30, 2026: kickoff memo, Constitution v1, benchmark specification, agent workflow specification, and initial OpenClaw/Hermes adapter notes.
2. July 2026: beta workflow with logging, validated benchmark package, baseline scoring, and first agent runtime implementation.
3. August 1 to August 28, 2026: main experiments, expert review notes, interim progress report, executive summary, and preliminary result tables.
4. August 31 to September 25, 2026: final benchmark package, agent workflow package, example OpenClaw/Hermes materials, reproducibility package, and draft final report.
5. September 28 to October 2, 2026: final paper, final deliverables package, and presentation deck.

## Working Conventions

- Keep numeric reserving logic deterministic and separately testable.
- Treat model generated narrative as reviewable output, not as source of truth calculation.
- Log workflow states in a way that supports replay and human audit.
- Keep the actuarial workflow portable across agent runtimes; treat OpenClaw and Hermes as examples, not hard dependencies.
- Use synthetic or public data only unless a separate data governance decision is documented.

## Immediate Next Steps

1. Finalize the benchmark case schema in `benchmarks/cases/`.
2. Draft the first constitutional rule set in `workflows/agent-runtimes/`.
3. Define deterministic reserving adapters under `src/reserving_workflow/`.
4. Specify hosted API and local LLM test configurations, including GPU or workstation needs.
5. Build a minimal evaluation harness and test fixtures under `tests/`.
