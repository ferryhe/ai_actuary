# ADK local workbench

Status: Active.

This page is the current operating guide for the local AI Actuary Operator
Console plus the optional Google ADK Developer Web. Historical ADK planning
notes remain under `docs/archive/`; use this document for the supported local
launcher, package, cleanup, and browser-smoke commands.

## Supported surfaces

- Operator Console/API: `http://127.0.0.1:8000/console`
- ADK Developer Web: `http://127.0.0.1:8001`

Both defaults are loopback-only. Use explicit ports when the defaults are busy:

```bash
python scripts/run_local_workbench.py --api-port 8123 --adk-port 8124
```

The console link to ADK Developer Web is rendered from the actual configured
ADK URL, so non-default ports are visible in the browser instead of falling back
to hardcoded `8001`.

## Source checkout quickstart

Use Python 3.11 for the ADK surface:

```bash
pip install -e '.[dev,adk-dev]'
python scripts/run_local_workbench.py
```

For API-only work in an environment without `google.adk`, run:

```bash
pip install -e '.[dev]'
python scripts/run_local_workbench.py --disable-adk --smoke
```

The launcher prints the actual Operator Console URL, the actual ADK URL or
disabled state, and the sanitized diagnostics log path. It refuses occupied
ports before starting children.

## Installed wheel quickstart

The wheel exposes stable console entry points:

```bash
ai-actuary-package-audit
ai-actuary-workbench --disable-adk --smoke
ai-actuary-cleanup --repo-root . --execute
```

Profile A installs `ai-actuary[api]` and must not import or require
`google.adk`. Profile B installs `ai-actuary[api,adk-dev,browser-smoke]` in a
disposable Python 3.11 environment and verifies the full two-UI ADK path.

Set `PYTHONNOUSERSITE=1` and clear `PYTHONPATH` for installed-wheel drills so
imports prove they come from the venv `site-packages`, not from a source
checkout.

## Browser smoke

Run API-only browser smoke from a source checkout:

```bash
pip install -e '.[dev,browser-smoke]'
python -m playwright install chromium
python scripts/browser_smoke_local_workbench.py --disable-adk
```

The script starts the workbench on unused loopback ports, opens Chromium with
Playwright, unlocks the Operator Console through the real bootstrap route,
checks the visible page, verifies the actual ADK link, and captures evidence
under `tmp/browser-smoke-local-workbench/<timestamp>/`:

- `operator_console.png`
- `trace.zip`
- `network_summary.json`
- `console_summary.json`
- `cleanup_evidence.json`
- `result.json`

Full two-UI mode intentionally fails with `environment_unavailable` when
`google.adk`, the `adk` executable, Playwright, or the pinned Chromium browser
is absent. Run the full mode only in the Profile B venv. Full mode uses the
pinned repository Playwright package
(`playwright==1.59.0`) and its pinned Chromium revision, then proves:

- Operator Console and ADK Developer Web are both visible and linked with the
  actual loopback ports.
- A confirmed ADK Developer Web session/run protocol produces one run ID,
  operation ID, provenance/correlation ID, and logical artifacts. The smoke
  then uses the same ADK conversation to inspect the post-run summary and
  cross-checks those values against direct API reads and the Operator Console
  selected-run view.
- The post-run ADK screenshot is captured from the real ADK Developer Web UI
  origin after execution. The stock ADK UI may not render every AI Actuary
  run/review/artifact field; when that happens, the evidence marks
  `rendered_fields_complete=false` and keeps the protocol/API parity JSON as
  the authoritative field proof. Raw REST session JSON is not labeled as ADK UI
  evidence.
- Real ADK debug trace records must contain nonempty trace/span data linked to
  the session/invocation and run/correlation. Ordinary ADK session events are
  not accepted as trace proof.
- Review decision authority is asymmetric: ADK receives HTTP 403 for
  `/reviews/{review_id}/decision`; the Operator session succeeds and writes
  `review_decision.json` plus `review_decision.md`.
