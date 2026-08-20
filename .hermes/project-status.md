# ai_actuary Project Status

Last updated: 2026-05-20T19:14:28Z

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
- Codex CLI upgrade/review follow-up ✅ (2026-05-20T19:14:28Z): upgraded global `@openai/codex` from `0.121.0` to `0.132.0`; `codex -c 'model="gpt-5.5"' review --base origin/main` now runs. Accepted one valid review finding: the v1 contract boundary text still said there were no runtime/CLI implementation changes, contradicting the current file-artifact CLI tool surface. Rephrased the section as v1 boundaries and clarified that CLI file-artifact entrypoints plus exported schemas are in scope while HTTP/MCP/ai_interface expansion remains out of scope.

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

## ADK Multi-PR Route — PR2 Delivery (2026-08-19)

- Scope: PR2 read-only ADK control-plane adapter only; PR3–PR6 capabilities remain out of scope.
- Branch/worktree: `feat/adk-readonly-control-plane-adapter-v2` in a new isolated continuation worktree; the prior unpublished branch is retained for audit.
- Baseline: `origin/main` at `1670dd7c8bb1792ed8792357168548141adac0cc`; no open pull requests at start.
- Stage: new-attempt review cycle 3 remediation is complete; the branch is awaiting fresh cycle 4 independent review.
- Review cycles: `3/5` for this new PR attempt; the prior branch's `5/5` audit record remains unchanged.
- Task heartbeat: `ai-actuary-pr2-continuation-heartbeat` is `ACTIVE`, scoped to this continuation, and must be deleted after merge/cleanup or a confirmed stop-rule blocker.
- TDD evidence: first shared-client collection failed with the expected missing-package error; final focused suite passed (`143 passed, 6 skipped`) and full suite passed (`322 passed, 10 skipped`).
- Platform coverage note: POSIX FIFO/device/descriptor-race checks are present but skip on Windows and must run in Linux CI.
- Cycle 1 blockers: embedded absolute-path/common-credential redaction gaps; Windows intermediate-directory TOCTOU; Linux symlink test not reaching the no-follow branch; stable rejection for malformed/non-finite JSON hardening.
- Cycle 1 verification: focused `163 passed, 6 skipped`; PR1/ADK regression `24 passed, 3 skipped`; Windows junction-swap/handle-leak `2 passed`; isolated Linux full suite `347 passed, 5 skipped`; `git diff --check` passed.
- Cycle 2 blockers: nested Windows drive component escape; post-intermediate-handle junction race; dictionary-key/camelCase/JWT/Basic/rooted-path redaction gaps; Hermes polling/summary not yet shared; missing explicit ADK/API/Console minimum-acceptance parity assertions.
- Cycle 2 verification: focused `173 passed, 6 skipped`; Hermes + isolated ASGI acceptance `5 passed`; Windows adversarial `3 passed`; PR1/ADK regression `24 passed, 3 skipped`; Python 3.11 Linux/LF full suite `357 passed, 5 skipped`; `git diff --check` passed.
- Cycle 3 blockers: Windows trusted-root ancestor junction race; compressed-response expansion before byte cap; standalone Basic/all-caps/fileName sanitizer variants; server-safe run_manifest rejected by client revalidation; per-artifact required schema and identity gaps.
- Cycle 3 verification: TDD RED `25 failed`; focused `207 passed, 6 skipped`; PR1/ADK regression `24 passed, 3 skipped`; Windows trusted-root races `2 passed`; Windows race/handle-leak group `4 passed`; artifact schema/identity and authoritative fixtures `29 passed`; Python 3.11 Linux/LF full suite `389 passed, 7 skipped`; `git diff --check` passed.
- Cycle 4 blockers: real workflow parent manifest/artifact truth divergence; client request/response run and artifact identity plus provenance not bound; credential-assignment/PEM/auth-header value sanitizer gaps; committed local worktree path.
- Cycle 4 verification: TDD RED `28 failed, 5 passed`; targeted GREEN `33 passed`; focused `240 passed, 6 skipped`; PR1 core `24 passed, 3 skipped`; ADK 2.7.1 regression `27 passed`; Windows invariance/security/tool group `29 passed`; sanitizer adversarial `20 passed`; Python 3.11 Linux/LF full suite `422 passed, 7 skipped`; Ruff, `git diff --check`, local-path scan, cleanup, and port checks passed.
- Continuation blockers authorized for one TDD batch: list-runs operator/workspace response identity; Windows same-root intermediate junction replacement; remaining snake/camel secret assignment variants; artifact-list provenance and nested review/packet/decision identities.
- Continuation TDD/verification: initial targeted RED `20 failed, 3 passed`, targeted GREEN `23 passed`; helper/uppercase extension RED `6 failed, 7 passed`, GREEN `13 passed`; focused `268 passed, 7 skipped`; Windows full `445 passed, 11 skipped, 2 deselected`; Python 3.11 Linux/LF full `448 passed, 7 skipped`; ADK 2.7.1 `32 passed, 2 skipped`; storage invariance, review GET/POST, real workflow, Windows junction, POSIX safety, Ruff, diff/path/secret scans, cleanup, and ports passed.
- New-attempt cycle 1 blockers: Windows top-level trusted-root double-rename TOCTOU during lexical final-file open; compound API/access-key sanitizer variants plus an ordinary `token count` false positive; review packet `case_id` not bound to the outer persisted review identity.
- New-attempt cycle 1 review evidence: all three independent gates reproduced the Windows race; focused `268 passed, 7 skipped`; PR1 regression `24 passed, 3 skipped`; clean Windows/LF and Linux full suites otherwise passed; worktree, diff, cleanup, process, and port checks passed.
- New-attempt cycle 1 remediation: Windows final artifacts now open relative to a pinned trusted-root handle through `NtCreateFile`, with delete sharing disabled on the final object; compound API/access-key names and assignments redact across case/separator styles while ordinary token-count language remains; review records and packets are bound to registered review/run/case identity with legacy missing fields filled only in memory.
- New-attempt cycle 1 verification: TDD RED `19 failed, 7 passed` plus token-count extension RED `3 failed, 2 passed`; focused `293 passed, 7 skipped` and controller overlap `309 passed, 10 skipped`; Windows adversarial `13 passed, 4 skipped`; PR1 `24 passed, 3 skipped`; ADK 2.7.1 `61 passed, 2 skipped`; Linux Python 3.11 LF full `470 passed, 8 skipped`; Windows full `465 passed, 11 skipped, 2 failed` only for unchanged CRLF checksum baselines; Ruff, diff, path/credential, cleanup, container, process, and port gates passed.
- New-attempt cycle 2 blockers: manifest and review-packet metadata GETs bypass the bounded descriptor reader and may follow symlinks, block on FIFOs, or probe manifest-controlled paths; persisted review workspace and nested decision identities are not bound and legacy decision POST discards in-memory identity completion; artifact-projection client does not bind inner data identity; natural-language access/header credentials and separated token-count forms are inconsistent in the sanitizer.
- New-attempt cycle 2 review evidence: Reviewer 1, Reviewer 2, and Tester all returned FAIL with zero Critical findings; focused `293 passed, 7 skipped`; independent clean LF Windows and Linux full suites each reached `475 passed, 8 skipped`; Windows pinned-root and Linux POSIX projection-reader matrices passed, isolating the unsafe reads to non-projection metadata/review paths; worktree and cleanup gates passed.
- New-attempt cycle 2 remediation: manifest/review-packet reads and metadata presence now share the bounded descriptor-pinned/no-follow reader anchored only to registry artifact roots; present manifest run/case/shape fields are bound while legacy omissions remain compatible; review workspace/packet/decision identities and legacy decision writes are bound; projection envelopes carry authoritative case/tool relations; semantic sanitizer rules cover access/header credentials and separator-independent token-count language.
- New-attempt cycle 2 verification: TDD RED `24 failed, 51 passed`, targeted GREEN `75 passed`; controller overlap `388 passed, 20 skipped`; Windows focused `368 passed, 17 skipped`; Windows LF full `533 passed, 21 skipped`; Linux Python 3.11 editable/source full `546 passed, 8 skipped`; Linux POSIX `136 passed, 5 skipped` only for Windows-specific tests; ADK 2.7.1 `79 passed, 3 skipped` with exact 12 read-only tools and model-free web routes all HTTP 200; PR1 `24 passed, 3 skipped`; Ruff, diff, schema/golden, path/credential, storage-invariance, cleanup, process, and port gates passed.
- New-attempt cycle 3 blockers: manifest and target artifact/presence reads do not share one pinned trusted-root handle across a request; persisted review records still use ordinary filesystem reads and can follow links or block on FIFOs; compact/all-caps and common Authorization/private-key credential text bypasses sanitizer; shared client does not fully bind counts, event type/status, missing outer review identity, or deterministic legacy method; idempotent legacy decision POST returns before persisting its in-memory identity completion.
- New-attempt cycle 3 review evidence: Reviewer 1 and Reviewer 2 returned FAIL while Tester returned PASS; zero Critical findings; clean LF Windows core/ADK suites reached `533/536 passed` with `21/18 skipped`, Linux Python 3.11 full `546 passed, 8 skipped`; exact 12-tool ADK discovery, model-free web smoke, current Windows/Linux single-file security readers, dependency isolation, PR1 regression, diff, and cleanup gates passed.
- New-attempt cycle 3 remediation: added a neutral request-scoped pinned JSON root shared by control-plane and review storage; manifest, artifact, and presence reads now remain on one POSIX directory descriptor or Windows `RootDirectory` handle; review records use the same bounded no-follow/non-blocking reader; sanitizer, envelope/event/review/method identity checks, and legacy idempotent identity persistence were tightened.
- New-attempt cycle 3 verification: focused `405 passed, 21 skipped` and controller overlap `405 passed, 23 skipped`; Windows artifact `144 passed, 13 skipped`; ADK 2.7.1 `87 passed, 3 skipped`; clean Python 3.11 Linux/LF full `592 passed, 9 skipped`; Linux artifact security `23 passed`, review storage `10 passed`, and real ASGI FIFO matrix `11 passed`; exact 12 read-only tools, storage invariance, real Chainladder/MiniMax parity, Hermes, Ruff, compile, diff, credential scan, cleanup, and port gates passed. Windows-only full-suite noise remained limited to one unchanged same-tick sorting flake and two unchanged CRLF checksum fixtures, each independently disproved as a PR2 regression.
- Updated: `2026-08-20T03:55:32.6773588-04:00`.
- Updated: `2026-08-20T03:03:10.3756091-04:00`.
- Updated: `2026-08-20T02:39:15.4123034-04:00`.
- Updated: `2026-08-20T01:55:07.3084823-04:00`.
- Updated: `2026-08-20T01:35:31.8451443-04:00`.
- Updated: `2026-08-20T01:02:56.3063891-04:00`.
- Updated: `2026-08-20T00:44:59.6857150-04:00`.
- Updated: `2026-08-20T00:13:12.2484124-04:00`.
- Updated: `2026-08-19T20:35:03.7128077-04:00`.
- Updated: `2026-08-19T20:16:08.3452311-04:00`.
- Updated: `2026-08-19T19:54:16.3437495-04:00`.
- Updated: `2026-08-19T19:14:17.9354550-04:00`.
- Updated: `2026-08-19T18:36:25.3388224-04:00`.
- Updated: `2026-08-19T18:05:18.1966082-04:00`.

