# ai_actuary Project Status

Last updated: 2026-05-20

## Identity

- Project: ai_actuary
- Worker slug: ai-actuary
- Repo path: /home/ec2-user/work/ai_actuary
- Remote: git@github.com:ferryhe/ai_actuary.git
- Current branch: feat/tool-cli-entrypoints

## Current Objective

- PR1 (#27) was squash-merged after clean remote state.
- PR2 is open as #28: https://github.com/ferryhe/ai_actuary/pull/28
- This tick inspected PR2 remote checks/reviews/comments, accepted three in-scope Copilot comments, pushed a follow-up fix, and stopped to wait for the next 15-minute remote review/check cycle.

## Files Added or Modified in This Run

- `src/reserving_workflow/artifacts/replay.py`
- `src/reserving_workflow/review/generator.py`
- `src/reserving_workflow/tools_cli/repeatability_check.py`
- `src/reserving_workflow/tools_cli/review_generator.py`
- `tests/test_replay_hooks.py`
- `tests/test_tools_cli.py`
- `.hermes/ai_actuary_tool_decomposition_rollout_state.json`
- `.hermes/project-status.md`

## Verification

- `PYTHONPATH=src python -m pytest tests/test_tools_cli.py tests/test_replay_hooks.py -q` ✅ (`16 passed`)
- `PYTHONPATH=src python -m pytest tests -q` ✅ (`177 passed`)
- Substitute review gate: independent delegate reviewers reviewed the follow-up diff; one review found the review-packet relative path fix incomplete, it was fixed, and re-review reported no blocking issues.

## Remote Review / Checks

- `gh pr view 28` showed `mergeStateStatus=CLEAN`, `mergeable=MERGEABLE`, and Copilot review with 3 inline comments.
- `gh pr checks 28` reported no checks configured.
- Inline PR comments fetched via `gh api repos/ferryhe/ai_actuary/pulls/28/comments --paginate`: 3 comments accepted and addressed:
  - Resolve review packet output dirs safely when manifest artifact paths are relative.
  - Remove unreachable repeatability CLI validation duplicated by argparse.
  - Preserve boolean `matches_saved_result` and add `saved_result_present` for missing saved artifacts.

## Dirty / Untracked State Noticed

- `docs/plans/2026-05-20-ai-actuary-tool-decomposition-pr-plan.md` remains intentionally untracked and untouched.
- Test-generated untracked `tmp/` artifacts were removed.

## Next Safe Action

- On the next scheduled tick, inspect PR #28 checks, reviews, and inline comments again. If there are no further actionable comments and mergeability remains clean, squash merge PR2, sync `main`, then start PR3 (`feat/tool-artifact-runner`).
