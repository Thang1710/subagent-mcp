from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from spikes.phase0a.fixtures import fixture_envelope, write_evidence_index
from spikes.phase0a.live_evidence import (
    _GATE_NAMES,
    adjudicate_gate_set,
    live_fixture,
    load_indexed_fixtures,
    main,
    phase_decision,
    rebuild_live_evidence_index,
    regenerate_report,
    section_19_1_rows,
)


VERSION = "2.1.224 (Claude Code)"
COMMITTED_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase0a" / "current"


def _fixture(kind: str, payload: dict, *, missing: list[str] | None = None) -> dict:
    return fixture_envelope(
        kind=kind,
        observed_cli_version=VERSION,
        source_kind="bounded_test_projection",
        source_sha256="f" * 64,
        payload=payload,
        observed=sorted(payload),
        missing=[] if missing is None else missing,
    )


def _write_fixture(root: Path, name: str, fixture: dict) -> None:
    (root / name).write_text(
        json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


def _host_payload(cli_digest: str = "a" * 64) -> dict:
    return {
        "auth": {
            "api_provider": "firstParty",
            "auth_method": "claude.ai",
            "logged_in": True,
        },
        "cli_content_sha256": cli_digest,
        "identity_stable": True,
        "status": "ready",
        "wrapper_rejection": {
            "desktop_runtime_accepted": False,
            "rejection_evidence_complete": True,
        },
    }


def test_live_fixture_is_sanitized_and_versioned(tmp_path: Path) -> None:
    fixture = live_fixture(
        gate_id="standalone_cli",
        observed_cli_version="2.1.224 (Claude Code)",
        cli_sha256="a" * 64,
        source_sha256="b" * 64,
        payload={"identity_stable": True, "roster_state_count": 0},
        observed=["identity_stable"],
        missing=[],
    )

    assert fixture["kind"] == "live_host"
    assert fixture["payload"]["gate"] == "standalone_cli"
    assert fixture["payload"]["identity_stable"] is True
    assert "canonical_path" not in json.dumps(fixture)


def test_live_fixture_rejects_private_payload_values() -> None:
    with pytest.raises(ValueError):
        live_fixture(
            gate_id="standalone_cli",
            observed_cli_version="2.1.224 (Claude Code)",
            cli_sha256="a" * 64,
            source_sha256="b" * 64,
            payload={"email": "person@example.test"},
            observed=[],
            missing=[],
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"canonical_path": "D:\\private\\repo"},
        {"native_session": "private"},
        {"safe_value": "D:\\private\\repo"},
        {"safe_value": "/private/repo"},
    ],
)
def test_live_fixture_rejects_path_and_session_data(payload) -> None:
    with pytest.raises(ValueError, match="private|path|session"):
        live_fixture(
            gate_id="standalone_cli",
            observed_cli_version="2.1.224 (Claude Code)",
            cli_sha256="a" * 64,
            source_sha256="b" * 64,
            payload=payload,
            observed=[],
            missing=[],
        )


def test_live_fixture_rejects_reserved_metadata_override() -> None:
    with pytest.raises(ValueError, match="reserved"):
        live_fixture(
            gate_id="standalone_cli",
            observed_cli_version="2.1.224 (Claude Code)",
            cli_sha256="a" * 64,
            source_sha256="b" * 64,
            payload={"gate": "context_attestation"},
            observed=[],
            missing=[],
        )


