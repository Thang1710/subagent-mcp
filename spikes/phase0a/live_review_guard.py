from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

try:
    from .core import read_fd_bounded, write_json_atomic
    from .locking import locked_file
except ImportError:  # Direct command-hook execution has no package context.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from spikes.phase0a.core import read_fd_bounded, write_json_atomic
    from spikes.phase0a.locking import locked_file


_MAX_INPUT_BYTES = 1024 * 1024
_MAX_STRING_BYTES = 8192
_OUTPUT_MODES = {"content", "files_with_matches", "count"}
_GREP_BOOL_KEYS = {"-i", "-n", "multiline"}
_GREP_INT_KEYS = {"-B", "-A", "-C", "context", "head_limit", "offset"}
_PAGES = re.compile(r"^[0-9]{1,4}(?:-[0-9]{1,4})?(?:,[0-9]{1,4}(?:-[0-9]{1,4})?)*$")


class ReviewGuardError(ValueError):
    pass


def _bounded_string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ReviewGuardError(f"{label} must be a string")
    if "\x00" in value or len(value.encode("utf-8")) > _MAX_STRING_BYTES:
        raise ReviewGuardError(f"{label} exceeds the bounded schema")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReviewGuardError(f"{label} must be a non-negative integer")
    return value


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReviewGuardError("review path is unavailable") from exc
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _confined_path(root: Path, raw: str, *, require_file: bool = False) -> Path:
    supplied = Path(_bounded_string(raw, "tool path"))
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ReviewGuardError("tool path escaped the review export") from exc
    current = resolved_root
    relative = resolved.relative_to(resolved_root)
    if _is_reparse(resolved_root):
        raise ReviewGuardError("review export root is a reparse point")
    for part in relative.parts:
        current = current / part
        if _is_reparse(current):
            raise ReviewGuardError("tool path contains a reparse point")
    if require_file and not resolved.is_file():
        raise ReviewGuardError("Read target must be a regular file")
    return resolved


def _relative_pattern(value: Any, label: str) -> str:
    pattern = _bounded_string(value, label)
    windows = PureWindowsPath(pattern)
    posix = PurePosixPath(pattern.replace("\\", "/"))
    if (
        pattern.startswith(("/", "\\"))
        or windows.is_absolute()
        or bool(windows.drive)
        or posix.is_absolute()
        or ".." in windows.parts
        or ".." in posix.parts
    ):
        raise ReviewGuardError(f"{label} must be a confined relative pattern")
    return pattern


def _validate_read(tool_input: Mapping[str, Any], root: Path) -> None:
    allowed = {"file_path", "offset", "limit", "pages"}
    if set(tool_input) - allowed or "file_path" not in tool_input:
        raise ReviewGuardError("Read input schema mismatch")
    _confined_path(root, _bounded_string(tool_input["file_path"], "Read.file_path"), require_file=True)
    for key in ("offset", "limit"):
        if key in tool_input:
            _nonnegative_int(tool_input[key], f"Read.{key}")
    if "pages" in tool_input:
        pages = _bounded_string(tool_input["pages"], "Read.pages")
        if len(pages) > 128 or _PAGES.fullmatch(pages) is None:
            raise ReviewGuardError("Read.pages is invalid")
        for part in pages.split(","):
            bounds = [int(value) for value in part.split("-")]
            if any(value > 9999 for value in bounds) or (
                len(bounds) == 2 and bounds[0] > bounds[1]
            ):
                raise ReviewGuardError("Read.pages is invalid")


def _validate_glob(tool_input: Mapping[str, Any], root: Path) -> None:
    if set(tool_input) - {"pattern", "path"} or "pattern" not in tool_input:
        raise ReviewGuardError("Glob input schema mismatch")
    _relative_pattern(tool_input["pattern"], "Glob.pattern")
    if "path" in tool_input:
        _confined_path(root, _bounded_string(tool_input["path"], "Glob.path"))


