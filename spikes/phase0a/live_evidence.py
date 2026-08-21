from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .fixtures import (
    fixture_envelope,
    sha256_file,
    validate_fixture,
    write_evidence_index,
)
from . import report as _report


_GATE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ABSOLUTE_WINDOWS = re.compile(r"(?i)^[a-z]:[\\/]")
_PRIVATE_KEY_TOKENS = {
    "account", "cwd", "device", "email", "inode", "org", "organization", "path",
    "prompt", "raw", "session", "transcript",
}
_FORBIDDEN_PUBLIC_KEYS = {
    "plugin_count",
    "total_cost_usd",
    "usage_cost_metadata",
    "cost_metadata",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "input_tokens",
    "output_tokens",
}
_DECLARED_NATIVE_FIELDS = (
    "effective_setting_sources",
    "effective_effort",
    "effective_auto_compaction_window_tokens",
    "effective_auto_compaction_trigger_percent",
    "effective_auto_compaction_trigger_formula",
    "effective_auto_compaction_trigger_tokens",
    "auto_memory_mode",
    "effective_cleanup_period",
    "claude_md_sources",
    "rule_sources",
    "skill_sources",
    "agent_sources",
    "extension_sources_attested",
    "inherited_hook_sources",
    "subagent_mcp_hook_sources",
    "declared_mcp_servers",
    "tool_allow_rules",
    "tool_deny_rules",
    "nested_agent_cap",
    "nested_agent_depth",
    "additional_directories",
    "system_preset_attested",
    "system_append_attested",
    "content_hashes",
    "attestation_sources",
)
_GATE_NAMES = (
    "standalone_cli", "subscription_auth", "credential_precedence",
    "observer_visibility", "lifecycle_commands", "agents_json_schema",
    "context_init_subset", "context_attestation", "init_only_capability",
    "plugin_disable_effective", "strict_mcp_pre_spawn", "project_manifest",
    "windows_handle_release", "session_start_hook", "worktree_create_hook",
    "worktree_remove_hook", "stop_hook", "stop_failure_hook",
    "daemon_stop_race", "agent_view_overhead", "background_concurrency",
)
_SECTION_REQUIREMENTS = (
    ("1. Auth precedence", ("subscription_auth", "credential_precedence")),
    ("2. Standalone identity and Desktop-wrapper rejection", ("standalone_cli", "observer_visibility")),
    (
        "3. Lifecycle commands and all roster states",
        ("lifecycle_commands", "agents_json_schema", "session_start_hook"),
    ),
    (
        "4. Strict declared MCP before spawn",
        ("strict_mcp_pre_spawn", "init_only_capability"),
    ),
    ("5. Project manifest and bounded handle cleanup", ("project_manifest", "windows_handle_release")),
    ("6. WorktreeCreate and WorktreeRemove", ("worktree_create_hook", "worktree_remove_hook")),
    ("7. Background Stop and StopFailure", ("stop_hook", "stop_failure_hook")),
    ("8. Active daemon stop/respawn race", ("daemon_stop_race",)),
    ("9. Agent View overhead and concurrency", ("agent_view_overhead", "background_concurrency")),
    ("10. Declared-native context and cost", ("context_init_subset", "context_attestation", "plugin_disable_effective")),
)


def _validate_public_projection(value: Any, *, key: str = "payload") -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("private live projection key is not a string")
            normalized = re.sub(
                r"[^a-z0-9]+", "_", _CAMEL_BOUNDARY.sub("_", raw_key).casefold(),
            ).strip("_")
            tokens = set(normalized.split("_"))
            aggregate_boolean = (
                type(item) is bool
                and normalized.endswith(("_match", "_present", "_equal", "_stable"))
            )
            if tokens & _PRIVATE_KEY_TOKENS and not aggregate_boolean:
                raise ValueError(f"private path/session field is forbidden: {raw_key}")
            _validate_public_projection(item, key=normalized)
    elif isinstance(value, list):
        for item in value:
            _validate_public_projection(item, key=key)
    elif isinstance(value, str):
        if value.startswith(("/", "\\\\")) or _ABSOLUTE_WINDOWS.match(value):
            raise ValueError(f"private absolute path is forbidden in {key}")


