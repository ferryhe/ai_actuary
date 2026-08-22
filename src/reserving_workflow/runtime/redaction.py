"""Shared redaction helpers for runtime-facing envelopes and diagnostics."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit


REDACTED = "[redacted]"
_SAFE_STATUS_KEYS = {
    "formerly_valid_status_before_rotation",
    "rotation_status",
    "rotated_credential_rejected_status",
    "new_credential_accepted_status",
    "missing_csrf_status",
    "invalid_csrf_status",
}

_PEM_PRIVATE_KEY_MARKER = re.compile(
    r"(?i)-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
_URL_USERINFO = re.compile(
    r"(?i)\b[A-Z][A-Z0-9+.-]{1,31}://[^\s/@]{1,256}@"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:"
    r"\bauthorization\s*:?\s*basic\s+\S+|"
    r"\bbasic\s+[A-Za-z0-9+/=_-]+|"
    r"\bbearer\s+\S+|"
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])|"
    r"\bsessionid\s*[:=]\s*\S+|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}|"
    r"(?<![A-Za-z0-9])gh[po]_[A-Za-z0-9]{8,}|"
    r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{8,}|"
    r"(?<![A-Za-z0-9])xox[a-z]-[A-Za-z0-9-]{8,}|"
    r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}|"
    r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{20,}"
    r")"
)
_SENSITIVE_WORDS = re.compile(
    r"\b(?:secret|password|passphrase|credentials?|cookies?|sessionid|"
    r"shared[-_ ]?secret|registry[-_ ]?path|review[-_ ]?store|"
    r"artifact[-_ ]?root|temp(?:orary)?[-_ ]?dir|file[-_ ]?name|basic[-_ ]?value)\b",
    re.IGNORECASE,
)
_CREDENTIAL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "code",
    "cookie",
    "key",
    "password",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
}


def sanitize_for_runtime(value: Any) -> Any:
    """Return a JSON-safe value with paths and credential-bearing data removed."""

    if isinstance(value, dict):
        return {
            str(key): sanitize_for_runtime(item)
            for key, item in value.items()
            if not is_sensitive_key(str(key))
        }
    if isinstance(value, list):
        return [sanitize_for_runtime(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_runtime(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return "[unsupported]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported]"


def sanitize_text(value: str) -> str:
    """Redact sensitive strings while preserving ordinary logical IDs."""

    sanitized_url = _sanitize_url(value)
    if sanitized_url is not None:
        return sanitized_url
    if looks_like_absolute_path(value) or looks_sensitive(value):
        return REDACTED
    return value


def is_sensitive_key(key: str) -> bool:
    candidate = key.strip()
    if candidate in _SAFE_STATUS_KEYS:
        return False
    if (
        looks_like_absolute_path(candidate)
        or _PEM_PRIVATE_KEY_MARKER.search(candidate)
        or _URL_USERINFO.search(candidate)
    ):
        return True
    tokens = _semantic_tokens(candidate)
    compact = "".join(tokens)
    if _has_sensitive_semantics(tokens, compact=compact):
        return True
    path_suffixes = (
        "path",
        "paths",
        "root",
        "roots",
        "filename",
        "filenames",
        "url",
        "urls",
    )
    if compact != "curl" and compact.endswith(path_suffixes):
        return True
    if compact in {"reviewdelivery", "operatorparams"}:
        return True
    if "apikey" in compact or "accesskey" in compact:
        return True
    if compact in {
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "session",
        "sessionid",
        "authheader",
        "authorization",
        "apikey",
        "accesskey",
        "privatekey",
        "clientsecret",
        "secretkey",
        "accesstoken",
        "refreshtoken",
        "sharedsecret",
        "tokenvalue",
        "authtoken",
        "registrypath",
        "reviewstorepath",
        "artifactroot",
        "temppath",
        "tempdir",
        "filename",
        "basicvalue",
    }:
        return True
    sensitive_tokens = set(tokens) & {
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "passphrases",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "session",
        "sessions",
        "secret",
        "auth",
        "authentication",
        "authorization",
        "header",
        "headers",
        "private",
    }
    if sensitive_tokens:
        return True
    if any(
        token == "token"
        and (index + 1 >= len(tokens) or tokens[index + 1] != "count")
        for index, token in enumerate(tokens)
    ):
        return True
    normalized = "_".join(tokens)
    if normalized in {
        "path",
        "paths",
        "filename",
        "url",
        "registry_path",
        "artifact_root",
        "artifact_paths",
        "manifest_path",
        "record_path",
        "json_path",
        "markdown_path",
        "review_delivery",
        "review_store",
        "review_store_dir",
        "operator_params",
        "temp_dir",
        "secret",
        "token",
        "api_key",
        "access_key",
        "private_key",
        "client_secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "session",
        "session_id",
        "sessionid",
        "auth",
        "authentication",
        "authorization",
        "header",
        "headers",
        "private",
    }:
        return True
    return bool(tokens and tokens[-1] in {"path", "paths", "root"})


def looks_like_absolute_path(value: str) -> bool:
    candidate = value.strip()
    return (
        bool(re.search(r"(?i)file:(?://|\\\\)", candidate))
        or bool(re.search(r"[A-Za-z]:[\\/]", candidate))
        or bool(re.search(r"(?:\\\\|(?<!:)//)[^\\/\s]+[\\/][^\\/\s]+", candidate))
        or bool(
            re.search(
                r"(?:^|[^A-Za-z0-9._~\\/])\\(?![\\\s])[^\\/\s]+(?:[\\/][^\\/\s]+)*",
                candidate,
            )
        )
        or bool(re.search(r"(?:^|[^A-Za-z0-9._~/])/(?![/\s])", candidate))
    )


def looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    if (
        _contains_sensitive_assignment(value)
        or _PEM_PRIVATE_KEY_MARKER.search(value)
        or _URL_USERINFO.search(value)
    ):
        return True
    tokens = _semantic_tokens(value)
    if _has_sensitive_semantics(tokens):
        return True
    if any(
        (token in {"api", "access"} and next_token == "key")
        or (token in {"auth", "authorization"} and next_token == "header")
        for token, next_token in zip(tokens, tokens[1:])
    ):
        return True
    if any(
        token == "token"
        and (index + 1 >= len(tokens) or tokens[index + 1] != "count")
        for index, token in enumerate(tokens)
    ):
        return True
    return bool(_SENSITIVE_WORDS.search(lowered) or _SECRET_VALUE.search(value))


def _sanitize_url(value: str) -> str | None:
    if "://" not in value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return REDACTED
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return REDACTED
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold() in _CREDENTIAL_QUERY_KEYS for key, _ in query):
        safe_query = "&".join(
            f"{key}={REDACTED}" if key.casefold() in _CREDENTIAL_QUERY_KEYS else f"{key}={raw_value}"
            for key, raw_value in query
        )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, safe_query, parsed.fragment))
    if parsed.scheme.lower() == "file":
        return REDACTED
    return value


def _semantic_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    current: list[str] = []
    for index, character in enumerate(value):
        if not character.isalnum():
            if current:
                tokens.append("".join(current).casefold())
                current = []
            continue
        previous = value[index - 1] if index else ""
        following = value[index + 1] if index + 1 < len(value) else ""
        if (
            character.isupper()
            and current
            and (
                previous.islower()
                or previous.isdigit()
                or (previous.isupper() and following.islower())
            )
        ):
            tokens.append("".join(current).casefold())
            current = []
        current.append(character)
    if current:
        tokens.append("".join(current).casefold())
    return tuple(tokens)


def _contains_sensitive_assignment(value: str) -> bool:
    assignment_boundary = "\r\n;,&|()[]{}"
    segment_start = 0
    for index, character in enumerate(value):
        if character in assignment_boundary:
            segment_start = index + 1
            continue
        if character not in {":", "="}:
            continue
        start = max(segment_start, index - 128)
        candidate = value[start:index].strip()
        if candidate and is_sensitive_key(candidate):
            return True
        segment_start = index + 1
    return False


def _has_sensitive_semantics(
    tokens: tuple[str, ...],
    *,
    compact: str | None = None,
) -> bool:
    joined = compact if compact is not None else "".join(tokens)
    if any(
        marker in joined
        for marker in (
            "apikey",
            "accesskey",
            "privatekey",
            "secretkey",
            "authheader",
        )
    ):
        return True
    if set(tokens) & {
        "auth",
        "authentication",
        "authorization",
        "header",
        "headers",
    }:
        return True
    return any(
        token == "token"
        and (index + 1 >= len(tokens) or tokens[index + 1] != "count")
        for index, token in enumerate(tokens)
    )


__all__ = [
    "REDACTED",
    "is_sensitive_key",
    "looks_like_absolute_path",
    "looks_sensitive",
    "sanitize_for_runtime",
    "sanitize_text",
]