- Draft-only workflow starts fail closed; published catalog workflows remain
  the only ADK execution targets.
- Browser console, network summary, trace, screenshots, and cleanup evidence
  are written in the evidence directory, and unreleased owned ports fail the
  smoke.

The smoke sets `AI_ACTUARY_BROWSER_SMOKE_RUNNER=1` only for its owned local
workbench child process. That local-only runner is model-free so the browser
gate does not require OpenAI credentials; normal launcher/API runs do not use
it.

## Startup, stop, and lifecycle diagnostics

The launcher performs preflight before starting children: Python/ADK version
checks, loopback port conflict checks, state-directory preparation, and
capability secret generation. It records sanitized JSONL diagnostics under
`tmp/local-workbench-diagnostics/launcher.jsonl` with:

- bounded version summary for Python, `reserving_workflow`, Google ADK, and the
  ADK executable name;
- per-component status for control plane and ADK Developer Web;
- child identities, readiness outcome, and actual ports;
- startup failure reason, child-exit reason, signal shutdown cause, and
  explicit exit-code mapping.

Normal smoke exits with code `0`. Startup/preflight/runtime failures return
`1`. SIGINT/SIGTERM use the standard `128 + signal` mapping after child cleanup.
On Windows, the launcher attaches itself and children to a Job Object before
starting workbench children so parent death does not leave orphan API/ADK
processes.

## Local state and cleanup

Developer-owned state can be safely removed:

- `tmp/adk-dev/sessions`
- `tmp/adk-dev/traces`
- `tmp/adk-dev/artifacts`
- `tmp/adk-workflow-drafts`
- `tmp/adk-workflow-exports`
- `tmp/adk-evaluations`
- `tmp/local-workbench-diagnostics`

Business state is preserved by cleanup:

- `tmp/run-registry.json`
- `tmp/api-artifacts`
- `tmp/reviews`
- `tmp/review-outbox`
- `tmp/batch`

Preview cleanup first:

```bash
ai-actuary-cleanup --repo-root .
```

Execute only after reviewing the exact targets:

```bash
ai-actuary-cleanup --repo-root . --execute
```

Cleanup rejects empty targets, root/home/repository roots, glob patterns,
unresolved environment variables, and paths outside the repository `tmp/`
developer-owned boundary.

Launcher-owned ADK session, artifact, trace, and diagnostic directories are
created and revalidated as owner-private before use. On POSIX systems chmod
failures fail closed instead of being ignored. On Windows the launcher removes
inherited broad access, grants only the current owner plus system/admin
principals, validates that broad groups such as Authenticated Users/Everyone do
not retain access, and applies the same privacy check to sensitive files such
as the ADK SQLite session database and diagnostics JSONL. Separately, installed
ADK app resources are materialized into a launcher-owned temporary tree that is
readable by ADK but effectively immutable to create, replace, rename, and
delete attempts until launcher cleanup removes the protection.

## Capability and security limits

The local workbench uses capability credentials, not production identity:

- Operator Console gets a short-lived HttpOnly session through the launcher
  handoff/bootstrap flow and must send CSRF + same-origin mutation requests.
- ADK Developer gets a Bearer credential only in its child process environment.
- ADK can start/read ADK-scoped runs and artifacts, but cannot submit review
  decisions, export operator reports, mutate Operator-owned runs, or read host
  paths.
- Operator Console is the local human-review authority and can inspect/decide
  ADK-created review-required runs.

No non-loopback host, TLS, SSO/RBAC, production CORS, hosted deployment, or
automatic Git publication is provided by this phase.

## Workspace, drafts, publication, and rollback

ADK execution is restricted to published built-in workflow catalog entries:
`chainladder-basic` and `chainladder-validated`. Workflow Lab drafts remain
under `tmp/adk-workflow-drafts`; exports are immutable review artifacts under
`tmp/adk-workflow-exports`; publication to source checkout/Git is a separate
human-controlled flow and is never automatic from the launcher or browser
smoke.