def _validate_publication_fixture(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"publication fixture contains exact private metadata: {key}")
            _validate_publication_fixture(item)
    elif isinstance(value, list):
        for item in value:
            _validate_publication_fixture(item)


def live_fixture(
    *,
    gate_id: str,
    observed_cli_version: str,
    cli_sha256: str,
    source_sha256: str,
    payload: dict[str, Any],
    observed: list[str],
    missing: list[str],
) -> dict[str, Any]:
    """Build a public live-evidence envelope from an already sanitized projection."""
    if not _GATE_ID.fullmatch(gate_id):
        raise ValueError("gate_id must be a bounded public category")
    if not _SHA256.fullmatch(cli_sha256):
        raise ValueError("cli_sha256 must be a lowercase SHA-256")
    if not _SHA256.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256")
    if not isinstance(payload, dict):
        raise ValueError("live fixture payload must be an object")
    if {"gate", "cli_content_sha256"} & set(payload):
        raise ValueError("live fixture payload contains reserved metadata")
    _validate_public_projection(payload)
    _validate_publication_fixture(payload)
    return fixture_envelope(
        kind="live_host",
        observed_cli_version=observed_cli_version,
        source_kind="bounded_live_projection",
        source_sha256=source_sha256,
        payload={"gate": gate_id, "cli_content_sha256": cli_sha256, **payload},
        observed=observed,
        missing=missing,
    )


def rebuild_live_evidence_index(
    fixture_root: str | Path,
    observed_cli_version: str,
) -> dict[str, Any]:
    """Validate every committed JSON fixture and rebuild the deterministic index."""
    return write_evidence_index(fixture_root, observed_cli_version)


