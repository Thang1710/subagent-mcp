import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from spikes.phase0a import contracts, fixtures as fixtures_module
from spikes.phase0a.contracts import (
    normalize_stream_bytes,
    write_context_fixture,
    write_live_context_fixture,
)
from spikes.phase0a.fixtures import (
    _normalize_key,
    fixture_envelope,
    read_retained_source,
    validate_fixture,
    write_evidence_index,
    write_model_outcomes_fixture,
    write_strict_mcp_fixture,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase0a" / "current"
COMMITTED_FIXTURES = sorted(FIXTURE_ROOT.glob("*.json"))
if not COMMITTED_FIXTURES:
    raise RuntimeError(f"no committed Phase 0a fixtures found under {FIXTURE_ROOT}")


def _init_event(**changes):
    event = {
        "type": "system",
        "subtype": "init",
        "model": "sonnet",
        "tools": [],
        "mcp_servers": [],
        "plugins": [],
        "capabilities": [],
        "permissionMode": "default",
        "cwd": "C:\\repo",
    }
    event.update(changes)
    return event


def test_retained_source_reader_hashes_the_exact_bytes(tmp_path):
    root = tmp_path / "retained"
    root.mkdir()
    source = root / "source.json"
    source.write_bytes(b'{"safe":true}\n')

    retained = read_retained_source(root, source)

    assert retained.data == b'{"safe":true}\n'
    assert retained.sha256 == hashlib.sha256(retained.data).hexdigest()


def test_retained_source_reader_accepts_exact_size_limit(tmp_path, monkeypatch):
    root = tmp_path / "retained"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"1234")
    monkeypatch.setattr(fixtures_module, "_MAX_RETAINED_SOURCE_BYTES", 4)

    assert read_retained_source(root, source).data == b"1234"


def test_retained_source_reader_rejects_limit_plus_one(tmp_path, monkeypatch):
    root = tmp_path / "retained"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"12345")
    monkeypatch.setattr(fixtures_module, "_MAX_RETAINED_SOURCE_BYTES", 4)

    with pytest.raises(ValueError, match="64 MiB limit"):
        read_retained_source(root, source)


def test_retained_source_reader_rejects_out_of_root_file(tmp_path):
    root = tmp_path / "retained"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="outside retained root") as error:
        read_retained_source(root, outside)

    assert str(outside) not in str(error.value)


def test_retained_source_reader_hides_missing_source_path(tmp_path):
    root = tmp_path / "retained"
    root.mkdir()
    missing = root / "private-source.json"

    with pytest.raises(ValueError, match="retained source unavailable") as error:
        read_retained_source(root, missing)

    assert str(missing) not in str(error.value)


def test_retained_source_reader_rejects_symlink(tmp_path, monkeypatch):
    root = tmp_path / "retained"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = root / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        original_lstat = Path.lstat

        def symlink_lstat(path):
            if path == link:
                return os.stat_result((stat.S_IFLNK, 0, 0, 0, 0, 0, 0, 0, 0, 0))
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", symlink_lstat)

    with pytest.raises(ValueError, match="symlink"):
        read_retained_source(root, link)


def test_retained_source_reader_rejects_mutation_during_read(tmp_path, monkeypatch):
    root = tmp_path / "retained"
    root.mkdir()
    source = root / "source.json"
    source.write_bytes(b'{"safe":true}\n')
    original_open = Path.open

    class MutatingReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            data = self.handle.read(size)
            with original_open(source, "wb") as output:
                output.write(b"changed")
                output.flush()
                os.fsync(output.fileno())
            return data

    def mutating_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == source.resolve() and args and args[0] == "rb":
            return MutatingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", mutating_open)

    with pytest.raises(ValueError, match="changed during read"):
        read_retained_source(root, source)


def test_retained_source_reader_rejects_open_boundary_replacement_before_read(tmp_path, monkeypatch):
    root = tmp_path / "retained"
    root.mkdir()
    source = root / "source.json"
    replacement = root / "replacement.json"
    source.write_bytes(b'{"safe":true}\n')
    replacement.write_bytes(b'{"different":true}\n')
    original_open = Path.open

    class NoReadReplacement:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def fileno(self):
            return self.handle.fileno()

        def read(self):
            raise AssertionError("replacement bytes must not be read")

    def replacing_open(path, *args, **kwargs):
        if path == source.resolve() and args and args[0] == "rb":
            return NoReadReplacement(original_open(replacement, *args, **kwargs))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replacing_open)

    with pytest.raises(ValueError, match="identity changed before read"):
        read_retained_source(root, source)


