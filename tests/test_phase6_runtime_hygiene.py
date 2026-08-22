from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import stat
import sys
import tomllib
import asyncio
from contextlib import redirect_stdout
from io import StringIO
from types import ModuleType, SimpleNamespace
from typing import Any
from pathlib import Path
import zipfile

import httpx
import pytest
from conftest import authenticated_request_kwargs, create_authenticated_app


def test_runtime_sanitizer_redacts_urls_paths_headers_and_credentials(tmp_path: Path) -> None:
    from reserving_workflow.runtime.redaction import sanitize_for_runtime

    payload = {
        "run_id": "adk-run-123",
        "operation_id": "op_123",
        "correlation_id": "corr_123",
        "message": "failed with Authorization: Bearer sk-secret1234567890",
        "query": "http://127.0.0.1:8000/callback?token=secret&run_id=adk-run-123",
        "artifact_root": str(tmp_path / "artifacts"),
        "cookie": "sessionid=secret",
        "adk_rotation": {
            "formerly_valid_status_before_rotation": 200,
            "rotation_status": 200,
            "rotated_credential_rejected_status": 401,
            "new_credential_accepted_status": 200,
            "new_credential": "browser-smoke-adk-credential-rotated",
        },
        "csrf_mutation": {
            "missing_csrf_status": 403,
            "invalid_csrf_status": 403,
        },
        "nested": {
            "review_store_path": str(tmp_path / "reviews"),
            "logical_id": "chainladder-basic",
        },
    }

    sanitized = sanitize_for_runtime(payload)

    assert sanitized["run_id"] == "adk-run-123"
    assert sanitized["operation_id"] == "op_123"
    assert sanitized["correlation_id"] == "corr_123"
    assert sanitized["message"] == "[redacted]"
    assert sanitized["query"] == "http://127.0.0.1:8000/callback?token=[redacted]&run_id=adk-run-123"
    assert sanitized["adk_rotation"] == {
        "formerly_valid_status_before_rotation": 200,
        "rotation_status": 200,
        "rotated_credential_rejected_status": 401,
        "new_credential_accepted_status": 200,
    }
    assert sanitized["csrf_mutation"] == {
        "missing_csrf_status": 403,
        "invalid_csrf_status": 403,
    }
    assert sanitized["nested"] == {"logical_id": "chainladder-basic"}
    serialized = json.dumps(sanitized)
    assert str(tmp_path) not in serialized
    assert "sk-secret" not in serialized
    assert "sessionid" not in serialized


def test_runtime_sanitizer_matrix_covers_phase6_surfaces(tmp_path: Path) -> None:
    from reserving_workflow.runtime.redaction import sanitize_for_runtime

    secret = "Authorization: Bearer sk-secret1234567890"
    absolute_path = str(tmp_path / "api-artifacts" / "run-1" / "run_manifest.json")
    payload = {
        surface: {
            "error_code": "stable_error_code",
            "logical_id": "chainladder-basic",
            "correlation_id": "corr_123",
            "message": f"{secret} at {absolute_path}",
            "callback": "http://127.0.0.1:8000/callback?token=secret&run_id=run-1",
            "cookie": "sessionid=secret",
            "artifact_root": absolute_path,
        }
        for surface in (
            "logs",
            "errors",
            "traces",
            "diagnostics",
            "tool_results",
            "evaluation_reports",
            "browser_visible_errors",
        )
    }

    sanitized = sanitize_for_runtime(payload)
    serialized = json.dumps(sanitized)

    for surface in payload:
        assert sanitized[surface]["error_code"] == "stable_error_code"
        assert sanitized[surface]["logical_id"] == "chainladder-basic"
        assert sanitized[surface]["correlation_id"] == "corr_123"
        assert sanitized[surface]["message"] == "[redacted]"
        assert sanitized[surface]["callback"] == "http://127.0.0.1:8000/callback?token=[redacted]&run_id=run-1"
        assert "cookie" not in sanitized[surface]
        assert "artifact_root" not in sanitized[surface]
    assert "sk-secret" not in serialized
    assert str(tmp_path) not in serialized
    assert "sessionid" not in serialized


def test_console_state_redacts_artifact_roots_at_browser_visible_boundary(tmp_path: Path) -> None:
    from reserving_workflow.api.app import ApiSettings

    class Task:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    class TaskContracts:
        WorkerTask = Task

    class Runner:
        @staticmethod
        def run_openai_governed_workflow(task: Task, *, user_prompt: str | None = None) -> dict[str, object]:
            del user_prompt
            artifact_dir = Path(task.inputs["artifact_dir"])
            artifact_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = artifact_dir / "run_manifest.json"
            packet_json = artifact_dir / "review_packet.json"
            packet_md = artifact_dir / "review_packet.md"
            review_packet = {
                "case_id": task.case_ref,
                "run_id": task.run_id,
                "status": "review_required",
                "failed_checks": ["threshold"],
                "packet_paths": {
                    "json": str(packet_json),
                    "markdown": str(packet_md),
                },
            }
            packet_json.write_text(json.dumps(review_packet), encoding="utf-8")
            packet_md.write_text("# Review Packet\n", encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {
                        "case_id": task.case_ref,
                        "run_id": task.run_id,
                        "artifact_root": str(artifact_dir),
                        "artifact_paths": {
                            "run_manifest": str(manifest_path),
                            "review_packet": str(packet_json),
                            "review_packet_markdown": str(packet_md),
                        },
                    }
                ),
                encoding="utf-8",
            )
            return {
                "worker_result": {
                    "status": "needs_review",
                    "case_id": task.case_ref,
                    "run_id": task.run_id,
                    "summary": "needs review",
                    "artifact_paths": {
                        "run_manifest": str(manifest_path),
                        "review_packet": str(packet_json),
                    },
                    "metrics": {},
                    "review_reasons": ["threshold"],
                    "errors": [],
                },
                "final_output": {"case_id": task.case_ref, "worker_status": "needs_review"},
                "review_packet": review_packet,
            }

    app = create_authenticated_app(
        settings=ApiSettings(
            registry_path=tmp_path / "run-registry.json",
            artifact_root=tmp_path / "artifacts",
        ),
        runner_module=Runner,
        task_contracts_module=TaskContracts,
    )

    async def request(method: str, path: str, **kwargs: object) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.request(
                method,
                path,
                **authenticated_request_kwargs(method, dict(kwargs)),
            )

    run = asyncio.run(request("POST", "/runs", json={"case_id": "visible-redaction"})).json()
    state = asyncio.run(request("GET", f"/console/state?run_id={run['run_id']}")).json()
    serialized = json.dumps(state)

    assert str(tmp_path) not in serialized
    assert "packet_paths" not in serialized
    assert "artifact_root" not in state["artifact_panel"]
    assert "artifact_root" not in state["selected_run"]
    assert state["artifact_panel"]["artifact_root_ref"] == f"run:{run['run_id']}:artifacts"
    assert state["artifact_panel"]["artifact_paths"]["run_manifest"] == "run_manifest.json"
    assert state["artifact_panel"]["artifact_paths"]["review_packet"] == "review_packet.json"
    assert (
        state["artifact_panel"]["artifact_paths"]["review_packet_markdown"]
        == "review_packet.md"
    )
    assert state["review_panel"]["packet"]["run_id"] == run["run_id"]
    assert state["review_panel"]["packet"]["failed_checks"] == ["threshold"]
    assert "json_path" not in state["review_panel"]
    assert "markdown_path" not in state["review_panel"]
    assert "record_path" not in state["review_panel"]


