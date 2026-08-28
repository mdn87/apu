from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path
from types import TracebackType
from typing import Self


class LockUnavailableError(RuntimeError):
    """Raised when a process lock cannot be acquired before its deadline."""


class ProcessLock:
    """Cross-platform advisory process lock backed by one private file byte.

    The lock is blocking by default. Supplying ``timeout`` bounds the wait and
    raises :class:`LockUnavailableError` when another process retains the lock.
    Separate lock files let callers choose the narrowest useful serialization
    domain without duplicating the platform-specific locking protocol.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float | None = None,
        poll_interval: float = 0.01,
    ) -> None:
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise ValueError("lock timeout must be a non-negative number or null")
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or poll_interval <= 0
        ):
            raise ValueError("lock poll interval must be positive")
        self.path = Path(path)
        self.timeout = float(timeout) if timeout is not None else None
        self.poll_interval = float(poll_interval)
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
        if self._descriptor is not None:
            raise RuntimeError("process lock is already held")
        _ensure_private_directory(self.path.parent)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"lock path is not a regular file: {self.path}")
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            if metadata.st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            _acquire(
                descriptor,
                path=self.path,
                timeout=self.timeout,
                poll_interval=self.poll_interval,
            )
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(path)
    if os.name == "posix":
        path.chmod(stat.S_IMODE(path.stat().st_mode) & 0o700)


def _acquire(
    descriptor: int,
    *,
    path: Path,
    timeout: float | None,
    poll_interval: float,
) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            _try_lock(descriptor)
            return
        except OSError as error:
            if not _is_contention(error):
                raise
            if deadline is not None and time.monotonic() >= deadline:
                raise LockUnavailableError(
                    f"process lock is unavailable: {path}"
                ) from error
            time.sleep(poll_interval)


def _try_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _is_contention(error: OSError) -> bool:
    return isinstance(error, BlockingIOError) or error.errno in {
        errno.EACCES,
        errno.EAGAIN,
    }
