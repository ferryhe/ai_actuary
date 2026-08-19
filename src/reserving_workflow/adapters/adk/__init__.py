"""Local-only Google ADK developer runtime boundary.

This package deliberately does not import ``google.adk``. The optional ADK
dependency is owned by ``developer_workflows`` and the local launcher.
"""

from reserving_workflow.adapters.adk.local_runtime import LocalWorkbenchConfig

__all__ = ["LocalWorkbenchConfig"]
