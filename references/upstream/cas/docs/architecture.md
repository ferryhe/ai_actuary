# Architecture Overview

## Design Principle

This project treats the LLM as a governed component inside a reserving workflow, not as the source of actuarial truth. Agent runtimes such as OpenClaw or Hermes coordinate workflow execution through adapters; they do not own the actuarial logic.

## Target System

### 1. Intake Layer

- loss triangle inputs,
- scenario metadata,
- benchmark identifiers,
- run configuration.

### 2. Deterministic Calculation Layer

- chainladder style reserving calculations,
- reproducible numeric outputs,
- validation ready intermediate values.

### 3. Context and Retrieval Layer

- reserving policy context,
- benchmark notes,
- constitutional rule references,
- explanation context for narrative generation.

### 4. Narrative Layer

- explanation drafting,
- anomaly commentary,
- justification text for selected assumptions and exclusions.

### 5. Governance Layer

- hard constraints,
- soft guidance checks,
- review triggers,
- approval checkpoints,
- run logging and replay metadata.

### 6. Model Execution Layer

- hosted model API route for rapid frontier model comparison,
- local LLM serving route for company environment testing,
- model routing configuration,
- workstation or GPU requirements for local tests,
- token, API, and server cost tracking.

### 7. Agent Runtime Layer

- portable adapter interface,
- example OpenClaw runtime wiring,
- example Hermes runtime wiring,
- optional later agent runtime wiring if a stronger candidate becomes available,
- runtime specific permissions and tool access,
- normalized workflow artifacts.

### 8. Evaluation Layer

- baseline comparison runs,
- repeatability analysis,
- auditability review,
- explanation quality review,
- escalation quality review.

## Repository Mapping

- `src/reserving_workflow/`: shared code, schemas, adapters, and validators.
- `workflows/agent-runtimes/`: workflow definitions, constitutional rule assets, model route notes, agent adapter notes, and example runtime docs.
- `benchmarks/`: case definitions, rubrics, and experiment facing metadata.
- `tests/`: unit and integration tests around deterministic boundaries and workflow checks.
- `docs/`: architecture, planning, and architectural decisions.

## Core Boundaries

### Deterministic Boundary

Numeric reserving output must remain independently testable and reproducible without the LLM.

### Governance Boundary

Any output released by the workflow must pass constitutional checks or be explicitly escalated.

### Agent Boundary

Agent runtimes may orchestrate tasks, call tools, manage memory, and route review events, but deterministic reserving calculations, constitutional rules, scoring rubrics, and audit artifact schemas remain runtime neutral.

### Model Boundary

Hosted model APIs and local LLM deployments are both treated as replaceable model execution routes. Neither route owns the reserving calculation, the constitutional rules, or the audit artifact schema.

### Audit Boundary

Each run should be reconstructable from stored inputs, prompts, tool calls, approvals, and outputs.

## Initial Implementation Priorities

1. Define canonical input and output schemas.
2. Add deterministic adapter interfaces.
3. Define constitutional rule representation.
4. Define agent runtime adapter contracts.
5. Define hosted API and local LLM configuration patterns.
6. Define workflow artifact logging schema.
7. Add benchmark case and rubric schemas.
