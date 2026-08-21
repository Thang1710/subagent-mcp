from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from spikes.phase0a import live_review_guard as guard


def _payload(root: Path, tool_name: str, tool_input: dict) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "tool-1",
        "session_id": "session-1",
        "transcript_path": "opaque-native-transcript",
        "permission_mode": "dontAsk",
        "cwd": str(root.resolve()),
    }


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Read", {"file_path": "pkg/module.py", "offset": 0, "limit": 20, "pages": "1-2,4"}),
        ("Glob", {"pattern": "**/*.py", "path": "pkg"}),
        (
            "Grep",
            {
                "pattern": "needle",
                "path": "pkg",
                "glob": "**/*.py",
                "type": "py",
                "output_mode": "content",
                "-i": False,
                "-n": True,
                "multiline": False,
                "-B": 0,
                "-A": 2,
                "-C": 1,
                "context": 3,
                "head_limit": 50,
                "offset": 0,
            },
        ),
    ],
)
def test_exact_read_glob_grep_schemas_are_allowed(
    tmp_path: Path, tool_name: str, tool_input: dict,
) -> None:
    root = tmp_path / "export"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "module.py").write_text("needle\n", encoding="utf-8")

    assert guard.validate_review_tool_call(_payload(root, tool_name, tool_input), root)


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Read", {"file_path": "pkg/module.py", "unknown": True}),
        ("Read", {"file_path": "pkg/module.py", "offset": -1}),
        ("Glob", {"pattern": "../*.py"}),
        ("Glob", {"pattern": "C:\\private\\*.py"}),
        ("Glob", {"pattern": "\\\\server\\share\\*.py"}),
        ("Grep", {"pattern": "x", "glob": "../*.py"}),
        ("Grep", {"pattern": "x", "output_mode": "raw"}),
        ("Grep", {"pattern": "x", "-i": 1}),
        ("Write", {"file_path": "pkg/module.py", "content": "change"}),
    ],
)
def test_unapproved_schema_or_pattern_is_denied(
    tmp_path: Path, tool_name: str, tool_input: dict,
) -> None:
    root = tmp_path / "export"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "module.py").write_text("x\n", encoding="utf-8")

    with pytest.raises(guard.ReviewGuardError):
        guard.validate_review_tool_call(_payload(root, tool_name, tool_input), root)


def test_absolute_or_parent_escape_and_reparse_are_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "export"
    root.mkdir()
    inside = root / "inside.py"
    inside.write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("private\n", encoding="utf-8")

    with pytest.raises(guard.ReviewGuardError, match="escaped"):
        guard.validate_review_tool_call(
            _payload(root, "Read", {"file_path": str(outside)}), root,
        )

    original = guard._is_reparse
    monkeypatch.setattr(
        guard,
        "_is_reparse",
        lambda path: path == inside.resolve() or original(path),
    )
    with pytest.raises(guard.ReviewGuardError, match="reparse"):
        guard.validate_review_tool_call(
            _payload(root, "Read", {"file_path": "inside.py"}), root,
        )


def test_direct_hook_execution_exits_two_on_denial_and_audit_stores_counts_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    root.mkdir()
    (root / "allowed.py").write_text("ok\n", encoding="utf-8")
    audit = tmp_path / "audit.json"
    script = Path(guard.__file__).resolve()
    argv = [
        sys.executable,
        str(script),
        "--export-root",
        str(root),
        "--audit",
        str(audit),
    ]
    allowed = subprocess.run(
        argv,
        input=json.dumps(_payload(root, "Read", {"file_path": "allowed.py"})).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    denied = subprocess.run(
        argv,
        input=json.dumps(_payload(root, "Glob", {"pattern": "../*"})).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert allowed.returncode == 0
    assert denied.returncode == 2
    assert json.loads(audit.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "allow_count": 1,
        "deny_count": 1,
    }
    serialized = audit.read_text(encoding="utf-8")
    assert "allowed.py" not in serialized
    assert str(root) not in serialized


def test_review_settings_pin_one_pretool_guard_and_disable_recursion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    root.mkdir()
    audit = tmp_path / "audit.json"
    settings = guard.build_review_guard_settings(
        sys.executable, Path(guard.__file__), root, audit,
    )

    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert hook["command"] == str(Path(sys.executable).resolve())
    assert hook["args"][1:] == [
        "--export-root", str(root.resolve()), "--audit", str(audit.resolve()),
    ]
    assert settings["enabledPlugins"] == {
        "codex@openai-codex": False,
        "bridge@agent-bridge": False,
    }
    assert settings["permissions"]["deny"] == [
        "mcp__codex__*",
        "mcp__agent_bridge__*",
        "mcp__subagent_harness_mcp__*",
    ]
