# ADR 0001: Repository Scope and Separation of Concerns

## Status

Accepted

## Context

The proposal requires a repository that can support benchmark design, deterministic reserving logic, governed workflow orchestration, agent runtime portability, hosted model API tests, local LLM tests, and final reproducibility materials without collapsing all concerns into a single code path.

## Decision

The repository will separate:

- benchmark assets,
- workflow and agent runtime assets,
- model execution configuration for hosted APIs and local LLM trials,
- implementation code,
- tests,
- project documentation.

Deterministic reserving logic will remain independent from LLM narrative generation. Governance rules and review gates will be represented explicitly rather than only through prompts. Agent runtimes such as OpenClaw and Hermes will be treated as interchangeable orchestration surfaces through adapter boundaries, not as the source of actuarial truth.

## Consequences

- The repo is easier to grow without restructuring later.
- Testing can focus on deterministic and governance boundaries separately.
- Documentation can stay aligned with proposal deliverables.
- Future agent frameworks can be evaluated without rewriting benchmark, reserving, or scoring logic.
- Hosted API usage, local LLM serving, GPU or workstation needs, and OpenClaw or Hermes server costs can be tracked without changing actuarial workflow code.
