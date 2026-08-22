# Project Plan

This is the current project-plan handoff for the AI Actuary local workbench. The historical Prompt/PR status plan is archived at `archive/project-plan.md`; this page is kept current for active operators and reviewers.

## Completed

- Prompt 8: governed operator run artifacts, review packet generation, and repeatability checks.
- Prompt 9: operator handoff/report export path, artifact replay, and review flow hardening.
- Prompt 10: documentation handoff, role split, and local operator workflow guide.

Issue #40 is in release-review. Do not mark it complete until local review and
the required browser/package/rollback validation gates pass.

## Not Yet Implemented

- Production deployment, TLS, authentication/RBAC, and broader CORS are intentionally outside the local developer workbench scope.
- ADK upgrades beyond the pinned local development version require a new compatibility pass.
- Automatic publishing of draft workflows to business production remains out of scope; publishing stays operator-controlled.

## Next Recommended Steps

1. Run the clean-environment commands in `adk-local-workbench.md` before release review.
2. Review retained browser smoke evidence for Operator Console / ADK Developer Web / API parity.
3. Confirm rollback evidence binds the baseline, candidate, and restored wheels by SHA-256.
4. Keep business-state cleanup separate from developer artifact cleanup.

## Step-by-Step Handoff Guide

Human steps:

1. Select the candidate wheel and record its SHA-256.
2. Run Profile A `[api]` and Profile B `[api,adk-dev,browser-smoke]` from outside the checkout.
3. Inspect the browser smoke screenshots, sanitized logs, trace archive, ACL proof, and rollback summary.
4. Decide whether the candidate is acceptable for PR review.

Agent steps:

1. Preserve loopback-only defaults and actual-port readiness links.
2. Apply the shared sanitizer to browser-visible errors, diagnostics, logs, traces, tool output, and retained evidence.
3. Validate owner-private state directories/files for developer and business state.
4. Keep rollback summaries path-free while binding wheels, install stages, package resources, and business-state checksums.
