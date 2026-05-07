from __future__ import annotations

import json
from pathlib import Path

from reserving_workflow.api.app import _run_validation_step
from reserving_workflow.contracts.control_plane import ValidatedToolInput


def test_run_validation_step_writes_manifest_once_with_run_manifest_artifact(tmp_path):
    result = _run_validation_step(
        case_id="validated-case",
        artifact_dir=tmp_path,
        tool_input=ValidatedToolInput(tool_id="chainladder", inputs={"sample_name": "RAA", "method_variant": "chainladder"}),
        case_payload={
            "case_id": "validated-case",
            "metadata": {"chainladder_sample": "RAA"},
            "run_config": {"method": "chainladder"},
        },
    )

    run_manifest_path = Path(result["final_output"]["artifact_manifest_path"])
    manifest_payload = json.loads(run_manifest_path.read_text(encoding="utf-8"))

    assert manifest_payload["artifact_paths"]["run_manifest"] == str(run_manifest_path.resolve())
    assert manifest_payload["artifact_paths"]["validated_input"].endswith("validated_input.json")
    assert manifest_payload["artifact_paths"]["case_input"].endswith("case_input.json")
    assert manifest_payload["artifact_paths"]["validation_result"].endswith("validation_result.json")
