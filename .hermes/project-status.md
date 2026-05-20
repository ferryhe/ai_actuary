# ai_actuary Project Status

Last updated: 2026-05-20

## Identity

- Project: ai_actuary
- Worker slug: ai-actuary
- Repo path: /home/ec2-user/work/ai_actuary
- Remote: git@github.com:ferryhe/ai_actuary.git
- Current branch: feat/tool-artifact-runner

## Current Objective

- PR1 (#27) was squash-merged after clean remote state.
- PR2 (#28) was squash-merged after clean remote state.
- PR3 local implementation is complete and verified; preparing/opening PR for `feat/tool-artifact-runner`.

## Files Added or Modified in This Run

- `src/reserving_workflow/tool_runner/__init__.py`
- `src/reserving_workflow/tool_runner/contracts.py`
- `src/reserving_workflow/tool_runner/catalog.py`
- `src/reserving_workflow/tool_runner/runner.py`
- `scripts/run_tool_pipeline.py`
- `tests/fixtures/tool_pipelines/actuarial_reserving_review.yaml`
- `tests/test_tool_runner.py`
- `.hermes/ai_actuary_tool_decomposition_rollout_state.json`
- `.hermes/project-status.md`

## Verification

- `PYTHONPATH=src python -m pytest tests/test_tool_runner.py -q` ✅ (`6 passed`)
- `PYTHONPATH=src python scripts/run_tool_pipeline.py --pipeline tests/fixtures/tool_pipelines/actuarial_reserving_review.yaml --input tests/fixtures/tool_contracts/golden_run/case_input.json --artifact-root tmp/pipeline-smoke --json` ✅
- `PYTHONPATH=src python -m pytest tests/test_tools_cli.py tests/test_tool_runner.py tests/test_replay_hooks.py -q` ✅ (`22 passed`)
- `PYTHONPATH=src python -m pytest tests -q` ✅ (`183 passed`)
- Codex review gate attempted with `gpt-5.2-codex`; blocked because this ChatGPT account does not support that model in Codex CLI.
- Substitute review gate: independent delegate reviewers reviewed the implementation; accepted blockers around stable setup errors, artifact templates, output sandboxing/existence, and required output refs were fixed; final re-review found no blockers.

## Remote Review / Checks

- PR3 not yet through a remote 15-minute review cycle at this checkpoint.
- PR2 (#28) had no further actionable comments/checks and was squash-merged at the start of this run.

## Dirty / Untracked State Noticed

- `docs/plans/2026-05-20-ai-actuary-tool-decomposition-pr-plan.md` remains intentionally untracked and untouched.
- Test/smoke-generated `tmp/` artifacts were removed.

## Next Safe Action

- Create/open PR3, then stop this scheduled tick and inspect remote checks/reviews/comments on the next 15-minute cycle.
