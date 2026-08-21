from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from spikes.phase0a import live_host
from spikes.phase0a.core import ProbeResult
from spikes.phase0a.live_host import (
    _run_windows_handle_branch,
    collect_cli_capabilities,
    build_inventory_projection,
    classify_inventory,
    inspect_runtime_ownership,
    collect_host_evidence,
    load_bound_host_capabilities,
    load_bound_host_identity,
    project_manifest_evidence,
    run_windows_handle_matrix,
    write_bound_host_identity,
)
from spikes.phase0a.manifest import scan_project


def _result(name: str, stdout: str, *, exit_code: int = 0) -> ProbeResult:
    return ProbeResult(
        name=name,
        argv=(),
        cwd=None,
        started_at="2026-08-20T00:00:00+00:00",
        duration_ms=0,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        timed_out=False,
    )


class FakeCli:
    def __init__(self, path: Path, *, logged_in: bool = True) -> None:
        self.path = path
        self.logged_in = logged_in
        self.calls: list[str] = []
        self.argv: list[tuple[str, ...]] = []

    def run(self, name, argv, **_kwargs):
        self.calls.append(name)
        self.argv.append(tuple(argv))
        outputs = {
            "version": "2.1.224 (Claude Code)\n",
            "auth_status": json.dumps({
                "loggedIn": self.logged_in,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
            }),
            "agents_json": json.dumps([{
                "id": "private-row",
                "sessionId": "private-session",
                "name": "private-name",
                "cwd": "C:\\private",
                "state": "working",
            }]),
        }
        return _result(name, outputs[name])


def test_missing_cli_returns_install_required_without_spawn(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    result = collect_host_evidence(
        tmp_path / "live", tmp_path / "missing.exe", {}, project_root=tmp_path,
        runner=lambda _name, argv, **_kwargs: calls.append(tuple(argv)),
    )

    assert result == {"status": "INSTALL_REQUIRED", "next_action": "recheck"}
    assert calls == []


def test_version_process_start_failure_is_incompatible(tmp_path: Path) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"not executable")

    result = collect_host_evidence(
        tmp_path / "live",
        cli_path,
        {},
        project_root=tmp_path,
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("start failed")),
    )

    assert result == {"status": "incompatible", "reason": "version_probe_failed"}


def test_logged_out_stops_before_roster_query(tmp_path: Path) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")
    fake_cli = FakeCli(cli_path, logged_in=False)

    result = collect_host_evidence(
        tmp_path / "live", cli_path, {}, project_root=tmp_path, runner=fake_cli.run,
    )

    assert result["status"] == "AUTH_REQUIRED"
    assert fake_cli.calls == ["version", "auth_status"]


def test_ready_preflight_uses_only_exact_read_only_argv(tmp_path: Path) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")
    fake_cli = FakeCli(cli_path)

    collect_host_evidence(
        tmp_path / "live", cli_path, {}, project_root=tmp_path, runner=fake_cli.run,
    )

    expected_cli = str(cli_path.resolve())
    assert fake_cli.argv == [
        (expected_cli, "--version"),
        (expected_cli, "auth", "status"),
        (expected_cli, "agents", "--json", "--all"),
    ]


def test_capability_probe_uses_only_documented_help_safe_surfaces(tmp_path: Path) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")
    calls: list[tuple[str, ...]] = []

    def runner(name, argv, **_kwargs):
        calls.append(tuple(argv))
        command = argv[1] if len(argv) == 3 else ""
        return _result(name, f"Usage: claude {command}\n")

    result = collect_cli_capabilities(cli_path, {}, runner=runner)

    expected = str(cli_path.resolve())
    assert calls == [
        (expected, "--help"),
        (expected, "stop", "--help"),
        (expected, "respawn", "--help"),
        (expected, "attach", "--help"),
        (expected, "rm", "--help"),
    ]
    assert result == {
        "top_level_help_recognized": True,
        "tools_empty_documented": False,
        "prompt_suggestions_false_documented": False,
        "stop_help_recognized": True,
        "respawn_help_recognized": True,
        "attach_help_recognized": True,
        "rm_help_recognized": True,
    }
    assert not any("--bg" in argv or "--worktree" in argv for argv in calls)


def test_capability_probe_recognizes_observed_help_syntax_without_prose(
    tmp_path: Path,
) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")

    def runner(name, argv, **_kwargs):
        if argv[1:] == ["--help"]:
            return _result(name, """Usage: claude [options]
  --tools <tools...>
  --prompt-suggestions [value]
""")
        command = argv[1]
        return _result(name, f"Usage: claude {command}\n")

    result = collect_cli_capabilities(cli_path, {}, runner=runner)

    assert result["tools_empty_documented"] is True
    assert result["prompt_suggestions_false_documented"] is True


