from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from spikes.phase0a import live_context
from spikes.phase0a.live_common import BoundCliIdentity, _scope_from_dict, _verify_private_path
from spikes.phase0a.live_host import write_bound_host_identity
from spikes.phase0a.live_common import LiveCircuitResult, approval_digest
from spikes.phase0a.live_context import (
    CONTEXT_FINAL_MARKER,
    CONTEXT_PROMPT,
    ContextPaths,
    build_context_execution_manifest,
    build_context_argv,
    build_context_scope,
    execute_context,
    materialize_context,
    preview_context,
    instruction_observation,
    project_context_result,
    run_context_arms,
)


def _paths(tmp_path: Path) -> ContextPaths:
    return ContextPaths(
        cwd=tmp_path / "repo",
        settings=tmp_path / "settings.json",
        empty_mcp=tmp_path / "empty-mcp.json",
        event_log=tmp_path / "events.jsonl",
    )


def _circuit(**changes) -> LiveCircuitResult:
    values = {
        "classification": "success",
        "exit_code": 0,
        "model": "claude-sonnet-5",
        "effort": "low",
        "requested_auto_compaction_window": 274000,
        "requested_auto_compaction_trigger_percent": None,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_auto_compaction_window": None,
        "effective_auto_compaction_trigger_percent": None,
        "effective_auto_compaction_trigger_tokens": None,
        "tools": (),
        "mcp_server_count": 0,
        "plugin_count": 0,
        "is_using_overage": False,
        "rate_statuses": ("allowed_warning",),
        "source_sha256": "a" * 64,
        "stream_bytes": 100,
        "final_marker_matched": True,
        "sanitized_final_text": None,
        "provider_error_code": None,
        "unknown_top_level_fields": (),
        "init_envelope_observed": True,
        "result_envelope_observed": True,
        "timeout_phase": None,
    }
    values.update(changes)
    return LiveCircuitResult(**values)


def _bound_identity(cli: Path) -> BoundCliIdentity:
    return BoundCliIdentity.capture(cli, version="2.1.224")


def _write_ready_host(root: Path, cli: Path, *, context_flags: bool = True) -> Path:
    return write_bound_host_identity(
        root,
        cli,
        {
            "status": "ready",
            "observed_cli_version": "2.1.224",
            "cli_content_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
            "identity_stable": True,
        },
        capabilities={
            "tools_empty_documented": context_flags,
            "prompt_suggestions_false_documented": context_flags,
        },
    )


