import errno
from pathlib import Path

import pytest

from spikes.phase0a import locking


def test_windows_busy_error_accepts_eacces_and_rejects_other_errors():
    assert locking._is_busy_lock_error(OSError(errno.EACCES, "busy"), windows=True)
    assert not locking._is_busy_lock_error(OSError(errno.EBADF, "bad fd"), windows=True)
    assert not locking._is_busy_lock_error(OSError(errno.EINVAL, "invalid"), windows=True)


def test_locked_file_retries_nonblocking_until_deadline(tmp_path: Path, monkeypatch):
    attempts = 0
    clock = iter((0.0, 0.0, 0.001, 0.01))

    def always_busy(_fd):
        nonlocal attempts
        attempts += 1
        return False

    monkeypatch.setattr(locking, "_try_lock", always_busy)
    monkeypatch.setattr(locking.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(locking.time, "sleep", lambda _seconds: None)
    with pytest.raises(TimeoutError, match="lock timeout"):
        with locking.locked_file(
            tmp_path / "busy.lock", timeout_seconds=0.01, poll_seconds=0.001
        ):
            pass
    assert attempts > 1


def test_locked_file_unlocks_after_body_failure(tmp_path: Path):
    target = tmp_path / "event.lock"
    with pytest.raises(RuntimeError, match="body failed"):
        with locking.locked_file(target, timeout_seconds=1):
            raise RuntimeError("body failed")
    with locking.locked_file(target, timeout_seconds=1):
        pass


def test_locked_file_does_not_retry_after_deadline(tmp_path: Path, monkeypatch):
    attempts = 0
    clock = iter((0.0, 0.0, 2.0))

    def busy_then_available(_fd):
        nonlocal attempts
        attempts += 1
        return attempts > 1

    monkeypatch.setattr(locking, "_try_lock", busy_then_available)
    monkeypatch.setattr(locking, "_unlock", lambda _fd: None)
    monkeypatch.setattr(locking.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(locking.time, "sleep", lambda _seconds: None)
    with pytest.raises(TimeoutError, match="lock timeout"):
        with locking.locked_file(
            tmp_path / "deadline.lock", timeout_seconds=1, poll_seconds=0.1
        ):
            pass
    assert attempts == 1


@pytest.mark.parametrize(
    "timeout_seconds,poll_seconds,match",
    [
        (0, 0.1, "timeout_seconds"),
        (-1, 0.1, "timeout_seconds"),
        (1, 0, "poll_seconds"),
        (1, -0.1, "poll_seconds"),
    ],
)
def test_locked_file_rejects_nonpositive_timeouts_before_open(
    tmp_path: Path, monkeypatch, timeout_seconds, poll_seconds, match
):
    monkeypatch.setattr(
        locking.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lock file must not be opened")
        ),
    )
    with pytest.raises(ValueError, match=match):
        with locking.locked_file(
            tmp_path / "invalid.lock",
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        ):
            pass
