# ai_actuary Project Status

Last updated: 2026-05-20T19:02:18Z

## Identity

- Project: ai_actuary
- Worker slug: ai-actuary
- Repo path: /home/ec2-user/work/ai_actuary
- Remote: git@github.com:ferryhe/ai_actuary.git
- Current branch: docs/refresh-project-docs

## Current Objective

- Refresh project documentation on `docs/refresh-project-docs`: archive stale plans/reports/prompts, update README/docs to current local workbench state, and add a standalone HTML introduction/usage guide.
- Tool-decomposition rollout is complete for the non-optional cross-repo sequence in `.hermes/ai_actuary_tool_decomposition_rollout_state.json`.
- PR1 (#27), PR2 (#28), PR3 (#29), and PR6 (#30) in `ai_actuary` are merged.
- PR4 (#41) and PR5 (#42) in `ai_interface` are merged.
- PR7 (optional chainladder HTTP adapter) remains skipped because no concrete need for lower startup overhead, concurrency, health checks, or long-lived service deployment is present.
- PR8 (optional MCP adapter) remains skipped because no request for multi-agent-framework discoverability is present.

## Files Added or Modified in This Run

- `README.md`
- `docs/README.md`
- `docs/project-introduction.html`
- `docs/architecture.md`
- `docs/architecture/overview.md`
- `docs/contracts/*.md`
- `docs/operator_handoff.md`
- `docs/project-plan.md`
- `docs/archive/`
- `.hermes/project-status.md`
- `.hermes/ai_actuary_tool_decomposition_rollout_state.json`

## Verification

- Live GitHub inspection completed at 2026-05-20T15:23:25Z ✅: `gh pr list --state open` for `ai_actuary` returned no open PRs; `gh pr view 27` reports `state=MERGED`; `gh pr checks 27` reports no checks configured; inline Copilot comments fetched via `gh api repos/ferryhe/ai_actuary/pulls/27/comments --paginate` are the already-handled prior comments.
- Live GitHub inspection for `ai_interface` completed at 2026-05-20T15:23:25Z ✅: `gh pr list --repo ferryhe/ai_interface --state open` returned no open PRs; local `ai_interface` status is clean on `main...origin/main`.
- Cron/timer inspection completed at 2026-05-20T15:23:25Z ✅: no Hermes rollout entry was visible in the user crontab, user systemd timers, or system systemd timers from this shell, so there was no local scheduled entry to remove from here.
- Docs-refresh validation ✅: active-doc local link scan found no missing relative markdown links.
- Focused contract/tool validation ✅: `PYTHONPATH=src python -m pytest tests/test_contract_schema_export.py tests/test_tool_contract_compat_manifest.py tests/test_tools_cli.py tests/test_tool_runner.py -q` passed (`25 passed`).
- Full test suite ✅: `PYTHONPATH=src python -m pytest tests -q` passed (`192 passed`).
- Pre-PR review gate: Codex CLI blocked because `gpt-5.2-codex` is not supported with the current ChatGPT account; substitute delegate review found generated `tmp/` artifacts and scope risk in `.hermes/` state. `tmp/` artifacts were removed; `.hermes/` updates are intentionally retained as tracked project status/rollout metadata.
- PR #31 remote comment follow-up ✅ (2026-05-20T19:02:18Z): Copilot's inline comment about stale `PR1 exports...` wording in `docs/contracts/actuarial-tool-manifest-v1.md` was valid and fixed with PR-agnostic wording. Validation: `git diff --check` passed; contract wording assertion passed; `PYTHONPATH=src python -m pytest tests/test_contract_schema_export.py tests/test_tool_contract_compat_manifest.py -q` passed (`8 passed`). Codex CLI review gate was attempted with ChatGPT 5.5 via `codex -c 'model="gpt-5.5"' review --uncommitted`, but the installed Codex CLI reported that gpt-5.5 requires a newer CLI version.

## Dirty / Untracked State Noticed

- The rollout plan doc has been moved to `docs/archive/plans/2026-05-20-ai-actuary-tool-decomposition-pr-plan.md` for historical traceability.
- Generated local `tmp/` review artifacts from prior runs were removed from the working tree and are not part of this docs PR.

## Next Safe Action

- No further scheduled rollout tick is needed. If a concrete production requirement appears for an HTTP service adapter or MCP tool discoverability, start PR7 or PR8 from latest `origin/main` with a fresh branch and focused scope.

## Optional PR7/PR8 Reassessment — 2026-05-20T15:37:57Z

Conclusion: PR7/PR8 are not worth continuing right now, so the autonomous rollout is paused.

- PR7 HTTP adapter: skip for now. The repo already has a FastAPI control plane with health/preflight/tool/run surfaces, and the current ai_interface integration uses the CLI pipeline adapter. A narrow `/calculate` adapter would duplicate semantics unless there is a concrete need for lower startup overhead, concurrency, or long-lived deployment.
- PR8 MCP adapter: skip for now. There is no current request for ai_actuary tools to be discoverable by multiple MCP-capable agent frameworks. Adding MCP now would add dependency/protocol/security surface before a caller exists.
- Scheduler: Hermes cron job `b32233a7eddf` was paused to avoid unnecessary follow-up ticks.

Next safe action: reopen PR7 only for a persistent-service/concurrency requirement; reopen PR8 only for a real MCP multi-agent discovery requirement.
