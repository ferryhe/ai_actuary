from __future__ import annotations

from pathlib import Path

import pytest

import reserving_workflow.adapters.adk.workflow_lab as workflow_lab_module

pytest.importorskip(
    "google.adk", reason="Workflow Lab is provided by the adk-dev extra"
)

from reserving_workflow.adapters.adk.workflow_lab import (  # noqa: I001
    ADK_VERSION,
    BUILDER_DECISION,
    EXECUTABLE_REFERENCE_FIELDS,
    WorkflowLab,
    WorkflowLabError,
    load_frozen_agent_config_schema,
)


SAFE_AGENT = """\
agent_class: SequentialAgent
name: workflow_lab_example
description: A model-free declarative workflow used by the Workflow Lab.
"""

SAFE_POLICY = """\
schema_version: ai-actuary.workflow-policy.v1
capability: adk-developer
workspace_id: adk-development
confirmation_required: true
publishing: git-review-only
tool_ids:
  - chainladder
workflow_ids:
  - chainladder-basic
python_fqns: []
write_tool_ids: []
"""


def _draft_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "index").write_bytes(b"index-before")
    draft = repo / "tmp" / "adk-workflow-drafts" / "example"
    draft.mkdir(parents=True)
    (draft / "root_agent.yaml").write_text(SAFE_AGENT, encoding="utf-8")
    (draft / "workflow_policy.yaml").write_text(SAFE_POLICY, encoding="utf-8")
    return repo, draft


def _write_root(draft: Path, content: str) -> None:
    (draft / "root_agent.yaml").write_text(content, encoding="utf-8")


def test_builder_decision_is_evidence_bound_fallback() -> None:
    assert BUILDER_DECISION["decision"] == "FALLBACK"
    assert BUILDER_DECISION["adk_version"] == "2.7.1"
    assert BUILDER_DECISION["controlled_surface"] == "project-cli"
    evidence = " ".join(BUILDER_DECISION["evidence"])
    assert "tmp=false" in evidence
    assert "__adk_agent_builder_assistant" in evidence
    assert "write_files" in evidence
    assert BUILDER_DECISION["native_builder_exposed"] is False


def test_frozen_schema_is_local_locked_and_has_no_network_refs() -> None:
    schema_bytes, schema = load_frozen_agent_config_schema()

    assert ADK_VERSION == "2.7.1"
    assert len(schema_bytes) > 100_000
    assert schema["title"] == "AgentConfig"
    assert all(ref.startswith("#/") for ref in _all_refs(schema))


def test_executable_reference_inventory_covers_the_adk_2_7_1_surface() -> None:
    assert set(EXECUTABLE_REFERENCE_FIELDS) == {
        "agent_class",
        "sub_agents[].config_path",
        "sub_agents[].code",
        "before_agent_callbacks[].name",
        "after_agent_callbacks[].name",
        "model_code.name",
        "input_schema.name",
        "output_schema.name",
        "tools[].name",
        "tools[].args",
        "before_model_callbacks[].name",
        "after_model_callbacks[].name",
        "before_tool_callbacks[].name",
        "after_tool_callbacks[].name",
        "generate_content_config",
    }


