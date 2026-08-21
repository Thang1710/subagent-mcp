from contextlib import contextmanager
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from spikes.phase0a import locking, worktree_hook
from spikes.phase0a.core import fingerprint, run_argv


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("probe\n", encoding="utf-8")
    commands = (
        ["git", "-C", str(repo), "init", "-b", "main"],
        ["git", "-C", str(repo), "add", "README.md"],
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Phase0a",
            "-c",
            "user.email=phase0a@example.invalid",
            "commit",
            "-m",
            "init",
        ],
    )
    for index, argv in enumerate(commands):
        result = run_argv(f"git-{index}", argv)
        assert result.exit_code == 0, result.stderr
    return repo


def _payload(repo: Path, name: str = "probe-one") -> dict[str, str]:
    return {
        "session_id": "session",
        "cwd": str(repo),
        "hook_event_name": "WorktreeCreate",
        "name": name,
    }


def _porcelain(*paths: Path) -> str:
    return "".join(
        f"worktree {path.resolve()}\0HEAD deadbeef\0detached\0\0"
        for path in paths
    )


@pytest.mark.real_git_worktree
def test_create_worktree_writes_lease_and_path_event(tmp_path: Path):
    repo = _repo(tmp_path)
    event_log = tmp_path / "events.jsonl"
    lease_ack = tmp_path / "lease.json"
    target = worktree_hook.create_worktree(
        repo,
        tmp_path / "worktrees",
        event_log,
        lease_ack,
        tmp_path / "create.lock",
        "execution-1",
        _payload(repo),
        lambda _path: None,
    )
    try:
        assert target.is_dir()
        assert json.loads(lease_ack.read_text(encoding="utf-8"))["worktree_path"] == str(target)
        event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[-1])
        assert event["name"] == "probe-one"
        assert event["worktree_path"] == str(target)
        assert event["execution_id"] == "execution-1"
    finally:
        run_argv("cleanup", ["git", "-C", str(repo), "worktree", "remove", str(target)])


@pytest.mark.real_git_worktree
def test_create_worktree_rejects_unsafe_name_without_side_effect(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="worktree name"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            tmp_path / "lease.json",
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo, "../escape"),
            lambda _path: None,
        )
    assert not (tmp_path / "lease.json").exists()


@pytest.mark.real_git_worktree
def test_event_failure_rolls_back_new_worktree_and_ack(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    lease_ack = tmp_path / "lease.json"

    def fail_event(*_args, **_kwargs):
        raise OSError("event unavailable")

    monkeypatch.setattr(worktree_hook, "append_event", fail_event)
    with pytest.raises(OSError, match="event unavailable"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            lease_ack,
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            lambda _path: None,
        )
    assert not (tmp_path / "worktrees" / "probe-one").exists()
    assert not lease_ack.exists()
    listing = run_argv(
        "list",
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
    )
    assert listing.stdout.count("worktree ") == 1


def test_rollback_failure_retains_recovery_record(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    lease = tmp_path / "lease.json"
    target = tmp_path / "worktrees" / "probe-one"

    def fake_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    states = iter(("ABSENT", "PRESENT"))
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: next(states),
        raising=False,
    )
    monkeypatch.setattr(
        worktree_hook,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event failed")),
    )
    monkeypatch.setattr(
        worktree_hook,
        "run_argv",
        lambda *_args, **_kwargs: SimpleNamespace(exit_code=1, stderr="busy"),
    )

    with pytest.raises(RuntimeError, match="RECOVERY_REQUIRED"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            lease,
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            lambda _path: None,
        )
    recovery = json.loads(lease.read_text(encoding="utf-8"))
    assert recovery["status"] == "recovery_required"
    assert recovery["worktree_path"] == str(target)
    assert "stderr" not in recovery


def test_handoff_failure_rolls_back_before_ack_removal(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    lease = tmp_path / "lease.json"
    target = tmp_path / "worktrees" / "probe-one"
    common = tmp_path / "common"
    common.mkdir()

    def fake_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    def cleanup(*_args, **_kwargs):
        target.rmdir()
        return SimpleNamespace(exit_code=0, stderr="")

    def fail_handoff(_path):
        raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    states = iter(("ABSENT", "PRESENT", "ABSENT"))
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: next(states),
        raising=False,
    )
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worktree_hook, "run_argv", cleanup)

    with pytest.raises(BrokenPipeError, match="stdout closed"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            lease,
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            fail_handoff,
        )
    assert not target.exists()
    assert not lease.exists()


