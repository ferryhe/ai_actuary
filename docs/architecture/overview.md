# AI Actuary Combined Architecture

This repo follows a three-layer composition model:

1. **CAS Core** — deterministic actuarial truth, constitution rules, benchmark schemas, artifacts.
2. **OpenAI Planner** — workflow planning, routing, guardrails, orchestration.
3. **Hermes Workers** — execution, packaging, notifications, recurring operations, process memory.

See `docs/plans/openai-hermes-composition-design.md` for the full design and phased project plan.
