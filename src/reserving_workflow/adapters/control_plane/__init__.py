"""Shared installable control-plane client and safe read projections."""

from .client import AdkControlPlaneClient, ReadOnlyControlPlaneClient
from .contracts import (
    ArtifactMetadata,
    ArtifactProjection,
    HealthStatus,
    PreflightStatus,
    ToolDetail,
    ToolSummary,
    WorkflowSummary,
)
from .errors import (
    ControlPlaneContractError,
    ControlPlaneError,
    ControlPlaneResponseError,
    ControlPlaneTransportError,
)

__all__ = [
    "ArtifactMetadata",
    "ArtifactProjection",
    "AdkControlPlaneClient",
    "ControlPlaneContractError",
    "ControlPlaneError",
    "ControlPlaneResponseError",
    "ControlPlaneTransportError",
    "HealthStatus",
    "PreflightStatus",
    "ReadOnlyControlPlaneClient",
    "ToolDetail",
    "ToolSummary",
    "WorkflowSummary",
]