def test_rebuild_live_evidence_index_includes_only_committed_fixture_files(tmp_path: Path) -> None:
    first = live_fixture(
        gate_id="standalone_cli", observed_cli_version="2.1.224 (Claude Code)",
        cli_sha256="a" * 64, source_sha256="b" * 64, payload={}, observed=[], missing=[],
    )
    (tmp_path / "live-host.json").write_text(json.dumps(first, sort_keys=True) + "\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("not a fixture", encoding="utf-8")

    index = rebuild_live_evidence_index(tmp_path, "2.1.224 (Claude Code)")

    entry = index["payload"]["fixtures"]["live-host.json"]
    assert entry["sha256"] == hashlib.sha256((tmp_path / "live-host.json").read_bytes()).hexdigest()
    assert set(index["payload"]["fixtures"]) == {"live-host.json"}


def test_committed_index_adjudicates_the_exact_gate_set() -> None:
    gates = adjudicate_gate_set(COMMITTED_ROOT / "evidence-index.json", COMMITTED_ROOT)

    assert set(gates) == set(_GATE_NAMES)
    assert len(gates) == 21
    assert gates["context_init_subset"]["status"] == "PASS"
    assert gates["context_attestation"]["status"] == "BLOCKED"
    assert gates["worktree_remove_hook"]["status"] == "BLOCKED"
    assert phase_decision(gates)["status"] == "BLOCKED"
    assert len(section_19_1_rows(gates)) == 10


@pytest.mark.parametrize("nonpass_gate", _GATE_NAMES)
def test_any_single_nonpass_gate_keeps_phase_decision_blocked(nonpass_gate: str) -> None:
    gates = {
        name: {"status": "PASS", "evidence": "indexed named evidence"}
        for name in _GATE_NAMES
    }
    gates[nonpass_gate] = {
        "status": "BLOCKED",
        "evidence": "required named evidence is missing",
    }

    assert phase_decision(gates)["status"] == "BLOCKED"


def test_index_hash_mismatch_and_unindexed_json_fail_closed(tmp_path: Path) -> None:
    _write_fixture(tmp_path, "auth-status.json", _fixture("auth_status", {"auth": {}}))
    write_evidence_index(tmp_path, VERSION)
    (tmp_path / "auth-status.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="malformed|required|identity mismatch"):
        load_indexed_fixtures(tmp_path / "evidence-index.json", tmp_path)

    _write_fixture(tmp_path, "auth-status.json", _fixture("auth_status", {"auth": {}}))
    write_evidence_index(tmp_path, VERSION)
    (tmp_path / "gates.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fixture root and evidence index disagree"):
        load_indexed_fixtures(tmp_path / "evidence-index.json", tmp_path)


def test_requested_274000_without_effective_trigger_cannot_pass(tmp_path: Path) -> None:
    _write_fixture(tmp_path, "live-host.json", _fixture("live_host", _host_payload()))
    _write_fixture(
        tmp_path,
        "live-context.json",
        _fixture(
            "live_context_attestation",
            {
                "cli_content_sha256": "a" * 64,
                "status": "PASS",
                "declared_native_attestation": "complete",
                "missing_fields": [],
                "requested_auto_compaction_window_tokens": 274000,
                "requested_auto_compaction_trigger_tokens": 274000,
                "effective_auto_compaction_window_tokens": None,
                "effective_auto_compaction_trigger_percent": None,
                "effective_auto_compaction_trigger_tokens": None,
            },
        ),
    )
    write_evidence_index(tmp_path, VERSION)

    gates = adjudicate_gate_set(tmp_path / "evidence-index.json", tmp_path)

    assert gates["context_attestation"]["status"] == "BLOCKED"


def test_lifecycle_gate_requires_all_five_states_and_clean_terminal_controls(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path, "live-host.json", _fixture("live_host", _host_payload()))
    _write_fixture(
        tmp_path,
        "live-background-matrix.json",
        _fixture(
            "live_background_matrix",
            {
                "cli_content_sha256": "a" * 64,
                "state_presence": {
                    "working": True,
                    "needs_input_or_blocked": True,
                    "done": True,
                    "failed": False,
                    "stopped": True,
                },
                "lifecycle_commands_status": "PASS",
                "stop_failure_hook_status": "PASS",
            },
        ),
    )
    write_evidence_index(tmp_path, VERSION)

    gates = adjudicate_gate_set(tmp_path / "evidence-index.json", tmp_path)

    assert gates["agents_json_schema"]["status"] == "BLOCKED"
    assert gates["lifecycle_commands"]["status"] == "BLOCKED"


def test_live_cli_identity_drift_prevents_context_pass(tmp_path: Path) -> None:
    full_payload = {
        "cli_content_sha256": "b" * 64,
        "status": "PASS",
        "declared_native_attestation": "complete",
        "missing_fields": [],
    }
    for field in (
        "effective_setting_sources", "effective_effort",
        "effective_auto_compaction_window_tokens",
        "effective_auto_compaction_trigger_percent",
        "effective_auto_compaction_trigger_formula",
        "effective_auto_compaction_trigger_tokens", "auto_memory_mode",
        "effective_cleanup_period", "claude_md_sources", "rule_sources",
        "skill_sources", "agent_sources", "extension_sources_attested",
        "inherited_hook_sources", "subagent_mcp_hook_sources",
        "declared_mcp_servers", "tool_allow_rules", "tool_deny_rules",
        "nested_agent_cap", "nested_agent_depth", "additional_directories",
        "system_preset_attested", "system_append_attested", "content_hashes",
        "attestation_sources",
    ):
        full_payload[field] = "attested"
    full_payload.update({
        "requested_model": "claude-sonnet-5",
        "requested_effort": "low",
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_percent": None,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_model": "claude-sonnet-5",
        "effective_effort": "low",
        "effective_setting_sources": "user,project,local",
        "effective_auto_compaction_window_tokens": 342500,
        "effective_auto_compaction_trigger_percent": 80,
        "effective_auto_compaction_trigger_formula": "window * percent / 100",
        "effective_auto_compaction_trigger_tokens": 274000,
        "tool_count": 0,
        "mcp_server_count": 0,
        "plugin_disable_effective": "PASS",
        "relative_plugin_delta": 1,
        "is_using_overage": False,
        "final_marker_matched": True,
        "checkout_clean": True,
        "usage_credits_off_confirmed": True,
        "hook_error_observed": False,
        "attested_configuration": "foreground_no_tools",
        "production_equivalent_attestation": "outstanding",
        "system_preset_attested": True,
        "system_append_attested": True,
        "extension_sources_attested": True,
    })
    _write_fixture(tmp_path, "live-host.json", _fixture("live_host", _host_payload("a" * 64)))
    _write_fixture(tmp_path, "live-context.json", _fixture("live_context_attestation", full_payload))
    write_evidence_index(tmp_path, VERSION)

    gates = adjudicate_gate_set(tmp_path / "evidence-index.json", tmp_path)

    assert gates["context_attestation"]["status"] == "BLOCKED"


def test_cleanup_gate_rejects_retained_row_or_count_mismatch(tmp_path: Path) -> None:
    _write_fixture(tmp_path, "live-host.json", _fixture("live_host", _host_payload()))
    _write_fixture(
        tmp_path,
        "live-worktree-remove.json",
        _fixture(
            "live_worktree_remove",
            {
                "cli_content_sha256": "a" * 64,
                "status": "PASS",
                "audited_target_count": 1,
                "removal_attempt_count": 1,
                "removal_success_count": 1,
                "worktree_remove_hook_count": 1,
                "residual_count": 0,
                "retained_group_f_row_only_count": 1,
                "all_worktree_remove_events_matched": True,
                "all_paths_absent": True,
                "all_rows_absent": True,
                "unrelated_state_unchanged": True,
                "provider_native_remove_only": True,
                "direct_transcript_edit_count": 0,
                "fallback_git_or_filesystem_remove_count": 0,
            },
        ),
    )
    write_evidence_index(tmp_path, VERSION)

    assert adjudicate_gate_set(
        tmp_path / "evidence-index.json", tmp_path,
    )["worktree_remove_hook"]["status"] == "BLOCKED"


def test_regenerate_report_is_byte_identical_and_ignores_external_gate_file(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    shutil.copytree(COMMITTED_ROOT, fixture_root)
    report_path = tmp_path / "report.md"
    report_path.write_text(
        "# Report\n\nNarrative\n\n"
        "<!-- BEGIN GENERATED GATES -->\nold\n<!-- END GENERATED GATES -->\n\n"
        "<!-- BEGIN GENERATED SECTION 19.1 -->\nold\n"
        "<!-- END GENERATED SECTION 19.1 -->\n\n"
        "<!-- BEGIN GENERATED PHASE DECISION -->\nold\n"
        "<!-- END GENERATED PHASE DECISION -->\n",
        encoding="utf-8",
    )
    (tmp_path / "gates.json").write_text(
        '{"context_attestation":{"status":"PASS","evidence":"typed"}}',
        encoding="utf-8",
    )

    first = regenerate_report(
        evidence_index=fixture_root / "evidence-index.json",
        fixture_root=fixture_root,
        generated_at="2026-08-20T00:00:00+07:00",
        output=report_path,
    )
    first_bytes = report_path.read_bytes()
    second = regenerate_report(
        evidence_index=fixture_root / "evidence-index.json",
        fixture_root=fixture_root,
        generated_at="2026-08-20T00:00:00+07:00",
        output=report_path,
    )

    assert first == second
    assert report_path.read_bytes() == first_bytes
    assert b"Narrative" in first_bytes
    assert b"context_attestation | BLOCKED" in first_bytes


def test_cli_has_no_gate_status_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["live-evidence", "--gates", "typed.json"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_committed_public_fixtures_have_no_exact_cost_token_or_plugin_counts() -> None:
    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(COMMITTED_ROOT.glob("*.json"))
    )
    for forbidden in (
        '"total_cost_usd"', '"usage_cost_metadata"', '"cost_metadata"',
        '"plugin_count"', '"input_tokens"', '"output_tokens"',
        '"cache_creation_input_tokens"', '"cache_read_input_tokens"',
    ):
        assert forbidden not in serialized