def test_tool_cli_error_json_sanitizes_exception_boundaries(tmp_path: Path) -> None:
    from reserving_workflow.tools_cli import _common

    sensitive = tmp_path / "private" / "run_manifest.json"

    def action() -> object:
        raise FileNotFoundError(f"missing {sensitive} with Authorization: Bearer sk-secret123456789")

    stream = StringIO()
    with redirect_stdout(stream):
        exit_code = _common.run_tool("chainladder", action)

    payload = json.loads(stream.getvalue())
    serialized = json.dumps(payload)
    assert exit_code == 1
    assert payload["error_category"] == "io_error"
    assert payload["tool_id"] == "chainladder"
    assert str(tmp_path) not in serialized
    assert "sk-secret" not in serialized


def test_cleanup_plan_classifies_exact_targets_and_preserves_business_state(
    tmp_path: Path,
) -> None:
    from reserving_workflow.runtime.cleanup import build_local_state_cleanup_plan

    repo_root = tmp_path / "repo"
    developer_target = repo_root / "tmp" / "adk-dev" / "sessions"
    business_target = repo_root / "tmp" / "api-artifacts"
    developer_target.mkdir(parents=True)
    business_target.mkdir(parents=True)
    (developer_target / "sessions.db").write_text("temporary", encoding="utf-8")
    (business_target / "run.json").write_text("business", encoding="utf-8")

    plan = build_local_state_cleanup_plan(repo_root)

    cleanup_targets = {
        item["state_id"]: item for item in plan["cleanup_targets"]
    }
    preserved_targets = {
        item["state_id"]: item for item in plan["preserved_targets"]
    }

    assert cleanup_targets["adk_sessions"]["target"] == str(developer_target.resolve())
    assert cleanup_targets["adk_sessions"]["exists"] is True
    assert cleanup_targets["adk_sessions"]["ownership"] == "developer"
    assert cleanup_targets["adk_sessions"]["retention"] == "delete_on_request"
    assert preserved_targets["business_artifacts"]["target"] == str(business_target.resolve())
    assert preserved_targets["business_artifacts"]["cleanup_allowed"] is False
    assert preserved_targets["run_registry"]["cleanup_allowed"] is False
    assert plan["summary"]["cleanup_target_count"] >= 1
    assert plan["summary"]["preserved_target_count"] >= 3


@pytest.mark.parametrize(
    "unsafe",
    [
        "",
        ".",
        "$AI_ACTUARY_TMP",
        "%AI_ACTUARY_TMP%",
        "tmp/*",
    ],
)
def test_cleanup_target_validation_rejects_ambiguous_roots_and_patterns(
    tmp_path: Path,
    unsafe: str,
) -> None:
    from reserving_workflow.runtime.cleanup import UnsafeCleanupTargetError, validate_cleanup_target

    with pytest.raises(UnsafeCleanupTargetError):
        validate_cleanup_target(unsafe, repo_root=tmp_path)


def test_cleanup_execution_removes_only_allowlisted_developer_targets(tmp_path: Path) -> None:
    from reserving_workflow.runtime.cleanup import execute_cleanup_plan

    repo_root = tmp_path / "repo"
    developer_target = repo_root / "tmp" / "adk-dev" / "sessions"
    business_target = repo_root / "tmp" / "reviews"
    developer_target.mkdir(parents=True)
    business_target.mkdir(parents=True)
    (developer_target / "sessions.db").write_text("temporary", encoding="utf-8")
    (business_target / "review_record.json").write_text("business", encoding="utf-8")

    result = execute_cleanup_plan(repo_root, dry_run=False)

    assert result["ok"] is True
    assert not developer_target.exists()
    assert (business_target / "review_record.json").read_text(encoding="utf-8") == "business"
    assert any(
        item["state_id"] == "adk_sessions" and item["status"] == "removed"
        for item in result["actions"]
    )
    assert all("reviews" not in item["target"] for item in result["actions"])


def test_pyproject_declares_stable_phase6_console_entry_points() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["scripts"] == {
        "ai-actuary-cleanup": "reserving_workflow.runtime.cleanup:main",
        "ai-actuary-package-audit": "reserving_workflow.runtime.package_audit:main",
        "ai-actuary-workbench": "reserving_workflow.cli.local_workbench:main",
    }


def test_package_resource_audit_checks_console_schema_and_workflow_resources() -> None:
    from reserving_workflow.runtime.package_audit import audit_package_resources

    payload = audit_package_resources()

    assert payload["ok"] is True
    assert payload["resources"]["operator_console"]["present"] is True
    assert payload["resources"]["adk_agent_schema"]["present"] is True
    assert payload["resources"]["workflow_lab_example"]["present"] is True
    assert payload["resources"]["developer_adk_app"]["present"] is True
    assert payload["resources"]["workflow_task_contracts"]["present"] is True
    assert payload["resources"]["workflow_openai_runner"]["present"] is True
    assert payload["google_adk_imported"] is False


def test_operator_entrypoint_loads_packaged_workflow_sources(monkeypatch, tmp_path: Path) -> None:
    import reserving_workflow.operator_entrypoint as operator_entrypoint

    installed_like_module = tmp_path / "Lib" / "site-packages" / "reserving_workflow" / "operator_entrypoint.py"
    installed_like_module.parent.mkdir(parents=True)
    installed_like_module.write_text("# installed-like module path", encoding="utf-8")
    monkeypatch.setattr(operator_entrypoint, "__file__", str(installed_like_module))

    task_contracts_path = operator_entrypoint._workflow_source_path(
        "agent-runtimes",
        "hermes-worker",
        "task_contracts.py",
    )

    assert task_contracts_path.is_file()
    assert task_contracts_path.name == "task_contracts.py"