def test_capability_probe_retains_legacy_boolean_prompt_syntax(
    tmp_path: Path,
) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")

    def runner(name, argv, **_kwargs):
        if argv[1:] == ["--help"]:
            return _result(name, """Usage: claude [options]
  --tools <tools...>
  --prompt-suggestions <boolean>
""")
        command = argv[1]
        return _result(name, f"Usage: claude {command}\n")

    result = collect_cli_capabilities(cli_path, {}, runner=runner)

    assert result["tools_empty_documented"] is True
    assert result["prompt_suggestions_false_documented"] is True


def test_capability_probe_rejects_generic_help_for_unknown_subcommands(tmp_path: Path) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")

    result = collect_cli_capabilities(
        cli_path,
        {},
        runner=lambda name, _argv, **_kwargs: _result(name, "Usage: claude [options]\n"),
    )

    assert result["top_level_help_recognized"] is True
    assert result["stop_help_recognized"] is False
    assert result["respawn_help_recognized"] is False
    assert result["attach_help_recognized"] is False
    assert result["rm_help_recognized"] is False


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({}, "expected_clean"),
        ({"plan_owned_count": 2, "residual_count": 2}, "plan_owned_residual"),
        ({"unknown_count": 1, "residual_count": 1}, "user_or_unknown_residual"),
        ({"plan_owned_count": 1, "residual_count": 2}, "recovery_required"),
    ],
)
def test_inventory_classification_fails_closed(counts, expected) -> None:
    assert classify_inventory(**counts) == expected


def test_runtime_ownership_projection_is_path_free(tmp_path: Path) -> None:
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    wrapper = home / "bin" / "claude.cmd"
    cache = appdata / "Claude" / "claude-code" / "2.1.224"
    standalone = home / ".local" / "bin" / "claude.exe"
    wrapper.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    standalone.parent.mkdir(parents=True)
    wrapper.write_text("@%APPDATA%\\Claude\\claude-code\\2.1.224\\claude.exe %*", encoding="utf-8")
    (cache / "claude.exe").write_bytes(b"desktop-owned-cache")
    standalone.write_bytes(b"standalone")

    result = inspect_runtime_ownership(
        standalone,
        {"USERPROFILE": str(home), "APPDATA": str(appdata)},
    )

    assert result["wrapper_present"] is True
    assert result["wrapper_distinct_from_standalone"] is True
    assert result["versioned_cache_dependency_present"] is True
    assert result["selected_cache_target_observed"] is True
    assert result["rejection_evidence_complete"] is True
    assert result["desktop_runtime_accepted"] is False
    assert str(tmp_path) not in json.dumps(result)


def test_host_commands_run_while_cli_lease_is_open(tmp_path: Path, monkeypatch) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")
    active = {"value": False}

    class Lease:
        def __enter__(self):
            active["value"] = True
            return self

        def verify_init_ack(self):
            assert active["value"] is True

        def __exit__(self, *_args):
            active["value"] = False

    monkeypatch.setattr(live_host.BoundExecutableManifest, "lease", lambda _self: Lease())
    fake_cli = FakeCli(cli_path)

    def runner(name, argv, **kwargs):
        assert active["value"] is True
        return fake_cli.run(name, argv, **kwargs)

    assert collect_host_evidence(
        tmp_path / "live", cli_path, {}, project_root=tmp_path, runner=runner,
    )["status"] == "ready"
    assert active["value"] is False


