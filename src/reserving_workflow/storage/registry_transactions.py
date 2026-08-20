"""Cross-thread/process transaction lock for the local run registry."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def locked_registry_transaction(path: str | Path) -> Iterator[None]:
    """Serialize one registry read-modify-replace transaction."""

    registry_path = Path(path).expanduser().absolute()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_name(f"{registry_path.name}.adk.lock")
    with _thread_lock(registry_path), lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised in Linux CI
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised in Linux CI
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["locked_registry_transaction"]