def test_installed_workbench_reuses_shared_launcher_and_materializes_read_only_agents() -> None:
    from reserving_workflow.cli import local_workbench, workbench_launcher

    assert local_workbench.run_workbench is workbench_launcher.run_workbench
    module_text = Path(local_workbench.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in module_text
    assert "raise SystemExit(main())" in module_text

    with local_workbench._packaged_agents_dir() as agents_dir:
        assert agents_dir.name == "developer_workflows"
        assert (agents_dir / "ai_actuary_developer" / "agent.py").is_file()
        writable_files = [
            path
            for path in agents_dir.rglob("*")
            if path.is_file() and (path.stat().st_mode & stat.S_IWRITE)
        ]
        assert writable_files == []

    assert not agents_dir.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL immutability regression")
def test_packaged_agents_dir_is_effectively_immutable_on_windows() -> None:
    from reserving_workflow.cli import local_workbench

    with local_workbench._packaged_agents_dir() as agents_dir:
        agent_file = agents_dir / "ai_actuary_developer" / "agent.py"
        assert agent_file.is_file()
        mutation_failures = local_workbench._prove_tree_effectively_read_only(agents_dir)

    assert set(mutation_failures) == {"create", "replace", "rename", "delete"}
    assert not agents_dir.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL privacy regression")
def test_local_workbench_state_directories_are_owner_private_on_windows(tmp_path: Path) -> None:
    from reserving_workflow.adapters.adk import local_runtime

    state_dir = tmp_path / "state" / "sessions"
    metadata = local_runtime._secure_directory(state_dir)

    assert metadata["private"] is True
    assert "Authenticated Users" not in " ".join(metadata["unsafe_principals"])
    assert "BUILTIN\\Users" not in " ".join(metadata["unsafe_principals"])


def test_windows_acl_privacy_parser_rejects_unknown_and_guest_principals() -> None:
    from reserving_workflow.adapters.adk import local_runtime

    acl_text = "\n".join(
        [
            r"C:\state BUILTIN\Administrators:(F)",
            r"          NT AUTHORITY\SYSTEM:(F)",
            r"          EXAMPLE\current-user:(F)",
            r"          BUILTIN\Guests:(RX)",
            r"          EXAMPLE\unknown-service:(RX)",
            "Successfully processed 1 files; Failed processing 0 files",
        ]
    )

    metadata = local_runtime._windows_privacy_metadata_from_text(
        acl_text,
        path=Path(r"C:\state"),
        allowed_principals={
            "BUILTIN\\ADMINISTRATORS",
            "NT AUTHORITY\\SYSTEM",
            "EXAMPLE\\CURRENT-USER",
        },
    )

    assert metadata["private"] is False
    assert any("Guests" in item for item in metadata["unsafe_principals"])
    assert any("unknown-service" in item for item in metadata["unsafe_principals"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL privacy regression")
def test_local_workbench_state_privacy_removes_foreign_guest_ace_on_windows(tmp_path: Path) -> None:
    import subprocess
    from reserving_workflow.adapters.adk import local_runtime

    state_dir = tmp_path / "state" / "api-artifacts"
    state_dir.mkdir(parents=True)
    subprocess.run(
        ["icacls", str(state_dir), "/grant", "BUILTIN\\Guests:(RX)"],
        check=True,
        capture_output=True,
        text=True,
    )

    metadata = local_runtime._secure_directory(state_dir)

    assert metadata["private"] is True
    assert "Guests" not in " ".join(metadata["unsafe_principals"])


def test_local_workbench_config_prepares_business_and_developer_state_roots(tmp_path: Path) -> None:
    from reserving_workflow.adapters.adk.local_runtime import LocalWorkbenchConfig

    config = LocalWorkbenchConfig.from_repo_root(tmp_path)

    config.prepare_state_directories()

    for relative in (
        "tmp",
        "tmp/adk-dev/sessions",
        "tmp/adk-dev/traces",
        "tmp/adk-dev/artifacts",
        "tmp/adk-workflow-drafts",
        "tmp/adk-workflow-exports",
        "tmp/adk-evaluations",
        "tmp/local-workbench-diagnostics",
        "tmp/api-artifacts",
        "tmp/reviews",
        "tmp/review-outbox",
        "tmp/batch",
    ):
        assert (tmp_path / relative).is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL privacy regression")
def test_prepared_state_root_makes_new_registry_private_on_windows(tmp_path: Path) -> None:
    from reserving_workflow.adapters.adk import local_runtime
    from reserving_workflow.adapters.adk.local_runtime import LocalWorkbenchConfig

    config = LocalWorkbenchConfig.from_repo_root(tmp_path)

    config.prepare_state_directories()
    registry_path = tmp_path / "tmp" / "run-registry.json"
    registry_path.write_text('{"runs":[]}', encoding="utf-8")

    metadata = local_runtime._windows_privacy_metadata(registry_path)

    assert metadata["private"] is True
    assert metadata["unsafe_principals"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod fail-closed regression")
def test_local_workbench_state_privacy_fails_closed_when_chmod_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pathlib import Path as PathClass
    from reserving_workflow.adapters.adk import local_runtime

    original_chmod = PathClass.chmod

    def failing_chmod(self: Path, mode: int, *args: object, **kwargs: object) -> None:
        if self == tmp_path / "state":
            raise OSError("chmod denied")
        original_chmod(self, mode, *args, **kwargs)

    monkeypatch.setattr(PathClass, "chmod", failing_chmod)

    with pytest.raises(PermissionError, match="Unable to secure"):
        local_runtime._secure_directory(tmp_path / "state")


def test_browser_smoke_pins_playwright_and_requires_review_boundary_by_default() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    browser_smoke = _load_browser_smoke_module()

    assert metadata["project"]["optional-dependencies"]["browser-smoke"] == [
        f"playwright=={browser_smoke.EXPECTED_PLAYWRIGHT_VERSION}"
    ]
    assert browser_smoke.EXPECTED_CHROMIUM_VERSION == "147.0.7727.15"
    assert browser_smoke.parse_args([]).exercise_review_boundary is False
    assert browser_smoke.parse_args(["--exercise-review-boundary"]).exercise_review_boundary is True
    script_text = Path("scripts/browser_smoke_local_workbench.py").read_text(encoding="utf-8")
    assert "Deprecated no-op retained" in script_text
    assert "_verify_adk_console_api_parity_and_review_boundary" in script_text


def test_browser_smoke_starts_full_run_through_adk_developer_protocol() -> None:
    browser_smoke = _load_browser_smoke_module()
    requested_urls: list[str] = []

    class Response:
        def __init__(self, payload: object, *, status: int = 200) -> None:
            self._payload = payload
            self.status = status

        def json(self) -> object:
            return self._payload

    class Request:
        def post(self, url: str, **kwargs: object) -> Response:
            requested_urls.append(url)
            if url.endswith("/sessions/browser-smoke-session"):
                return Response({"id": "browser-smoke-session", "events": []})
            if url.endswith("/run_sse"):
                return Response(
                    'data: {"content":{"parts":[{"functionResponse":{"name":"start_workflow_run","response":{"ok":true,"run_id":"run-1","operation_id":"op-1","correlation_id":"corr-1"}}}]}}\n\n'
                )
            raise AssertionError(f"unexpected POST {url}")

        def get(self, url: str, **kwargs: object) -> Response:
            requested_urls.append(url)
            return Response({"id": "browser-smoke-session", "events": []})

    class Context:
        request = Request()

    result = browser_smoke._start_workflow_run_through_adk_developer_web(
        Context(),
        target=browser_smoke.SmokeTarget(
            api_url="http://127.0.0.1:9000",
            adk_url="http://127.0.0.1:9001",
            api_port=9000,
            adk_port=9001,
        ),
        workflow_id="chainladder-basic",
        case_id="case-1",
        inputs={"review_threshold_origin_count": 2},
        session_id="browser-smoke-session",
        invocation_id="browser-smoke-invocation",
        timeout=1.0,
    )

    assert result["run_id"] == "run-1"
    assert any(url == "http://127.0.0.1:9001/run_sse" for url in requested_urls)
    assert not any(url == "http://127.0.0.1:9000/adk/runs" for url in requested_urls)


def test_browser_smoke_requires_adk_post_run_session_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser_smoke = _load_browser_smoke_module()
    requested_urls: list[str] = []

    class Response:
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class Request:
        def get(self, url: str, **kwargs: object) -> Response:
            requested_urls.append(url)
            if url.endswith("/sessions/browser-smoke-conversation"):
                return Response(
                    {
                        "id": "browser-smoke-conversation",
                        "events": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "function_call": {
                                                "name": "adk_request_confirmation",
                                                "id": "confirm-1",
                                            }
                                        }
                                    ]
                                }
                            },
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "function_response": {
                                                "name": "start_workflow_run",
                                                "response": {
                                                    "run_id": "run-1",
                                                    "operation_id": "op-1",
                                                    "correlation_id": "corr-1",
                                                },
                                            }
                                        }
                                    ]
                                }
                            },
                        ],
                    }
                )
            if "/debug/trace/session/" in url:
                return Response(
                    {
                        "spans": [
                            {
                                "name": "ai_actuary.start_workflow_run",
                                "trace_id": 123,
                                "span_id": 456,
                                "attributes": {
                                    "gcp.vertex.agent.session_id": "browser-smoke-conversation",
                                    "gen_ai.conversation.id": "browser-smoke-conversation",
                                    "ai_actuary.adk.invocation_id": "browser-smoke-invocation",
                                    "ai_actuary.run_id": "run-1",
                                    "ai_actuary.operation_id": "op-1",
                                    "ai_actuary.correlation_id": "corr-1",
                                    "ai_actuary.workflow_id": "chainladder-basic",
                                    "ai_actuary.case_id": "case-1",
                                },
                            }
                        ]
                    }
                )
            raise AssertionError(f"unexpected GET {url}")

    class Context:
        request = Request()

    monkeypatch.setattr(
        browser_smoke,
        "_inspect_workflow_run_through_adk_developer_web",
        lambda *args, **kwargs: {
            "summary": {
                "run_id": "run-1",
                "status": "needs_review",
                "review_status": "review_required",
                "artifact_ids": ["run_manifest", "workflow_summary", "review_packet"],
            },
            "adk_events": [{"functions": ["summarize_run"]}],
        },
    )

    result = browser_smoke._capture_adk_developer_session_evidence(
        Context(),
        target=browser_smoke.SmokeTarget(
            api_url="http://127.0.0.1:9000",
            adk_url="http://127.0.0.1:9001",
            api_port=9000,
            adk_port=9001,
        ),
        session_id="browser-smoke-conversation",
        invocation_id="browser-smoke-invocation",
        run_id="run-1",
        operation_id="op-1",
        correlation_id="corr-1",
        review_id="review-run-1",
        expected_status="needs_review",
        expected_review_status="review_required",
        expected_artifact_ids={"run_manifest", "workflow_summary", "review_packet"},
        evidence_dir=tmp_path,
        timeout=1.0,
    )

    evidence = json.loads((tmp_path / "adk_developer_session_after_run.json").read_text())
    assert result["post_run_evidence"] == "adk_developer_session_after_run.json"
    assert result["trace_available"] is True
    assert evidence["summary"]["review_status"] == "review_required"
    assert len(evidence["conversation_events"]) == 2
    assert evidence["trace_evidence"]["span_count"] == 1
    assert any(url == "http://127.0.0.1:9001/apps/ai_actuary_developer/users/browser-smoke-user/sessions/browser-smoke-conversation" for url in requested_urls)


