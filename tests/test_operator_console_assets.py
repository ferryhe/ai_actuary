from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx

from reserving_workflow.api.app import ApiSettings, create_app
from reserving_workflow.interfaces.operator_console import load_operator_console_html


EXPECTED_CONSOLE_CHARACTER_COUNT = 53_797
EXPECTED_CONSOLE_UTF8_BYTE_COUNT = 53_825
EXPECTED_CONSOLE_SHA256 = "ad84892a79563005ca85b70c785e171ef899a34a8ddd3fddbd627d3e609ad0a3"


def test_operator_console_asset_matches_reviewed_pr1_document() -> None:
    body = load_operator_console_html()
    encoded = body.encode("utf-8")

    assert len(body) == EXPECTED_CONSOLE_CHARACTER_COUNT
    assert len(encoded) == EXPECTED_CONSOLE_UTF8_BYTE_COUNT
    assert hashlib.sha256(encoded).hexdigest() == EXPECTED_CONSOLE_SHA256


def test_console_route_serves_extracted_asset_without_behavior_change(tmp_path: Path) -> None:
    app = create_app(
        settings=ApiSettings(
            registry_path=tmp_path / "run-registry.json",
            artifact_root=tmp_path / "artifacts",
        )
    )

    async def get_console() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.get("/console")

    response = asyncio.run(get_console())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == load_operator_console_html()


def test_api_module_no_longer_embeds_console_document() -> None:
    app_source = (
        Path(__file__).parents[1] / "src" / "reserving_workflow" / "api" / "app.py"
    ).read_text(encoding="utf-8")

    assert "<!doctype html>" not in app_source
    assert "def _operator_console_html" not in app_source


def test_console_links_to_loopback_developer_web_without_scripted_behavior() -> None:
    body = load_operator_console_html()

    assert 'href="http://127.0.0.1:8001"' in body
    assert 'class="developer-web-link"' in body
    assert 'target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body
    assert ".developer-web-link { color: #d9e8ff; }" in body
    assert "development-only" in body.lower()
