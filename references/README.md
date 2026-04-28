# Upstream Reference Materials

This folder snapshots the minimum upstream materials needed to design and build the combined AI actuary system.

## Sources

- `cas/` — copied from `ferryhe/CAS_Constitutional_AI_Workflow`
- `openai-agents/` — copied from `openai/openai-agents-python`
- `hermes/` — copied from local Hermes Agent source/docs (`NousResearch/hermes-agent` checkout on this machine)

## Purpose

These files are reference inputs for architecture, contracts, workflow design, and implementation planning. They are not the new system itself.

## Notes

- Keep deterministic actuarial logic and governance rules in this repo's own source tree, not inside copied upstream files.
- When upstream sources change materially, refresh these snapshots intentionally rather than editing them in place.