def test_browser_smoke_parses_wrapped_adk_summary_events() -> None:
    browser_smoke = _load_browser_smoke_module()

    result = browser_smoke._find_adk_summary_result(
        [
            {
                "content": {
                    "parts": [
                        {
                            "function_response": {
                                "name": "summarize_run",
                                "response": {
                                    "ok": True,
                                    "run_id": "run-1",
                                    "summary": {
                                        "ok": True,
                                        "data": {
                                            "run_id": "run-1",
                                            "status": "needs_review",
                                            "review_status": "review_required",
                                            "artifact_ids": ["run_manifest"],
                                        },
                                    },
                                },
                            }
                        }
                    ]
                }
            }
        ],
        run_id="run-1",
    )

    assert result is not None
    assert result["summary"]["status"] == "needs_review"


def test_browser_smoke_evidence_json_uses_shared_sanitizer(tmp_path: Path) -> None:
    browser_smoke = _load_browser_smoke_module()
    evidence_path = tmp_path / "evidence.json"

    browser_smoke._write_json(
        evidence_path,
        {
            "run_id": "run-1",
            "message": f"failed at {tmp_path / 'private' / 'trace.zip'}",
            "headers": {"Authorization": "Bearer sk-secret123456789"},
            "artifact_root": str(tmp_path / "artifacts"),
        },
    )

    payload = evidence_path.read_text(encoding="utf-8")
    assert "run-1" in payload
    assert str(tmp_path) not in payload
    assert "sk-secret" not in payload


def test_browser_smoke_evidence_scanner_rejects_retained_secret_path_and_cookie_leaks(
    tmp_path: Path,
) -> None:
    browser_smoke = _load_browser_smoke_module()

    (tmp_path / "local_workbench.stdout.log").write_text(
        f"Diagnostics log: {tmp_path / 'tmp' / 'local-workbench-diagnostics' / 'launcher.jsonl'}\n",
        encoding="utf-8",
    )
    (tmp_path / "network_summary.json").write_text(
        json.dumps(
            {
                "headers": {"Authorization": "Bearer browser-smoke-adk-credential"},
                "cookie": "sessionid=browser-smoke-session",
                "artifact_root": str(tmp_path / "api-artifacts"),
            }
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(tmp_path / "trace.zip", "w") as archive:
        archive.writestr(
            "trace.trace",
            "Cookie: sessionid=browser-smoke-session\n"
            "Authorization: Bearer browser-smoke-adk-credential\n"
            f"path={tmp_path / 'profile-b' / '.venv' / 'Lib' / 'site-packages'}",
        )

    scan = browser_smoke.scan_evidence_tree(tmp_path)

    assert scan["ok"] is False
    assert scan["leak_count"] >= 3
    assert {item["kind"] for item in scan["leaks"]} >= {
        "credential",
        "cookie",
        "host_path",
    }


def test_browser_smoke_sanitizes_child_logs_and_trace_archive_before_scan(tmp_path: Path) -> None:
    browser_smoke = _load_browser_smoke_module()

    raw_stdout = tmp_path / "local_workbench.stdout.raw.log"
    raw_stdout.write_text(
        "Diagnostics log: C:\\Project\\ai_actuary_issue40\\tmp\\local-workbench-diagnostics\\launcher.jsonl\n",
        encoding="utf-8",
    )
    browser_smoke._sanitize_text_file(
        raw_stdout,
        tmp_path / "local_workbench.stdout.log",
    )
    with zipfile.ZipFile(tmp_path / "trace.raw.zip", "w") as archive:
        archive.writestr(
            "trace.network",
            "Authorization: Bearer browser-smoke-adk-credential\nCookie: sessionid=abc",
        )

    browser_smoke._sanitize_trace_archive(
        tmp_path / "trace.raw.zip",
        tmp_path / "trace.zip",
    )

    assert browser_smoke.scan_evidence_tree(tmp_path)["ok"] is True
    assert "C:\\Project" not in (tmp_path / "local_workbench.stdout.log").read_text(encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "trace.zip") as archive:
        payload = archive.read("trace.network").decode("utf-8")
    assert "browser-smoke-adk-credential" not in payload
    assert "sessionid=abc" not in payload


def test_browser_smoke_evidence_scanner_includes_result_metadata(tmp_path: Path) -> None:
    browser_smoke = _load_browser_smoke_module()

    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "evidence_dir": "/tmp/pytest-of-runner/pytest-0/browser-evidence",
                "route": "route:runs/{run_id}/review",
                "status": 200,
            }
        ),
        encoding="utf-8",
    )

    scan = browser_smoke.scan_evidence_tree(tmp_path)

    assert scan["ok"] is False
    assert any(item["file"] == "result.json" for item in scan["leaks"])


