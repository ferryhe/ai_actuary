"""Side-effect-free Workflow Lab tool references approved by project policy."""

from __future__ import annotations


def read_run_status(run_id: str) -> dict[str, object]:
    """Return a fail-closed placeholder until a governed read client is bound.

    Workflow Lab validation only checks this stable Python reference; it never
    imports or executes draft-selected tools. Runtime binding remains a Phase 3
    control-plane responsibility and cannot fall through to a registry/store.
    """

    return {
        "ok": False,
        "run_id": str(run_id),
        "code": "governed_read_client_required",
    }


__all__ = ["read_run_status"]