def test_retained_source_reader_rejects_identity_drift_after_read(tmp_path, monkeypatch):
    root = tmp_path / "retained"
    root.mkdir()
    source = root / "source.json"
    source.write_bytes(b'{"safe":true}\n')
    replacement = root / "replacement.json"
    original_open = Path.open

    class ReplacingReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            result = self.handle.__exit__(*args)
            with original_open(replacement, "wb") as output:
                output.write(b'{"safe":true}\n')
            os.replace(replacement, source)
            return result

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            return self.handle.read(size)

    def replacing_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == source.resolve() and args and args[0] == "rb":
            return ReplacingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", replacing_open)

    with pytest.raises(ValueError, match="identity changed during read"):
        read_retained_source(root, source)


def test_normalize_stream_bytes_parses_the_supplied_snapshot_only():
    data = (
        json.dumps(_init_event()).encode("utf-8")
        + b'\n{"type":"result","subtype":"success","is_error":false}\n'
    )

    assert normalize_stream_bytes(data)["result"]["is_error"] is False


@pytest.mark.parametrize("path", COMMITTED_FIXTURES, ids=lambda path: path.name)
def test_committed_fixture_replays_against_schema(path: Path):
    validate_fixture(json.loads(path.read_text(encoding="utf-8")))


def test_fixture_envelope_never_persists_source_path_or_run_id():
    envelope = fixture_envelope(
        kind="auth_status",
        observed_cli_version="2.1.224 (Claude Code)",
        source_kind="auth_status_json",
        source_sha256="a" * 64,
        payload={"auth": {"logged_in": True}},
        observed=["auth.logged_in"],
        missing=[],
    )
    serialized = json.dumps(envelope)
    assert "source_path" not in serialized
    assert "run_id" not in serialized


def test_context_fixture_explicitly_lists_unattested_declared_native_fields():
    fixture = json.loads((FIXTURE_ROOT / "context-attestation.json").read_text(encoding="utf-8"))
    assert "setting_sources" in fixture["coverage"]["missing"]
    assert "auto_compaction_window" in fixture["coverage"]["missing"]
    assert "nested_agent_cap" in fixture["coverage"]["missing"]


@pytest.mark.parametrize(
    "payload",
    [
        {"fixture_schema_version": 1},
        {
            "fixture_schema_version": 1,
            "kind": "sample",
            "observed_cli_version": "2.1.224 (Claude Code)",
            "source": {"kind": "sample", "sha256": "A" * 64},
            "coverage": {"observed": [], "missing": []},
            "payload": {},
        },
    ],
)
def test_validate_fixture_rejects_invalid_shape_or_digest(payload):
    with pytest.raises(ValueError):
        validate_fixture(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "native"},
        {"api_key": "secret"},
        {"nested": {"source_path": "ignored"}},
        {"safe": "person@example.test"},
        {"safe": "C:\\Users\\someone\\private"},
        {"safe": "/root/private"},
    ],
)
def test_validate_fixture_rejects_sensitive_keys_and_values(payload):
    with pytest.raises(ValueError):
        fixture_envelope(
            kind="sample",
            observed_cli_version="2.1.224 (Claude Code)",
            source_kind="sample_json",
            source_sha256="a" * 64,
            payload=payload,
            observed=[],
            missing=[],
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"accountId": "private"},
        {"accountID": "private"},
        {"APIKey": "private"},
        {"APIToken": "private"},
        {"OAuthToken": "private"},
        {"pluginName": "private"},
        {"nativeSessionId": "private"},
        {"nativeID": "private"},
        {"promptText": "private"},
        {"resultText": "private"},
        {"rawResponse": "private"},
        {"createdAt": 123},
        {"credentialValue": "private"},
        {"authToken": "private"},
        {"oauthTokens": "private"},
        {"clientSecret": "private"},
        {"passwordValue": "private"},
        {"sessionCookie": "private"},
        {"emailAddress": "private"},
        {"organizationId": "private"},
        {"requestIdentifier": "private"},
    ],
)
def test_validate_fixture_rejects_normalized_sensitive_key_classes(payload):
    with pytest.raises(ValueError, match="forbidden fixture key class"):
        fixture_envelope(
            kind="sample",
            observed_cli_version="2.1.224 (Claude Code)",
            source_kind="sample_json",
            source_sha256="a" * 64,
            payload=payload,
            observed=[],
            missing=[],
        )