def test_browser_smoke_trace_evidence_requires_nonempty_correlated_spans(tmp_path: Path) -> None:
    browser_smoke = _load_browser_smoke_module()

    class Response:
        status = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"spans": [{"name": "unrelated"}]}

    class Request:
        @staticmethod
        def get(url: str, **kwargs: object) -> Response:
            del url, kwargs
            return Response()

    class Context:
        request = Request()

    with pytest.raises(browser_smoke.BrowserSmokeError, match="trace evidence"):
        browser_smoke._fetch_adk_trace_evidence(
            Context(),
            target=browser_smoke.SmokeTarget(
                api_url="http://127.0.0.1:9000",
                adk_url="http://127.0.0.1:9001",
                api_port=9000,
                adk_port=9001,
            ),
            session_id="browser-smoke-session",
            invocation_id="browser-smoke-invocation",
            run_id="run-1",
            correlation_id="corr-1",
            evidence_dir=tmp_path,
            timeout=1.0,
        )
    summary = json.loads((tmp_path / "adk_debug_trace_summary.json").read_text(encoding="utf-8"))
    assert summary["endpoint_status"] == 200
    assert summary["record_count"] == 1
    assert summary["joined_span"] is None
    assert summary["run_linked"] is False
    assert summary["correlation_linked"] is False


def test_browser_smoke_trace_evidence_rejects_session_event_only_fallback() -> None:
    browser_smoke = _load_browser_smoke_module()

    class Response:
        status = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"spans": [{"name": "adk-debug-span", "session_id": "browser-smoke-session"}]}

    class Request:
        @staticmethod
        def get(url: str, **kwargs: object) -> Response:
            del url, kwargs
            return Response()

    class Context:
        request = Request()

    with pytest.raises(browser_smoke.BrowserSmokeError, match="real ADK trace"):
        browser_smoke._fetch_adk_trace_evidence(
            Context(),
            target=browser_smoke.SmokeTarget(
                api_url="http://127.0.0.1:9000",
                adk_url="http://127.0.0.1:9001",
                api_port=9000,
                adk_port=9001,
            ),
            session_id="browser-smoke-session",
            invocation_id="browser-smoke-invocation",
            run_id="run-1",
            correlation_id="corr-1",
            session_events=[
                {
                    "event": "ordinary-session-event",
                    "session_id": "browser-smoke-session",
                    "invocation_id": "browser-smoke-invocation",
                    "run_id": "run-1",
                    "correlation_id": "corr-1",
                }
            ],
            timeout=1.0,
        )


def test_adk_start_workflow_trace_span_noops_without_opentelemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from developer_workflows.ai_actuary_developer import tools

    monkeypatch.setattr(tools, "_optional_otel_trace", lambda: None)

    tools._record_start_workflow_trace_span(
        session_id="browser-smoke-session",
        invocation_id="browser-smoke-invocation",
        workflow_id="chainladder-basic",
        case_id="case-1",
        result={
            "ok": True,
            "data": {
                "run_id": "run-1",
                "operation_id": "op-1",
                "correlation_id": "corr-1",
            },
        },
    )


def test_adk_start_workflow_trace_span_emits_safe_logical_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from developer_workflows.ai_actuary_developer import tools

    captured: dict[str, str] = {}

    class Span:
        def __enter__(self) -> "Span":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        @staticmethod
        def set_attribute(key: str, value: str) -> None:
            captured[key] = value

    class Tracer:
        @staticmethod
        def start_as_current_span(name: str) -> Span:
            captured["span_name"] = name
            return Span()

    monkeypatch.setattr(
        tools,
        "_optional_otel_trace",
        lambda: SimpleNamespace(get_tracer=lambda name: Tracer()),
    )

    tools._record_start_workflow_trace_span(
        session_id="browser-smoke-session",
        invocation_id="browser-smoke-invocation",
        workflow_id="chainladder-basic",
        case_id="case-1",
        result={
            "ok": True,
            "data": {
                "run_id": "run-1",
                "operation_id": "op-1",
                "correlation_id": "corr-1",
            },
        },
    )

    assert captured == {
        "span_name": "ai_actuary.start_workflow_run",
        "gen_ai.conversation.id": "browser-smoke-session",
        "gcp.vertex.agent.session_id": "browser-smoke-session",
        "ai_actuary.adk.invocation_id": "browser-smoke-invocation",
        "ai_actuary.run_id": "run-1",
        "ai_actuary.operation_id": "op-1",
        "ai_actuary.correlation_id": "corr-1",
        "ai_actuary.workflow_id": "chainladder-basic",
        "ai_actuary.case_id": "case-1",
    }


def test_browser_smoke_agent_direct_execution_records_project_span() -> None:
    agent_text = Path("developer_workflows/ai_actuary_developer/agent.py").read_text(
        encoding="utf-8"
    )
    direct_start = agent_text.index(
        "read_tools._default_execution_client_factory().start_workflow_run"
    )
    span_call = agent_text.index("read_tools._record_start_workflow_trace_span")
    response_event = agent_text.index(
        'yield _browser_smoke_function_response_event(\n            self.name,\n            ctx,\n            "start_workflow_run"',
        direct_start,
    )
    assert direct_start < span_call < response_event
    direct_path = agent_text[direct_start:response_event]
    assert 'result={"ok": True, "data": result}' in direct_path
    assert "session_id=str(ctx.session.id)" in direct_path
    assert "invocation_id=str(ctx.invocation_id)" in direct_path


def test_browser_smoke_trace_evidence_accepts_real_session_project_span() -> None:
    browser_smoke = _load_browser_smoke_module()
    requested: list[str] = []

    class Response:
        def __init__(self, payload: object, *, status: int = 200) -> None:
            self._payload = payload
            self.status = status

        def json(self) -> object:
            return self._payload

    class Request:
        @staticmethod
        def get(url: str, **kwargs: object) -> Response:
            del kwargs
            requested.append(url)
            if url.endswith("/debug/trace/session/browser-smoke-session"):
                return Response(
                    [
                        {
                            "name": "ai_actuary.start_workflow_run",
                            "trace_id": 123,
                            "span_id": 456,
                            "attributes": {
                                "gcp.vertex.agent.session_id": "browser-smoke-session",
                                "gen_ai.conversation.id": "browser-smoke-session",
                                "ai_actuary.adk.invocation_id": "browser-smoke-invocation",
                                "ai_actuary.run_id": "run-1",
                                "ai_actuary.operation_id": "op-1",
                                "ai_actuary.correlation_id": "corr-1",
                                "ai_actuary.workflow_id": "chainladder-basic",
                                "ai_actuary.case_id": "case-1",
                            },
                        }
                    ]
                )
            if url.endswith("/debug/trace/event-start"):
                return Response({"detail": "Trace not found"}, status=404)
            return Response({})

    class Context:
        request = Request()

    result = browser_smoke._fetch_adk_trace_evidence(
        Context(),
        target=browser_smoke.SmokeTarget(
            api_url="http://127.0.0.1:9000",
            adk_url="http://127.0.0.1:9001",
            api_port=9000,
            adk_port=9001,
        ),
        session_id="browser-smoke-session",
        invocation_id="browser-smoke-invocation",
        run_id="run-1",
        correlation_id="corr-1",
        session_events=[{"id": "event-start"}],
        timeout=1.0,
    )

    assert result["source"] == "adk_debug_trace"
    assert result["record_count"] == 1
    assert result["event_trace_count"] == 0
    assert result["joined_span"]["name"] == "ai_actuary.start_workflow_run"
    assert result["joined_span"]["attributes"]["ai_actuary.run_id"] == "run-1"
    assert result["run_or_correlation_linked"] is True
    assert any(url.endswith("/debug/trace/event-start") for url in requested)