Trace/correlation/evaluation layering is intentionally separate:

- run provenance/correlation IDs are stored with ADK run registry entries and
  projected through path-free ADK reads;
- browser traces and screenshots are local smoke evidence only;
- ADK evaluation/benchmark evidence lives under `tmp/adk-evaluations` and is
  cleanup-eligible developer evidence unless a business workflow explicitly
  exports it elsewhere.

Rollback drills use one built candidate wheel and its SHA-256, install baseline
→ candidate → baseline in disposable venvs, and verify either representative
business state survives or schema drift fails closed with a backup/restore
path. Disable/uninstall drills prove `--disable-adk`, Profile A `[api]`,
uninstalled ADK, and intentionally incompatible ADK versions fail or fall back
without importing `google.adk` into core/API paths.

Installed-wheel upgrade procedure:

```powershell
$env:PYTHONNOUSERSITE = "1"
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
python -m venv C:/Project/ai_actuary_issue40_upgrade/.venv
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip install --upgrade pip
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip install "C:/Project/baseline/ai_actuary-0.1.0-py3-none-any.whl[api]"
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip install --force-reinstall "C:/Project/candidate/ai_actuary-0.1.0-py3-none-any.whl[api,adk-dev,browser-smoke]"
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/ai-actuary-package-audit.exe
```

Expected verification output: import location contains `site-packages`, package
resources are complete for the candidate, and the recorded wheel SHA-256
matches the candidate under review.

Rollback baseline → candidate → baseline procedure:

```powershell
$state = "C:/Project/ai_actuary_issue40_rollback_state"
$backup = "C:/Project/ai_actuary_issue40_rollback_backup"
Copy-Item "$state/run-registry.json" "$backup/run-registry.json" -Force
Copy-Item "$state/reviews" "$backup/reviews" -Recurse -Force
Copy-Item "$state/api-artifacts" "$backup/api-artifacts" -Recurse -Force
$businessRead = @'
import json
from pathlib import Path
from reserving_workflow.runtime.run_registry import list_runs
from reserving_workflow.storage.local import LocalArtifactStore, LocalReviewStore
state = Path(r"C:/Project/ai_actuary_issue40_rollback_state")
print(json.dumps({
    "runs": len(list_runs(state / "run-registry.json")),
    "reviews": len(LocalReviewStore(state / "reviews").list_reviews()),
    "artifacts": len(LocalArtifactStore().list_artifacts(state / "api-artifacts")),
}, sort_keys=True))
'@
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip install --force-reinstall "C:/Project/baseline/ai_actuary-0.1.0-py3-none-any.whl[api]"
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -c $businessRead
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip install --force-reinstall "C:/Project/candidate/ai_actuary-0.1.0-py3-none-any.whl[api,adk-dev,browser-smoke]"
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -c $businessRead
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/ai-actuary-package-audit.exe
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip install --force-reinstall "C:/Project/baseline/ai_actuary-0.1.0-py3-none-any.whl[api]"
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -c $businessRead
```

Expected verification output: each stage proof records the installed wheel SHA,
install command exit code, import path, distribution metadata, actual entry
points, a package-resource audit for the candidate wheel, and a successful
business/core read of the run registry, review store, and artifact refs at every
stage. The rollback summary
must distinguish baseline/candidate/restored by wheel SHA even when the package
version string remains `0.1.0`.

Disable/uninstall and incompatible-ADK procedures:

```powershell
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m reserving_workflow.cli.local_workbench --disable-adk --smoke --api-port 8123 --adk-port 8124
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip uninstall -y google-adk
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m reserving_workflow.cli.local_workbench --smoke
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m pip install --force-reinstall "google-adk!=2.7.1"
C:/Project/ai_actuary_issue40_upgrade/.venv/Scripts/python -m reserving_workflow.cli.local_workbench --smoke
```