def _read_public_json(path: Path, label: str) -> dict[str, Any]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or metadata.st_size > 8 * 1024 * 1024
    ):
        raise ValueError(f"{label} must be a bounded direct file")
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def load_indexed_fixtures(
    evidence_index: str | Path,
    fixture_root: str | Path,
) -> dict[str, dict[str, Any]]:
    supplied_root = Path(fixture_root).absolute()
    try:
        root_metadata = supplied_root.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("fixture root is unavailable") from exc
    if (
        supplied_root.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or getattr(root_metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ValueError("fixture root must be a direct directory")
    root = supplied_root.resolve(strict=True)

    def same_path(left: Path, right: Path) -> bool:
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
            os.path.normpath(str(right))
        )

    supplied_index = Path(evidence_index).absolute()
    expected_index = root / "evidence-index.json"
    if not same_path(supplied_index, expected_index):
        raise ValueError("evidence index must be the fixture root's canonical index")
    index = _read_public_json(supplied_index, "evidence index")
    index_path = supplied_index.resolve(strict=True)
    if not same_path(index_path, expected_index):
        raise ValueError("evidence index escaped the fixture root")
    validate_fixture(index)
    if index.get("kind") != "evidence_index":
        raise ValueError("evidence index fixture kind is invalid")
    entries = index.get("payload", {}).get("fixtures")
    if not isinstance(entries, dict) or not entries or len(entries) > 128:
        raise ValueError("evidence index entries are invalid")
    actual_names = {
        path.name for path in root.iterdir()
        if path.suffix.casefold() == ".json" and path.name != index_path.name
    }
    if set(entries) != actual_names:
        raise ValueError("fixture root and evidence index disagree")
    expected_source = hashlib.sha256(json.dumps(
        entries, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    if index.get("source") != {
        "kind": "committed_fixture_set", "sha256": expected_source,
    }:
        raise ValueError("evidence index source digest is invalid")
    version = index.get("observed_cli_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("evidence index CLI version is invalid")
    loaded: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(entries.items()):
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}\.json", name)
            or "/" in name
            or "\\" in name
            or not isinstance(entry, dict)
            or set(entry) != {"sha256", "kind"}
            or _SHA256.fullmatch(str(entry.get("sha256", ""))) is None
            or not isinstance(entry.get("kind"), str)
            or not entry["kind"]
        ):
            raise ValueError("evidence index entry is invalid")
        candidate = root / name
        fixture = _read_public_json(candidate, f"fixture {name}")
        path = candidate.resolve(strict=True)
        if not same_path(path.parent, root) or not same_path(path, candidate):
            raise ValueError("indexed fixture escaped the fixture root")
        validate_fixture(fixture)
        _validate_publication_fixture(fixture)
        if (
            sha256_file(candidate) != entry["sha256"]
            or fixture.get("kind") != entry["kind"]
            or fixture.get("observed_cli_version") != version
        ):
            raise ValueError(f"indexed fixture identity mismatch: {name}")
        loaded[name] = fixture
    return loaded


def _gate(status: str, evidence: str) -> dict[str, str]:
    if status not in {"PASS", "FAIL", "UNKNOWN", "BLOCKED"}:
        raise ValueError("invalid adjudicated gate status")
    return {"status": status, "evidence": evidence}


def _fixture(
    fixtures: dict[str, dict[str, Any]],
    name: str,
    kind: str,
) -> dict[str, Any] | None:
    value = fixtures.get(name)
    if value is None:
        return None
    if value.get("kind") != kind:
        raise ValueError(f"fixture {name} has an unexpected kind")
    return value


def _live_cli_digest(fixture: dict[str, Any] | None) -> str | None:
    if fixture is None:
        return None
    payload = fixture.get("payload")
    value = payload.get("cli_content_sha256") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def _same_live_cli(
    fixture: dict[str, Any] | None,
    authoritative_digest: str | None,
) -> bool:
    return authoritative_digest is not None and _live_cli_digest(fixture) == authoritative_digest


def _auth_passes(payload: Any) -> bool:
    auth = payload.get("auth") if isinstance(payload, dict) else None
    return isinstance(auth, dict) and auth == {
        "api_provider": "firstParty",
        "auth_method": "claude.ai",
        "logged_in": True,
    }


def _strict_mcp_passes(payload: Any) -> bool:
    return isinstance(payload, dict) and all((
        payload.get("declared_server_count") == 0,
        payload.get("strict_marker_spawned") is False,
        payload.get("control_marker_spawned") is True,
        payload.get("strict_exit_success") is True,
        payload.get("control_exit_success") is True,
    ))


def _context_init_subset_passes(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("init_subset_status") == "PASS":
        return all((
            payload.get("final_marker_matched") is True,
            payload.get("checkout_clean") is True,
            payload.get("mcp_server_count") == 0,
            payload.get("tool_count") == 0,
            payload.get("is_using_overage") is False,
            payload.get("hook_error_observed") is False,
        ))
    init = payload.get("init")
    result = payload.get("final_result")
    rates = payload.get("rate_limit_advisory")
    return bool(
        isinstance(init, dict)
        and init.get("cwd_present") is True
        and init.get("mcp_servers") == []
        and isinstance(init.get("tool_count"), int)
        and not isinstance(init.get("tool_count"), bool)
        and init.get("tool_count") >= 0
        and isinstance(init.get("forbidden_surface_presence"), dict)
        and all(value is False for value in init["forbidden_surface_presence"].values())
        and isinstance(result, dict)
        and result.get("is_error") is False
        and type(payload.get("cost_metadata_present")) is bool
        and payload.get("plugin_disable_effective") == "BLOCKED"
        and payload.get("relative_plugin_delta") is None
        and isinstance(rates, list)
        and all(
            isinstance(rate, dict) and rate.get("is_using_overage") is False
            for rate in rates
        )
    )


def _declared_native_shapes_pass(payload: dict[str, Any]) -> bool:
    list_fields = (
        "claude_md_sources", "rule_sources", "skill_sources", "agent_sources",
        "inherited_hook_sources", "subagent_mcp_hook_sources",
        "declared_mcp_servers", "tool_allow_rules", "tool_deny_rules",
        "additional_directories",
    )
    if any(not isinstance(payload.get(field), list) for field in list_fields):
        return False
    if payload.get("nested_agent_cap") != 4 or payload.get("nested_agent_depth") != 1:
        return False
    if payload.get("extension_sources_attested") is not True:
        return False
    if not isinstance(payload.get("auto_memory_mode"), (str, bool)):
        return False
    cleanup = payload.get("effective_cleanup_period")
    if (
        isinstance(cleanup, bool)
        or not isinstance(cleanup, (int, float, str))
        or isinstance(cleanup, str) and not cleanup.strip()
    ):
        return False
    if payload.get("system_preset_attested") is not True:
        return False
    if payload.get("system_append_attested") is not True:
        return False
    if not isinstance(payload.get("content_hashes"), (dict, list)):
        return False
    if not isinstance(payload.get("attestation_sources"), (dict, list)):
        return False
    return True


def _full_context_passes(payload: Any, missing: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    window = payload.get("effective_auto_compaction_window_tokens")
    percent = payload.get("effective_auto_compaction_trigger_percent")
    formula = payload.get("effective_auto_compaction_trigger_formula")
    setting_sources = payload.get("effective_setting_sources")
    normalized_sources = (
        tuple(setting_sources) if isinstance(setting_sources, list) else setting_sources
    )
    if (
        isinstance(window, bool)
        or not isinstance(window, int)
        or window <= 0
        or isinstance(percent, bool)
        or not isinstance(percent, (int, float))
        or not 0 < float(percent) <= 100
        or not isinstance(formula, str)
        or not formula.strip()
    ):
        return False
    calculated_trigger = window * float(percent) / 100
    return all((
        payload.get("status") == "PASS",
        payload.get("declared_native_attestation") == "complete",
        payload.get("missing_fields") == [],
        missing == [],
        all(field in payload and payload[field] is not None for field in _DECLARED_NATIVE_FIELDS),
        _declared_native_shapes_pass(payload),
        payload.get("requested_model") == "claude-sonnet-5",
        payload.get("requested_effort") == "low",
        payload.get("requested_setting_sources") == "user,project,local",
        payload.get("requested_auto_compaction_window_tokens") == 274000,
        payload.get("requested_auto_compaction_trigger_percent") is None,
        payload.get("requested_auto_compaction_trigger_tokens") == 274000,
        payload.get("effective_model") == "claude-sonnet-5",
        payload.get("effective_effort") == "low",
        normalized_sources in {"user,project,local", ("user", "project", "local")},
        payload.get("effective_auto_compaction_trigger_tokens") == 274000,
        abs(calculated_trigger - 274000) < 1e-9,
        payload.get("tool_count") == 0,
        payload.get("mcp_server_count") == 0,
        payload.get("plugin_disable_effective") == "PASS",
        payload.get("relative_plugin_delta") == 1,
        payload.get("is_using_overage") is False,
        payload.get("final_marker_matched") is True,
        payload.get("checkout_clean") is True,
        payload.get("usage_credits_off_confirmed") is True,
        payload.get("hook_error_observed") is False,
        payload.get("attested_configuration") == "foreground_no_tools",
        payload.get("production_equivalent_attestation") == "outstanding",
    ))


def adjudicate_gate_set(
    evidence_index: str | Path,
    fixture_root: str | Path,
) -> dict[str, dict[str, str]]:
    fixtures = load_indexed_fixtures(evidence_index, fixture_root)
    gates = {
        name: _gate("BLOCKED", "Required committed sanitized evidence is missing.")
        for name in _GATE_NAMES
    }

    auth_fixture = _fixture(fixtures, "auth-status.json", "auth_status")
    host = _fixture(fixtures, "live-host.json", "live_host")
    host_payload = host.get("payload", {}) if host else {}
    host_cli_digest = _live_cli_digest(host)
    auth_payload = (
        host_payload if _auth_passes(host_payload)
        else auth_fixture.get("payload", {}) if auth_fixture else {}
    )
    if _auth_passes(auth_payload):
        gates["subscription_auth"] = _gate(
            "PASS", "Indexed subscription auth evidence is first-party claude.ai.",
        )

    wrapper = host_payload.get("wrapper_rejection") if isinstance(host_payload, dict) else None
    if host and host_cli_digest is not None and all((
        host_payload.get("status") == "ready",
        host_payload.get("identity_stable") is True,
        isinstance(host_payload.get("cli_content_sha256"), str),
        _SHA256.fullmatch(str(host_payload.get("cli_content_sha256", ""))) is not None,
        isinstance(wrapper, dict),
        wrapper.get("rejection_evidence_complete") is True,
        wrapper.get("desktop_runtime_accepted") is False,
    )):
        gates["standalone_cli"] = _gate(
            "PASS", "live-host.json binds the standalone identity and rejects the Desktop runtime.",
        )
    else:
        gates["standalone_cli"] = _gate(
            "UNKNOWN", "No indexed live host fixture proves standalone identity and wrapper rejection.",
        )

    overrides = host_payload.get("credential_override_presence") if isinstance(host_payload, dict) else None
    if host and host_cli_digest is not None and isinstance(overrides, dict) and overrides and all(
        value is False for value in overrides.values()
    ) and host_payload.get("credential_override_rejection_observed") is True:
        gates["credential_precedence"] = _gate(
            "PASS", "live-host.json records the approved real override-rejection path.",
        )

    manifest = host_payload.get("project_manifest") if isinstance(host_payload, dict) else None
    if host and host_cli_digest is not None and isinstance(manifest, dict) and all((
        manifest.get("blocked_count") == 0,
        isinstance(manifest.get("manifest_digest"), str),
        _SHA256.fullmatch(str(manifest.get("manifest_digest", ""))) is not None,
    )):
        gates["project_manifest"] = _gate(
            "PASS", "live-host.json records a current trusted project manifest.",
        )

    handles = _fixture(fixtures, "live-windows-handles.json", "live_windows_handles")
    handle_payload = handles.get("payload", {}) if handles else {}
    branches = handle_payload.get("branches") if isinstance(handle_payload, dict) else None
    required_branches = {"success", "timeout", "cancelled", "child_failure", "start_failure"}
    if handles and _same_live_cli(handles, host_cli_digest) and handle_payload.get("status") == "pass" and isinstance(branches, dict) and set(branches) == required_branches and all(
        isinstance(value, dict)
        and value.get("held_save_denied") is True
        and value.get("save_after_release") is True
        for value in branches.values()
    ):
        gates["windows_handle_release"] = _gate(
            "PASS", "live-windows-handles.json covers all five required release branches.",
        )

    init_fixture = _fixture(fixtures, "live-init-strict-mcp.json", "live_init_strict_mcp")
    init_payload = init_fixture.get("payload", {}) if init_fixture else {}
    for gate_name, field in (
        ("init_only_capability", "init_only_capability"),
        ("observer_visibility", "observer_visibility"),
        ("strict_mcp_pre_spawn", "strict_mcp_pre_spawn"),
    ):
        if init_fixture and _same_live_cli(init_fixture, host_cli_digest) and init_payload.get("status") == "pass" and init_payload.get(field) is True:
            gates[gate_name] = _gate("PASS", f"live-init-strict-mcp.json proves {gate_name}.")
    if gates["observer_visibility"]["status"] != "PASS":
        gates["observer_visibility"] = _gate(
            "UNKNOWN", "No indexed live observer fixture proves equal standalone identity.",
        )
    if gates["strict_mcp_pre_spawn"]["status"] != "PASS":
        strict = _fixture(fixtures, "strict-mcp-control.json", "strict_mcp_control")
        if strict and _strict_mcp_passes(strict.get("payload")):
            gates["strict_mcp_pre_spawn"] = _gate(
                "PASS", "strict-mcp-control.json contains the indexed strict/control differential.",
            )

    live_context = _fixture(fixtures, "live-context.json", "live_context_attestation")
    context_fixture = (
        live_context
        if _same_live_cli(live_context, host_cli_digest)
        else _fixture(fixtures, "context-attestation.json", "context_attestation")
    )
    context_payload = context_fixture.get("payload", {}) if context_fixture else {}
    if context_fixture and _context_init_subset_passes(context_payload):
        gates["context_init_subset"] = _gate(
            "PASS", f"{('live-context.json' if live_context else 'context-attestation.json')} validates the bounded init subset.",
        )
    if live_context and _same_live_cli(live_context, host_cli_digest) and _full_context_passes(
        context_payload, live_context.get("coverage", {}).get("missing"),
    ):
        gates["context_attestation"] = _gate(
            "PASS", "live-context.json attests every declared-native field and exact trigger 274000.",
        )
    if live_context and _same_live_cli(live_context, host_cli_digest) and all((
        context_payload.get("plugin_disable_effective") == "PASS",
        context_payload.get("relative_plugin_delta") == 1,
    )):
        gates["plugin_disable_effective"] = _gate(
            "PASS", "live-context.json contains the positive plugin-disable differential.",
        )

    lifecycle = _fixture(
        fixtures, "live-background-lifecycle.json", "live_background_lifecycle",
    )
    lifecycle_payload = lifecycle.get("payload", {}) if lifecycle else {}
    if lifecycle and _same_live_cli(lifecycle, host_cli_digest) and lifecycle_payload.get("status") == "PASS":
        if lifecycle_payload.get("session_start_observed") is True:
            gates["session_start_hook"] = _gate(
                "PASS", "live-background-lifecycle.json records SessionStart delivery.",
            )
        if all((
            lifecycle_payload.get("worktree_create_observed") is True,
            lifecycle_payload.get("handoff_equality_observed") is True,
            lifecycle_payload.get("handoff_precedes_first_write") is True,
            lifecycle_payload.get("common_dir_equality_observed") is True,
        )):
            gates["worktree_create_hook"] = _gate(
                "PASS", "live-background-lifecycle.json proves the WorktreeCreate handoff transaction.",
            )
        if lifecycle_payload.get("stop_hook_observed") is True:
            gates["stop_hook"] = _gate(
                "PASS", "live-background-lifecycle.json records a successful Stop hook.",
            )
        if all((
            lifecycle_payload.get("active_stop_stable_observation_count") == 2,
            lifecycle_payload.get("respawn_identity_equal") is True,
            lifecycle_payload.get("respawn_working_observed") is True,
            lifecycle_payload.get("final_state_category") == "done",
            lifecycle_payload.get("stop_hook_observed") is True,
            lifecycle_payload.get("proof_only_change") is True,
            lifecycle_payload.get("proof_bytes_matched") is True,
            lifecycle_payload.get("proof_delete_count") == 1,
            lifecycle_payload.get("final_checkout_clean") is True,
        )):
            gates["daemon_stop_race"] = _gate(
                "PASS", "live-background-lifecycle.json proves the bounded stop/respawn race.",
            )

    matrix = _fixture(fixtures, "live-background-matrix.json", "live_background_matrix")
    matrix_payload = matrix.get("payload", {}) if matrix else {}
    state_presence = matrix_payload.get("state_presence") if isinstance(matrix_payload, dict) else None
    matrix_identity_matches = _same_live_cli(matrix, host_cli_digest)
    if matrix and matrix_identity_matches and isinstance(state_presence, dict) and set(state_presence) == {
        "working", "needs_input_or_blocked", "done", "failed", "stopped",
    } and all(value is True for value in state_presence.values()):
        gates["agents_json_schema"] = _gate(
            "PASS", "live-background-matrix.json covers every required roster state.",
        )
    if matrix and matrix_identity_matches and all((
        matrix_payload.get("lifecycle_commands_status") == "PASS",
        gates["agents_json_schema"]["status"] == "PASS",
        matrix_payload.get("needs_input_observed") is True,
        matrix_payload.get("denied_write_observed") is True,
        matrix_payload.get("attach_observed") is True,
        matrix_payload.get("attach_same_session") is True,
        matrix_payload.get("needs_input_checkout_clean") is True,
        matrix_payload.get("needs_input_stable_stop_observation_count") == 2,
        matrix_payload.get("needs_input_stop_hook_observed") is True,
    )):
        gates["lifecycle_commands"] = _gate(
            "PASS", "live-background-matrix.json proves the documented lifecycle command set.",
        )
    if matrix and matrix_identity_matches and matrix_payload.get("stop_failure_hook_status") == "PASS":
        gates["stop_failure_hook"] = _gate(
            "PASS", "live-background-matrix.json records a naturally observed StopFailure hook.",
        )
    if matrix and matrix_identity_matches and all((
        matrix_payload.get("simultaneous_active_observed") is True,
        matrix_payload.get("observed_floor") == 2,
        matrix_payload.get("policy_cap") == 2,
        matrix_payload.get("concurrency_stable_stop_observation_count") == 2,
        matrix_payload.get("concurrency_stop_hook_observed") is True,
        matrix_payload.get("concurrency_checkout_clean") is True,
    )):
        gates["background_concurrency"] = _gate(
            "PASS", "live-background-matrix.json proves two simultaneous approved rows.",
        )
    if matrix and matrix_identity_matches and matrix_payload.get("agent_view_overhead") == "PASS" and matrix_payload.get("agent_view_source_kind") == "official_per_session":
        gates["agent_view_overhead"] = _gate(
            "PASS", "live-background-matrix.json contains official per-session Agent View accounting.",
        )
    else:
        gates["agent_view_overhead"] = _gate(
            "UNKNOWN", "No indexed official per-session Agent View accounting surface is available.",
        )

    removal = _fixture(fixtures, "live-worktree-remove.json", "live_worktree_remove")
    removal_payload = removal.get("payload", {}) if removal else {}
    removal_missing = removal.get("coverage", {}).get("missing") if removal else None
    if removal and _same_live_cli(removal, host_cli_digest) and all((
        removal_payload.get("status") == "PASS",
        removal_payload.get("audited_target_count", 0) > 0,
        removal_missing == [],
        removal_payload.get("removal_attempt_count") == removal_payload.get("audited_target_count"),
        removal_payload.get("removal_success_count") == removal_payload.get("audited_target_count"),
        removal_payload.get("worktree_remove_hook_count") == removal_payload.get("audited_target_count"),
        removal_payload.get("residual_count") == 0,
        removal_payload.get("retained_group_f_row_only_count") == 0,
        removal_payload.get("all_worktree_remove_events_matched") is True,
        removal_payload.get("all_paths_absent") is True,
        removal_payload.get("all_rows_absent") is True,
        removal_payload.get("unrelated_state_unchanged") is True,
        removal_payload.get("provider_native_remove_only") is True,
        removal_payload.get("direct_transcript_edit_count") == 0,
        removal_payload.get("fallback_git_or_filesystem_remove_count") == 0,
    )):
        gates["worktree_remove_hook"] = _gate(
            "PASS", "live-worktree-remove.json proves exact provider-native disposable release.",
        )

    if set(gates) != set(_GATE_NAMES):
        raise AssertionError("adjudicator did not produce the exact gate set")
    return gates


def section_19_1_rows(
    gates: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    if set(gates) != set(_GATE_NAMES):
        raise ValueError("section 19.1 requires the exact adjudicated gate set")
    mapped_gates = tuple(
        gate for _requirement, dependencies in _SECTION_REQUIREMENTS
        for gate in dependencies
    )
    if set(mapped_gates) != set(_GATE_NAMES) or len(mapped_gates) != len(set(mapped_gates)):
        raise AssertionError("section 19.1 must map every gate exactly once")
    rows: list[dict[str, str]] = []
    for requirement, dependencies in _SECTION_REQUIREMENTS:
        statuses = [gates[name]["status"] for name in dependencies]
        if all(status == "PASS" for status in statuses):
            outcome = "PASS"
        elif any(status in {"FAIL", "BLOCKED"} for status in statuses):
            outcome = "BLOCKED"
        else:
            outcome = "UNKNOWN"
        rows.append({
            "requirement": requirement,
            "outcome": outcome,
            "evidence": ", ".join(
                f"{name}={gates[name]['status']}" for name in dependencies
            ),
        })
    return rows


def phase_decision(gates: dict[str, dict[str, str]]) -> dict[str, Any]:
    rows = section_19_1_rows(gates)
    accepted = all(row["outcome"] == "PASS" for row in rows)
    return {
        "phase_0a_accepted": accepted,
        "phase_0b_may_begin": accepted,
        "status": "PASS" if accepted else "BLOCKED",
        "nonpass_requirements": [
            row["requirement"] for row in rows if row["outcome"] != "PASS"
        ],
    }


def regenerate_report(
    *,
    evidence_index: str | Path,
    fixture_root: str | Path,
    generated_at: str,
    output: str | Path,
) -> dict[str, Any]:
    gates = adjudicate_gate_set(evidence_index, fixture_root)
    rows = section_19_1_rows(gates)
    decision = phase_decision(gates)
    _report._update_adjudicated_report(
        output,
        gates=gates,
        section_rows=rows,
        decision=decision,
        generated_at=generated_at,
    )
    return {
        "gate_count": len(gates),
        "section_requirement_count": len(rows),
        "decision": decision["status"],
        "report_sha256": sha256_file(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    regenerate = subparsers.add_parser("regenerate-report")
    regenerate.add_argument("--evidence-index", type=Path, required=True)
    regenerate.add_argument("--fixture-root", type=Path, required=True)
    regenerate.add_argument("--generated-at", required=True)
    regenerate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command != "regenerate-report":
        parser.error("unknown command")
    result = regenerate_report(
        evidence_index=args.evidence_index,
        fixture_root=args.fixture_root,
        generated_at=args.generated_at,
        output=args.output,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