@pytest.mark.parametrize(
    ("key", "normalized"),
    [
        ("APIKey", "api_key"),
        ("APIToken", "api_token"),
        ("OAuthToken", "o_auth_token"),
        ("nativeID", "native_id"),
        ("accountID", "account_id"),
    ],
)
def test_normalize_key_splits_acronym_camel_case(key, normalized):
    assert _normalize_key(key) == normalized


def test_validate_fixture_accepts_safe_presence_keys_and_unknown_fields():
    fixture_envelope(
        kind="sample",
        observed_cli_version="2.1.224 (Claude Code)",
        source_kind="sample_json",
        source_sha256="a" * 64,
        payload={
            "session_id_present": True,
            "pid_present": False,
            "cwd_present": True,
            "native_session_id_present": True,
            "plugin_count": 3,
            "forbidden_surface_presence": {"codex": False},
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_read_input_tokens": 4,
            "final_result": {"result_is_error": False, "result_subtype": "success"},
            "auth": {"auth_method": "first_party"},
            "future_safe_field": "kept",
        },
        observed=[],
        missing=[],
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt_text_present": "raw prompt"},
        {"plugin_count": "3"},
        {"input_tokens": -1},
        {"forbidden_surface_presence": {"codex": "false"}},
        {"hook_error_observed": "false"},
        {"usage_credits_off_confirmed": 1},
    ],
)
def test_validate_fixture_rejects_malformed_safe_aggregates(payload):
    with pytest.raises(ValueError, match="aggregate"):
        fixture_envelope(
            kind="sample",
            observed_cli_version="2.1.224 (Claude Code)",
            source_kind="sample_json",
            source_sha256="a" * 64,
            payload=payload,
            observed=[],
            missing=[],
        )


def test_evidence_index_hashes_every_other_committed_fixture():
    index_path = FIXTURE_ROOT / "evidence-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = index["payload"]["fixtures"]
    expected = {path.name for path in FIXTURE_ROOT.glob("*.json")} - {index_path.name}
    assert set(entries) == expected
    for name, entry in entries.items():
        data = (FIXTURE_ROOT / name).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        fixture = json.loads(data)
        assert entry["kind"] == fixture["kind"]


