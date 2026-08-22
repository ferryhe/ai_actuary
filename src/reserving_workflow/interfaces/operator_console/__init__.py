"""Packaged Operator Console resource loader."""

from html import escape
from importlib.resources import files


ADK_DEVELOPER_WEB_URL_TOKEN = "__AI_ACTUARY_ADK_DEVELOPER_WEB_URL__"
DEFAULT_ADK_DEVELOPER_WEB_URL = "http://127.0.0.1:8001"


def load_operator_console_html() -> str:
    """Load the Operator Console document from its packaged resource."""

    return files(__package__).joinpath("console.html").read_text(encoding="utf-8")


def render_operator_console_html(
    *,
    adk_url: str = DEFAULT_ADK_DEVELOPER_WEB_URL,
) -> str:
    """Render the Operator Console with runtime-local development links."""

    html = load_operator_console_html()
    if ADK_DEVELOPER_WEB_URL_TOKEN not in html:
        raise RuntimeError("Operator Console ADK URL marker is missing")
    return html.replace(ADK_DEVELOPER_WEB_URL_TOKEN, escape(adk_url, quote=True), 1)


__all__ = [
    "ADK_DEVELOPER_WEB_URL_TOKEN",
    "DEFAULT_ADK_DEVELOPER_WEB_URL",
    "load_operator_console_html",
    "render_operator_console_html",
]
