# ADK Workflow Lab

The Phase 4 Workflow Lab is a controlled, declarative draft and Git-review
preparation surface for `google-adk==2.7.1`. Native ADK Visual Builder is not
exposed; the feasibility decision is recorded in
[ADR 0004](architecture/adr-0004-adk-workflow-lab-builder-fallback.md).

## Ownership

- Published resources live in the installable
  `reserving_workflow.developer_workflows` package and are accessed with
  `importlib.resources`.
- A launcher that needs a real directory can call
  `WorkflowLab.for_installed_runtime(state_root).materialize_published_workflows()`.
  The result is versioned, digest-addressed, server-owned, and read-only.
  On Windows it has a protected owner-Full/consumer-Read+Execute DACL and an
  inherited Medium Integrity `NoWriteUp` label. The isolated consumer is a
  scrubbed, privilege-disabled Low Integrity process, so it can read/traverse
  but cannot create, replace, rename, delete, or change ACLs. The server owner
  retains Full access for guarded cleanup. POSIX launchers use an independent
  low-privilege identity against `0555` directories and `0444` files; running
  the consumer as the server owner is outside this boundary.
- Source-checkout drafts live only at
  `tmp/adk-workflow-drafts/<app>/`.
- Immutable exports live only at
  `tmp/adk-workflow-exports/<export_id>/`.
- Git diff/export is available only when the draft is inside an explicit Git
  source checkout. Installed-wheel mode reports that Git publishing is
  unavailable instead of pretending the repository exists.

Both writable roots are covered by the repository's ignored `tmp/` root. A
draft contains only `root_agent.yaml`, `workflow_policy.yaml`, and optional
`sub_agents/*.yaml`. Python, plugins, archives, binary files, links, hardlinks,
junctions/reparse points, alternate data streams, reserved names, and
case-colliding paths are rejected. Basenames use an ASCII portable allowlist,
so Windows-forbidden/control characters and backslashes are rejected on every
host. Existing materialized resources are rechecked for identity, links,
reparse points, content digest, read-only permission state, owner, DACL, and
mandatory integrity label before reuse.

## Validation order

The order is fixed and reported in the validation result:

1. byte, file-count, path, link, identity, and regular-file preflight using
   handle-pinned duplicate reads with nanosecond metadata and content-digest
   stability checks;
2. safe YAML parsing with duplicate keys and anchors/aliases rejected;
3. recursive blocked-key and executable-reference checks;
4. pure-data validation against the byte-for-byte frozen ADK 2.7.1
   `AgentConfig.json` selected by an allowlisted built-in agent class;
5. project policy for model, Python FQN, control-plane `tool_id`, Workflow
   Catalog `workflow_id`, Phase 3 capability/workspace/confirmation, and
   Git-only publishing;
6. a scrubbed, offline subprocess that validates the locked ADK config models,
   resolves `config_path` relative to each referencing file as ADK 2.7.1 does,
   validates a root-reachable acyclic graph with unique agent names, and
   records zero model calls and zero external network calls.

Before step 6, and inside step 6, the Workflow Lab never calls ADK
`from_config`, resolves a FQN, imports draft-selected code, instantiates an
agent/tool/callback, or executes a tool. Tool `args`, custom agent classes,
callback/schema/model code references, sub-agent code references, and GenAI
tool configuration are rejected. The one approved custom-tool FQN is allowed
only in `tools[].name` and must also be declared in project policy.
Every `LlmAgent`, including referenced children, must explicitly declare the
single approved `gemini-2.5-flash` model; ADK defaults are never inherited.
`confirmation_required` must be YAML boolean `true`; numeric or string values
that merely compare equal or look truthy are rejected.

The project config surface is closed even when ADK 2.7.1 accepts additional
fields. Every agent may use only `agent_class`, `name`, `description`, and
name-only `sub_agents[].config_path` references. `SequentialAgent` and
`ParallelAgent` add no fields; `LoopAgent` may add a positive integer
`max_iterations`; `LlmAgent` may add the exact approved `model`, a plain-text
`instruction`, and name-only `tools`. All other ADK fields are rejected by
project policy. In particular, `static_instruction`, `output_key`, Content and
Part request data (`fileData`, `inlineData`, and `functionResponse`), tool
arguments, and callback/request-shaping fields are outside the approved
surface. This applies equally to the root, referenced children, and
unreferenced draft files.

## Validate and export

Create an isolated app draft, then run:

