"""Stable, non-disclosing errors for the shared control-plane client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(eq=False)
class ControlPlaneError(Exception):
    """Base error safe to serialize into an agent-visible envelope."""

    code: str
    message: str
    status_code: int | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }


class ControlPlaneTransportError(ControlPlaneError):
    """A timeout or connection failure before a valid response."""


class ControlPlaneResponseError(ControlPlaneError):
    """An HTTP or bounded-response failure."""


class ControlPlaneContractError(ControlPlaneError):
    """A response that does not satisfy the expected typed contract."""


def error_for_status(status_code: int) -> ControlPlaneResponseError:
    if status_code == 404:
        return ControlPlaneResponseError(
            code="not_found",
            message="Control-plane resource was not found.",
            status_code=status_code,
        )
    if status_code == 503:
        return ControlPlaneResponseError(
            code="service_unavailable",
            message="Control plane is temporarily unavailable.",
            status_code=status_code,
            retryable=True,
        )
    if status_code in {500, 502, 504}:
        return ControlPlaneResponseError(
            code="server_error",
            message="Control plane returned an internal error.",
            status_code=status_code,
            retryable=True,
        )
    return ControlPlaneResponseError(
        code="http_error",
        message="Control plane rejected the request.",
        status_code=status_code,
    )