def test_uncertain_add_reconciles_partial_target(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    common = tmp_path / "common"
    common.mkdir()
    cleanup_calls: list[list[str]] = []
    cleanup_timeouts: list[float] = []

    def uncertain_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
            raise TimeoutError("add outcome unknown")
        return ""

    def cleanup(_name, argv, **kwargs):
        cleanup_calls.append(argv)
        cleanup_timeouts.append(kwargs["timeout_seconds"])
        target.rmdir()
        return SimpleNamespace(exit_code=0, stderr="")

    monkeypatch.setattr(worktree_hook, "_git", uncertain_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    states = iter(("ABSENT", "PRESENT", "ABSENT"))
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: next(states),
        raising=False,
    )
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worktree_hook, "run_argv", cleanup)

    with pytest.raises(TimeoutError, match="outcome unknown"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            tmp_path / "lease.json",
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            lambda _path: None,
        )
    assert cleanup_calls == [
        ["git", "-C", str(repo.resolve()), "worktree", "remove", str(target)]
    ]
    assert len(cleanup_timeouts) == 1 and 0 < cleanup_timeouts[0] <= 30


def test_main_handoff_writes_exactly_one_newline_terminated_path(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = json.dumps(_payload(repo)).encode("utf-8")
    writes: list[tuple[int, bytes]] = []

    def write_once(fd, data):
        writes.append((fd, data))
        return len(data)

    class BufferedStdoutForbidden:
        def write(self, _value):
            raise AssertionError("buffered stdout is not the commit point")

        def flush(self):
            raise AssertionError("stdout flush must not be required")

    def fake_create(
        passed_repo,
        worktree_root,
        event_log,
        lease_ack,
        creation_lock,
        execution_id,
        passed_payload,
        handoff,
    ):
        assert passed_repo == repo
        assert worktree_root == tmp_path / "worktrees"
        assert event_log == tmp_path / "events.jsonl"
        assert lease_ack == tmp_path / "lease.json"
        assert creation_lock == tmp_path / "create.lock"
        assert execution_id == "execution-1"
        assert passed_payload == _payload(repo)
        target = tmp_path / "worktrees" / "probe-one"
        handoff(target)
        return target

    monkeypatch.setattr(worktree_hook, "read_fd_bounded", lambda _fd, _limit: payload)
    monkeypatch.setattr(worktree_hook, "create_worktree", fake_create)
    monkeypatch.setattr(worktree_hook.os, "write", write_once)
    monkeypatch.setattr(worktree_hook.sys, "stdout", BufferedStdoutForbidden())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worktree_hook.py",
            "--repo",
            str(repo),
            "--worktree-root",
            str(tmp_path / "worktrees"),
            "--event-log",
            str(tmp_path / "events.jsonl"),
            "--lease-ack",
            str(tmp_path / "lease.json"),
            "--creation-lock",
            str(tmp_path / "create.lock"),
            "--execution-id",
            "execution-1",
        ],
    )

    assert worktree_hook.main() == 0
    expected = (str(tmp_path / "worktrees" / "probe-one") + "\n").encode("utf-8")
    assert writes == [(1, expected)]


def test_main_rejects_over_limit_payload_before_json_parsing(
    tmp_path: Path, monkeypatch
):
    parsed = False
    payload = tmp_path / "oversized-payload.json"
    payload.write_bytes(b"x" * 1_048_577)
    bounded_read = worktree_hook.read_fd_bounded

    with payload.open("rb") as stream:

        def fail_read(fd, limit):
            assert fd == 0
            return bounded_read(stream.fileno(), limit)

        def track_json(_raw):
            nonlocal parsed
            parsed = True
            raise AssertionError("JSON parsing must not run")

        monkeypatch.setattr(worktree_hook, "read_fd_bounded", fail_read)
        monkeypatch.setattr(worktree_hook.json, "loads", track_json)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "worktree_hook.py",
                "--repo",
                "repo",
                "--worktree-root",
                "worktrees",
                "--event-log",
                "events.jsonl",
                "--lease-ack",
                "lease.json",
                "--creation-lock",
                "create.lock",
                "--execution-id",
                "execution-1",
            ],
        )

        with pytest.raises(ValueError, match="exceeds 1048576 bytes"):
            worktree_hook.main()

    assert parsed is False