def test_browser_smoke_rendered_adk_parity_requires_visible_post_run_fields(tmp_path: Path) -> None:
    browser_smoke = _load_browser_smoke_module()

    class Locator:
        def inner_text(self, *, timeout: float) -> str:
            del timeout
            return (
                "run-1 needs_review corr-1 op-1 review_required "
                "run_manifest workflow_summary review_packet"
            )

    class Page:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def goto(self, url: str, **kwargs: object) -> None:
            del kwargs
            self.urls.append(url)

        def wait_for_timeout(self, timeout: int) -> None:
            del timeout

        def locator(self, selector: str) -> Locator:
            assert selector == "body"
            return Locator()

        def screenshot(self, *, path: str, full_page: bool) -> None:
            del full_page
            Path(path).write_bytes(b"png")

    page = Page()
    result = browser_smoke._capture_rendered_adk_post_run_evidence(
        page,
        target=browser_smoke.SmokeTarget(
            api_url="http://127.0.0.1:9000",
            adk_url="http://127.0.0.1:9001",
            api_port=9000,
            adk_port=9001,
        ),
        session_id="browser-smoke-session",
        run_id="run-1",
        operation_id="op-1",
        correlation_id="corr-1",
        expected_status="needs_review",
        expected_review_status="review_required",
        expected_artifact_ids={"run_manifest", "workflow_summary", "review_packet"},
        evidence_dir=tmp_path,
        timeout=1.0,
    )

    assert result["checked"] is True
    assert result["screenshot"] == "adk_developer_web_post_run.png"
    assert result["surface"] == "adk_developer_web_ui"
    assert result["source"] != "adk_developer_web_session"
    assert not any(
        "/apps/ai_actuary_developer/users/browser-smoke-user/sessions/browser-smoke-session"
        in url
        for url in page.urls
    )
    assert page.urls == ["http://127.0.0.1:9001/"]


