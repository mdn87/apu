"""Cooperative, reentrant exclusion for evidence writes and retention."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_LOCAL = threading.local()


@contextmanager
def behavior_lock(state_home: Path):
    root = Path(state_home).expanduser().resolve()
    key = os.path.normcase(str(root))
    held = getattr(_LOCAL, "held", set())
    if key in held:
        yield
        return
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("behavior state is busy; retry after the active operation")
    try:
        from .state import ensure_private_directory

        ensure_private_directory(root)
        path = root / ".behavior.lock"
        if path.is_symlink():
            raise ValueError("behavior lock cannot be a link")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError(
                    "behavior state is busy; retry after the active operation"
                ) from error
            _LOCAL.held = held | {key}
            try:
                yield
            finally:
                _LOCAL.held = held
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


def serialized_behavior(function):
    """Hold through read/derive/write; an absent store has no prunable evidence."""

    @wraps(function)
    def wrapped(state_home, *args, **kwargs):
        if not Path(state_home).exists():
            return function(state_home, *args, **kwargs)
        with behavior_lock(state_home):
            return function(state_home, *args, **kwargs)

    return wrapped