def test_registration_state_uses_validated_nul_porcelain(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    calls = []

    def list_worktrees(name, argv, **_kwargs):
        calls.append((name, argv))
        return SimpleNamespace(
            exit_code=0,
            timed_out=False,
            stdout=_porcelain(target),
            stderr="",
        )

    monkeypatch.setattr(worktree_hook, "run_argv", list_worktrees)

    assert worktree_hook._registration_state(repo, target, 12) == "PRESENT"
    assert calls == [
        (
            "git-worktree-list",
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ],
        )
    ]


def test_registration_state_returns_unknown_for_empty_malformed_and_error(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    malformed = (
        ("empty output", ""),
        ("missing record terminator", f"worktree {target}\0HEAD deadbeef\0detached\0"),
        ("missing worktree field", "HEAD deadbeef\0detached\0\0"),
        ("empty worktree path", "worktree \0HEAD deadbeef\0detached\0\0"),
        ("relative worktree path", "worktree relative/path\0HEAD deadbeef\0detached\0\0"),
        ("empty HEAD", f"worktree {target}\0HEAD \0detached\0\0"),
        ("duplicate HEAD", f"worktree {target}\0HEAD one\0HEAD two\0detached\0\0"),
        (
            "duplicate branch",
            f"worktree {target}\0HEAD one\0branch refs/heads/a\0branch refs/heads/b\0\0",
        ),
        ("duplicate detached", f"worktree {target}\0HEAD one\0detached\0detached\0\0"),
        ("duplicate locked", f"worktree {target}\0HEAD one\0detached\0locked\0locked reason\0\0"),
        ("duplicate prunable", f"worktree {target}\0HEAD one\0detached\0prunable\0prunable reason\0\0"),
        (
            "branch and detached",
            f"worktree {target}\0HEAD one\0branch refs/heads/a\0detached\0\0",
        ),
        ("bare with HEAD", f"worktree {target}\0bare\0HEAD one\0branch refs/heads/a\0\0"),
        ("bare with status", f"worktree {target}\0bare\0locked\0\0"),
        ("missing branch or detached", f"worktree {target}\0HEAD one\0\0"),
        ("status without HEAD", f"worktree {target}\0locked\0\0"),
        ("unknown field", f"worktree {target}\0HEAD one\0detached\0future value\0\0"),
    )

    for label, raw in malformed:
        monkeypatch.setattr(
            worktree_hook,
            "run_argv",
            lambda *_args, raw=raw, **_kwargs: SimpleNamespace(
                exit_code=0,
                timed_out=False,
                stdout=raw,
                stderr="",
            ),
        )
        assert worktree_hook._registration_state(repo, target, 12) == "UNKNOWN", label

    monkeypatch.setattr(
        worktree_hook,
        "run_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("list failed")),
    )
    assert worktree_hook._registration_state(repo, target, 12) == "UNKNOWN"


def test_registration_state_returns_unknown_when_canonicalization_fails(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    stdout = _porcelain(repo)
    original_resolve = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        if path == target:
            raise OSError("canonicalization failed")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(
        worktree_hook,
        "run_argv",
        lambda *_args, **_kwargs: SimpleNamespace(
            exit_code=0,
            timed_out=False,
            stdout=stdout,
            stderr="",
        ),
    )
    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    assert worktree_hook._registration_state(repo, target, 12) == "UNKNOWN"


@pytest.mark.parametrize("state", ["PRESENT", "UNKNOWN"])
def test_pre_add_registration_must_be_proven_absent(
    tmp_path: Path, monkeypatch, state: str
):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    git_calls = []

    def fake_git(_argv, name, *_args):
        git_calls.append(name)
        return ""

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: state,
        raising=False,
    )
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)

    with pytest.raises((FileExistsError, RuntimeError), match="registration"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            tmp_path / "lease.json",
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            lambda _path: None,
        )
    assert git_calls == []


def test_cleanup_exception_retains_sanitized_recovery_record(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    lease = tmp_path / "lease.json"

    def fake_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    states = iter(("ABSENT", "PRESENT"))
    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: next(states),
        raising=False,
    )
    monkeypatch.setattr(
        worktree_hook,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event failed")),
    )
    monkeypatch.setattr(
        worktree_hook,
        "run_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup-secret")),
    )

    with pytest.raises(RuntimeError, match="RECOVERY_REQUIRED"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            lease,
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            lambda _path: None,
        )
    recovery = json.loads(lease.read_text(encoding="utf-8"))
    assert recovery["status"] == "recovery_required"
    assert "cleanup-secret" not in json.dumps(recovery)


