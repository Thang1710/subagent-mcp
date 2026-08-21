from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from spikes.phase0a import live_init
from spikes.phase0a.core import ProbeResult
from spikes.phase0a.live_common import BoundCliIdentity, _scope_from_dict, approval_digest
from spikes.phase0a.live_host import write_bound_host_identity
from spikes.phase0a.live_init import (
    GroupAPaths,
    adjudicate_group_a,
    assert_no_credential_overrides,
    build_group_a_execution_manifest,
    build_group_a_scope,
    build_group_a_settings,
    build_init_argv,
    materialize_group_a,
    observe_init_arm,
    run_group_a,
)


def _paths(tmp_path: Path) -> GroupAPaths:
    return GroupAPaths(
        cwd=tmp_path / "repo",
        settings=tmp_path / "settings.json",
        empty_mcp=tmp_path / "empty-mcp.json",
        event_log=tmp_path / "events.jsonl",
    )


def _bound_identity(cli: Path) -> BoundCliIdentity:
    return BoundCliIdentity.capture(cli, version="2.1.224")


def _write_host_identity(root: Path, cli: Path) -> Path:
    return write_bound_host_identity(root, cli, {
        "status": "ready",
        "observed_cli_version": "2.1.224",
        "cli_content_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        "identity_stable": True,
    })


