from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:
    from .core import read_fd_bounded, run_argv, write_json_atomic
    from .hook_sink import append_event
    from .locking import locked_file
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from spikes.phase0a.core import read_fd_bounded, run_argv, write_json_atomic
    from spikes.phase0a.hook_sink import append_event
    from spikes.phase0a.locking import locked_file


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PRESENT = "PRESENT"
_ABSENT = "ABSENT"
_UNKNOWN = "UNKNOWN"
_WORK_TIMEOUT_SECONDS = 120
_ROLLBACK_TIMEOUT_SECONDS = 30


class _HandoffAmbiguousError(RuntimeError):
    pass


def _remaining(deadline: float, label: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{label} deadline exceeded")
    return remaining


def _git(argv: list[str], name: str, timeout_seconds: float) -> str:
    if timeout_seconds <= 0:
        raise TimeoutError(f"{name} deadline exceeded")
    result = run_argv(name, argv, timeout_seconds=timeout_seconds)
    if getattr(result, "timed_out", False):
        raise TimeoutError(f"{name} timed out")
    if result.exit_code != 0:
        raise RuntimeError(f"{name} failed: {result.stderr}")
    return result.stdout.strip()


def _common_dir(repo: Path, timeout_seconds: float) -> Path:
    raw = _git(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        "git-common-dir",
        timeout_seconds,
    )
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve(strict=True)


def _write_ack_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _registration_state(
    repo: Path,
    target: Path,
    timeout_seconds: float,
) -> str:
    try:
        result = run_argv(
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
            timeout_seconds=timeout_seconds,
        )
    except BaseException:
        return _UNKNOWN
    raw = getattr(result, "stdout", None)
    if (
        result.exit_code != 0
        or getattr(result, "timed_out", False)
        or not isinstance(raw, str)
        or not raw
        or not raw.endswith("\0\0")
    ):
        return _UNKNOWN

    try:
        expected = os.path.normcase(str(target.resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        return _UNKNOWN
    observed_paths: set[str] = set()
    records = raw[:-2].split("\0\0")
    if not records:
        return _UNKNOWN
    try:
        for record in records:
            fields = record.split("\0")
            if len(fields) < 2 or not fields[0].startswith("worktree "):
                return _UNKNOWN
            raw_path = fields[0].removeprefix("worktree ")
            if not raw_path or any(not field for field in fields[1:]):
                return _UNKNOWN
            path = Path(raw_path)
            if not path.is_absolute():
                return _UNKNOWN
            statuses = fields[1:]
            if statuses != ["bare"]:
                counts = {
                    "HEAD": 0,
                    "branch": 0,
                    "detached": 0,
                    "locked": 0,
                    "prunable": 0,
                }
                for field in statuses:
                    if field.startswith("HEAD ") and field.removeprefix("HEAD "):
                        counts["HEAD"] += 1
                    elif (
                        field.startswith("branch ")
                        and field.removeprefix("branch ")
                    ):
                        counts["branch"] += 1
                    elif field == "detached":
                        counts["detached"] += 1
                    elif field == "locked" or (
                        field.startswith("locked ")
                        and field.removeprefix("locked ")
                    ):
                        counts["locked"] += 1
                    elif field == "prunable" or (
                        field.startswith("prunable ")
                        and field.removeprefix("prunable ")
                    ):
                        counts["prunable"] += 1
                    else:
                        return _UNKNOWN
                if (
                    counts["HEAD"] != 1
                    or counts["branch"] + counts["detached"] != 1
                    or counts["locked"] > 1
                    or counts["prunable"] > 1
                ):
                    return _UNKNOWN
            canonical = os.path.normcase(
                str(path.resolve(strict=False))
            )
            if canonical in observed_paths:
                return _UNKNOWN
            observed_paths.add(canonical)
    except (OSError, RuntimeError, ValueError):
        return _UNKNOWN
    return _PRESENT if expected in observed_paths else _ABSENT


def _record_recovery(
    lease_ack: Path,
    acknowledgement: dict[str, Any],
    target: Path,
    original: BaseException,
    *,
    failure: str,
    cleanup_exit_code: int | None = None,
    registration_state: str | None = None,
) -> None:
    recovery = dict(acknowledgement)
    recovery.update({"status": "recovery_required", "failure": failure})
    if cleanup_exit_code is not None:
        recovery["cleanup_exit_code"] = cleanup_exit_code
    if registration_state is not None:
        recovery["registration_state"] = registration_state
    try:
        write_json_atomic(lease_ack, recovery)
    except BaseException:
        raise RuntimeError(
            f"RECOVERY_REQUIRED: recovery record write failed for {target}"
        ) from original
    raise RuntimeError(f"RECOVERY_REQUIRED: {failure} for {target}") from original


def _rollback_or_record(
    repo: Path,
    target: Path,
    lease_ack: Path,
    acknowledgement: dict[str, Any],
    original: BaseException,
    rollback_deadline: float,
) -> None:
    try:
        cleanup = run_argv(
            "git-worktree-rollback",
            ["git", "-C", str(repo), "worktree", "remove", str(target)],
            timeout_seconds=_remaining(rollback_deadline, "rollback"),
        )
    except BaseException:
        _record_recovery(
            lease_ack,
            acknowledgement,
            target,
            original,
            failure="cleanup_error",
        )
    cleanup_exit_code = cleanup.exit_code
    if getattr(cleanup, "timed_out", False):
        _record_recovery(
            lease_ack,
            acknowledgement,
            target,
            original,
            failure="cleanup_timeout",
            cleanup_exit_code=cleanup_exit_code,
        )
    try:
        target_exists = target.exists()
    except BaseException:
        target_exists = None
    try:
        registration_state = _registration_state(
            repo,
            target,
            _remaining(rollback_deadline, "rollback verification"),
        )
    except BaseException:
        registration_state = _UNKNOWN
    if (
        cleanup_exit_code == 0
        and target_exists is False
        and registration_state == _ABSENT
    ):
        try:
            lease_ack.unlink(missing_ok=True)
        except BaseException:
            _record_recovery(
                lease_ack,
                acknowledgement,
                target,
                original,
                failure="ack_unlink_error",
                cleanup_exit_code=cleanup_exit_code,
                registration_state=registration_state,
            )
        return
    _record_recovery(
        lease_ack,
        acknowledgement,
        target,
        original,
        failure="rollback_failed",
        cleanup_exit_code=cleanup_exit_code,
        registration_state=registration_state,
    )


def create_worktree(
    repo: Path,
    worktree_root: Path,
    event_log: Path,
    lease_ack: Path,
    creation_lock: Path,
    execution_id: str,
    payload: dict[str, Any],
    handoff: Callable[[Path], None],
) -> Path:
    work_deadline = time.monotonic() + _WORK_TIMEOUT_SECONDS
    committed_target: Path | None = None
    try:
        with locked_file(
            creation_lock,
            timeout_seconds=_remaining(work_deadline, "worktree transaction"),
        ):
            expected_repo = repo.resolve(strict=True)
            if payload.get("hook_event_name") != "WorktreeCreate":
                raise ValueError("unexpected hook event")
            name = payload.get("name")
            if not isinstance(name, str) or _NAME.fullmatch(name) is None:
                raise ValueError("invalid worktree name")
            cwd = Path(str(payload.get("cwd", ""))).resolve(strict=True)
            if cwd != expected_repo:
                raise ValueError("hook cwd does not match expected repository")

            worktree_root.mkdir(parents=True, exist_ok=True)
            root = worktree_root.resolve(strict=True)
            target = root / name
            if target.exists() or lease_ack.exists():
                raise FileExistsError(
                    "worktree target or lease acknowledgement already exists"
                )
            registration_state = _registration_state(
                expected_repo,
                target,
                _remaining(work_deadline, "registration check"),
            )
            if registration_state == _PRESENT:
                raise FileExistsError("worktree registration already exists")
            if registration_state != _ABSENT:
                raise RuntimeError("worktree registration state is unknown")

            common_before = _common_dir(
                expected_repo,
                _remaining(work_deadline, "common-dir check"),
            )
            acknowledgement = {
                "execution_id": execution_id,
                "repository_common_dir": str(common_before),
                "worktree_path": str(target),
                "status": "leased",
            }
            try:
                _git(
                    [
                        "git",
                        "-C",
                        str(expected_repo),
                        "worktree",
                        "add",
                        "--detach",
                        str(target),
                        "HEAD",
                    ],
                    "git-worktree-add",
                    _remaining(work_deadline, "worktree add"),
                )
                if _common_dir(
                    target,
                    _remaining(work_deadline, "created common-dir check"),
                ) != common_before:
                    raise RuntimeError("created worktree common-dir mismatch")
                _remaining(work_deadline, "lease acknowledgement")
                _write_ack_exclusive(lease_ack, acknowledgement)
                _remaining(work_deadline, "event acknowledgement")
                event = dict(payload)
                event.update(
                    {"execution_id": execution_id, "worktree_path": str(target)}
                )
                append_event(event_log, event)
                _remaining(work_deadline, "stdout handoff")
                handoff(target)
                committed_target = target
                return target
            except BaseException as error:
                rollback_deadline = time.monotonic() + _ROLLBACK_TIMEOUT_SECONDS
                if isinstance(error, _HandoffAmbiguousError):
                    _record_recovery(
                        lease_ack,
                        acknowledgement,
                        target,
                        error,
                        failure="handoff_ambiguous",
                    )
                try:
                    target_exists = target.exists()
                except BaseException:
                    target_exists = None
                try:
                    registration_state = _registration_state(
                        expected_repo,
                        target,
                        _remaining(rollback_deadline, "failure reconciliation"),
                    )
                except BaseException:
                    registration_state = _UNKNOWN
                if target_exists is False and registration_state == _ABSENT:
                    try:
                        lease_ack.unlink(missing_ok=True)
                    except BaseException:
                        _record_recovery(
                            lease_ack,
                            acknowledgement,
                            target,
                            error,
                            failure="ack_unlink_error",
                            registration_state=registration_state,
                        )
                    raise
                _rollback_or_record(
                    expected_repo,
                    target,
                    lease_ack,
                    acknowledgement,
                    error,
                    rollback_deadline,
                )
                try:
                    append_event(
                        event_log,
                        {
                            "hook_event_name": "WorktreeRollback",
                            "execution_id": execution_id,
                            "name": name,
                            "worktree_path": str(target),
                        },
                    )
                except BaseException:
                    pass
                raise
    except BaseException:
        if committed_target is not None:
            return committed_target
        raise


def _stdout_handoff(path: Path) -> None:
    data = (str(path) + "\n").encode("utf-8")
    try:
        written = os.write(1, data)
    except OSError as error:
        raise BrokenPipeError("stdout write failed before commit") from error
    if written == len(data):
        return
    if written == 0:
        raise BrokenPipeError("stdout write wrote zero bytes")
    raise _HandoffAmbiguousError("stdout write outcome is ambiguous")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--worktree-root", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--lease-ack", type=Path, required=True)
    parser.add_argument("--creation-lock", type=Path, required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()
    raw = read_fd_bounded(0, 1_048_576)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    create_worktree(
        args.repo,
        args.worktree_root,
        args.event_log,
        args.lease_ack,
        args.creation_lock,
        args.execution_id,
        payload,
        _stdout_handoff,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
