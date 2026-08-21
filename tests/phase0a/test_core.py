import json
import os
import sys
import threading
from pathlib import Path

import pytest

from spikes.phase0a.core import (
    fingerprint,
    read_fd_bounded,
    redact_data,
    redact_text,
    run_argv,
    write_json_atomic,
)


def test_run_argv_captures_success_without_shell():
    result = run_argv(
        "echo",
        [sys.executable, "-c", "print('ok')"],
        timeout_seconds=5,
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.timed_out is False
    assert result.argv[0] == sys.executable


def test_run_argv_reports_timeout():
    result = run_argv(
        "timeout",
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout_seconds=0.05,
    )
    assert result.timed_out is True
    assert result.exit_code is None


def test_redact_text_masks_supported_credentials():
    value = "ANTHROPIC_API_KEY=sk-ant-secret Bearer abc.def.ghi"
    redacted = redact_text(value)
    assert "sk-ant-secret" not in redacted
    assert "abc.def.ghi" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_redact_text_masks_all_precedence_credentials():
    text = (
        "ANTHROPIC_API_KEY=one ANTHROPIC_AUTH_TOKEN=two "
        "CLAUDE_CODE_OAUTH_TOKEN=three Authorization: Bearer four "
        "person@example.com"
    )
    redacted = redact_text(text)
    assert all(
        secret not in redacted
        for secret in ("one", "two", "three", "four", "person@example.com")
    )


def test_redact_data_masks_sensitive_keys_and_pii():
    redacted = redact_data({
        "email": "person@example.com",
        "orgId": "org-private",
        "request_id": "req-private",
        "nested": {"password": "secret", "safe": "ok"},
    })
    assert redacted == {
        "email": "[REDACTED]",
        "orgId": "[REDACTED]",
        "request_id": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "safe": "ok"},
    }


def test_read_fd_bounded_reads_until_eof_across_chunks():
    read_fd, write_fd = os.pipe()
    payload = b"abc" * 100_000

    def write_all():
        try:
            view = memoryview(payload)
            while view:
                written = os.write(write_fd, view)
                view = view[written:]
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=write_all)
    writer.start()
    try:
        assert read_fd_bounded(read_fd, len(payload)) == payload
    finally:
        os.close(read_fd)
        writer.join(timeout=2)
    assert not writer.is_alive()


def test_read_fd_bounded_rejects_limit_plus_one():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"12345")
        os.close(write_fd)
        write_fd = -1
        with pytest.raises(ValueError, match="exceeds 4 bytes"):
            read_fd_bounded(read_fd, 4)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_fingerprint_is_stable_without_disclosing_input():
    value = fingerprint("native-session-id")
    assert value == fingerprint("native-session-id")
    assert "native-session-id" not in value
    assert len(value) == 64


def test_write_json_atomic_replaces_complete_document(tmp_path: Path):
    target = tmp_path / "result.json"
    write_json_atomic(target, {"value": 1})
    write_json_atomic(target, {"value": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}
    assert list(tmp_path.glob("*.tmp")) == []