def test_unknown_post_cleanup_registration_retains_recovery(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    lease = tmp_path / "lease.json"

    def fake_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    def cleanup(*_args, **_kwargs):
        target.rmdir()
        return SimpleNamespace(exit_code=0, timed_out=False, stderr="")

    states = iter(("ABSENT", "PRESENT", "UNKNOWN"))
    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: next(states),
        raising=False,
    )
    monkeypatch.setattr(
        worktree_hook,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event failed")),
    )
    monkeypatch.setattr(worktree_hook, "run_argv", cleanup)

    with pytest.raises(RuntimeError, match="RECOVERY_REQUIRED"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            lease,
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            lambda _path: None,
        )
    assert json.loads(lease.read_text(encoding="utf-8"))["status"] == "recovery_required"


def test_ack_unlink_exception_retains_recovery(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    lease = tmp_path / "lease.json"
    original_unlink = Path.unlink

    def fake_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    def cleanup(*_args, **_kwargs):
        target.rmdir()
        return SimpleNamespace(exit_code=0, timed_out=False, stderr="")

    def guarded_unlink(path, *args, **kwargs):
        if path == lease:
            raise OSError("unlink-secret")
        return original_unlink(path, *args, **kwargs)

    states = iter(("ABSENT", "PRESENT", "ABSENT"))
    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: next(states),
        raising=False,
    )
    monkeypatch.setattr(
        worktree_hook,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event failed")),
    )
    monkeypatch.setattr(worktree_hook, "run_argv", cleanup)
    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    with pytest.raises(RuntimeError, match="RECOVERY_REQUIRED"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            lease,
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            lambda _path: None,
        )
    recovery = json.loads(lease.read_text(encoding="utf-8"))
    assert recovery["status"] == "recovery_required"
    assert "unlink-secret" not in json.dumps(recovery)


def test_short_stdout_write_retains_worktree_and_recovery(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    lease = tmp_path / "lease.json"
    cleanup_calls = []

    def fake_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: "ABSENT",
        raising=False,
    )
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worktree_hook,
        "run_argv",
        lambda *args, **_kwargs: cleanup_calls.append(args),
    )
    monkeypatch.setattr(worktree_hook.os, "write", lambda _fd, data: len(data) - 1)
    monkeypatch.setattr(worktree_hook.sys, "stdout", io.StringIO())

    with pytest.raises(RuntimeError, match="RECOVERY_REQUIRED"):
        worktree_hook.create_worktree(
            repo,
            tmp_path / "worktrees",
            tmp_path / "events.jsonl",
            lease,
            tmp_path / "create.lock",
            "execution-1",
            _payload(repo),
            worktree_hook._stdout_handoff,
        )
    assert target.is_dir()
    assert json.loads(lease.read_text(encoding="utf-8"))["status"] == "recovery_required"
    assert cleanup_calls == []


def test_post_commit_unlock_error_does_not_revoke_visible_path(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    lease = tmp_path / "lease.json"

    def fake_git(_argv, name, *_args):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    @contextmanager
    def unlock_fails(*_args, **_kwargs):
        yield
        raise OSError("unlock failed after commit")

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: "ABSENT",
        raising=False,
    )
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worktree_hook, "locked_file", unlock_fails)

    result = worktree_hook.create_worktree(
        repo,
        tmp_path / "worktrees",
        tmp_path / "events.jsonl",
        lease,
        tmp_path / "create.lock",
        "execution-1",
        _payload(repo),
        lambda _path: None,
    )
    assert result == target
    assert target.is_dir()
    assert json.loads(lease.read_text(encoding="utf-8"))["status"] == "leased"