def _validate_grep(tool_input: Mapping[str, Any], root: Path) -> None:
    allowed = {
        "pattern", "path", "glob", "type", "output_mode",
        *_GREP_BOOL_KEYS, *_GREP_INT_KEYS,
    }
    if set(tool_input) - allowed or "pattern" not in tool_input:
        raise ReviewGuardError("Grep input schema mismatch")
    _bounded_string(tool_input["pattern"], "Grep.pattern", allow_empty=True)
    if "path" in tool_input:
        _confined_path(root, _bounded_string(tool_input["path"], "Grep.path"))
    if "glob" in tool_input:
        _relative_pattern(tool_input["glob"], "Grep.glob")
    if "type" in tool_input:
        value = _bounded_string(tool_input["type"], "Grep.type")
        if re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", value) is None:
            raise ReviewGuardError("Grep.type is invalid")
    if "output_mode" in tool_input and tool_input["output_mode"] not in _OUTPUT_MODES:
        raise ReviewGuardError("Grep.output_mode is invalid")
    for key in _GREP_BOOL_KEYS:
        if key in tool_input and type(tool_input[key]) is not bool:
            raise ReviewGuardError(f"Grep.{key} must be a boolean")
    for key in _GREP_INT_KEYS:
        if key in tool_input:
            _nonnegative_int(tool_input[key], f"Grep.{key}")


def validate_review_tool_call(payload: Mapping[str, Any], export_root: str | Path) -> bool:
    if not isinstance(payload, Mapping) or payload.get("hook_event_name") != "PreToolUse":
        raise ReviewGuardError("review hook payload is invalid")
    if set(payload) - {
        "hook_event_name", "tool_name", "tool_input", "session_id", "cwd",
        "transcript_path", "permission_mode", "tool_use_id",
    }:
        raise ReviewGuardError("review hook payload contains unsupported fields")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if tool_name not in {"Read", "Glob", "Grep"} or not isinstance(tool_input, Mapping):
        raise ReviewGuardError("review tool is not approved")
    root = Path(export_root).resolve(strict=True)
    for key in ("session_id", "transcript_path", "permission_mode", "tool_use_id"):
        if key in payload:
            _bounded_string(payload[key], f"hook {key}")
    raw_cwd = payload.get("cwd")
    if raw_cwd is not None:
        cwd = _confined_path(root, _bounded_string(raw_cwd, "hook cwd"))
        if cwd != root:
            raise ReviewGuardError("review hook cwd must equal the export root")
    if tool_name == "Read":
        _validate_read(tool_input, root)
    elif tool_name == "Glob":
        _validate_glob(tool_input, root)
    else:
        _validate_grep(tool_input, root)
    return True


def _record_audit(path: Path, *, allowed: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(path.with_suffix(path.suffix + ".lock"), timeout_seconds=10):
        if path.exists():
            if _is_reparse(path):
                raise ReviewGuardError("review audit is a reparse point")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReviewGuardError("review audit is malformed") from exc
        else:
            value = {"schema_version": 1, "allow_count": 0, "deny_count": 0}
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "allow_count", "deny_count"}
            or value.get("schema_version") != 1
            or any(
                isinstance(value.get(key), bool)
                or not isinstance(value.get(key), int)
                or value.get(key) < 0
                for key in ("allow_count", "deny_count")
            )
        ):
            raise ReviewGuardError("review audit schema is invalid")
        value["allow_count" if allowed else "deny_count"] += 1
        write_json_atomic(path, value)


def build_review_guard_settings(
    python_exe: str | Path,
    guard_script: str | Path,
    export_root: str | Path,
    audit_path: str | Path,
) -> dict[str, Any]:
    return {
        "hooks": {
            "PreToolUse": [{
                "hooks": [{
                    "type": "command",
                    "command": str(Path(python_exe).resolve()),
                    "args": [
                        str(Path(guard_script).resolve()),
                        "--export-root", str(Path(export_root).resolve()),
                        "--audit", str(Path(audit_path).resolve()),
                    ],
                    "timeout": 30,
                }],
            }],
        },
        "enabledPlugins": {
            "codex@openai-codex": False,
            "bridge@agent-bridge": False,
        },
        "permissions": {
            "deny": [
                "mcp__codex__*",
                "mcp__agent_bridge__*",
                "mcp__subagent_harness_mcp__*",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = read_fd_bounded(0, _MAX_INPUT_BYTES)
        payload = json.loads(raw.decode("utf-8"))
        validate_review_tool_call(payload, args.export_root)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReviewGuardError, ValueError):
        try:
            _record_audit(args.audit, allowed=False)
        except Exception:
            pass
        print("review path guard denied the tool call", file=sys.stderr)
        return 2
    _record_audit(args.audit, allowed=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