Expected verification output: `--disable-adk` succeeds without importing
`google.adk`; missing or incompatible ADK exits fail-closed with a diagnostics
envelope and an installed-mode recovery hint such as
`pip install "ai-actuary[api,adk-dev]"`.
If you run the command through the pip shim, the uninstall step is
`pip uninstall google-adk` (with `-y` added for unattended drills).

Backup/restore state procedure:

```powershell
Copy-Item "$backup/run-registry.json" "$state/run-registry.json" -Force
Copy-Item "$backup/reviews" "$state/reviews" -Recurse -Force
Copy-Item "$backup/api-artifacts" "$state/api-artifacts" -Recurse -Force
```

Expected verification output: registry/review/artifact checksums before
candidate, after candidate, and after rollback match, or the schema
compatibility check reports fail-closed and restores from backup.

## Clean-environment verification commands

Use disposable venvs outside the checkout for installed-wheel validation:

```bash
python -m build
python -m venv C:/Project/ai_actuary_issue40_profile_a/.venv
PYTHONNOUSERSITE=1 PYTHONPATH= C:/Project/ai_actuary_issue40_profile_a/.venv/Scripts/pip install dist/ai_actuary-*.whl[api]
PYTHONNOUSERSITE=1 PYTHONPATH= C:/Project/ai_actuary_issue40_profile_a/.venv/Scripts/ai-actuary-package-audit
PYTHONNOUSERSITE=1 PYTHONPATH= C:/Project/ai_actuary_issue40_profile_a/.venv/Scripts/ai-actuary-workbench --disable-adk --smoke

python -m venv C:/Project/ai_actuary_issue40_profile_b/.venv
PYTHONNOUSERSITE=1 PYTHONPATH= C:/Project/ai_actuary_issue40_profile_b/.venv/Scripts/pip install dist/ai_actuary-*.whl[api,adk-dev,browser-smoke]
PYTHONNOUSERSITE=1 PYTHONPATH= C:/Project/ai_actuary_issue40_profile_b/.venv/Scripts/python -m playwright install chromium
PYTHONNOUSERSITE=1 PYTHONPATH= C:/Project/ai_actuary_issue40_profile_b/.venv/Scripts/ai-actuary-package-audit
PYTHONNOUSERSITE=1 PYTHONPATH= C:/Project/ai_actuary_issue40_profile_b/.venv/Scripts/ai-actuary-workbench --smoke
```

On PowerShell, set `PYTHONNOUSERSITE=1` and remove `PYTHONPATH` through
`$env:PYTHONNOUSERSITE = "1"; Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue`
before invoking the same venv commands.

## Troubleshooting

- `environment_unavailable` from browser smoke means the requested mode lacks
  Playwright, the pinned Chromium revision, `google.adk`, or the `adk`
  executable.
- Port conflict / `Loopback port conflict` means the launcher refused to start
  because the requested API/ADK port was already accepting connections. Free
  the port or rerun with unused `--api-port` and `--adk-port` values.
- Readiness timeout means one child started but a health/UI endpoint did not
  become ready. Inspect the pending endpoint, `children_started`,
  `startup_failed`, and child log evidence in the diagnostics JSONL.
- Child exit means a named child process stopped. Inspect the component, PID,
  command label, exit code, and retained stdout/stderr before rerunning.
- `capability_forbidden` on review decision is expected for ADK; use the
  Operator Console/session for human review decisions.
- Missing OpenAI/Gemini credentials affect normal model-backed planner/chat
  paths. Browser smoke full mode remains model-free through its explicit
  smoke-only runner.

## Related docs

- [Project README](../README.md)
- [ADK Workflow Lab](adk-workflow-lab.md)
- [Control-plane contract](contracts/control-plane.md)
- [Local capability credential ADR](architecture/adr-0003-local-capability-credential-transport.md)
