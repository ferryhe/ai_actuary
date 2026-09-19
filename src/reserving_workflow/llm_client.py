"""Shared OpenAI-compatible chat helper for the optional model slots.

The runtime keeps numeric truth deterministic, but two slots may optionally
call a model, each with its own provider, model id, and credential:

- reviewer  (``review/ai_reviewer.py``) — advisory guidance on review packets
- narrative (``ai_narrative.py``)       — optional drafting of narrative text

Both slots read their configuration from the environment on every call, so
planning, review, and drafting can use different providers at the same time.
Anything OpenAI-compatible works: pass a ``*_BASE_URL`` and a matching key.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

DISABLED_VALUES = {"0", "false", "no", "off"}
EMPTY_RESPONSE_ERROR = "empty_model_response: the model returned no usable content"
_PROVIDER_KEY_HINTS = {"deepseek": "DEEPSEEK_API_KEY"}


def env_first(*names: str, default: str | None = None) -> str | None:
    """First non-empty environment value among ``names``."""

    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def provider_key_fallbacks(model: str | None, base_url: str | None) -> tuple[str, ...]:
    """Provider-owned key names, so one `.env` key can serve several slots."""

    hint = f"{model or ''} {base_url or ''}".lower()
    return tuple(name for token, name in _PROVIDER_KEY_HINTS.items() if token in hint)


def resolve_llm_settings(
    *,
    enabled_var: str,
    model_var: str,
    base_url_var: str,
    api_key_var: str,
    max_tokens_var: str,
    timeout_var: str,
    default_model: str,
    default_max_tokens: int,
    default_timeout: float,
    default_enabled: str = "1",
    fallback_base_url_var: str = "OPENAI_BASE_URL",
    fallback_api_key_var: str = "OPENAI_API_KEY",
) -> dict[str, Any]:
    """Resolve one model slot from the environment."""

    enabled_raw = env_first(enabled_var) or default_enabled
    raw_max_tokens = env_first(max_tokens_var) or str(default_max_tokens)
    try:
        max_tokens = int(raw_max_tokens)
    except ValueError:
        max_tokens = default_max_tokens
    raw_timeout = env_first(timeout_var) or str(default_timeout)
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = default_timeout
    model = env_first(model_var) or default_model
    base_url = env_first(base_url_var, fallback_base_url_var)
    return {
        "enabled": str(enabled_raw).strip().lower() not in DISABLED_VALUES,
        "model": model,
        "base_url": base_url,
        "api_key": env_first(
            api_key_var,
            *provider_key_fallbacks(model, base_url),
            fallback_api_key_var,
        ),
        "max_tokens": max_tokens,
        "timeout": timeout,
    }


def chat_json(
    *,
    system_prompt: str,
    user_prompt: str,
    settings: dict[str, Any],
    is_useful: Callable[[dict[str, Any]], bool] | None = None,
    attempts: int = 2,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call a chat model and parse JSON; returns ``(payload, error)``.

    Never raises. ``payload`` is ``None`` when ``error`` is set. An empty
    response is retried once before ``EMPTY_RESPONSE_ERROR`` is returned.
    """

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - declared by openai-agents
        return None, f"openai_sdk_unavailable: {exc}"

    client_kwargs: dict[str, Any] = {
        "api_key": settings.get("api_key"),
        "timeout": settings.get("timeout"),
    }
    if settings.get("base_url"):
        client_kwargs["base_url"] = settings["base_url"]
    useful = is_useful or (lambda payload: bool(payload))
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        client = OpenAI(**client_kwargs)
        last_payload: dict[str, Any] | None = None
        for _ in range(max(1, attempts)):
            content = _create_completion(client, settings, messages)
            payload = parse_model_json(content)
            if useful(payload):
                return payload, None
            last_payload = payload
        return last_payload, EMPTY_RESPONSE_ERROR
    except Exception as exc:  # noqa: BLE001 - optional slots must never break a run
        return None, describe_error(exc)


def _create_completion(client: Any, settings: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """Call chat completions, tolerating both token-limit parameter names."""

    try:
        completion = client.chat.completions.create(
            model=settings["model"],
            messages=messages,
            max_completion_tokens=settings["max_tokens"],
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        if "max_completion_tokens" not in str(exc):
            raise
        completion = client.chat.completions.create(
            model=settings["model"],
            messages=messages,
            max_tokens=settings["max_tokens"],
            response_format={"type": "json_object"},
        )
    choices = getattr(completion, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    return str(getattr(message, "content", "") or "")


def parse_model_json(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model response is not JSON")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response is not a JSON object")
    return parsed


def describe_error(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def truncate(value: Any, *, limit: int = 4000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return f"{text[:limit]}... [truncated]"