def test_context_argv_is_exact_foreground_no_tools(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    paths = _paths(tmp_path)

    argv = build_context_argv(cli, paths)

    assert argv == (
        str(cli.resolve()),
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-hook-events",
        "--model", "claude-sonnet-5",
        "--effort", "low",
        "--autocompact", "274000",
        "--setting-sources", "user,project,local",
        "--settings", str(paths.settings.resolve()),
        "--tools", "",
        "--disallowedTools",
        "mcp__codex__*",
        "mcp__agent_bridge__*",
        "mcp__subagent_harness_mcp__*",
        "--permission-mode", "dontAsk",
        "--prompt-suggestions", "false",
        "--strict-mcp-config",
        "--mcp-config", str(paths.empty_mcp.resolve()),
        "--no-session-persistence",
        CONTEXT_PROMPT,
    )
    assert CONTEXT_FINAL_MARKER in CONTEXT_PROMPT
    assert "--bare" not in argv
    assert "--safe-mode" not in argv
    assert "--fallback-model" not in argv


def test_context_projection_keeps_requested_and_effective_fields_separate() -> None:
    result = project_context_result(
        _circuit(),
        instruction_observation={
            "delivery_observed": True,
            "instruction_event_count": 1,
            "source_categories": ["project"],
            "content_hashes": ["b" * 64],
            "load_reasons": ["startup"],
        },
        checkout_clean=True,
        usage_credits_off_confirmed=False,
    )

    assert result["status"] == "CAPABILITY_MISSING"
    assert result["requested_auto_compaction_window_tokens"] == 274000
    assert result["requested_auto_compaction_trigger_tokens"] == 274000
    assert result["effective_auto_compaction_window_tokens"] is None
    assert result["effective_auto_compaction_trigger_percent"] is None
    assert result["effective_auto_compaction_trigger_tokens"] is None
    assert result["effective_effort"] == "low"
    assert result["background_eligible"] is False
    assert result["attested_configuration"] == "foreground_no_tools"
    assert result["production_equivalent_attestation"] == "outstanding"
    assert "effective_auto_compaction_window_tokens" in result["missing_fields"]


def test_context_projection_allows_background_subset_only_after_credit_confirmation() -> None:
    result = project_context_result(
        _circuit(),
        instruction_observation=instruction_observation(b""),
        checkout_clean=True,
        usage_credits_off_confirmed=True,
    )

    assert result["status"] == "CAPABILITY_MISSING"
    assert result["background_eligible"] is True
    assert result["declared_native_attestation"] == "incomplete"


def test_context_projection_blocks_overage_or_nonempty_tools() -> None:
    overage = project_context_result(
        _circuit(is_using_overage=True),
        instruction_observation=instruction_observation(b""),
        checkout_clean=True,
        usage_credits_off_confirmed=True,
    )
    tools = project_context_result(
        _circuit(tools=("Read",)),
        instruction_observation=instruction_observation(b""),
        checkout_clean=True,
        usage_credits_off_confirmed=True,
    )

    assert overage["status"] == "BLOCKED"
    assert overage["background_eligible"] is False
    assert tools["status"] == "BLOCKED"


@pytest.mark.parametrize(
    "changes",
    [
        {"classification": "quota_paused"},
        {"classification": "protocol_error"},
        {"model": "claude-opus-5"},
        {"effort": "high"},
        {"mcp_server_count": 1},
        {"final_marker_matched": False},
        {"requested_auto_compaction_window": None},
        {"requested_auto_compaction_trigger_tokens": None},
        {"rate_statuses": ("unknown",)},
        {"stderr_bytes": 12},
    ],
)
def test_context_projection_blocks_every_direct_circuit_breaker(changes) -> None:
    result = project_context_result(
        _circuit(**changes),
        instruction_observation=instruction_observation(b""),
        checkout_clean=True,
        usage_credits_off_confirmed=True,
    )

    assert result["status"] == "BLOCKED"
    assert result["background_eligible"] is False


def test_context_projection_keeps_only_sanitized_terminal_diagnostics() -> None:
    result = project_context_result(
        _circuit(
            classification="timeout",
            exit_code=1,
            final_marker_matched=False,
            is_using_overage=None,
            rate_statuses=(),
            result_envelope_observed=False,
            timeout_phase="post_init",
        ),
        instruction_observation=instruction_observation(b""),
        checkout_clean=True,
        usage_credits_off_confirmed=False,
    )

    assert result["terminal_classification"] == "timeout"
    assert result["process_exit_code"] == 1
    assert result["init_envelope_observed"] is True
    assert result["result_envelope_observed"] is False
    assert result["timeout_phase"] == "post_init"
    assert "stream_bytes" not in result
    assert "sanitized_final_text" not in result


def test_context_projection_blocks_structured_setting_source_mismatch() -> None:
    result = project_context_result(
        _circuit(),
        instruction_observation=instruction_observation(b""),
        checkout_clean=True,
        usage_credits_off_confirmed=True,
        structured_context={"effective_setting_sources": "project"},
    )

    assert result["status"] == "BLOCKED"


def test_instruction_observation_is_bounded_and_path_free() -> None:
    events = (
        json.dumps({
            "hook_event_name": "InstructionsLoaded",
            "instructions_loaded": {
                "source_category": "project",
                "content_sha256": "b" * 64,
                "load_reason": "startup",
            },
        })
        + "\n"
        + json.dumps({"hook_event_name": "SessionStart", "cwd": "C:/private"})
        + "\n"
    ).encode("utf-8")

    result = instruction_observation(events)

    assert result == {
        "delivery_observed": True,
        "instruction_event_count": 1,
        "source_categories": ["project"],
        "content_hashes": ["b" * 64],
        "load_reasons": ["startup"],
    }
    assert "private" not in json.dumps(result)


def test_context_control_failure_stops_before_required_arm() -> None:
    calls = []

    def invoke(argv):
        calls.append(argv)
        return _circuit(classification="quota_paused")

    result = run_context_arms(
        ("context",),
        control_argv=("control",),
        invoke=invoke,
        checkout_is_clean=lambda: True,
    )

    assert calls == [("control",)]
    assert result["context"] is None
    assert result["control"].classification == "quota_paused"


def test_checkout_drift_after_control_prevents_required_arm() -> None:
    calls = []
    cleanliness = iter((True, False))

    with pytest.raises(PermissionError, match="drifted"):
        run_context_arms(
            ("context",),
            control_argv=("control",),
            invoke=lambda argv: calls.append(argv) or _circuit(),
            checkout_is_clean=lambda: next(cleanliness),
        )

    assert calls == [("control",)]


def test_context_scope_binds_one_or_two_exact_provider_launches() -> None:
    one = build_context_scope(
        git_head="a" * 40,
        cli_sha256="b" * 64,
        executable_manifest_sha256="c" * 64,
        context_argv=("claude", "context"),
        control_argv=None,
    )
    two = build_context_scope(
        git_head="a" * 40,
        cli_sha256="b" * 64,
        executable_manifest_sha256="c" * 64,
        context_argv=("claude", "context"),
        control_argv=("claude", "control"),
    )

    assert one.max_provider_session_launches == 1
    assert one.side_effects[0].argv_template == ("claude", "context")
    assert two.max_provider_session_launches == 2
    assert [item.kind for item in two.side_effects] == [
        "provider_control_launch", "provider_launch",
    ]
    assert len(approval_digest(two)) == 64


def test_materializer_creates_a_fresh_clean_disposable_git_repo(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    hook_sink = Path(live_context.__file__).with_name("hook_sink.py")

    materialized = materialize_context(
        tmp_path / "run",
        cli=cli,
        python_exe=Path(sys.executable),
        hook_sink=hook_sink,
        bound_identity=_bound_identity(cli),
    )

    status = subprocess.run(
        ["git", "-C", str(materialized.paths.cwd), "status", "--porcelain=v1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
        shell=False,
    )
    settings = json.loads(materialized.paths.settings.read_text(encoding="utf-8"))
    assert status.stdout == ""
    assert (materialized.paths.cwd / "CLAUDE.md").is_file()
    assert set(settings["hooks"]) == {"InstructionsLoaded"}
    assert json.loads(materialized.paths.empty_mcp.read_text(encoding="utf-8")) == {
        "mcpServers": {},
    }
    assert materialized.control_argv is None
    _verify_private_path(materialized.paths.cwd.parent, directory=True)


def test_materializer_rejects_a_nonempty_root_without_mutation(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    root = tmp_path / "run"
    root.mkdir()
    sentinel = root / "owned.txt"
    sentinel.write_text("unchanged", encoding="utf-8")

    with pytest.raises(FileExistsError, match="fresh empty root"):
        materialize_context(
            root,
            cli=cli,
            python_exe=Path(sys.executable),
            hook_sink=Path(live_context.__file__).with_name("hook_sink.py"),
            bound_identity=_bound_identity(cli),
        )

    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_control_plugin_is_static_and_contains_no_executable_surface() -> None:
    root = Path(__file__).parents[1] / "fixtures" / "phase0a" / "control-plugin"
    files = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )

    assert files == [
        ".claude-plugin/plugin.json",
        "skills/subagent-harness-mcp-control/SKILL.md",
    ]
    manifest = json.loads((root / files[0]).read_text(encoding="utf-8"))
    assert manifest == {
        "name": "subagent-harness-mcp-phase0a-control",
        "version": "0.0.0",
        "description": "Static Phase 0a plugin visibility control.",
    }
    serialized = json.dumps(manifest).casefold()
    assert "hooks" not in serialized
    assert "mcpservers" not in serialized


def test_context_execution_manifest_binds_argv_cwd_and_generated_files(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    hook_sink = Path(live_context.__file__).with_name("hook_sink.py")
    materialized = materialize_context(
        tmp_path / "run",
        cli=cli,
        python_exe=Path(sys.executable),
        hook_sink=hook_sink,
        bound_identity=_bound_identity(cli),
    )

    digest, manifest, contract = build_context_execution_manifest(
        materialized,
        python_exe=Path(sys.executable),
        hook_sink=hook_sink,
    )

    assert len(digest) == 64
    assert contract["context_argv"] == list(materialized.context_argv)
    assert contract["control_argv"] is None
    assert contract["cwd"] == str(materialized.paths.cwd.resolve())
    assert contract["startup_timeout_seconds"] == 30
    assert contract["post_init_timeout_seconds"] == 120
    assert contract["file_manifest_sha256"] == manifest.sha256
    assert set(contract["generated_file_sha256"]) == {
        "CLAUDE.md", "declared-empty.json", "settings.json",
    }
    assert set(contract["mutable_outputs"]) == {
        "consumed-side-effects.json",
        "events.jsonl",
        "live-context-candidate.json",
    }
    assert set(contract["mutable_outputs"]) == {
        "consumed-side-effects.json",
        "events.jsonl",
        "live-context-candidate.json",
    }


def test_preview_requires_task2_context_flag_capabilities(tmp_path: Path, monkeypatch) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    _write_ready_host(tmp_path / "host", cli, context_flags=False)
    monkeypatch.setattr(live_context, "_git_checkpoint", lambda _root: ("a" * 40, False))

    with pytest.raises(PermissionError, match="exact context flags"):
        preview_context(
            tmp_path / "context",
            cli=cli,
            project_root=tmp_path,
            python_exe=Path(sys.executable),
            hook_sink=Path(live_context.__file__).with_name("hook_sink.py"),
        )

    assert not (tmp_path / "context").exists()


def test_preview_writes_digest_bound_scope_without_provider_spawn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    _write_ready_host(tmp_path / "host", cli)
    monkeypatch.setattr(live_context, "_git_checkpoint", lambda _root: ("a" * 40, False))
    monkeypatch.setattr(
        live_context,
        "run_stream_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider spawn")),
        raising=False,
    )

    preview = preview_context(
        tmp_path / "context",
        cli=cli,
        project_root=tmp_path,
        python_exe=Path(sys.executable),
        hook_sink=Path(live_context.__file__).with_name("hook_sink.py"),
    )

    pending = json.loads((tmp_path / "context" / "pending-scope.json").read_text(encoding="utf-8"))
    scope = _scope_from_dict(pending)
    assert preview["scope"] == pending
    assert preview["scope_sha256"] == approval_digest(scope)
    assert scope.max_provider_session_launches == 1
    assert preview["plugin_control"] == "unsupported"
    target_names = {Path(value).name for value in scope.side_effects[0].exact_targets}
    assert {
        "consumed-side-effects.json",
        "events.jsonl",
        "live-context-candidate.json",
    } <= target_names


def test_instruction_wait_collects_events_until_the_bounded_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = b'{"hook_event_name":"InstructionsLoaded","first":true}\n'
    final = first + b'{"hook_event_name":"InstructionsLoaded","second":true}\n'
    reads = iter((first, final))
    clock = iter((0.0, 0.5, 1.0))
    monkeypatch.setattr(live_context, "_read_events_once", lambda _path: next(reads))
    monkeypatch.setattr(live_context.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(live_context.time, "sleep", lambda _seconds: None)

    observed = live_context._read_events_until(
        tmp_path / "events.jsonl", timeout_seconds=1.0,
    )

    assert observed == final
    target_names = {Path(value).name for value in scope.side_effects[0].exact_targets}
    assert {
        "consumed-side-effects.json",
        "events.jsonl",
        "live-context-candidate.json",
    } <= target_names


def test_instruction_wait_collects_events_until_the_bounded_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = b'{"hook_event_name":"InstructionsLoaded","first":true}\n'
    final = first + b'{"hook_event_name":"InstructionsLoaded","second":true}\n'
    reads = iter((first, final))
    clock = iter((0.0, 0.5, 1.0))
    monkeypatch.setattr(live_context, "_read_events_once", lambda _path: next(reads))
    monkeypatch.setattr(live_context.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(live_context.time, "sleep", lambda _seconds: None)

    observed = live_context._read_events_until(
        tmp_path / "events.jsonl", timeout_seconds=1.0,
    )

    assert observed == final


def test_execute_rejects_credential_override_before_claim_or_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    claimed = []
    monkeypatch.setattr(
        live_context,
        "claim_execution_authorization",
        lambda *_args, **_kwargs: claimed.append(True),
    )

    with pytest.raises(PermissionError, match="credential override"):
        execute_context(
            tmp_path / "context",
            cli=tmp_path / "claude.exe",
            project_root=tmp_path,
            approval=tmp_path / "approval.json",
            python_exe=Path(sys.executable),
            hook_sink=Path(live_context.__file__).with_name("hook_sink.py"),
            env={"ANTHROPIC_API_KEY": "forbidden"},
        )

    assert claimed == []


def test_execute_consumes_exact_provider_side_effect_and_projects_sanitized_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake cli")
    _write_ready_host(tmp_path / "host", cli)
    monkeypatch.setattr(live_context, "_git_checkpoint", lambda _root: ("a" * 40, False))
    preview_context(
        tmp_path / "context",
        cli=cli,
        project_root=tmp_path,
        python_exe=Path(sys.executable),
        hook_sink=Path(live_context.__file__).with_name("hook_sink.py"),
    )
    order = []
    authorization = object()
    monkeypatch.setattr(
        live_context,
        "claim_execution_authorization",
        lambda *_args, **_kwargs: order.append("claim") or authorization,
    )

    def consume(observed_authorization, kind, state, ledger, *, invoke, **_kwargs):
        assert observed_authorization is authorization
        assert state == {}
        assert Path(ledger).name == "consumed-side-effects.json"
        order.append(kind)
        scope = _scope_from_dict(json.loads(
            (tmp_path / "context" / "pending-scope.json").read_text(encoding="utf-8")
        ))
        argv = next(effect.argv_template for effect in scope.side_effects if effect.kind == kind)
        return invoke(argv)

    monkeypatch.setattr(live_context, "consume_side_effect", consume)

    def stream(argv, **kwargs):
        order.append("provider")
        assert tuple(argv) == _scope_from_dict(json.loads(
            (tmp_path / "context" / "pending-scope.json").read_text(encoding="utf-8")
        )).side_effects[0].argv_template
        assert kwargs["requested_auto_compaction_window"] == 274000
        assert kwargs["requested_auto_compaction_trigger_tokens"] == 274000
        assert kwargs["timeout_seconds"] == 30
        assert kwargs["post_init_timeout_seconds"] == 120
        assert kwargs["final_marker"] == CONTEXT_FINAL_MARKER
        return _circuit()

    monkeypatch.setattr(live_context, "run_stream_command", stream)
    monkeypatch.setattr(
        live_context,
        "_read_events_until",
        lambda _path, **_kwargs: (
            json.dumps({
                "hook_event_name": "InstructionsLoaded",
                "instructions_loaded": {
                    "source_category": "project",
                    "content_sha256": "b" * 64,
                    "load_reason": "startup",
                },
            }) + "\n"
        ).encode("utf-8"),
    )

    result = execute_context(
        tmp_path / "context",
        cli=cli,
        project_root=tmp_path,
        approval=tmp_path / "approval.json",
        python_exe=Path(sys.executable),
        hook_sink=Path(live_context.__file__).with_name("hook_sink.py"),
        env={},
        confirm_usage_credits_off=lambda: order.append("confirm") or True,
    )

    assert order == ["claim", "provider_launch", "provider", "confirm"]
    assert result["status"] == "CAPABILITY_MISSING"
    assert result["background_eligible"] is True
    assert result["instructions_loaded"]["source_categories"] == ["project"]
    candidate = json.loads(
        (tmp_path / "context" / "live-context-candidate.json").read_text(
            encoding="utf-8"
        )
    )
    assert candidate["source"]["sha256"] == "a" * 64
    assert candidate["payload"]["usage_credits_off_confirmed"] is True
    assert candidate["payload"]["terminal_classification"] == "success"
    assert candidate["payload"]["process_exit_code"] == 0
    assert candidate["payload"]["init_envelope_observed"] is True
    assert candidate["payload"]["result_envelope_observed"] is True
    assert candidate["payload"]["timeout_phase"] is None
    candidate = json.loads(
        (tmp_path / "context" / "live-context-candidate.json").read_text(
            encoding="utf-8"
        )
    )
    assert candidate["source"]["sha256"] == "a" * 64
    assert candidate["payload"]["usage_credits_off_confirmed"] is True


def test_main_preview_routes_without_execute_approval(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    expected = {
        "scope": {"schema_version": 1},
        "scope_sha256": "a" * 64,
        "execution_contract": {"context_argv": ["claude"]},
        "plugin_control": "unsupported",
    }
    calls = []
    monkeypatch.setattr(
        live_context,
        "preview_context",
        lambda root, **kwargs: calls.append((root, kwargs)) or expected,
    )

    assert live_context.main([
        "--preview",
        "--root", str(tmp_path / "context"),
        "--cli", str(tmp_path / "claude.exe"),
    ]) == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out) == expected


def test_main_execute_requests_exact_post_result_credit_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    confirmations = []

    def fake_execute(_root, **kwargs):
        confirmations.append(kwargs["confirm_usage_credits_off"]())
        return {"init_subset_status": "PASS", "background_eligible": confirmations[-1]}

    monkeypatch.setattr(live_context, "execute_context", fake_execute)
    monkeypatch.setattr("builtins.input", lambda: "CONFIRM_USAGE_CREDITS_OFF")

    assert live_context.main([
        "--execute",
        "--approval", str(tmp_path / "approved-B.json"),
        "--root", str(tmp_path / "context"),
        "--cli", str(tmp_path / "claude.exe"),
    ]) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert confirmations == [True]
    assert lines[0] == {
        "confirmation_required": "usage_credits_remain_off",
        "enter_exactly": "CONFIRM_USAGE_CREDITS_OFF",
    }
    assert lines[1]["background_eligible"] is True


def test_main_execute_requests_exact_post_result_credit_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    confirmations = []

    def fake_execute(_root, **kwargs):
        confirmations.append(kwargs["confirm_usage_credits_off"]())
        return {"init_subset_status": "PASS", "background_eligible": confirmations[-1]}

    monkeypatch.setattr(live_context, "execute_context", fake_execute)
    monkeypatch.setattr("builtins.input", lambda: "CONFIRM_USAGE_CREDITS_OFF")

    assert live_context.main([
        "--execute",
        "--approval", str(tmp_path / "approved-B.json"),
        "--root", str(tmp_path / "context"),
        "--cli", str(tmp_path / "claude.exe"),
    ]) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert confirmations == [True]
    assert lines[0] == {
        "confirmation_required": "usage_credits_remain_off",
        "enter_exactly": "CONFIRM_USAGE_CREDITS_OFF",
    }
    assert lines[1]["background_eligible"] is True