```bash
python scripts/validate_adk_workflow.py tmp/adk-workflow-drafts/<app>
python scripts/export_adk_workflow_diff.py tmp/adk-workflow-drafts/<app> --check
```

`--check` additionally requires the generated patch to pass
`git apply --check`; validation and source-integrity proof always run.

The export command creates a new immutable directory. The caller cannot choose
its ID, output path, or filenames. `manifest.json` is written last as the
exclusive-create commit marker. Each object is written, flushed, and read back
through one new handle; Windows creates the final name with `CREATE_NEW`, while
POSIX retains the staging descriptor through its exclusive same-directory
link and verifies that the final name has the same identity. An existing
object is never replaced.
Candidate YAML, manifest, and no-index patch use UTF-8, LF, sorted keys/files,
POSIX path labels, and SHA-256 v1 framing. They contain no time, export ID,
absolute path, branch name, or host separator.

The trusted project draft gateway, validation, and export share one per-app
lock. Gateway writes occur only inside `draft_write_session`; direct client
filesystem access is outside the Workflow Lab boundary. Windows input handles
deny write and delete sharing while bytes are copied into the server-owned
snapshot. POSIX gateway writes use the same lock, while duplicate pinned reads
and metadata/content rechecks detect out-of-band writers before parsed bytes
are accepted.

Published targets use a separate immutable snapshot rule. Only declared YAML
participates in the candidate/diff. The sole retained Python object may be the
app-root `__init__.py`, and it must be a canonical ASCII one-line docstring
stub with no executable statement. Its path, type, bytes, size, and digest are
bound into the published-tree digest and manifest even though the patch
preserves it. Authoritative `__pycache__`, `.pyc`, every other Python or
executable, and unknown objects fail closed. Patch labels are repository-relative under
`src/reserving_workflow/developer_workflows/<app>/`, so a temporary checkout
can run both `git apply --check` and an actual apply without touching the real
index or deleting package bookkeeping.

Every filesystem ancestor from the absolute volume root through the output
directory remains pinned for the full transaction. POSIX uses no-follow
directory descriptors and relative create/link/unlink operations; Windows
uses no-reparse directory handles that deliberately do not share delete
access. Cleanup and permission walkers never descend a symlink or junction.
After file handles are made read-only, all owned directories are locked; every
final pathname is then rebound to its pinned descriptor and its complete bytes
are revalidated, including the manifest commit marker. A final no-follow
topology scan must exactly match the declared candidate files, patch, manifest,
and directories; an extra file, directory, link, reparse point, or special
object rejects the complete export.
Unified patches preserve the standard no-final-newline marker and
use `/dev/null` for additions and deletions, so `git apply --check` parses them.

The deterministic input key contains the canonical draft digest, target
published-tree digest, ADK version, frozen-schema digest, policy digest, and
exporter version. The patch is generated from handle-pinned in-memory
snapshots; the exporter does not read or refresh the Git index. The command
wrapper separately captures the Git index bytes, all tracked bytes, `src/`,
published workflows, Workflow Catalog, and porcelain-v2 before and after and
fails if any protected state changes.

Direct Python callers receive an active `ExportReceipt` proof lease. They must
use it as a context manager or call `finalize()` only after their post-export
integrity decision. Until then, live descriptors keep the complete output tree
bound and nonreplaceable. Leaving the context with an exception, explicitly
revoking the receipt, or abandoning an active receipt removes only its
descriptor-bound manifest marker; successful context exit consumes the lease
and preserves the committed export. The CLI follows this lifecycle around its
post-export integrity check and result serialization.

All Git inspection sets `GIT_OPTIONAL_LOCKS=0`, including stale-index cases.
Tracked files, `src/`, published workflows, and the catalog are captured with
the same descriptor/handle-pinned no-follow rules. A symlink or Windows
junction/reparse object is recorded by its own target label or tag and is never
descended, so checkout-external bytes cannot enter an integrity digest.
Ignored and untracked filesystem objects are traversed without following links
and hashed by path, object type, and content digest without retaining secret
bytes. Empty directories, symlinks, and type replacements are included; FIFO,
device, socket, junction, and reparse contents are never opened. Only
`tmp/adk-workflow-drafts/**` and `tmp/adk-workflow-exports/**` are excluded.
Ignored run history, artifacts, reviews, credentials, environments, and other
state remain protected.

Validation/export do not create branches, commits, pushes, pull requests,
merges, online catalog writes, runs, artifacts, or reviews. Deleting a draft or
an export cannot address any published or historical runtime root.
