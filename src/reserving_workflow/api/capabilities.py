"""Local capability authentication and exhaustive route policy."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute


CapabilityClass = Literal["operator-console", "adk-developer"]


@dataclass(frozen=True)
class Principal:
    capability_class: CapabilityClass
    workspace_id: str
    source: str
    operator_id: str
    credential_generation: int
    transport: Literal["bearer", "session"]
    session_id: str | None = None


@dataclass(frozen=True)
class RoutePolicy:
    capabilities: frozenset[CapabilityClass] = frozenset()
    anonymous: bool = False


@dataclass
class _Session:
    session_id: str
    csrf_token: str
    expires_at: float
    credential_generation: int


@dataclass
class _BootstrapHandoff:
    claim_digest: bytes
    expires_at: float
    approved: bool = False
    used: bool = False


_BOTH = frozenset({"operator-console", "adk-developer"})
_OPERATOR = frozenset({"operator-console"})
_ADK = frozenset({"adk-developer"})


ROUTE_CAPABILITY_MATRIX: dict[tuple[str, str], RoutePolicy] = {
    ("GET", "/health"): RoutePolicy(anonymous=True),
    ("GET", "/health/preflight"): RoutePolicy(anonymous=True),
    ("GET", "/console"): RoutePolicy(anonymous=True),
    ("POST", "/auth/operator/bootstrap"): RoutePolicy(anonymous=True),
    ("POST", "/auth/operator/handoff/request"): RoutePolicy(anonymous=True),
    ("POST", "/auth/operator/handoff/approve"): RoutePolicy(anonymous=True),
    ("POST", "/auth/operator/handoff/claim"): RoutePolicy(anonymous=True),
    ("GET", "/console/state"): RoutePolicy(_OPERATOR),
    ("GET", "/tools"): RoutePolicy(_BOTH),
    ("GET", "/tools/{tool_id}"): RoutePolicy(_BOTH),
    ("GET", "/workflows"): RoutePolicy(_BOTH),
    ("GET", "/workflows/{workflow_id}"): RoutePolicy(_BOTH),
    ("POST", "/runs"): RoutePolicy(_OPERATOR),
    ("GET", "/runs"): RoutePolicy(_BOTH),
    ("GET", "/runs/{run_id}"): RoutePolicy(_BOTH),
    ("GET", "/runs/{run_id}/events"): RoutePolicy(_BOTH),
    ("POST", "/runs/{run_id}/rerun"): RoutePolicy(_OPERATOR),
    ("GET", "/runs/{run_id}/artifacts"): RoutePolicy(_BOTH),
    ("GET", "/runs/{run_id}/artifacts/{artifact_id}/projection"): RoutePolicy(_BOTH),
    ("GET", "/runs/{run_id}/results"): RoutePolicy(_BOTH),
    ("GET", "/runs/{run_id}/review-packet"): RoutePolicy(_BOTH),
    ("GET", "/runs/{run_id}/review"): RoutePolicy(_BOTH),
    ("POST", "/runs/{run_id}/report-export"): RoutePolicy(_OPERATOR),
    ("GET", "/reviews"): RoutePolicy(_BOTH),
    ("GET", "/reviews/{review_id}"): RoutePolicy(_BOTH),
    ("POST", "/reviews/{review_id}/decision"): RoutePolicy(_OPERATOR),
    ("POST", "/replay"): RoutePolicy(_OPERATOR),
    ("POST", "/repeatability"): RoutePolicy(_OPERATOR),
    ("POST", "/benchmarks/batch"): RoutePolicy(_OPERATOR),
    ("POST", "/adk/runs"): RoutePolicy(_ADK),
}


class CapabilityAuthority:
    """In-memory local authority; capability secrets never enter business state."""

    session_cookie_name = "ai_actuary_operator_session"

    def __init__(
        self,
        *,
        operator_credential: str,
        adk_credential: str,
        operator_bootstrap_token: str,
        session_ttl_seconds: float = 900.0,
        bootstrap_ttl_seconds: float = 60.0,
    ) -> None:
        for name, value in (
            ("operator credential", operator_credential),
            ("ADK credential", adk_credential),
            ("operator bootstrap token", operator_bootstrap_token),
        ):
            if not isinstance(value, str) or len(value) < 8:
                raise ValueError(f"{name} must be an unpredictable nonempty secret")
        if secrets.compare_digest(
            _secret_digest(operator_credential), _secret_digest(adk_credential)
        ):
            raise ValueError("Capability credentials must be independent")
        self._credential_digests: dict[CapabilityClass, bytes] = {
            "operator-console": _secret_digest(operator_credential),
            "adk-developer": _secret_digest(adk_credential),
        }
        self._adk_confirmation_key = adk_credential.encode("utf-8")
        self._credential_generations: dict[CapabilityClass, int] = {
            "operator-console": 1,
            "adk-developer": 1,
        }
        self._bootstrap_digest = _secret_digest(operator_bootstrap_token)
        self._bootstrap_expires_at = time.monotonic() + bootstrap_ttl_seconds
        self._bootstrap_used = False
        self._bootstrap_ttl_seconds = bootstrap_ttl_seconds
        self._session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, _Session] = {}
        self._bootstrap_handoffs: dict[str, _BootstrapHandoff] = {}

    def authenticate_bearer(self, authorization: str | None) -> Principal | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        candidate = authorization[7:]
        candidate_digest = _secret_digest(candidate)
        for capability in ("operator-console", "adk-developer"):
            if secrets.compare_digest(candidate_digest, self._credential_digests[capability]):
                return _principal(
                    capability,
                    generation=self._credential_generations[capability],
                    transport="bearer",
                )
        return None

    def authenticate_session(self, session_id: str | None) -> Principal | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= time.monotonic() or session.credential_generation != self._credential_generations["operator-console"]:
            self._sessions.pop(session_id, None)
            return None
        return _principal(
            "operator-console",
            generation=session.credential_generation,
            transport="session",
            session_id=session_id,
        )

    def session_csrf_matches(self, session_id: str | None, candidate: str | None) -> bool:
        if not session_id or not candidate:
            return False
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return secrets.compare_digest(_secret_digest(candidate), _secret_digest(session.csrf_token))

    def exchange_bootstrap(self, bootstrap_token: str) -> tuple[str, str, int]:
        candidate_digest = _secret_digest(bootstrap_token)
        valid = secrets.compare_digest(candidate_digest, self._bootstrap_digest)
        if self._bootstrap_used or time.monotonic() >= self._bootstrap_expires_at or not valid:
            raise ValueError("bootstrap_invalid")
        return self._issue_session()

    def create_bootstrap_handoff(self, claim_token: str) -> tuple[str, int]:
        now = time.monotonic()
        self._discard_expired_handoffs(now)
        if self._bootstrap_used or now >= self._bootstrap_expires_at:
            raise ValueError("bootstrap_invalid")
        if len(self._bootstrap_handoffs) >= 16:
            raise ValueError("bootstrap_unavailable")
        handoff_id = secrets.token_urlsafe(24)
        expires_at = min(
            self._bootstrap_expires_at,
            now + self._bootstrap_ttl_seconds,
        )
        self._bootstrap_handoffs[handoff_id] = _BootstrapHandoff(
            claim_digest=_secret_digest(claim_token),
            expires_at=expires_at,
        )
        return handoff_id, max(1, int(expires_at - now))

    def approve_bootstrap_handoff(
        self, bootstrap_token: str, handoff_id: str
    ) -> None:
        now = time.monotonic()
        candidate_digest = _secret_digest(bootstrap_token)
        valid_bootstrap = secrets.compare_digest(
            candidate_digest, self._bootstrap_digest
        )
        handoff = self._bootstrap_handoffs.get(handoff_id)
        if (
            self._bootstrap_used
            or now >= self._bootstrap_expires_at
            or not valid_bootstrap
            or handoff is None
            or handoff.used
            or handoff.expires_at <= now
        ):
            raise ValueError("bootstrap_invalid")
        handoff.approved = True
        self._bootstrap_used = True

    def claim_bootstrap_handoff(
        self, handoff_id: str, claim_token: str
    ) -> tuple[str, str, int]:
        now = time.monotonic()
        handoff = self._bootstrap_handoffs.get(handoff_id)
        expected_digest = (
            handoff.claim_digest if handoff is not None else b"\x00" * 32
        )
        valid_claim = secrets.compare_digest(
            _secret_digest(claim_token), expected_digest
        )
        if (
            handoff is None
            or not handoff.approved
            or handoff.used
            or handoff.expires_at <= now
            or not valid_claim
        ):
            raise ValueError("bootstrap_invalid")
        handoff.used = True
        return self._issue_session()

    def _discard_expired_handoffs(self, now: float) -> None:
        self._bootstrap_handoffs = {
            handoff_id: handoff
            for handoff_id, handoff in self._bootstrap_handoffs.items()
            if not handoff.used and handoff.expires_at > now
        }

    def _issue_session(self) -> tuple[str, str, int]:
        self._bootstrap_used = True
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        ttl = max(1, int(self._session_ttl_seconds))
        self._sessions[session_id] = _Session(
            session_id=session_id,
            csrf_token=csrf_token,
            expires_at=time.monotonic() + self._session_ttl_seconds,
            credential_generation=self._credential_generations["operator-console"],
        )
        return session_id, csrf_token, ttl

    def rotate(self, capability: CapabilityClass, new_credential: str) -> None:
        if capability not in self._credential_digests or len(str(new_credential)) < 8:
            raise ValueError("Invalid capability rotation request")
        other: CapabilityClass = "adk-developer" if capability == "operator-console" else "operator-console"
        new_digest = _secret_digest(new_credential)
        if secrets.compare_digest(new_digest, self._credential_digests[other]):
            raise ValueError("Capability credentials must be independent")
        self._credential_digests[capability] = new_digest
        if capability == "adk-developer":
            self._adk_confirmation_key = str(new_credential).encode("utf-8")
        self._credential_generations[capability] += 1
        if capability == "operator-console":
            self._sessions.clear()
            self._bootstrap_handoffs.clear()
            self._bootstrap_used = True

    def verify_adk_confirmation(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        candidate: str | None,
    ) -> bool:
        if not candidate:
            return False
        expected = hmac.new(
            self._adk_confirmation_key,
            f"{idempotency_key}:{request_fingerprint}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return secrets.compare_digest(_secret_digest(candidate), _secret_digest(expected))


def assert_route_matrix_complete(app: FastAPI) -> None:
    actual: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            actual.add((method.upper(), route.path))
    declared = set(ROUTE_CAPABILITY_MATRIX)
    missing = actual - declared
    stale = declared - actual
    if missing or stale:
        raise RuntimeError(
            f"Capability route matrix mismatch; unclassified={sorted(missing)!r}; "
            f"unregistered={sorted(stale)!r}"
        )


def route_policy_for_scope(app: FastAPI, *, method: str, path: str) -> RoutePolicy | None:
    normalized_method = method.upper()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path_regex.fullmatch(path):
            return ROUTE_CAPABILITY_MATRIX.get(
                (normalized_method, route.path),
                RoutePolicy(),
            )
    return None


def object_in_scope(principal: Principal, entry: dict[str, object]) -> bool:
    workspace = entry.get("workspace_id")
    source = entry.get("source")
    if source is None:
        provenance = entry.get("provenance")
        if isinstance(provenance, dict):
            source = provenance.get("source")
    if source is None:
        source = "operator-console"
    if workspace is None:
        workspace = "default-workspace"
    return secrets.compare_digest(str(workspace), principal.workspace_id) and secrets.compare_digest(
        str(source), principal.source
    )


def _principal(
    capability: CapabilityClass,
    *,
    generation: int,
    transport: Literal["bearer", "session"],
    session_id: str | None = None,
) -> Principal:
    if capability == "adk-developer":
        return Principal(
            capability_class=capability,
            workspace_id="adk-development",
            source="adk-developer",
            operator_id="adk-developer",
            credential_generation=generation,
            transport=transport,
            session_id=session_id,
        )
    return Principal(
        capability_class=capability,
        workspace_id="default-workspace",
        source="operator-console",
        operator_id="local-actuary",
        credential_generation=generation,
        transport=transport,
        session_id=session_id,
    )


def _secret_digest(value: str) -> bytes:
    return hashlib.sha256(str(value).encode("utf-8", errors="strict")).digest()


__all__ = [
    "CapabilityAuthority",
    "CapabilityClass",
    "Principal",
    "ROUTE_CAPABILITY_MATRIX",
    "RoutePolicy",
    "assert_route_matrix_complete",
    "object_in_scope",
    "route_policy_for_scope",
]
