import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spikes.phase0a.host_probe import (
    build_snapshot,
    compare_observers,
    credential_precedence_ok,
    executable_identity,
    path_record,
)


def test_path_record_reports_file_and_missing(tmp_path: Path):
    present = tmp_path / "present.bin"
    present.write_bytes(b"abc")
    record = path_record(present)
    assert record["exists"] is True
    assert record["size"] == 3
    assert record["device"] is not None
    assert record["inode"] is not None
    assert path_record(tmp_path / "missing")["exists"] is False


def test_compare_observers_reports_visibility_mismatch():
    left = {"paths": {"cache": {"exists": False}}}
    right = {"paths": {"cache": {"exists": True}}}
    assert compare_observers(left, right) == {
        "status": "mismatch",
        "mismatches": {"cache": {"left_exists": False, "right_exists": True}},
        "observed_present": ["cache"],
    }


def test_executable_identity_records_canonical_hash_and_file_identity(tmp_path: Path):
    binary = tmp_path / "claude"
    binary.write_bytes(b"native-binary")
    identity = executable_identity(binary, observed_version="2.1.224 (Claude Code)")
    assert identity["canonical_path"] == str(binary.resolve())
    assert identity["sha256"] == hashlib.sha256(b"native-binary").hexdigest()
    assert identity["size"] == len(b"native-binary")
    assert identity["device"] is not None
    assert identity["inode"] is not None
    assert identity["observed_version"] == "2.1.224 (Claude Code)"


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"],
)
def test_credential_precedence_rejects_every_override(name):
    assert credential_precedence_ok({name: "present"}) is False


def test_credential_precedence_accepts_absent_or_empty_values():
    assert credential_precedence_ok({}) is True
    assert credential_precedence_ok({"ANTHROPIC_API_KEY": ""}) is True


