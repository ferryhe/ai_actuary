# AI Actuary Documentation Index

This directory contains the current project documentation for the local Agentic Actuarial Workbench prototype.

## Current documents

- `../README.md` — primary setup, usage, and project overview.
- `../README.zh-CN.md` — primary overview in Chinese.
- `operations-manual.md` — end-to-end operating manual: flow, two calculations, handoffs, review and AI reviewer configuration.
- `operations-manual.zh-CN.md` — the same operating manual in Chinese.
- `project-introduction.html` — standalone HTML introduction and operator guide.
- `project-plan.md` — current handoff plan and Issue #40 release-review checklist.
- `architecture.md` — implementation architecture and runtime boundaries.
- `architecture/overview.md` — short architecture summary.
- `operator_handoff.md` — report-export and handoff artifact contract.
- `adk-operations-manual.md` — ADK developer surface: what it is, configuration, usage, guardrails.
- `adk-operations-manual.zh-CN.md` — the same ADK manual in Chinese.
- `adk-local-workbench.md` — active Phase 6 local launcher, browser smoke, package, and cleanup guide.
- `adk-workflow-lab.md` — Phase 4 declarative draft validation, export, and installed-layout contract.
- `architecture/adr-0004-adk-workflow-lab-builder-fallback.md` — ADK 2.7.1 native Builder fallback decision.
- `contracts/control-plane.md` — run/event/tool/workflow/review/report API contract.
- `contracts/actuarial-tool-manifest-v1.md` — CLI/file-artifact tool contract v1.
- `contracts/actuarial-artifact-layout-v1.md` — artifact layout v1.
- `contracts/tool-contract-compatibility-suite.md` — cross-repo compatibility fixture rules.

## Archived documents

Historical plans, reports, and implementation prompts were moved under `archive/`. They are retained for traceability but are not the current operating guide.

- `archive/project-plan.md` — historical Prompt/PR status plan; superseded by this index and `adk-local-workbench.md`.

## Rule of thumb

Use current docs for how to run or extend the project. Use archived docs only to understand why earlier design decisions were made.
