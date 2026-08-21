from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

try:
    from .contracts import normalize_stop_failure
    from .core import fingerprint, read_fd_bounded, redact_data
    from .locking import locked_file
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from spikes.phase0a.contracts import normalize_stop_failure
    from spikes.phase0a.core import fingerprint, read_fd_bounded, redact_data
    from spikes.phase0a.locking import locked_file


_SCALAR_FIELDS = (
    "hook_event_name",
    "model",
    "name",
    "agent_type",
    "execution_id",
    "worktree_path",
)

_STOP_FAILURE_CATEGORY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_DEFAULT_EVENTS = (
    "SessionStart",
    "WorktreeRemove",
    "Stop",
    "StopFailure",
)


def build_hook_settings(
    python_exe: Path,
    hook_sink: Path,
    event_log: Path,
    events: tuple[str, ...] = _DEFAULT_EVENTS,
    extra_args: tuple[str, ...] = (),
) -> dict[str, Any]:
    handler = {
        "type": "command",
        "command": str(python_exe.resolve()),
        "args": [
            str(hook_sink.resolve()),
            "--event-log",
            str(event_log.resolve()),
            *extra_args,
        ],
        "timeout": 30,
    }
    return {
        "enabledPlugins": {
            "codex@openai-codex": False,
            "bridge@agent-bridge": False,
        },
        "permissions": {
            "deny": [
                "mcp__codex__*",
                "mcp__agent_bridge__*",
                "mcp__subagent_harness_mcp__*",
            ]
        },
        "hooks": {
            event: [{"hooks": [handler.copy()]}]
            for event in events
        },
    }


def sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {
        key: redact_data(payload[key], key)
        for key in _SCALAR_FIELDS
        if isinstance(payload.get(key), (str, int, float, bool))
    }
    if isinstance(payload.get("session_id"), str):
        clean["session_fingerprint"] = fingerprint(payload["session_id"])
    if isinstance(payload.get("agent_id"), str):
        clean["agent_fingerprint"] = fingerprint(payload["agent_id"])
    observer_digest = payload.get("observer_cli_sha256")
    if isinstance(observer_digest, str) and _SHA256.fullmatch(observer_digest):
        clean["observer_cli_sha256"] = observer_digest
    if payload.get("hook_event_name") == "InstructionsLoaded":
        clean["instructions_loaded"] = _sanitize_instructions_loaded(payload)
    if payload.get("hook_event_name") == "StopFailure":
        clean["stop_failure"] = _sanitize_stop_failure(payload)
    return clean


def _safe_category(value: Any) -> str:
    if isinstance(value, str) and _STOP_FAILURE_CATEGORY.fullmatch(value):
        return value
    return "unknown"


def _sanitize_instructions_loaded(payload: dict[str, Any]) -> dict[str, Any]:
    result = {
        "source_category": _safe_category(payload.get("source")),
        "load_reason": _safe_category(payload.get("load_reason")),
    }
    content = payload.get("content")
    if isinstance(content, str):
        result["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return result


def _sanitize_stop_failure(payload: dict[str, Any]) -> dict[str, Any]:
    raw_category = payload.get("error")
    if (
        isinstance(raw_category, str)
        and _STOP_FAILURE_CATEGORY.fullmatch(raw_category)
        and redact_data(raw_category) == raw_category
    ):
        category = raw_category
    else:
        category = "invalid"
    retry_after = payload.get("retry_after")
    if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
        retry_after = None
    return normalize_stop_failure({"error": category, "retry_after": retry_after})


def observe_cli_identity(path: str | Path, expected_sha256: str) -> str:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("observer CLI digest must be a lowercase SHA-256")
    target = Path(path).absolute()
    try:
        before_path = target.stat(follow_symlinks=False)
        attributes = getattr(before_path, "st_file_attributes", 0)
        if target.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise PermissionError("observer CLI must be a direct file")
        canonical = target.resolve(strict=True)
        digest = hashlib.sha256()
        with canonical.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        stable = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
        )
        if stable(before) != stable(after) or stable(canonical.stat()) != stable(after):
            raise PermissionError("observer CLI identity changed during read")
    except OSError as exc:
        raise PermissionError("observer CLI identity is unavailable") from exc
    observed = digest.hexdigest()
    if observed != expected_sha256:
        raise PermissionError("observer CLI identity drifted")
    return observed


def append_event(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize_event(payload), ensure_ascii=False, sort_keys=True)
    with locked_file(target.with_suffix(target.suffix + ".lock"), timeout_seconds=10):
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--observer-cli", type=Path)
    parser.add_argument("--observer-cli-sha256")
    args = parser.parse_args()
    raw = read_fd_bounded(0, 1_048_576)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    if (args.observer_cli is None) != (args.observer_cli_sha256 is None):
        raise ValueError("observer CLI path and digest must be supplied together")
    if args.observer_cli is not None:
        payload["observer_cli_sha256"] = observe_cli_identity(
            args.observer_cli, args.observer_cli_sha256,
        )
    append_event(args.event_log, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