def test_browser_smoke_adk_confirmation_headers_are_bound_to_payload_and_key() -> None:
    browser_smoke = _load_browser_smoke_module()
    payload = {
        "workflow_id": "chainladder-basic",
        "case_id": "case-1",
        "inputs": {"review_threshold_origin_count": 2},
        "adk_app": "ai_actuary_developer",
        "adk_session_id": "session-1",
        "adk_invocation_id": "invocation-1",
    }

    headers = browser_smoke._adk_start_headers(
        payload=payload,
        idempotency_key="idempotency-1",
        adk_credential="test-adk-credential",
    )

    fingerprint = hashlib.sha256(
        browser_smoke._canonical_json(payload).encode("utf-8")
    ).hexdigest()
    expected_confirmation = hmac.new(
        b"test-adk-credential",
        f"idempotency-1:{fingerprint}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert headers["Authorization"] == "Bearer test-adk-credential"
    assert headers["Idempotency-Key"] == "idempotency-1"
    assert headers["X-ADK-Confirmation"] == expected_confirmation


def test_browser_smoke_cleanup_port_evidence_false_fails_success_path() -> None:
    browser_smoke = _load_browser_smoke_module()

    assert browser_smoke._cleanup_ports_released(
        {"ports": {"api": {"released": True}, "adk": {"released": True}}}
    )
    assert not browser_smoke._cleanup_ports_released(
        {"ports": {"api": {"released": False}, "adk": {"released": True}}}
    )


def test_browser_smoke_negative_host_origin_and_credential_rotation_are_required() -> None:
    script_text = Path("scripts/browser_smoke_local_workbench.py").read_text(encoding="utf-8")

    assert "_verify_negative_host_origin_rejection" in script_text
    assert "_verify_rotated_credential_rejects_formerly_valid_adk" in script_text
    assert "_verify_csrf_mutation_rejection" in script_text
    assert "rotated_credential_rejected_status" in script_text
    assert "missing_csrf_status" in script_text
    assert "invalid_csrf_status" in script_text
    assert "invalid_old_credential" not in script_text


def test_browser_smoke_rotation_probe_uses_no_cookie_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_smoke = _load_browser_smoke_module()
    calls: list[dict[str, Any]] = []
    statuses = iter([200, 200, 401, 200])

    def fake_no_cookies(
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> int:
        del timeout
        calls.append(
            {
                "url": url,
                "method": method,
                "data": data,
                "headers": dict(headers or {}),
            }
        )
        assert "Cookie" not in (headers or {})
        return next(statuses)

    class CookieCarryingRequest:
        @staticmethod
        def get(*args: object, **kwargs: object) -> None:
            del args, kwargs
            pytest.fail("rotation probe must not use Playwright's cookie-carrying context")

        post = get

    monkeypatch.setattr(browser_smoke, "_http_status_no_cookies", fake_no_cookies)

    result = browser_smoke._verify_rotated_credential_rejects_formerly_valid_adk(
        SimpleNamespace(request=CookieCarryingRequest()),
        target=browser_smoke.SmokeTarget(
            api_url="http://127.0.0.1:8000",
            adk_url="http://127.0.0.1:8001",
            api_port=8000,
            adk_port=8001,
        ),
        run_id="adk-run-1",
        formerly_valid_adk_credential="browser-smoke-adk-credential",
        evidence_dir=tmp_path,
        timeout=1.0,
    )

    assert result == {
        "checked": True,
        "run_id": "adk-run-1",
        "formerly_valid_status_before_rotation": 200,
        "rotation_status": 200,
        "rotated_credential_rejected_status": 401,
        "new_credential_accepted_status": 200,
    }
    assert [call["method"] for call in calls] == ["GET", "POST", "GET", "GET"]
    assert calls[1]["data"] == b'{"new_credential":"browser-smoke-adk-credential-rotated"}'
    evidence = json.loads((tmp_path / "adk_rotation_status.json").read_text(encoding="utf-8"))
    assert evidence["rotated_credential_rejected_status"] == 401
    assert "browser-smoke-adk-credential" not in json.dumps(evidence)


def test_launcher_diagnostics_record_child_identity_and_exit_reason(tmp_path: Path) -> None:
    from reserving_workflow.adapters.adk.local_runtime import LocalWorkbenchConfig
    from reserving_workflow.cli import workbench_launcher

    config = LocalWorkbenchConfig(
        repo_root=tmp_path,
        agents_dir=tmp_path / "developer_workflows",
        state_root=tmp_path / "adk-dev",
        session_database=tmp_path / "adk-dev" / "sessions" / "sessions.db",
        artifact_directory=tmp_path / "adk-dev" / "artifacts",
        diagnostics_log=tmp_path / "diagnostics" / "launcher.jsonl",
        control_plane_port=8123,
        adk_port=8124,
    )
    diagnostics = workbench_launcher.LauncherDiagnostics(config)

    class FailedProcess:
        pid = 4321

        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int:
            return 7

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 7

    child = FailedProcess()
    result = workbench_launcher.supervise_children(
        [child],
        smoke_check=None,
        stop_requested=lambda: False,
        poll_interval=0,
        diagnostics=diagnostics,
        child_identities=[
            {
                "component": "control_plane",
                "pid": 4321,
                "command_label": "python -m uvicorn",
                "port": 8123,
            }
        ],
    )

    events = [
        json.loads(line)
        for line in config.diagnostics_log.read_text(encoding="utf-8").splitlines()
    ]
    child_exit = next(item for item in events if item["event"] == "child_exit")
    launcher_exit = next(item for item in events if item["event"] == "launcher_exit")
    assert result == 7
    assert child_exit["details"]["component"] == "control_plane"
    assert child_exit["details"]["pid"] == 4321
    assert child_exit["details"]["exit_code"] == 7
    assert child_exit["details"]["reason"] == "child_exited"
    assert launcher_exit["details"]["shutdown_cause"] == "child_exit"


def test_launcher_preflight_failure_records_terminal_diagnostics_and_mode_aware_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reserving_workflow.adapters.adk.local_runtime import LocalWorkbenchConfig
    from reserving_workflow.cli import workbench_launcher

    config = LocalWorkbenchConfig(
        repo_root=tmp_path,
        agents_dir=tmp_path / "developer_workflows",
        state_root=tmp_path / "adk-dev",
        session_database=tmp_path / "adk-dev" / "sessions" / "sessions.db",
        artifact_directory=tmp_path / "adk-dev" / "artifacts",
        diagnostics_log=tmp_path / "diagnostics" / "launcher.jsonl",
        control_plane_port=8123,
        adk_port=8124,
    )
    monkeypatch.setattr(
        workbench_launcher,
        "validate_adk_runtime",
        lambda: (_ for _ in ()).throw(
            workbench_launcher.LauncherError(
                f"ADK Developer Web is not installed. Run: {workbench_launcher.install_hint()}"
            )
        ),
    )

    assert workbench_launcher.run_workbench(config, smoke=True, disable_adk=False) == 1

    events = [
        json.loads(line)
        for line in config.diagnostics_log.read_text(encoding="utf-8").splitlines()
    ]
    failure = next(item for item in events if item["event"] == "startup_failed")
    details = failure["details"]
    assert details["readiness"] == "preflight_failed"
    assert details["failure"]["category"] == "missing_adk"
    assert details["terminal_exit_code"] == 1
    assert details["shutdown_cause"] == "missing_adk"
    assert details["components"]["control_plane"]["status"] == "not_started"
    assert details["exit_code_mapping"]["startup_or_runtime_failure"] == 1
    assert "diagnostics" in details["diagnostics_ref"]


def test_launcher_port_conflict_recovery_hint_does_not_recommend_reinstall(tmp_path: Path) -> None:
    from reserving_workflow.adapters.adk.local_runtime import LocalWorkbenchConfig
    from reserving_workflow.cli import workbench_launcher

    config = LocalWorkbenchConfig(
        repo_root=tmp_path,
        agents_dir=tmp_path / "developer_workflows",
        state_root=tmp_path / "adk-dev",
        session_database=tmp_path / "adk-dev" / "sessions" / "sessions.db",
        artifact_directory=tmp_path / "adk-dev" / "artifacts",
        diagnostics_log=tmp_path / "diagnostics" / "launcher.jsonl",
        control_plane_port=8123,
        adk_port=8124,
    )

    details = workbench_launcher.startup_failure_details(
        config,
        workbench_launcher.LauncherError(
            "Loopback port conflict on 8123; no child process was started."
        ),
        component="preflight",
        disable_adk=False,
    )

    assert details["failure"]["category"] == "port_conflict"
    assert "pip install" not in details["failure"]["recovery_hint"]
    assert "--api-port" in details["failure"]["recovery_hint"]
    assert "--adk-port" in details["failure"]["recovery_hint"]


def test_launcher_runtime_failure_records_actual_child_state_and_hint(tmp_path: Path) -> None:
    from reserving_workflow.adapters.adk.local_runtime import LocalWorkbenchConfig
    from reserving_workflow.cli import workbench_launcher

    config = LocalWorkbenchConfig(
        repo_root=tmp_path,
        agents_dir=tmp_path / "developer_workflows",
        state_root=tmp_path / "adk-dev",
        session_database=tmp_path / "adk-dev" / "sessions" / "sessions.db",
        artifact_directory=tmp_path / "adk-dev" / "artifacts",
        diagnostics_log=tmp_path / "diagnostics" / "launcher.jsonl",
        control_plane_port=8123,
        adk_port=8124,
    )

    details = workbench_launcher.startup_failure_details(
        config,
        workbench_launcher.ReadinessTimeout("http://127.0.0.1:8124/dev-ui"),
        component="readiness",
        disable_adk=False,
        component_status={
            "control_plane": {"status": "ready", "port": 8123},
            "adk_developer_web": {"status": "starting", "port": 8124},
        },
        child_identities=[
            {
                "component": "control_plane",
                "pid": 1234,
                "command_label": "python -m uvicorn",
                "port": 8123,
            },
            {
                "component": "adk_developer_web",
                "pid": 1235,
                "command_label": "adk web",
                "port": 8124,
            },
        ],
        pending_endpoint="http://127.0.0.1:8124/dev-ui",
    )

    assert details["failure"]["category"] == "readiness_timeout"
    assert "pending endpoint" in details["failure"]["recovery_hint"]
    assert details["pending_endpoint"] == "endpoint:adk_developer_web"
    assert details["components"]["control_plane"]["status"] == "ready"
    assert details["components"]["adk_developer_web"]["status"] == "starting"
    assert details["child_identities"][0]["component"] == "control_plane"
    assert details["child_identities"][1]["pid"] == 1235


def test_installed_recovery_hint_does_not_require_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reserving_workflow.cli import workbench_launcher

    monkeypatch.setattr(workbench_launcher, "running_from_source_checkout", lambda: False)

    assert workbench_launcher.install_hint() == 'pip install "ai-actuary[api,adk-dev]"'
    assert "-e" not in workbench_launcher.install_hint()


def test_browser_smoke_runner_is_model_free_and_review_required(tmp_path: Path) -> None:
    from reserving_workflow.runtime import browser_smoke_runner

    class Task:
        inputs = {"artifact_dir": str(tmp_path), "case_payload": {"case_id": "case-1"}}
        run_id = "run-1"
        case_ref = "case-1"

    result = browser_smoke_runner.run_openai_governed_workflow(Task())

    assert result["worker_result"]["status"] == "needs_review"
    assert result["review_packet"]["run_id"] == "run-1"
    for filename in (
        "case_input.json",
        "deterministic_result.json",
        "narrative_draft.json",
        "constitution_check.json",
        "review_packet.json",
        "review_packet.md",
        "run_manifest.json",
    ):
        assert (tmp_path / filename).is_file()


def test_phase6_active_docs_are_indexed_and_relative_links_resolve() -> None:
    active_docs = _active_docs_from_index()
    for path in active_docs:
        assert path.is_file(), f"missing active doc: {path}"

    root_readme = Path("README.md").read_text(encoding="utf-8")
    docs_index = Path("docs/README.md").read_text(encoding="utf-8")
    workbench_doc = Path("docs/adk-local-workbench.md").read_text(encoding="utf-8")

    assert "docs/adk-local-workbench.md" in root_readme
    assert "adk-local-workbench.md" in docs_index
    assert "- `project-plan.md`" in docs_index
    assert "archive/project-plan.md" in docs_index
    assert "Issue #40 release-review checklist" in docs_index
    assert "Status: Active." in workbench_doc
    assert "Historical ADK planning" in workbench_doc
    assert "notes remain under `docs/archive/`" in workbench_doc
    assert "AI_ACTUARY_BROWSER_SMOKE_RUNNER=1" in workbench_doc
    assert "owner-private" in workbench_doc
    assert "effectively immutable" in workbench_doc
    assert "baseline \u2192 candidate \u2192 baseline" in workbench_doc
    assert "[api,adk-dev,browser-smoke]" in workbench_doc
    assert ".venv/Scripts/ai-actuary-package-audit.exe" in workbench_doc
    assert "LocalReviewStore" in workbench_doc
    assert "pip uninstall google-adk" in workbench_doc
    assert "pip install --force-reinstall" in workbench_doc
    assert "Port conflict" in workbench_doc
    project_plan = Path("docs/project-plan.md").read_text(encoding="utf-8")
    completed_section = project_plan.split("## Not Yet Implemented", 1)[0]
    assert "Issue #40 is in release-review" in project_plan
    assert "Issue #40 / ADK Roadmap Phase 6" not in completed_section
    assert "adk-local-workbench.md" in Path("docs/archive/README.md").read_text(encoding="utf-8")

    combined_active_docs = "\n".join(path.read_text(encoding="utf-8") for path in active_docs)
    forbidden_stale_phrases = (
        "PR1 does not add",
        "PR1 configures neither",
        "trace/evaluation capabilities not being wired",
        "validates and records the intended permission model",
        "through PR15",
        "Current Stage",
    )
    for phrase in forbidden_stale_phrases:
        assert phrase not in combined_active_docs
    unsupported_package_audit_commands = (
        r"reserving_workflow\.cli\.package_audit",
        r"ai-actuary-package-audit(?:\.exe)?\s+--state-root\b",
        r"package_audit\s+--state-root\b",
    )
    for pattern in unsupported_package_audit_commands:
        assert re.search(pattern, combined_active_docs) is None

    for doc in active_docs:
        body = doc.read_text(encoding="utf-8")
        for target in _markdown_relative_links(body):
            resolved = (doc.parent / target).resolve()
            assert resolved.exists(), f"{doc} links to missing relative target {target}"


def test_rollback_summary_binds_baseline_candidate_restored_wheels_and_sanitizes_paths(
    tmp_path: Path,
) -> None:
    from reserving_workflow.runtime.rollback import build_rollback_summary

    summary = build_rollback_summary(
        baseline_commit="baseline-commit",
        baseline_wheel={"path": tmp_path / "baseline.whl", "sha256": "b" * 64},
        candidate_wheel={"path": tmp_path / "candidate.whl", "sha256": "c" * 64},
        restored_wheel={"path": tmp_path / "baseline.whl", "sha256": "b" * 64},
        install_steps=[
            {"stage": "baseline", "command": "pip install baseline.whl", "exit_code": 0},
            {"stage": "candidate", "command": "pip install candidate.whl", "exit_code": 0},
            {"stage": "restored", "command": "pip install baseline.whl", "exit_code": 0},
        ],
        stage_proofs={
            "baseline": {
                "version": "0.1.0",
                "import_path": tmp_path / "baseline" / "site-packages",
                "dependencies_complete": True,
                "entry_points": [],
                "distribution_metadata": {"Name": "ai-actuary", "Version": "0.1.0"},
                "business_core_read": {"ok": True, "registry_records": 1, "review_records": 1},
            },
            "candidate": {
                "version": "0.1.0",
                "import_path": tmp_path / "candidate" / "site-packages",
                "dependencies_complete": True,
                "entry_points": ["ai-actuary-workbench", "ai-actuary-package-audit"],
                "distribution_metadata": {"Name": "ai-actuary", "Version": "0.1.0"},
                "business_core_read": {"ok": True, "registry_records": 1, "review_records": 1},
                "resource_audit": {"ok": True},
            },
            "restored": {
                "version": "0.1.0",
                "import_path": tmp_path / "restored" / "site-packages",
                "dependencies_complete": True,
                "entry_points": [],
                "distribution_metadata": {"Name": "ai-actuary", "Version": "0.1.0"},
                "business_core_read": {"ok": True, "registry_records": 1, "review_records": 1},
            },
        },
        business_state_checksums={
            "before_candidate": "a" * 64,
            "after_candidate": "a" * 64,
            "after_rollback": "a" * 64,
        },
        backup_restore={
            "backup_created": True,
            "restore_command": "copy backup registry",
            "artifact_root": str(tmp_path / "api-artifacts"),
        },
        resource_audits={"candidate": {"ok": True}},
        schema_compatibility={"fail_closed": False},
    )
    serialized = json.dumps(summary)

    assert summary["ok"] is True
    assert summary["candidate_wheel"]["sha256"] == "c" * 64
    assert summary["candidate_wheel"]["wheel_artifact"] == "candidate.whl"
    assert summary["candidate_wheel"]["wheel_ref"] == "wheel:candidate.whl"
    assert summary["baseline_wheel"]["sha256"] == "b" * 64
    assert summary["business_state"]["preserved"] is True
    assert summary["stage_proofs"]["baseline"]["entry_points"] == []
    assert summary["stage_proofs"]["candidate"]["entry_points"] == [
        "ai-actuary-workbench",
        "ai-actuary-package-audit",
    ]
    assert summary["stage_proofs"]["candidate"]["dependencies_complete"] is True
    assert summary["stage_proofs"]["candidate"]["business_core_read"]["ok"] is True
    assert summary["stage_proofs"]["candidate"]["resource_audit"]["ok"] is True
    assert str(tmp_path) not in serialized
    assert "artifact_root" not in serialized
    assert "filename" not in serialized


def _markdown_relative_links(body: str) -> list[Path]:
    links: list[Path] = []
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", body):
        raw_target = match.group(1).split("#", 1)[0].strip()
        if (
            not raw_target
            or raw_target.startswith(("http://", "https://", "mailto:"))
            or raw_target.startswith("#")
        ):
            continue
        links.append(Path(raw_target))
    return links


def _active_docs_from_index() -> list[Path]:
    docs_index = Path("docs/README.md")
    body = docs_index.read_text(encoding="utf-8")
    active: list[Path] = [Path("README.md"), docs_index]
    in_current = False
    for line in body.splitlines():
        if line.startswith("## Current documents"):
            in_current = True
            continue
        if line.startswith("## ") and in_current:
            break
        if not in_current:
            continue
        match = re.search(r"- `([^`]+)`", line)
        if not match:
            continue
        target = Path(match.group(1))
        resolved = (docs_index.parent / target).resolve()
        active.append(resolved.relative_to(Path.cwd().resolve()))
    return sorted(set(active), key=lambda item: item.as_posix())


def _load_browser_smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "browser_smoke_local_workbench",
        Path("scripts/browser_smoke_local_workbench.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