## ADK Multi-PR Route — PR1 Delivery Record — 2026-08-19

- Base branch: `main`
- Base SHA: `c802af9211a5c5718d74c7cf9e6e082c45e79022`
- Gate 0: `PASSED`
- Delivery branch: `feat/adk-dual-interface-foundation`
- PR1 scope: mechanical Console resource extraction, optional pinned ADK development dependency, a read-only health-only Developer Agent, a two-process launcher, and reciprocal Console/Developer Web entry points.
- Explicitly deferred to PR2–PR5: Tool Registry, Workflow Catalog, actuarial tool execution, write tools, Capability, Visual Builder draft publishing, Trace, and Evaluation.
- Managed review cycles: `4/5`
- Progress heartbeat: task-scoped automation `adk-pr1-progress-heartbeat`; it is not repository state and must be removed and verified stopped when the delivery task ends.
- Commit 1: `756abc0` (`refactor: extract operator console assets`), verified byte-for-byte against the embedded baseline before the required Developer Web link was added in commit 2.
- Local verification: default focused suite `91 passed, 4 skipped`; isolated ADK 2.7.1 focused suite `27 passed`; default-port dual-process smoke passed all seven HTTP checks and released both ports; missing-extra and occupied-port failures are explicit and leave no API child.
- Browser verification: Console renders the visible loopback Developer Web link with no JavaScript errors; ADK Web visibly shows `AI Actuary Developer (DEV)` and the Console address; code-first agent is discoverable while the Visual Builder edit control remains unavailable as documented.
- Review/test status: specification review passed; independent Tester passed. Code-quality cycle 1 found a Windows external-termination orphan risk. Cycle 2 then found the remaining `Popen`-to-child-assignment race. The final fix places the launcher itself in a kill-on-close Job Object before any child is created, so Windows atomically inherits containment; both steady-state and delayed-first-`Popen` parent-death tests pass. Final targeted re-review passed with no Critical or Important findings; its single stale-wording Minor was corrected.
- Final Tester re-check: passed on the two-commit product head with no blocker; default/ADK dependency boundaries, seven-route smoke, Windows steady-state termination, and delayed-first-`Popen` parent-death cleanup all passed.
- Remote review record: PR #35 was opened for this delivery. Its first `adk-dev` Linux job exposed one test-portability issue: the Windows error-path test assumed `ctypes.get_last_error` already existed on POSIX; the test-only monkeypatch now permits that Windows-only attribute to be created. Copilot reviewed 17/17 files and raised five comments: normalized child-exit messaging, remaining smoke timeout budget, and the duplicated model name were accepted and fixed; two custom-port link observations were classified as non-actionable because the published PR1 contract is fixed to `8000/8001` and the suppressed alternate-port arguments exist only for process-isolated integration tests. GitHub remains the source of truth for the PR's final CI, review, and merge state.