@pytest.mark.parametrize(
    ("payload", "code", "stage"),
    [
        (
            "agent_class: SequentialAgent\nname: one\nname: two\n",
            "yaml_duplicate_key",
            "safe_yaml",
        ),
        (
            "agent_class: SequentialAgent\nname: !!python/object/apply:os.system ['id']\n",
            "yaml_unsafe_tag",
            "safe_yaml",
        ),
        (
            "agent_class: SequentialAgent\nname: &n one\ndescription: *n\n",
            "yaml_alias_forbidden",
            "safe_yaml",
        ),
        (
            "agent_class: SequentialAgent\nname: one\nargs: {command: id}\n",
            "blocked_key",
            "code_references",
        ),
        (
            "agent_class: SequentialAgent\nname: one\nunknown: value\n",
            "schema_invalid",
            "adk_schema",
        ),
    ],
)
def test_validation_order_fails_at_the_expected_stage(
    tmp_path: Path, payload: str, code: str, stage: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(draft, payload)

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == code
    assert caught.value.stage == stage
    expected_prefix = ["preflight"]
    if stage != "safe_yaml":
        expected_prefix.append("safe_yaml")
    if stage in {"adk_schema", "project_policy", "isolated_contract"}:
        expected_prefix.append("code_references")
    assert caught.value.completed_stages == expected_prefix


@pytest.mark.parametrize(
    "payload",
    [
        "agent_class: reserving_workflow.storage.local.LocalRunStore\nname: bad\n",
        "agent_class: LlmAgent\nname: bad\ninstruction: x\nmodel_code: {name: os.system}\n",
        "agent_class: LlmAgent\nname: bad\ninstruction: x\ninput_schema: {name: evil.schemas.Input}\n",
        "agent_class: LlmAgent\nname: bad\ninstruction: x\noutput_schema: {name: evil.schemas.Output}\n",
        "agent_class: LlmAgent\nname: bad\ninstruction: x\nbefore_model_callbacks: [{name: evil.callback}]\n",
        "agent_class: LlmAgent\nname: bad\ninstruction: x\nbefore_model_callbacks: [{name: reserving_workflow.adapters.adk.approved_tools.read_run_status}]\n",
        "agent_class: LlmAgent\nname: bad\ninstruction: x\ntools: [{name: google_search}]\n",
        "agent_class: SequentialAgent\nname: bad\nsub_agents: [{code: evil.agent.root_agent}]\n",
    ],
)
def test_every_python_reference_is_checked_before_schema_or_import(
    tmp_path: Path, payload: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(draft, payload)

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code in {"agent_class_forbidden", "python_fqn_forbidden"}
    assert caught.value.stage == "code_references"
    assert caught.value.completed_stages == ["preflight", "safe_yaml"]


def test_python_fqn_tool_id_and_workflow_id_have_separate_allowlists(
    tmp_path: Path,
) -> None:
    repo, draft = _draft_repo(tmp_path)
    policy = SAFE_POLICY.replace(
        "python_fqns: []",
        "python_fqns:\n  - reserving_workflow.adapters.adk.approved_tools.read_run_status",
    )
    (draft / "workflow_policy.yaml").write_text(policy, encoding="utf-8")
    _write_root(
        draft,
        """\
agent_class: LlmAgent
name: bounded_reader
model: gemini-2.5-flash
instruction: Read an already-governed run.
tools:
  - name: reserving_workflow.adapters.adk.approved_tools.read_run_status
""",
    )

    report = WorkflowLab.for_source_checkout(repo).validate("example")
    assert report.python_fqns == (
        "reserving_workflow.adapters.adk.approved_tools.read_run_status",
    )
    assert report.tool_ids == ("chainladder",)
    assert report.workflow_ids == ("chainladder-basic",)

    for field, unknown, code in (
        ("tool_ids", "unknown-tool", "tool_id_forbidden"),
        ("workflow_ids", "unknown-workflow", "workflow_id_forbidden"),
        ("python_fqns", "unknown.module.call", "python_fqn_forbidden"),
    ):
        bad = policy.replace(
            f"{field}:\n  - "
            + (
                "reserving_workflow.adapters.adk.approved_tools.read_run_status"
                if field == "python_fqns"
                else "chainladder"
                if field == "tool_ids"
                else "chainladder-basic"
            ),
            f"{field}:\n  - {unknown}",
        )
        (draft / "workflow_policy.yaml").write_text(bad, encoding="utf-8")
        with pytest.raises(WorkflowLabError) as caught:
            WorkflowLab.for_source_checkout(repo).validate("example")
        assert caught.value.code == code
        assert caught.value.stage == "project_policy"


def test_every_llm_agent_requires_the_explicit_approved_model(tmp_path: Path) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(
        draft,
        "agent_class: LlmAgent\nname: root_llm\ninstruction: bounded\n",
    )
    with pytest.raises(WorkflowLabError) as root_error:
        WorkflowLab.for_source_checkout(repo).validate("example")
    assert root_error.value.code == "model_forbidden"
    assert root_error.value.stage == "project_policy"

    _write_root(
        draft,
        """\
agent_class: SequentialAgent
name: root_sequence
sub_agents:
  - config_path: sub_agents/child.yaml
""",
    )
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    (sub_agents / "child.yaml").write_text(
        "agent_class: LlmAgent\nname: child_llm\ninstruction: bounded\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowLabError) as child_error:
        WorkflowLab.for_source_checkout(repo).validate("example")
    assert child_error.value.code == "model_forbidden"
    assert child_error.value.stage == "project_policy"


def test_project_agent_config_allowlist_is_explicit_for_every_agent_class() -> None:
    assert workflow_lab_module._APPROVED_AGENT_CONFIG_KEYS == {
        "LlmAgent": frozenset(
            {
                "agent_class",
                "name",
                "description",
                "sub_agents",
                "model",
                "instruction",
                "tools",
            }
        ),
        "LoopAgent": frozenset(
            {
                "agent_class",
                "name",
                "description",
                "sub_agents",
                "max_iterations",
            }
        ),
        "ParallelAgent": frozenset(
            {"agent_class", "name", "description", "sub_agents"}
        ),
        "SequentialAgent": frozenset(
            {"agent_class", "name", "description", "sub_agents"}
        ),
    }


@pytest.mark.parametrize(
    "agent",
    [
        "agent_class: SequentialAgent\nname: bounded_sequence\n",
        "agent_class: ParallelAgent\nname: bounded_parallel\n",
        "agent_class: LoopAgent\nname: bounded_loop\nmax_iterations: 3\n",
        (
            "agent_class: LlmAgent\n"
            "name: bounded_llm\n"
            "model: gemini-2.5-flash\n"
            "instruction: Read only governed run state.\n"
        ),
    ],
)
def test_each_approved_agent_class_accepts_only_its_minimal_surface(
    tmp_path: Path, agent: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(draft, agent)

    report = WorkflowLab.for_source_checkout(repo).validate("example")

    assert report.completed_stages[-1] == "isolated_contract"


@pytest.mark.parametrize("location", ["root", "referenced_child", "orphan_child"])
def test_static_content_uri_is_rejected_for_root_and_every_child(
    tmp_path: Path, location: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    unsafe = """\
agent_class: LlmAgent
name: unsafe_content
model: gemini-2.5-flash
instruction: bounded
static_instruction:
  role: user
  parts:
    - fileData:
        fileUri: https://example.invalid/private.pdf
        mimeType: application/pdf
"""
    if location == "root":
        _write_root(draft, unsafe)
    else:
        sub_agents = draft / "sub_agents"
        sub_agents.mkdir()
        (sub_agents / "child.yaml").write_text(unsafe, encoding="utf-8")
        if location == "referenced_child":
            _write_root(
                draft,
                "agent_class: SequentialAgent\n"
                "name: root_sequence\n"
                "sub_agents:\n"
                "  - config_path: sub_agents/child.yaml\n",
            )

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "agent_config_key_forbidden"
    assert caught.value.stage == "project_policy"
    assert caught.value.completed_stages == [
        "preflight",
        "safe_yaml",
        "code_references",
        "adk_schema",
    ]


@pytest.mark.parametrize(
    "unapproved",
    [
        "output_key: unsafe_output\n",
        "static_instruction:\n  inlineData:\n    data: cHJpdmF0ZQ==\n    mimeType: text/plain\n",
        "static_instruction:\n  functionResponse:\n    name: bypass\n    response: {output: unsafe}\n",
    ],
)
def test_unapproved_llm_request_shaping_and_part_variants_fail_closed(
    tmp_path: Path, unapproved: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(
        draft,
        "agent_class: LlmAgent\n"
        "name: unsafe_request\n"
        "model: gemini-2.5-flash\n"
        "instruction: bounded\n" + unapproved,
    )

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "agent_config_key_forbidden"
    assert caught.value.stage == "project_policy"


@pytest.mark.parametrize(
    "agent",
    [
        "agent_class: SequentialAgent\nname: bad_shape\nsub_agents: null\n",
        (
            "agent_class: LlmAgent\n"
            "name: bad_shape\n"
            "model: gemini-2.5-flash\n"
            "instruction: bounded\n"
            "tools: null\n"
        ),
        "agent_class: LoopAgent\nname: bad_shape\nmax_iterations: 0\n",
    ],
)
def test_project_agent_nested_shapes_are_closed_world(
    tmp_path: Path, agent: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(draft, agent)

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "agent_config_shape_forbidden"
    assert caught.value.stage == "project_policy"


def test_agent_config_key_case_variants_fail_before_contract_probe(
    tmp_path: Path,
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(
        draft,
        "agent_class: LlmAgent\n"
        "name: unsafe_case\n"
        "model: gemini-2.5-flash\n"
        "instruction: bounded\n"
        "Static_instruction: hidden\n",
    )

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "schema_invalid"
    assert caught.value.stage == "adk_schema"


@pytest.mark.parametrize("value", ["1", "1.0", "'true'", "'yes'"])
def test_confirmation_required_is_exactly_boolean_true(
    tmp_path: Path, value: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    policy = (draft / "workflow_policy.yaml").read_text(encoding="utf-8")
    (draft / "workflow_policy.yaml").write_text(
        policy.replace(
            "confirmation_required: true", f"confirmation_required: {value}"
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "confirmation_required"
    assert caught.value.stage == "project_policy"


def test_isolated_contract_accepts_valid_multilevel_relative_agent_graph(
    tmp_path: Path,
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(
        draft,
        """\
agent_class: SequentialAgent
name: root_sequence
sub_agents:
  - config_path: sub_agents/child.yaml
""",
    )
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    (sub_agents / "child.yaml").write_text(
        """\
agent_class: SequentialAgent
name: child_sequence
sub_agents:
  - config_path: grandchild.yaml
""",
        encoding="utf-8",
    )
    (sub_agents / "grandchild.yaml").write_text(
        "agent_class: SequentialAgent\nname: grandchild_sequence\n",
        encoding="utf-8",
    )

    report = WorkflowLab.for_source_checkout(repo).validate("example")

    assert report.completed_stages[-1] == "isolated_contract"


@pytest.mark.parametrize(
    "case", ["wrong_relative", "cycle", "unreachable", "duplicate_name"]
)
def test_isolated_contract_rejects_invalid_declarative_agent_graph(
    tmp_path: Path, case: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    _write_root(
        draft,
        """\
agent_class: SequentialAgent
name: root_sequence
sub_agents:
  - config_path: sub_agents/child.yaml
""",
    )
    sub_agents = draft / "sub_agents"
    sub_agents.mkdir()
    child_ref = (
        "sub_agents/grandchild.yaml" if case == "wrong_relative" else "grandchild.yaml"
    )
    (sub_agents / "child.yaml").write_text(
        f"agent_class: SequentialAgent\nname: child_sequence\nsub_agents:\n  - config_path: {child_ref}\n",
        encoding="utf-8",
    )
    grandchild_name = (
        "child_sequence" if case == "duplicate_name" else "grandchild_sequence"
    )
    grandchild_tail = (
        "sub_agents:\n  - config_path: child.yaml\n" if case == "cycle" else ""
    )
    (sub_agents / "grandchild.yaml").write_text(
        f"agent_class: SequentialAgent\nname: {grandchild_name}\n{grandchild_tail}",
        encoding="utf-8",
    )
    if case == "unreachable":
        (sub_agents / "orphan.yaml").write_text(
            "agent_class: SequentialAgent\nname: orphan_sequence\n",
            encoding="utf-8",
        )

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == "contract_invalid"
    assert caught.value.stage == "isolated_contract"


@pytest.mark.parametrize(
    ("needle", "replacement", "code"),
    [
        (
            "capability: adk-developer",
            "capability: operator-console",
            "capability_forbidden",
        ),
        (
            "workspace_id: adk-development",
            "workspace_id: default-workspace",
            "workspace_forbidden",
        ),
        (
            "confirmation_required: true",
            "confirmation_required: false",
            "confirmation_required",
        ),
        ("publishing: git-review-only", "publishing: direct", "publishing_forbidden"),
        (
            "write_tool_ids: []",
            "write_tool_ids:\n  - chainladder",
            "write_tool_forbidden",
        ),
    ],
)
def test_phase3_capability_and_publishing_policy_cannot_be_bypassed(
    tmp_path: Path, needle: str, replacement: str, code: str
) -> None:
    repo, draft = _draft_repo(tmp_path)
    (draft / "workflow_policy.yaml").write_text(
        SAFE_POLICY.replace(needle, replacement), encoding="utf-8"
    )

    with pytest.raises(WorkflowLabError) as caught:
        WorkflowLab.for_source_checkout(repo).validate("example")

    assert caught.value.code == code
    assert caught.value.stage == "project_policy"


def test_contract_probe_is_model_free_offline_and_uses_isolated_snapshot(
    tmp_path: Path,
) -> None:
    repo, _ = _draft_repo(tmp_path)

    report = WorkflowLab.for_source_checkout(repo).validate("example")

    assert report.completed_stages == (
        "preflight",
        "safe_yaml",
        "code_references",
        "adk_schema",
        "project_policy",
        "isolated_contract",
    )
    assert report.adk_version == "2.7.1"
    assert report.model_calls == 0
    assert report.external_network_calls == 0
    assert report.contract_agent_class == "SequentialAgent"
    assert report.snapshot_root != str(repo)


def _all_refs(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                refs.append(child)
            refs.extend(_all_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_all_refs(child))
    return refs
