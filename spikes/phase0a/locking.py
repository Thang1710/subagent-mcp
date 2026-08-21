from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _is_busy_lock_error(error: OSError, *, windows: bool) -> bool:
    if windows:
        return error.errno == errno.EACCES or error.winerror == 33
    return error.errno in (errno.EACCES, errno.EAGAIN)


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _is_busy_lock_error(exc, windows=True):
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if _is_busy_lock_error(exc, windows=False):
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def locked_file(
    path: str | Path,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    acquired = False
    try:
        if os.path.getsize(lock_path) == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        deadline = time.monotonic() + timeout_seconds
        if not _try_lock(fd):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("lock timeout")
                time.sleep(min(poll_seconds, remaining))
                if time.monotonic() >= deadline:
                    raise TimeoutError("lock timeout")
                if _try_lock(fd):
                    break
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock(fd)
        finally:
            os.close(fd)
