from __future__ import annotations

import pytest

from reserving_workflow.workflows import WorkflowCatalog, WorkflowCatalogEntry, WorkflowStepEntry, build_builtin_workflow_catalog


def test_builtin_workflow_catalog_lists_chainladder_basic_summary():
    catalog = build_builtin_workflow_catalog()

    workflows = catalog.list_workflow_summaries()

    workflow_ids = {workflow["workflow_id"] for workflow in workflows}

    assert len(workflows) == 2
    assert workflow_ids == {"chainladder-basic", "chainladder-validated"}
    assert next(item for item in workflows if item["workflow_id"] == "chainladder-basic")["step_count"] == 1
    assert next(item for item in workflows if item["workflow_id"] == "chainladder-validated")["step_count"] == 2


def test_builtin_workflow_catalog_returns_chainladder_basic_detail():
    catalog = build_builtin_workflow_catalog()

    workflow = catalog.get_workflow("chainladder-basic")

    assert workflow.workflow_id == "chainladder-basic"
    assert workflow.steps[0].step_id == "chainladder"
    assert workflow.steps[0].tool_id == "chainladder"
    assert workflow.steps[0].step_kind == "execute"


def test_builtin_workflow_catalog_returns_validation_first_workflow_detail():
    catalog = build_builtin_workflow_catalog()

    workflow = catalog.get_workflow("chainladder-validated")

    assert workflow.workflow_id == "chainladder-validated"
    assert [step.step_id for step in workflow.steps] == ["validate", "execute"]
    assert [step.step_kind for step in workflow.steps] == ["validate", "execute"]


def test_workflow_catalog_rejects_duplicate_workflow_ids():
    first = WorkflowCatalogEntry(
        workflow_id="duplicate",
        title="First",
        description="First workflow.",
        steps=[WorkflowStepEntry(step_id="first", tool_id="chainladder", title="First step")],
    )
    second = WorkflowCatalogEntry(
        workflow_id="duplicate",
        title="Second",
        description="Second workflow.",
        steps=[WorkflowStepEntry(step_id="second", tool_id="chainladder", title="Second step")],
    )

    with pytest.raises(ValueError, match="Duplicate workflow id"):
        WorkflowCatalog(entries=[first, second])


def test_workflow_catalog_rejects_unsafe_workflow_ids():
    with pytest.raises(ValueError, match="workflow_id must be a single safe path component"):
        WorkflowCatalogEntry(
            workflow_id="../escape",
            title="Unsafe",
            description="Unsafe workflow.",
            steps=[WorkflowStepEntry(step_id="safe", tool_id="chainladder", title="Safe")],
        )


def test_workflow_catalog_rejects_unsafe_step_ids():
    with pytest.raises(ValueError, match="step_id must be a single safe path component"):
        WorkflowStepEntry(step_id="nested/escape", tool_id="chainladder", title="Unsafe step")
