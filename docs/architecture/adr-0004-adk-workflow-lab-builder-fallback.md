# ADR 0004: ADK 2.7.1 Workflow Lab Builder fallback

## Status

Accepted for Phase 4.

## Decision

The Workflow Lab uses the project-owned `validate_adk_workflow.py` and
`export_adk_workflow_diff.py` command surfaces. It does not launch or proxy the
ADK 2.7.1 Visual Builder and does not expose its native Builder routes.

This is a `FALLBACK`, not a claim that the native Builder is a security
boundary. The controlled commands accept only an app directory under
`tmp/adk-workflow-drafts/`, run the fixed validation sequence, and create a new
server-owned immutable export under `tmp/adk-workflow-exports/`. They never
accept an output path, modify published workflows, or publish through Git.

## ADK 2.7.1 evidence

The installed `google-adk==2.7.1` implementation was inspected before the
decision:

- `/dev/apps/{app_name}/builder/save` defaults `tmp=false` and writes the app
  root;
- native cancel/read/save behavior is registered by the Developer Web without
  a route-level deny switch suitable for this boundary;
- the special `__adk_agent_builder_assistant` is loadable by `AgentLoader`;
- its `write_files` and `delete_files` tools support Python and other arbitrary
  project files.

Reliable interception would therefore require maintaining a complete security
proxy in front of behavior that remains independently writable. Phase 4 does
not weaken the draft/published boundary to preserve the native canvas.

## Isolation and credentials

The fallback is a one-shot project CLI, not a web service. It has no listening
port, CORS surface, model client, operator session, `operator-console`
credential, or `adk-developer` credential. Its isolated contract subprocess
receives a scrubbed environment, blocks socket connections, and performs only
ADK config-model validation. It does not invoke ADK `from_config`, resolve
Python references, instantiate agents/tools/callbacks, or execute tools.

Phase 3 Developer Web remains a separate launcher child with its existing
fixed loopback port, code-first agents root, capability, and workspace. The
Workflow Lab draft root never contains `developer_workflows/`, `src/`, the
Workflow Catalog, or a link/mount to those locations.