def test_write_context_fixture_declares_subset_and_removes_plugin_names(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "model": "fable",
                        "tools": ["Read", "mcp__codex__review"],
                        "mcp_servers": [],
                        "plugins": [
                            {"name": "codex@openai-codex"},
                            {"name": "bridge@agent-bridge"},
                            {"name": "ordinary-plugin"},
                        ],
                        "capabilities": ["interrupt_v1"],
                        "permissionMode": "default",
                        "cwd": "C:\\repo",
                    }
                ),
                json.dumps(
                    {
                        "type": "rate_limit_event",
                        "rate_limit_info": {
                            "status": "allowed",
                            "resetsAt": 123,
                            "isUsingOverage": False,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "stop_reason": "end_turn",
                        "total_cost_usd": 0.01,
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "private_future": "raw prompt text",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "context.json"

    fixture = write_context_fixture(
        stream,
        output,
        "2.1.224 (Claude Code)",
        retained_root=tmp_path,
    )

    payload = fixture["payload"]
    assert payload["init"]["tool_count"] == 2
    assert payload["init"]["forbidden_surface_presence"] == {
        "agent_bridge": True,
        "codex": True,
        "subagent_mcp": False,
    }
    assert "plugins" not in json.dumps(fixture)
    assert "mcp__codex__review" not in json.dumps(fixture)
    assert '"Read"' not in json.dumps(fixture)
    assert "ordinary-plugin" not in json.dumps(fixture)
    assert payload["cost_metadata_present"] is True
    assert payload["plugin_disable_effective"] == "BLOCKED"
    assert payload["relative_plugin_delta"] is None
    assert "total_cost_usd" not in json.dumps(fixture)
    assert "input_tokens" not in json.dumps(fixture)
    assert "resets_at" not in json.dumps(fixture)
    assert "raw prompt text" not in json.dumps(fixture)
    assert payload["rate_limit_advisory"][0]["usage_credits_disabled_inferred"] is False
    assert "usage_credits_disabled_inferred" in fixture["coverage"]["observed"]
    assert "background_environment_equivalence" in fixture["coverage"]["missing"]
    assert "tool_count" in fixture["coverage"]["observed"]
    assert "forbidden_surface_presence" in fixture["coverage"]["observed"]
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_live_context_writer_persists_only_sanitized_projection(tmp_path):
    output = tmp_path / "live-context.json"
    projection = {
        "cli_content_sha256": "c" * 64,
        "status": "CAPABILITY_MISSING",
        "init_subset_status": "PASS",
        "terminal_classification": "success",
        "process_exit_code": 0,
        "init_envelope_observed": True,
        "result_envelope_observed": True,
        "timeout_phase": None,
        "requested_model": "claude-sonnet-5",
        "requested_effort": "low",
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_percent": None,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_model": "claude-sonnet-5",
        "effective_effort": "low",
        "effective_setting_sources": None,
        "effective_auto_compaction_window_tokens": None,
        "effective_auto_compaction_trigger_percent": None,
        "effective_auto_compaction_trigger_formula": None,
        "effective_auto_compaction_trigger_tokens": None,
        "auto_memory_mode": None,
        "effective_cleanup_period": None,
        "claude_md_sources": None,
        "rule_sources": None,
        "skill_sources": None,
        "agent_sources": None,
        "extension_sources_attested": False,
        "inherited_hook_sources": None,
        "subagent_mcp_hook_sources": None,
        "declared_mcp_servers": None,
        "tool_allow_rules": None,
        "tool_deny_rules": None,
        "nested_agent_cap": None,
        "nested_agent_depth": None,
        "additional_directories": None,
        "system_preset_attested": False,
        "system_append_attested": False,
        "content_hashes": None,
        "attestation_sources": None,
        "tool_count": 0,
        "mcp_server_count": 0,
        "plugin_disable_effective": "BLOCKED",
        "relative_plugin_delta": None,
        "is_using_overage": False,
        "rate_statuses": ["allowed_warning"],
        "final_marker_matched": True,
        "checkout_clean": True,
        "instructions_loaded": {
            "delivery_observed": True,
            "instruction_event_count": 1,
            "source_categories": ["project"],
            "content_hashes": ["b" * 64],
            "load_reasons": ["startup"],
        },
        "attested_configuration": "foreground_no_tools",
        "production_equivalent_attestation": "outstanding",
        "declared_native_attestation": "incomplete",
        "background_eligible": False,
        "usage_credits_off_confirmed": False,
        "hook_error_observed": False,
        "missing_fields": [
            "effective_auto_compaction_trigger_tokens",
            "plugin_disable_effective",
        ],
        "cwd": "C:/private",
        "session_id": "private-session",
        "plugin_names": ["private-plugin"],
    }

    fixture = write_live_context_fixture(
        projection,
        output,
        "2.1.224 (Claude Code)",
        source_sha256="a" * 64,
    )

    serialized = json.dumps(fixture)
    assert fixture["kind"] == "live_context_attestation"
    assert fixture["payload"]["plugin_disable_effective"] == "BLOCKED"
    assert fixture["payload"]["relative_plugin_delta"] is None
    assert fixture["payload"]["terminal_classification"] == "success"
    assert fixture["payload"]["process_exit_code"] == 0
    assert fixture["payload"]["init_envelope_observed"] is True
    assert fixture["payload"]["result_envelope_observed"] is True
    assert fixture["payload"]["timeout_phase"] is None
    assert "plugin_count" not in serialized
    assert "C:/private" not in serialized
    assert "private-session" not in serialized
    assert "private-plugin" not in serialized
    assert fixture["coverage"]["missing"] == projection["missing_fields"]
    validate_fixture(json.loads(output.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "payload",
    [
        {"plugin_disable_effective": True},
        {"relative_plugin_delta": -1},
        {"relative_plugin_delta": "1"},
    ],
)
def test_context_plugin_aggregates_are_typed(payload):
    with pytest.raises(ValueError, match="plugin aggregate"):
        fixture_envelope(
            kind="sample",
            observed_cli_version="2.1.224 (Claude Code)",
            source_kind="sample_json",
            source_sha256="a" * 64,
            payload=payload,
            observed=[],
            missing=[],
        )


def test_contracts_main_writes_versioned_auth_agents_and_stop_fixtures(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    agents = tmp_path / "agents.json"
    version = tmp_path / "version.txt"
    output = tmp_path / "out"
    auth.write_text(
        json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}),
        encoding="utf-8",
    )
    agents.write_text(
        json.dumps([{"kind": "background", "waitingFor": "native-private-value"}]),
        encoding="utf-8",
    )
    version.write_text("2.1.224 (Claude Code)\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "contracts",
            "--auth",
            str(auth),
            "--agents",
            str(agents),
            "--version-file",
            str(version),
            "--output-dir",
            str(output),
            "--retained-root",
            str(tmp_path),
        ],
    )

    assert contracts.main() == 0
    for name in ("auth-status.json", "agents-normalized.json", "stop-failure-contract.json"):
        validate_fixture(json.loads((output / name).read_text(encoding="utf-8")))
    agents_fixture = json.loads((output / "agents-normalized.json").read_text(encoding="utf-8"))
    assert agents_fixture["payload"]["agents"][0]["waiting_for_present"] is True
    assert "native-private-value" not in json.dumps(agents_fixture)
    stop_fixture = json.loads((output / "stop-failure-contract.json").read_text(encoding="utf-8"))
    assert stop_fixture["payload"]["unknown_value_policy"] == (
        "map_to_unknown_with_bounded_redacted_safe_raw_category"
    )


def test_model_outcome_writer_uses_strict_normalized_fields_only(tmp_path):
    stream = tmp_path / "outcome.jsonl"
    stream.write_text(
        json.dumps(_init_event(model="claude-fable-5"))
        + "\n"
        + json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "errorCode": "credits_required",
                    "isUsingOverage": False,
                    "overageStatus": "rejected",
                    "overageDisabledReason": "out_of_credits",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "private result text",
                "total_cost_usd": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "model-outcomes.json"

    fixture = write_model_outcomes_fixture(
        [("fable", stream)],
        output,
        "2.1.224 (Claude Code)",
        retained_root=tmp_path,
    )

    outcome = fixture["payload"]["outcomes"][0]
    assert outcome == {
        "classification": "terminal_credits_required",
        "cost_metadata_present": True,
        "observed_model": "claude-fable-5",
        "rate_limits": [
            {
                "error_code": "credits_required",
                "is_using_overage": False,
                "overage_disabled_reason": "out_of_credits",
                "overage_status": "rejected",
                "status": "rejected",
                "usage_credits_disabled_inferred": True,
            }
        ],
        "requested_model": "fable",
        "result_is_error": True,
        "result_subtype": "error",
    }
    assert "private result text" not in output.read_text(encoding="utf-8")
    assert "usage_credits_disabled_inferred" in fixture["coverage"]["observed"]


def test_model_outcome_writer_rejects_missing_sources(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        write_model_outcomes_fixture(
            [],
            tmp_path / "model-outcomes.json",
            "2.1.224 (Claude Code)",
            retained_root=tmp_path,
        )


def test_model_outcome_writer_is_deterministic_for_duplicate_model_labels(tmp_path):
    streams = []
    for index, cost in enumerate((0.02, 0.01)):
        stream = tmp_path / f"outcome-{index}.jsonl"
        stream.write_text(
            json.dumps(_init_event(model="claude-sonnet-5"))
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "total_cost_usd": cost,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        streams.append(("claude-sonnet-5", stream))

    forward = write_model_outcomes_fixture(
        streams,
        tmp_path / "forward.json",
        "2.1.224 (Claude Code)",
        retained_root=tmp_path,
    )
    reverse = write_model_outcomes_fixture(
        list(reversed(streams)),
        tmp_path / "reverse.json",
        "2.1.224 (Claude Code)",
        retained_root=tmp_path,
    )

    assert forward == reverse


def _strict_role_sources(tmp_path, *, strict_marker=False, control_marker=True, strict_exit=0):
    strict_result = tmp_path / "strict-result.json"
    declared = tmp_path / "declared.json"
    control_result = tmp_path / "control-result.json"
    marker = tmp_path / "marker.txt"
    strict_result.write_text(
        json.dumps(
            {
                "exit_code": strict_exit,
                "hook_error_seen": False,
                "init_only_rejected": False,
                "marker_spawned": strict_marker,
            }
        ),
        encoding="utf-8",
    )
    declared.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    control_result.write_text(
        json.dumps(
            {
                "exit_code": 0,
                "hook_error_seen": False,
                "interpretation": "CONTROL_PROVED_STARTUP",
                "marker_spawned": control_marker,
            }
        ),
        encoding="utf-8",
    )
    marker.write_bytes(b"spawned")
    return [
        ("strict", [strict_result, declared]),
        ("control", [control_result, marker]),
    ]


def test_strict_mcp_writer_derives_differential_from_retained_artifacts(tmp_path):
    role_sources = _strict_role_sources(tmp_path)

    fixture = write_strict_mcp_fixture(
        output_path=tmp_path / "strict-mcp-control.json",
        observed_cli_version="2.1.224 (Claude Code)",
        retained_root=tmp_path,
        role_sources=role_sources,
    )

    expected_hashes = sorted(
        hashlib.sha256(path.read_bytes()).hexdigest()
        for _, paths in role_sources
        for path in paths
    )
    assert fixture["payload"] == {
        "control_exit_success": True,
        "control_marker_spawned": True,
        "declared_server_count": 0,
        "source_hashes": expected_hashes,
        "strict_exit_success": True,
        "strict_marker_spawned": False,
    }


def test_strict_mcp_writer_rejects_one_sided_roles(tmp_path):
    role_sources = _strict_role_sources(tmp_path)
    with pytest.raises(ValueError, match="exactly strict and control"):
        write_strict_mcp_fixture(
            output_path=tmp_path / "strict-mcp-control.json",
            observed_cli_version="2.1.224 (Claude Code)",
            retained_root=tmp_path,
            role_sources=role_sources[:1],
        )


def test_strict_mcp_writer_rejects_duplicate_roles(tmp_path):
    role_sources = _strict_role_sources(tmp_path)
    with pytest.raises(ValueError, match="duplicate strict MCP role"):
        write_strict_mcp_fixture(
            output_path=tmp_path / "strict-mcp-control.json",
            observed_cli_version="2.1.224 (Claude Code)",
            retained_root=tmp_path,
            role_sources=[role_sources[0], role_sources[0], role_sources[1]],
        )


def test_strict_mcp_writer_rejects_ambiguous_role(tmp_path):
    role_sources = _strict_role_sources(tmp_path)
    with pytest.raises(ValueError, match="ambiguous strict MCP role"):
        write_strict_mcp_fixture(
            output_path=tmp_path / "strict-mcp-control.json",
            observed_cli_version="2.1.224 (Claude Code)",
            retained_root=tmp_path,
            role_sources=[("unknown", role_sources[0][1]), role_sources[1]],
        )


def test_strict_mcp_writer_rejects_unclassified_role_artifact(tmp_path):
    role_sources = _strict_role_sources(tmp_path)
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({"future": "unclassified"}), encoding="utf-8")
    role_sources[0][1].append(extra)

    with pytest.raises(ValueError, match="ambiguous strict MCP strict-role artifacts"):
        write_strict_mcp_fixture(
            output_path=tmp_path / "strict-mcp-control.json",
            observed_cli_version="2.1.224 (Claude Code)",
            retained_root=tmp_path,
            role_sources=role_sources,
        )


def test_strict_mcp_writer_rejects_overlapping_result_and_config_classes(tmp_path):
    role_sources = _strict_role_sources(tmp_path)
    strict_result, declared = role_sources[0][1]
    overlap = json.loads(strict_result.read_text(encoding="utf-8"))
    overlap["mcpServers"] = {}
    strict_result.write_text(json.dumps(overlap), encoding="utf-8")
    declared.write_text(json.dumps({"future": "unclassified"}), encoding="utf-8")

    with pytest.raises(ValueError, match="overlapping strict MCP artifact classes"):
        write_strict_mcp_fixture(
            output_path=tmp_path / "strict-mcp-control.json",
            observed_cli_version="2.1.224 (Claude Code)",
            retained_root=tmp_path,
            role_sources=role_sources,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"strict_marker": True},
        {"control_marker": False},
        {"strict_exit": 1},
    ],
)
def test_strict_mcp_writer_rejects_mismatched_differential(tmp_path, changes):
    with pytest.raises(ValueError, match="strict MCP differential did not pass"):
        write_strict_mcp_fixture(
            output_path=tmp_path / "strict-mcp-control.json",
            observed_cli_version="2.1.224 (Claude Code)",
            retained_root=tmp_path,
            role_sources=_strict_role_sources(tmp_path, **changes),
        )


def test_evidence_index_writer_excludes_itself(tmp_path):
    for name, kind in (("one.json", "one"), ("two.json", "two")):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                fixture_envelope(
                    kind=kind,
                    observed_cli_version="2.1.224 (Claude Code)",
                    source_kind="test",
                    source_sha256="a" * 64,
                    payload={},
                    observed=[],
                    missing=[],
                ),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    index = write_evidence_index(tmp_path, "2.1.224 (Claude Code)")

    assert set(index["payload"]["fixtures"]) == {"one.json", "two.json"}
    assert "evidence-index.json" not in index["payload"]["fixtures"]


def test_evidence_index_writer_rejects_empty_fixture_set(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        write_evidence_index(tmp_path, "2.1.224 (Claude Code)")