def test_transaction_passes_decreasing_deadline_budgets(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    lock_timeouts = []
    git_timeouts = []
    tick = 0

    def monotonic():
        nonlocal tick
        tick += 1
        return float(tick)

    @contextmanager
    def capture_lock(_path, timeout_seconds, **_kwargs):
        lock_timeouts.append(timeout_seconds)
        yield

    def fake_run(name, _argv, *, timeout_seconds, **_kwargs):
        git_timeouts.append(timeout_seconds)
        if name == "git-worktree-list":
            stdout = _porcelain(repo)
        elif name == "git-common-dir":
            stdout = str(common)
        else:
            stdout = ""
            if name == "git-worktree-add":
                target.mkdir(parents=True)
        return SimpleNamespace(
            exit_code=0,
            timed_out=False,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(
        worktree_hook,
        "time",
        SimpleNamespace(monotonic=monotonic),
        raising=False,
    )
    monkeypatch.setattr(worktree_hook, "locked_file", capture_lock)
    monkeypatch.setattr(worktree_hook, "run_argv", fake_run)
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)

    assert worktree_hook.create_worktree(
        repo,
        tmp_path / "worktrees",
        tmp_path / "events.jsonl",
        tmp_path / "lease.json",
        tmp_path / "create.lock",
        "execution-1",
        _payload(repo),
        lambda _path: None,
    ) == target
    assert len(lock_timeouts) == 1 and 100 < lock_timeouts[0] <= 120
    assert git_timeouts and all(0 < value <= 120 for value in git_timeouts)
    assert git_timeouts == sorted(git_timeouts, reverse=True)


def test_same_creation_lock_covers_handoff_before_second_add(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    root = tmp_path / "worktrees"
    creation_lock = tmp_path / "create.lock"
    first_in_handoff = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    second_lock_failed = threading.Event()
    second_added = threading.Event()
    errors = []
    original_try_lock = locking._try_lock

    def instrument_try_lock(fd):
        acquired = original_try_lock(fd)
        if threading.current_thread().name == "second-create" and not acquired:
            second_lock_failed.set()
        return acquired

    def fake_git(argv, name, *_args):
        if name == "git-worktree-add":
            target = Path(argv[-2])
            target.mkdir(parents=True)
            if target.name == "probe-two":
                second_added.set()
        return ""

    def first_handoff(_path):
        first_in_handoff.set()
        assert release_first.wait(2)

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path, *_args: common)
    monkeypatch.setattr(
        worktree_hook,
        "_registration_state",
        lambda *_args: "ABSENT",
        raising=False,
    )
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(locking, "_try_lock", instrument_try_lock)

    def create(name, lease, handoff):
        try:
            if name == "execution-two":
                second_attempted.set()
            worktree_hook.create_worktree(
                repo,
                root,
                tmp_path / "events.jsonl",
                lease,
                creation_lock,
                name,
                _payload(repo, name.replace("execution-", "probe-")),
                handoff,
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(
        target=create,
        args=("execution-one", tmp_path / "lease-one.json", first_handoff),
    )
    second = threading.Thread(
        target=create,
        args=("execution-two", tmp_path / "lease-two.json", lambda _path: None),
        name="second-create",
    )
    first.start()
    assert first_in_handoff.wait(2)
    second.start()
    assert second_attempted.wait(2)
    assert second_lock_failed.wait(2)
    assert not second_added.wait(0.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert second_added.is_set()


@pytest.mark.real_git_worktree
def test_worktree_hook_subprocess_commits_stdout_ack_event_and_registration(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    event_log = tmp_path / "events.jsonl"
    lease = tmp_path / "lease.json"
    target = tmp_path / "worktrees" / "probe-one"
    argv = [
        sys.executable,
        "-B",
        str(Path(worktree_hook.__file__).resolve()),
        "--repo",
        str(repo),
        "--worktree-root",
        str(tmp_path / "worktrees"),
        "--event-log",
        str(event_log),
        "--lease-ack",
        str(lease),
        "--creation-lock",
        str(tmp_path / "create.lock"),
        "--execution-id",
        "execution-1",
    ]
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(_payload(repo)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
            shell=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == str(target) + "\n"
        acknowledgement = json.loads(lease.read_text(encoding="utf-8"))
        assert acknowledgement["status"] == "leased"
        assert acknowledgement["worktree_path"] == str(target)
        event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[-1])
        assert event["worktree_path"] == str(target)
        assert event["session_fingerprint"] == fingerprint(_payload(repo)["session_id"])
        serialized_event = json.dumps(event, sort_keys=True)
        assert json.dumps(_payload(repo)["session_id"]) not in serialized_event
        assert "session_id" not in event
        for field in (
            "last_assistant_message",
            "tool_input",
            "error_details",
            "transcript_path",
            "cwd",
        ):
            assert field not in event
        listing = run_argv(
            "list",
            ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"],
        )
        registered = {
            os.path.normcase(
                str(Path(field.removeprefix("worktree ")).resolve(strict=False))
            )
            for field in listing.stdout.split("\0")
            if field.startswith("worktree ")
        }
        assert os.path.normcase(str(target.resolve(strict=False))) in registered
    finally:
        if target.exists():
            run_argv(
                "cleanup",
                ["git", "-C", str(repo), "worktree", "remove", str(target)],
            )