def test_build_snapshot_discards_raw_auth_and_roster_values(tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_bytes(b"binary")
    responses = {
        ("--version",): "2.1.224 (Claude Code)\n",
        ("auth", "status"): '{"loggedIn":true,"authMethod":"claude.ai",'
        '"apiProvider":"firstParty","email":"private@example.com","orgId":"private-org"}',
        ("agents", "--json", "--all"): '[{"id":"short","sessionId":"private-session",'
        '"cwd":"C:/private","kind":"background","state":"done","pid":123,'
        '"waitingFor":"private question"}]',
    }

    def fake_runner(_name, argv, **_kwargs):
        return SimpleNamespace(
            exit_code=0,
            timed_out=False,
            stdout=responses[tuple(argv[1:])],
            stderr="",
        )

    snapshot = build_snapshot("test", cli, env={}, runner=fake_runner)
    serialized = json.dumps(snapshot)
    assert "private@example.com" not in serialized
    assert "private-org" not in serialized
    assert "private-session" not in serialized
    assert "C:/private" not in serialized
    assert "private question" not in serialized
    assert snapshot["auth"] == {
        "logged_in": True,
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
    }
    assert snapshot["agents"][0]["session_id_present"] is True
    assert snapshot["agents"][0]["waiting_for_present"] is True
    assert snapshot["credential_overrides_absent"] is True
    assert snapshot["credential_precedence_evidence"] == "not_observed"
    assert "credential_precedence_ok" not in snapshot


def test_equal_absent_observations_are_not_positive_visibility_evidence():
    result = compare_observers(
        {"paths": {"desktop_cache_root": {"exists": False}}},
        {"paths": {"desktop_cache_root": {"exists": False}}},
    )
    assert result == {
        "status": "not_observed",
        "mismatches": {},
        "observed_present": [],
    }


def _probe_result(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(exit_code=0, timed_out=False, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "version",
    [
        "2.1.224 (Claude Code)\nprivate-version",
        "2.1.224 (Claude Code) " + "x" * 300,
        "not-a-claude-version",
    ],
)
def test_build_snapshot_rejects_unbounded_or_noncontract_version_output(tmp_path: Path, version: str):
    cli = tmp_path / "claude"
    cli.write_bytes(b"binary")

    def fake_runner(_name, argv, **_kwargs):
        return _probe_result(version if argv[1:] == ["--version"] else "{}")

    snapshot = build_snapshot("test", cli, env={}, runner=fake_runner)
    assert snapshot["standalone_cli"]["status"] == "probe_failed"
    assert snapshot["standalone_cli"]["reason"] == "malformed_output"
    assert version not in json.dumps(snapshot)


def test_build_snapshot_rejects_nested_auth_values(tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_bytes(b"binary")
    responses = {
        ("--version",): "2.1.224 (Claude Code)\n",
        ("auth", "status"): '{"loggedIn":true,"authMethod":{"private":"value"},'
        '"apiProvider":"firstParty"}',
        ("agents", "--json", "--all"): "[]",
    }

    def fake_runner(_name, argv, **_kwargs):
        return _probe_result(responses[tuple(argv[1:])])

    snapshot = build_snapshot("test", cli, env={}, runner=fake_runner)
    assert snapshot["auth"]["reason"] == "malformed_output"
    assert "private" not in json.dumps(snapshot)


def test_build_snapshot_rejects_nested_waiting_for_values(tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_bytes(b"binary")
    responses = {
        ("--version",): "2.1.224 (Claude Code)\n",
        ("auth", "status"): '{"loggedIn":true,"authMethod":"claude.ai",'
        '"apiProvider":"firstParty"}',
        ("agents", "--json", "--all"): '[{"waitingFor":{"private":"question"}}]',
    }

    def fake_runner(_name, argv, **_kwargs):
        return _probe_result(responses[tuple(argv[1:])])

    snapshot = build_snapshot("test", cli, env={}, runner=fake_runner)
    assert snapshot["agents"]["reason"] == "malformed_output"
    assert "private" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    ("auth_payload", "agents_payload", "field"),
    [
        ("[]", "[]", "auth"),
        ('{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}', "{}", "agents"),
    ],
)
def test_build_snapshot_rejects_wrong_json_top_level_type(
    tmp_path: Path, auth_payload: str, agents_payload: str, field: str
):
    cli = tmp_path / "claude"
    cli.write_bytes(b"binary")
    responses = {
        ("--version",): "2.1.224 (Claude Code)\n",
        ("auth", "status"): auth_payload,
        ("agents", "--json", "--all"): agents_payload,
    }

    def fake_runner(_name, argv, **_kwargs):
        return _probe_result(responses[tuple(argv[1:])])

    snapshot = build_snapshot("test", cli, env={}, runner=fake_runner)
    assert snapshot[field] == {
        "status": "probe_failed",
        "reason": "malformed_output",
        "probe": {"exit_ok": True, "timed_out": False},
    }


def test_compare_observers_requires_matching_device_and_inode_identity():
    matched = compare_observers(
        {"paths": {"cache": {"exists": True, "device": 1, "inode": 2}}},
        {"paths": {"cache": {"exists": True, "device": 1, "inode": 2}}},
    )
    assert matched["status"] == "matched_present"
    different = compare_observers(
        {"paths": {"cache": {"exists": True, "device": 1, "inode": 2}}},
        {"paths": {"cache": {"exists": True, "device": 1, "inode": 3}}},
    )
    assert different["status"] == "mismatch"
    assert different["mismatches"]["cache"]["identity"] == "different"
    missing = compare_observers(
        {"paths": {"cache": {"exists": True}}},
        {"paths": {"cache": {"exists": True, "device": 1, "inode": 2}}},
    )
    assert missing["status"] == "mismatch"
    assert missing["mismatches"]["cache"]["identity"] == "missing"


def test_build_snapshot_rejects_identity_change_during_version_probe(tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_bytes(b"binary")
    calls: list[str] = []

    def fake_runner(name, _argv, **_kwargs):
        calls.append(name)
        if name == "version":
            cli.write_bytes(b"change")
            return _probe_result("2.1.224 (Claude Code)\n")
        raise AssertionError("auth and agents must not run after identity drift")

    snapshot = build_snapshot("test", cli, env={}, runner=fake_runner)
    assert calls == ["version"]
    assert snapshot["standalone_cli"]["reason"] == "identity_changed"
    assert snapshot["auth"] == {"status": "probe_not_run"}
    assert snapshot["agents"] == {"status": "probe_not_run"}