def test_group_a_strict_argv_is_exact_and_no_model_or_prompt(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    cli = tmp_path / "claude.exe"

    argv = build_init_argv(cli, paths, strict=True)

    assert argv == (
        str(cli.resolve()),
        "--init-only",
        "--no-session-persistence",
        "--setting-sources",
        "user,project,local",
        "--settings",
        str(paths.settings.resolve()),
        "--strict-mcp-config",
        "--mcp-config",
        str(paths.empty_mcp.resolve()),
        "--tools",
        "",
        "--prompt-suggestions",
        "false",
        "--disallowedTools",
        "mcp__codex__*",
        "mcp__agent_bridge__*",
        "mcp__subagent_harness_mcp__*",
    )
    assert "--bare" not in argv
    assert "--safe-mode" not in argv
    assert "--model" not in argv
    assert "--prompt" not in argv
    assert "--fallback-model" not in argv
    assert "--bg" not in argv
    assert "--worktree" not in argv


def test_group_a_control_differs_only_by_strict_flag(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    cli = tmp_path / "claude.exe"
    strict = list(build_init_argv(cli, paths, strict=True))
    control = list(build_init_argv(cli, paths, strict=False))

    strict.remove("--strict-mcp-config")
    assert strict == control


def test_group_a_settings_register_required_observer_hooks(tmp_path: Path) -> None:
    python_exe = tmp_path / "python.exe"
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    sink = tmp_path / "hook_sink.py"
    settings = build_group_a_settings(
        python_exe,
        sink,
        tmp_path / "events.jsonl",
        observer_cli=cli,
        observer_cli_sha256="a" * 64,
    )

    assert set(settings["hooks"]) == {"Setup", "SessionStart", "InstructionsLoaded"}
    for event in settings["hooks"]:
        handler = settings["hooks"][event][0]["hooks"][0]
        assert handler["command"] == str(python_exe.resolve())
        assert handler["args"][-4:] == [
            "--observer-cli", str(cli.resolve()),
            "--observer-cli-sha256", "a" * 64,
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


def test_group_a_adjudication_requires_strict_control_and_required_init_hooks() -> None:
    strict = {
        "exit_success": True,
        "marker_spawned": False,
        "marker_cleanup_confirmed": True,
        "model_event_count": 0,
        "rate_event_count": 0,
        "hook_error_count": 0,
        "hooks": ["InstructionsLoaded", "Setup"],
        "observer_identity_match": True,
    }
    control = {**strict, "marker_spawned": True}

    result = adjudicate_group_a(strict, control)

    assert result == {
        "status": "pass",
        "init_only_capability": True,
        "observer_visibility": True,
        "strict_mcp_pre_spawn": True,
        "observed_hooks": ["InstructionsLoaded", "Setup"],
        "marker_cleanup_confirmed": True,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"model_event_count": 1},
        {"rate_event_count": 1},
        {"hook_error_count": 1},
        {"observer_identity_match": False},
        {"hooks": ["SessionStart"]},
    ],
)
def test_group_a_adjudication_fails_closed(change) -> None:
    base = {
        "exit_success": True,
        "marker_spawned": False,
        "marker_cleanup_confirmed": True,
        "model_event_count": 0,
        "rate_event_count": 0,
        "hook_error_count": 0,
        "hooks": ["InstructionsLoaded", "Setup"],
        "observer_identity_match": True,
    }
    strict = {**base, **change}
    control = {**base, "marker_spawned": True}

    assert adjudicate_group_a(strict, control)["status"] == "blocked"


def test_group_a_scope_has_zero_provider_and_mutation_budget() -> None:
    scope = build_group_a_scope(
        git_head="a" * 40,
        cli_sha256="b" * 64,
        executable_manifest_sha256="c" * 64,
        trust_revision=3,
    )

    assert scope.gate_ids == (
        "init_only_capability",
        "observer_visibility",
        "strict_mcp_pre_spawn",
    )
    assert scope.side_effects == ()
    assert scope.max_provider_session_launches == 0
    assert scope.max_worktree_creates == 0
    assert scope.max_stop_respawn_actions == 0
    assert scope.max_attach_actions == 0
    assert scope.max_file_deletes == 0
    assert scope.max_removals == 0


def test_materialize_group_a_writes_exact_committed_build_outputs(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    marker = tmp_path / "marker_mcp.py"
    marker.write_text("print('unused')", encoding="utf-8")
    sink = tmp_path / "hook_sink.py"
    sink.write_text("print('unused')", encoding="utf-8")

    materialized = materialize_group_a(
        tmp_path / "run",
        cli=cli,
        python_exe=Path(sys.executable),
        marker_script=marker,
        hook_sink=sink,
        bound_identity=_bound_identity(cli),
    )

    assert materialized.cli_sha256
    assert materialized.strict_argv == build_init_argv(cli, materialized.paths, strict=True)
    assert materialized.control_argv == build_init_argv(cli, materialized.paths, strict=False)
    assert json.loads(materialized.paths.empty_mcp.read_text(encoding="utf-8")) == {"mcpServers": {}}
    settings = json.loads(materialized.paths.settings.read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"Setup", "SessionStart", "InstructionsLoaded"}
    assert materialized.marker_path.parent == (tmp_path / "run").resolve()


def test_run_group_a_invokes_only_strict_then_control() -> None:
    strict_argv = ("claude", "strict")
    control_argv = ("claude", "control")
    calls = []
    passing = {
        "exit_success": True,
        "marker_spawned": False,
        "marker_cleanup_confirmed": True,
        "model_event_count": 0,
        "rate_event_count": 0,
        "hook_error_count": 0,
        "hooks": ["InstructionsLoaded", "Setup"],
        "observer_identity_match": True,
    }

    def invoke(argv):
        calls.append(argv)
        return {**passing, "marker_spawned": argv == control_argv}

    result = run_group_a(strict_argv, control_argv, invoke=invoke)

    assert calls == [strict_argv, control_argv]
    assert result["status"] == "pass"


def test_run_group_a_stops_before_control_when_strict_is_unsafe() -> None:
    calls = []

    def invoke(argv):
        calls.append(argv)
        return {
            "exit_success": False,
            "marker_spawned": False,
            "marker_cleanup_confirmed": True,
            "model_event_count": 0,
            "rate_event_count": 0,
            "hook_error_count": 0,
            "hooks": [],
            "observer_identity_match": False,
        }

    assert run_group_a(("strict",), ("control",), invoke=invoke)["status"] == "blocked"
    assert calls == [("strict",)]


def test_run_group_a_runs_control_when_strict_is_safe_but_missing_required_hook() -> None:
    calls = []

    def invoke(argv):
        calls.append(argv)
        return {
            "exit_success": True,
            "marker_spawned": argv == ("control",),
            "marker_cleanup_confirmed": True,
            "model_event_count": 0,
            "rate_event_count": 0,
            "hook_error_count": 0,
            "hooks": ["InstructionsLoaded"],
            "observer_identity_match": True,
        }

    assert run_group_a(("strict",), ("control",), invoke=invoke)["status"] == "blocked"
    assert calls == [("strict",), ("control",)]


def test_observe_init_arm_keeps_only_counts_hooks_and_identity() -> None:
    probe = ProbeResult(
        name="init",
        argv=(),
        cwd=None,
        started_at="2026-08-20T00:00:00+00:00",
        duration_ms=1,
        exit_code=0,
        stdout='{"type":"system","subtype":"init"}\nprivate non-json output\n',
        stderr="",
        timed_out=False,
    )
    events = "\n".join(json.dumps({
        "hook_event_name": event,
        "observer_cli_sha256": "a" * 64,
    }) for event in ("Setup", "SessionStart", "InstructionsLoaded")) + "\n"

    result = observe_init_arm(
        probe,
        events.encode("utf-8"),
        marker_spawned=False,
        marker_cleanup_confirmed=True,
        expected_cli_sha256="a" * 64,
    )

    assert result == {
        "exit_success": True,
        "marker_spawned": False,
        "marker_cleanup_confirmed": True,
        "model_event_count": 0,
        "rate_event_count": 0,
        "hook_error_count": 0,
        "hooks": ["InstructionsLoaded", "SessionStart", "Setup"],
        "observer_identity_match": True,
    }


def test_observe_init_arm_counts_model_and_hook_errors_without_text() -> None:
    probe = ProbeResult(
        name="init",
        argv=(),
        cwd=None,
        started_at="2026-08-20T00:00:00+00:00",
        duration_ms=1,
        exit_code=0,
        stdout='{"type":"assistant","text":"private"}\n{"type":"rate_limit_event"}\n',
        stderr="hook failed with private path",
        timed_out=False,
    )

    result = observe_init_arm(
        probe,
        b'{"hook_event_name":"SessionStart","observer_cli_sha256":"b"}\n',
        marker_spawned=False,
        marker_cleanup_confirmed=True,
        expected_cli_sha256="a" * 64,
    )

    assert result["model_event_count"] == 1
    assert result["rate_event_count"] == 1
    assert result["hook_error_count"] == 1
    assert result["observer_identity_match"] is False
    assert "private" not in json.dumps(result)


def test_group_a_cleanup_failure_requires_recovery() -> None:
    strict = {
        "exit_success": True,
        "marker_spawned": False,
        "marker_cleanup_confirmed": False,
        "model_event_count": 0,
        "rate_event_count": 0,
        "hook_error_count": 0,
        "hooks": ["InstructionsLoaded", "Setup"],
        "observer_identity_match": True,
    }

    assert adjudicate_group_a(strict, {})["status"] == "recovery_required"


def _marker_record(token: str, *, pid: int = 1234) -> dict[str, object]:
    return {
        "schema_version": 1,
        "pid": pid,
        "ownership_token": token,
        "creation_identity": "windows:1234",
        "executable_sha256": "a" * 64,
    }


def test_marker_cleanup_accepts_verified_absence_without_exit_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "a" * 32
    started = tmp_path / "started.json"
    exited = tmp_path / "exited.json"
    started.write_text(json.dumps(_marker_record(token)), encoding="utf-8")
    exited.with_name(f"{exited.name}.1234.tmp").write_bytes(b"")
    monkeypatch.setattr(
        live_init,
        "_stop_owned_marker_process",
        lambda *_args, **_kwargs: (True, False),
    )

    spawned, clean = live_init._marker_state(
        SimpleNamespace(
            marker_path=started,
            marker_exit_path=exited,
            marker_token=token,
        ),
        Path(sys.executable),
    )

    assert spawned is True
    assert clean is True


def test_marker_cleanup_rejects_mismatched_canonical_exit_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "b" * 32
    started = tmp_path / "started.json"
    exited = tmp_path / "exited.json"
    started.write_text(json.dumps(_marker_record(token)), encoding="utf-8")
    exited.write_text(json.dumps(_marker_record(token, pid=5678)), encoding="utf-8")
    stop_calls = []
    monkeypatch.setattr(
        live_init,
        "_stop_owned_marker_process",
        lambda *_args, **_kwargs: stop_calls.append(True) or (True, False),
    )

    assert live_init._marker_state(
        SimpleNamespace(
            marker_path=started,
            marker_exit_path=exited,
            marker_token=token,
        ),
        Path(sys.executable),
    ) == (True, False)
    assert stop_calls == []


def test_marker_cleanup_rejects_unconfirmed_process_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "c" * 32
    started = tmp_path / "started.json"
    started.write_text(json.dumps(_marker_record(token)), encoding="utf-8")
    monkeypatch.setattr(
        live_init,
        "_stop_owned_marker_process",
        lambda *_args, **_kwargs: (False, False),
    )

    assert live_init._marker_state(
        SimpleNamespace(
            marker_path=started,
            marker_exit_path=tmp_path / "exited.json",
            marker_token=token,
        ),
        Path(sys.executable),
    ) == (True, False)


def test_group_a_execution_manifest_binds_both_exact_argv_arrays(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    marker = tmp_path / "marker_mcp.py"
    marker.write_text("print('unused')", encoding="utf-8")
    sink = tmp_path / "hook_sink.py"
    sink.write_text("print('unused')", encoding="utf-8")
    materialized = materialize_group_a(
        tmp_path / "run",
        cli=cli,
        python_exe=Path(sys.executable),
        marker_script=marker,
        hook_sink=sink,
        bound_identity=_bound_identity(cli),
    )

    original, _files, _contract = build_group_a_execution_manifest(
        materialized,
        python_exe=Path(sys.executable),
        marker_script=marker,
        hook_sink=sink,
    )
    changed, _changed_files, _changed_contract = build_group_a_execution_manifest(
        replace(materialized, control_argv=materialized.control_argv + ("unexpected",)),
        python_exe=Path(sys.executable),
        marker_script=marker,
        hook_sink=sink,
    )

    assert len(original) == 64
    assert original != changed


def test_group_a_execution_manifest_binds_owned_marker_process_spec(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    marker = tmp_path / "marker_mcp.py"
    marker.write_text("print('unused')", encoding="utf-8")
    sink = tmp_path / "hook_sink.py"
    sink.write_text("print('unused')", encoding="utf-8")
    materialized = materialize_group_a(
        tmp_path / "run",
        cli=cli,
        python_exe=Path(sys.executable),
        marker_script=marker,
        hook_sink=sink,
        bound_identity=_bound_identity(cli),
    )

    digest, files, contract = build_group_a_execution_manifest(
        materialized,
        python_exe=Path(sys.executable),
        marker_script=marker,
        hook_sink=sink,
    )

    assert contract["strict_argv"] == list(materialized.strict_argv)
    assert contract["control_argv"] == list(materialized.control_argv)
    assert contract["observed_cli_version"] == "2.1.224"
    assert contract["owned_marker_process"] == {
        "argv": [str(Path(sys.executable).resolve()), str(marker.resolve())],
        "deadline_seconds": 30,
        "environment": {
            "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER": str(materialized.marker_path.resolve()),
            "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT": str(
                materialized.marker_exit_path.resolve()
            ),
            "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN": materialized.marker_token,
        },
        "max_processes": 1,
    }
    manifest_names = {Path(entry.canonical_path).name for entry in files.entries}
    assert {"contracts.py", "core.py", "locking.py"} <= manifest_names
    assert len(digest) == 64


def test_preview_writes_direct_approval_scope_and_returns_hash_bound_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    marker = tmp_path / "marker_mcp.py"
    marker.write_text("print('unused')", encoding="utf-8")
    sink = tmp_path / "hook_sink.py"
    sink.write_text("print('unused')", encoding="utf-8")
    monkeypatch.setattr(live_init, "_git_checkpoint", lambda _root: ("a" * 40, False))
    _write_host_identity(tmp_path / "host", cli)

    preview = live_init.preview_group_a(
        tmp_path / "run",
        cli=cli,
        project_root=tmp_path,
        python_exe=Path(sys.executable),
        marker_script=marker,
        hook_sink=sink,
    )

    pending = json.loads((tmp_path / "run" / "pending-scope.json").read_text(encoding="utf-8"))
    parsed_scope = _scope_from_dict(pending)
    assert pending == preview["scope"]
    assert approval_digest(parsed_scope) == preview["scope_sha256"]
    assert parsed_scope.executable_manifest_sha256 == live_init.execution_contract_digest(
        preview["execution_contract"]
    )


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"],
)
def test_group_a_rejects_credential_override_before_invocation(name: str) -> None:
    with pytest.raises(PermissionError, match="credential override"):
        assert_no_credential_overrides({name: "present"})


def test_main_preview_prints_only_hash_bound_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {
        "scope": {"schema_version": 1},
        "scope_sha256": "a" * 64,
        "execution_contract": {"strict_argv": ["claude", "--init-only"]},
    }
    calls = []

    def fake_preview(root, **kwargs):
        calls.append((root, kwargs))
        return expected

    monkeypatch.setattr(live_init, "preview_group_a", fake_preview)

    exit_code = live_init.main([
        "--preview",
        "--root", str(tmp_path / "run"),
        "--cli", str(tmp_path / "claude.exe"),
    ])

    assert exit_code == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle ownership check")
def test_owned_marker_process_is_terminated_through_verified_handle(tmp_path: Path) -> None:
    marker_script = Path(__file__).parents[2] / "spikes" / "phase0a" / "marker_mcp.py"
    started = tmp_path / "started.json"
    exited = tmp_path / "exited.json"
    token = "a" * 32
    env = dict(os.environ)
    env.update({
        "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER": str(started),
        "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT": str(exited),
        "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN": token,
    })
    process = subprocess.Popen(
        [sys.executable, str(marker_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        shell=False,
    )
    try:
        deadline = time.monotonic() + 5
        while not started.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        record = live_init._read_marker_record(started, token)
        duplicate = subprocess.run(
            [sys.executable, str(marker_script)],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=5,
            check=False,
            shell=False,
        )

        stopped, forced = live_init._stop_owned_marker_process(
            record,
            expected_python=Path(sys.executable),
        )

        assert stopped is True
        assert forced is True
        assert duplicate.returncode != 0
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