def test_plan_owned_inventory_count_requires_valid_provenance(tmp_path: Path) -> None:
    ownership = tmp_path / "ownership.json"
    ownership.write_text(json.dumps({
        "schema_version": 1,
        "records": [
            {"kind": "row", "approval_digest": "a" * 64, "target_fingerprint": "b" * 64},
            {"kind": "worktree", "approval_digest": "c" * 64, "target_fingerprint": "d" * 64},
        ],
    }), encoding="utf-8")

    assert live_host.load_plan_owned_count(ownership) == 2

    ownership.write_text('{"schema_version":1,"records":[{"kind":"row"}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        live_host.load_plan_owned_count(ownership)


def test_inventory_projection_never_adopts_unowned_residuals() -> None:
    clean = build_inventory_projection(
        {"status": "ready", "roster": {"row_count": 0}},
        matching_process_count=0,
        live_worktree_count=0,
        plan_owned_count=0,
    )
    unknown = build_inventory_projection(
        {"status": "ready", "roster": {"row_count": 1}},
        matching_process_count=1,
        live_worktree_count=1,
        plan_owned_count=0,
    )

    assert clean["classification"] == "expected_clean"
    assert unknown == {
        "classification": "user_or_unknown_residual",
        "roster_row_count": 1,
        "matching_process_count": 1,
        "live_worktree_count": 1,
        "plan_owned_count": 0,
    }


def test_override_presence_stops_before_auth(tmp_path: Path) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")
    fake_cli = FakeCli(cli_path)

    result = collect_host_evidence(
        tmp_path / "live", cli_path, {"ANTHROPIC_API_KEY": "present"},
        project_root=tmp_path, runner=fake_cli.run,
    )

    assert result["status"] == "credential_override"
    assert fake_cli.calls == ["version"]


def test_public_host_evidence_has_no_path_or_identity_value(tmp_path: Path) -> None:
    cli_path = tmp_path / "claude.exe"
    cli_path.write_bytes(b"fake standalone cli")
    fake_cli = FakeCli(cli_path)

    evidence = collect_host_evidence(
        tmp_path / "live", cli_path, {}, project_root=tmp_path, runner=fake_cli.run,
    )
    serialized = json.dumps(evidence)

    assert evidence["status"] == "ready"
    assert "canonical_path" not in serialized
    assert str(cli_path) not in serialized
    assert "private-row" not in serialized
    assert "private-session" not in serialized
    assert "email" not in serialized.casefold()
    assert "org" not in serialized.casefold()
    assert "session" not in serialized.casefold()


def test_current_manifest_public_projection_omits_paths(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("safe instructions", encoding="utf-8")
    manifest = scan_project(tmp_path)

    evidence = project_manifest_evidence(manifest)

    assert set(evidence) == {
        "repository_kind",
        "instruction_count",
        "hook_target_count",
        "external_count",
        "blocked_count",
        "manifest_digest",
    }
    assert str(tmp_path) not in json.dumps(evidence)


def test_windows_handle_matrix_reports_only_branch_aggregates(tmp_path: Path) -> None:
    calls: list[str] = []

    result = run_windows_handle_matrix(
        tmp_path,
        branch_runner=lambda branch: calls.append(branch) or {
            "held_save_denied": True,
            "branch_observed": True,
            "release_latency_ms": 1,
            "save_after_release": True,
        },
    )

    assert calls == ["success", "timeout", "cancelled", "child_failure", "start_failure"]
    assert result["status"] == "pass"
    assert result["editor_application_canary"] == "not_run"
    assert result["branches"] == {
        "cancelled": {"held_save_denied": True, "save_after_release": True},
        "child_failure": {"held_save_denied": True, "save_after_release": True},
        "start_failure": {"held_save_denied": True, "save_after_release": True},
        "success": {"held_save_denied": True, "save_after_release": True},
        "timeout": {"held_save_denied": True, "save_after_release": True},
    }


def test_windows_handle_matrix_blocks_missing_branch_observation(tmp_path: Path) -> None:
    result = run_windows_handle_matrix(
        tmp_path,
        branch_runner=lambda _branch: {
            "held_save_denied": True,
            "save_after_release": True,
            "release_latency_ms": 1,
        },
    )

    assert result["status"] == "blocked"


def test_bound_host_identity_round_trips_and_rejects_cli_drift(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone")
    evidence = {
        "status": "ready",
        "observed_cli_version": "2.1.224",
        "cli_content_sha256": hashlib.sha256(b"standalone").hexdigest(),
        "identity_stable": True,
    }

    path = write_bound_host_identity(
        tmp_path / "host",
        cli,
        evidence,
        capabilities={
            "tools_empty_documented": True,
            "prompt_suggestions_false_documented": True,
            "stop_help_recognized": True,
            "respawn_help_recognized": True,
            "attach_help_recognized": False,
            "rm_help_recognized": False,
        },
    )
    identity = load_bound_host_identity(path, cli)

    assert identity.version == "2.1.224"
    assert identity.sha256 == evidence["cli_content_sha256"]
    assert load_bound_host_capabilities(path) == {
        "tools_empty_documented": True,
        "prompt_suggestions_false_documented": True,
        "stop_help_recognized": True,
        "respawn_help_recognized": True,
        "attach_help_recognized": False,
        "rm_help_recognized": False,
    }
    cli.write_bytes(b"drifted")
    with pytest.raises(PermissionError, match="identity drifted"):
        load_bound_host_identity(path, cli)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing canary")
def test_real_windows_handle_branch_releases_file_after_success(tmp_path: Path) -> None:
    result = _run_windows_handle_branch(tmp_path, "success")

    assert result["held_save_denied"] is True
    assert result["save_after_release"] is True
    assert result["release_latency_ms"] <= 5000
