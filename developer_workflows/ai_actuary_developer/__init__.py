"""ADK discovery package for the local AI Actuary developer agent."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name != "root_agent":
        raise AttributeError(name)
    from .agent import root_agent

    return root_agent

__all__ = ["root_agent"]
