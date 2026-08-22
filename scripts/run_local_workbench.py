"""Source-tree wrapper for the shared local workbench launcher."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from reserving_workflow.cli import workbench_launcher as _launcher


globals().update(
    {
        name: getattr(_launcher, name)
        for name in dir(_launcher)
        if not name.startswith("__")
    }
)


if __name__ == "__main__":
    raise SystemExit(_launcher.main())
