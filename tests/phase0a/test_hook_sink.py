import json
import hashlib
from pathlib import Path

import pytest

from spikes.phase0a.core import fingerprint
from spikes.phase0a.hook_sink import (
    append_event,
    build_hook_settings,
    observe_cli_identity,
    sanitize_event,
)


def test_sanitize_event_keeps_only_normalized_contract_fields():
    event = sanitize_event({
        "session_id": "native-session",
        "hook_event_name": "StopFailure",
        "error": "future_failure",
        "retry_after": 17,
        "execution_id": "execution-1",
        "name": "probe-one",
        "last_assistant_message": "private model output",
        "tool_input": {"password": "private"},
        "error_details": "raw provider body",
        "transcript_path": "C:/private/transcript.jsonl",
        "cwd": "C:/private/repo",
    })
    assert event == {
        "execution_id": "execution-1",
        "hook_event_name": "StopFailure",
        "name": "probe-one",
        "session_fingerprint": fingerprint("native-session"),
        "stop_failure": {
            "category": "unknown",
            "raw_category": "future_failure",
            "retry_after": 17,
        },
    }


def test_sanitize_event_never_keeps_content_heavy_fields():
    serialized = json.dumps(sanitize_event({
        "hook_event_name": "Stop",
        "last_assistant_message": "secret-output",
        "tool_input": {"headers": {"authorization": "Bearer nested.secret.value"}},
        "error_details": "request-private",
    }))
    assert "secret-output" not in serialized
    assert "nested.secret.value" not in serialized
    assert "request-private" not in serialized


@pytest.mark.parametrize(
    ("error", "retry_after", "private_value", "expected_retry_after"),
    [
        ("Authorization: Bearer private-token", 17, "private-token", 17),
        ("person@example.com", {"retry": "private-object"}, "person@example.com", None),
        ('{"message":"raw provider body"}', ["private-list"], "raw provider body", None),
    ],
)
def test_sanitize_stop_failure_rejects_unsafe_error_and_retry_values(
    error, retry_after, private_value, expected_retry_after
):
    event = sanitize_event({
        "hook_event_name": "StopFailure",
        "error": error,
        "retry_after": retry_after,
    })
    serialized = json.dumps(event)
    assert private_value not in serialized
    assert event["stop_failure"] == {
        "category": "unknown",
        "raw_category": "invalid",
        "retry_after": expected_retry_after,
    }


def test_sanitize_stop_failure_keeps_safe_future_category():
    event = sanitize_event({
        "hook_event_name": "StopFailure",
        "error": "future-provider-category_2",
        "retry_after": 17,
    })
    assert event["stop_failure"] == {
        "category": "unknown",
        "raw_category": "future-provider-category_2",
        "retry_after": 17,
    }


def test_append_event_writes_one_json_line(tmp_path: Path):
    target = tmp_path / "events.jsonl"
    append_event(target, {"hook_event_name": "SessionStart"})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["hook_event_name"] == "SessionStart"


def test_build_hook_settings_uses_exec_form_not_shell_quoting(tmp_path: Path):
    python_exe = tmp_path / "python.exe"
    sink = tmp_path / "hook_sink.py"
    settings = build_hook_settings(python_exe, sink, tmp_path / "events.jsonl")
    handler = settings["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"] == str(python_exe.resolve())
    assert handler["args"] == [
        str(sink.resolve()),
        "--event-log",
        str((tmp_path / "events.jsonl").resolve()),
    ]
    assert "shell" not in handler
    assert "WorktreeCreate" not in settings["hooks"]
    assert "WorktreeRemove" in settings["hooks"]
    assert settings["enabledPlugins"]["codex@openai-codex"] is False
    assert settings["enabledPlugins"]["bridge@agent-bridge"] is False
    assert settings["permissions"]["deny"] == [
        "mcp__codex__*",
        "mcp__agent_bridge__*",
        "mcp__subagent_harness_mcp__*",
    ]


def test_instructions_loaded_keeps_only_category_and_content_hash() -> None:
    event = sanitize_event({
        "hook_event_name": "InstructionsLoaded",
        "source": "project",
        "file_path": "D:/private/repo/CLAUDE.md",
        "content": "private instruction content",
        "load_reason": "startup",
        "observer_cli_sha256": "a" * 64,
    })

    assert event == {
        "hook_event_name": "InstructionsLoaded",
        "observer_cli_sha256": "a" * 64,
        "instructions_loaded": {
            "source_category": "project",
            "content_sha256": hashlib.sha256(b"private instruction content").hexdigest(),
            "load_reason": "startup",
        },
    }
    serialized = json.dumps(event)
    assert "D:/private" not in serialized
    assert "private instruction content" not in serialized


def test_instructions_loaded_rejects_path_like_source_category() -> None:
    event = sanitize_event({
        "hook_event_name": "InstructionsLoaded",
        "source": "D:/private/repo/CLAUDE.md",
        "content": "safe",
    })

    assert event["instructions_loaded"]["source_category"] == "unknown"


def test_hook_observer_hashes_cli_independently_and_rejects_drift(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone")
    expected = hashlib.sha256(b"standalone").hexdigest()

    assert observe_cli_identity(cli, expected) == expected
    cli.write_bytes(b"drifted")
    with pytest.raises(PermissionError, match="observer CLI identity drifted"):
        observe_cli_identity(cli, expected)
