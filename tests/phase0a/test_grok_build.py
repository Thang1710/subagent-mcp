import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import spikes.phase0a.grok_build as grok_build

from spikes.phase0a.grok_build import (
    GrokCliObservation,
    GrokHelpContract,
    adjudicate_no_model_contract,
    main,
    parse_help_contract,
    parse_inspect_summary,
    parse_model_catalog,
    parse_version,
    validate_sanitized_output,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "grok_build" / "contracts"

AUTHORIZED_ARGV = (
    ("--no-auto-update", "--version"),
    ("--no-auto-update", "--help"),
    ("--no-auto-update", "agent", "--help"),
    ("--no-auto-update", "agent", "stdio", "--help"),
    ("--no-auto-update", "inspect", "--json"),
    ("--no-auto-update", "models"),
)
EXPECTED_INSTALLED_KEYS = {
    "schema_version",
    "pair_state",
    "version_state",
    "help_state",
    "catalog_state",
    "extensions_discovered",
    "provider_key_environment_omitted",
    "cached_native_login",
    "no_extra_spend",
    "builtin_tool_inventory",
    "provider_readiness",
}


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _help_outputs() -> dict[tuple[str, ...], str]:
    text = _read("documented-help.txt")
    markers = (
        "=== grok --help ===",
        "=== grok agent --help ===",
        "=== grok agent stdio --help ===",
    )
    bodies: list[str] = []
    for index, marker in enumerate(markers):
        start = text.index(marker) + len(marker)
        end = text.index(markers[index + 1]) if index + 1 < len(markers) else len(text)
        bodies.append(text[start:end].strip("\r\n") + "\n")
    bodies[0] = bodies[0].replace(
        "Options:\n", "Options:\n  --no-auto-update       Disable automatic updates.\n"
    )
    return {
        ("--no-auto-update", "--help"): bodies[0],
        ("--no-auto-update", "agent", "--help"): bodies[1],
        ("--no-auto-update", "agent", "stdio", "--help"): bodies[2],
    }


class FakeRunner:
    def __init__(self, behavior=None, on_call=None):
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.behavior = behavior or {}
        self.on_call = on_call
        self.outputs = {
            ("--no-auto-update", "--version"): _read("documented-version.txt"),
            **_help_outputs(),
            ("--no-auto-update", "inspect", "--json"): _read("documented-inspect.json"),
            ("--no-auto-update", "models"): _read("documented-models.txt"),
        }

    def __call__(self, argv, *, env):
        self.calls.append((tuple(argv), dict(env)))
        suffix = tuple(argv[1:])
        if self.on_call is not None:
            self.on_call(len(self.calls), suffix)
        overrides = self.behavior.get(suffix, {})
        return SimpleNamespace(
            returncode=overrides.get("returncode", 0),
            stdout=overrides.get("stdout", self.outputs[suffix]),
            stderr=overrides.get("stderr", ""),
            timed_out=overrides.get("timed_out", False),
            overflow=overrides.get("overflow", False),
            malformed_utf8=overrides.get("malformed_utf8", False),
        )


class ControlledProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        terminate_wait_succeeds: bool = True,
        kill_wait_succeeds: bool = True,
        interrupt_on_poll: bool = False,
    ):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self.terminate_wait_succeeds = terminate_wait_succeeds
        self.kill_wait_succeeds = kill_wait_succeeds
        self.interrupt_on_poll = interrupt_on_poll
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float | None] = []

    def poll(self):
        if self.interrupt_on_poll:
            self.interrupt_on_poll = False
            raise KeyboardInterrupt
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.kill_calls and self.kill_wait_succeeds:
            self.returncode = -9
            return self.returncode
        if self.terminate_calls and self.terminate_wait_succeeds:
            self.returncode = -15
            return self.returncode
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired("fake", timeout)


@pytest.mark.skipif(os.name != "nt", reason="Windows path containment contract")
@pytest.mark.parametrize("executable_name", ["grok.exe", "grok.cmd"])
def test_default_locator_rejects_case_varied_repo_local_executable_before_runner_call(
    tmp_path: Path, monkeypatch, executable_name: str
):
    workspace = tmp_path / "Workspace"
    nested = workspace / "nested"
    executable = workspace / "tools" / executable_name
    (workspace / ".git").mkdir(parents=True)
    nested.mkdir()
    executable.parent.mkdir()
    executable.write_bytes(b"local executable")
    output = tmp_path / "blocked.json"
    runner = FakeRunner()
    monkeypatch.chdir(nested)
    monkeypatch.setattr(grok_build.shutil, "which", lambda _name: str(executable).swapcase())

    assert main(
        ["probe", "--output", str(output)], runner=runner, environ={"PATH": "safe"}
    ) == 0

    assert runner.calls == []
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["pair_state"] == "unavailable"
    assert payload["provider_readiness"] == "needs_canary"


@pytest.mark.skipif(os.name != "nt", reason="Windows path containment contract")
def test_repo_prefix_sibling_executable_is_not_misclassified_as_workspace_local(
    tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "repo"
    sibling = tmp_path / "repo-sibling"
    (workspace / ".git").mkdir(parents=True)
    sibling.mkdir()
    executable = sibling / "grok.exe"
    executable.write_bytes(b"external executable")
    output = tmp_path / "probe.json"
    runner = FakeRunner()
    monkeypatch.chdir(workspace)

    assert main(
        ["probe", "--output", str(output)],
        runner=runner,
        locator=lambda: str(executable),
        environ={"PATH": "safe"},
    ) == 0

    assert len(runner.calls) == 6


def test_run_command_timeout_terminates_then_kills_and_reaps_owned_process(monkeypatch):
    process = ControlledProcess(terminate_wait_succeeds=False)
    monkeypatch.setattr(grok_build.subprocess, "Popen", lambda *args, **kwargs: process)

    result = grok_build._run_command(
        ["fake"], env={}, timeout_seconds=0.0, cleanup_timeout_seconds=0.01
    )

    assert result.timed_out is True
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == -9
    assert process.wait_timeouts == [0.01, 0.01]


def test_run_command_uses_bounded_wait_after_normal_exit(monkeypatch):
    process = ControlledProcess()
    process.returncode = 0
    monkeypatch.setattr(grok_build.subprocess, "Popen", lambda *args, **kwargs: process)

    result = grok_build._run_command(
        ["fake"], env={}, timeout_seconds=1.0, cleanup_timeout_seconds=0.01
    )

    assert result.returncode == 0
    assert process.wait_timeouts == [0.01]


def test_run_command_pipe_saturation_uses_one_combined_bounded_budget():
    result = grok_build._run_command(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.buffer.write(b'x'*700000);"
                "sys.stderr.buffer.write(b'y'*700000)"
            ),
        ],
        env=os.environ,
        timeout_seconds=5.0,
        cleanup_timeout_seconds=1.0,
    )

    assert result.overflow is True
    assert len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) <= 1024 * 1024


def test_normalized_runner_result_enforces_one_combined_output_budget():
    result = grok_build._normalized_result(
        SimpleNamespace(returncode=0, stdout="x" * 600_000, stderr="y" * 600_000)
    )

    assert result.overflow is True
    assert len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) <= 1024 * 1024


def test_run_command_strict_utf8_marks_malformed_bytes_unavailable():
    result = grok_build._run_command(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\xff')"],
        env=os.environ,
        timeout_seconds=5.0,
        cleanup_timeout_seconds=1.0,
    )

    assert result.malformed_utf8 is True
    assert result.stdout == ""
    assert grok_build._command_ok(result) is False


def test_malformed_utf8_runner_result_cannot_establish_version_or_readiness(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    output = tmp_path / "probe.json"
    runner = FakeRunner({
        ("--no-auto-update", "--version"): {"malformed_utf8": True},
    })

    assert main(
        ["probe", "--output", str(output)],
        runner=runner,
        locator=lambda: str(executable),
        environ={"PATH": "safe"},
    ) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["version_state"] == "unavailable"
    assert payload["provider_readiness"] == "needs_canary"


def test_run_command_interrupt_still_reaps_owned_process(monkeypatch):
    process = ControlledProcess(interrupt_on_poll=True)
    monkeypatch.setattr(grok_build.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        grok_build._run_command(
            ["fake"], env={}, timeout_seconds=1.0, cleanup_timeout_seconds=0.01
        )

    assert process.terminate_calls == 1
    assert process.returncode == -15
    assert process.wait_timeouts == [0.01]


def test_run_command_start_failure_does_not_attempt_cleanup_of_unowned_process(monkeypatch):
    starts = []

    def fail_start(*args, **kwargs):
        starts.append((args, kwargs))
        raise OSError("start failed")

    monkeypatch.setattr(grok_build.subprocess, "Popen", fail_start)
    with pytest.raises(OSError, match="start failed"):
        grok_build._run_command(["fake"], env={})
    assert len(starts) == 1


def test_cleanup_failure_is_terminal_and_stops_later_probe_commands(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    output = tmp_path / "must-not-exist.json"
    calls = []

    def ambiguous_cleanup(argv, *, env):
        calls.append((argv, env))
        raise grok_build.ProbeCleanupError("probe cleanup failed")

    with pytest.raises(grok_build.ProbeCleanupError, match="cleanup"):
        main(
            ["probe", "--output", str(output)],
            runner=ambiguous_cleanup,
            locator=lambda: str(executable),
            environ={"PATH": "safe"},
        )

    assert len(calls) == 1
    assert not output.exists()


def test_run_command_cleanup_failure_is_specific_after_terminate_and_kill(monkeypatch):
    process = ControlledProcess(
        terminate_wait_succeeds=False,
        kill_wait_succeeds=False,
    )
    monkeypatch.setattr(grok_build.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(grok_build.ProbeCleanupError, match="cleanup"):
        grok_build._run_command(
            ["fake"], env={}, timeout_seconds=0.0, cleanup_timeout_seconds=0.01
        )

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_timeouts == [0.01, 0.01]


def test_reader_thread_cleanup_ambiguity_is_terminal(monkeypatch):
    process = ControlledProcess()
    release = threading.Event()
    readers = []

    def blocked_reader(*_args):
        readers.append(threading.current_thread())
        release.wait()

    monkeypatch.setattr(grok_build.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(grok_build, "_read_pipe", blocked_reader)
    try:
        with pytest.raises(grok_build.ProbeCleanupError, match="reader cleanup"):
            grok_build._run_command(
                ["fake"], env={}, timeout_seconds=0.0, cleanup_timeout_seconds=0.01
            )
    finally:
        release.set()
        for reader in readers:
            reader.join(timeout=1.0)


def test_reader_join_failure_is_terminal_and_still_closes_owned_pipes(monkeypatch):
    process = ControlledProcess()
    process.returncode = 0

    class FailingJoinThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            raise RuntimeError("join failed")

        def is_alive(self):
            return False

    monkeypatch.setattr(grok_build.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(grok_build.threading, "Thread", FailingJoinThread)

    with pytest.raises(grok_build.ProbeCleanupError, match="reader cleanup"):
        grok_build._run_command(
            ["fake"], env={}, timeout_seconds=1.0, cleanup_timeout_seconds=0.01
        )

    assert process.stdout.closed is True
    assert process.stderr.closed is True


@pytest.mark.parametrize("path_like", ["C:/Users/private", r"C:\Users\private"])
def test_model_catalog_rejects_windows_absolute_drive_paths(path_like: str):
    assert parse_model_catalog(f"{path_like}\tPrivate\n") == ()
    assert parse_model_catalog("provider/model:2028@preview\tOpaque\n") == (
        {"value": "provider/model:2028@preview", "label": "Opaque"},
    )


def test_probe_uses_six_no_update_commands_and_writes_only_categorical_schema(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"test grok executable")
    output = tmp_path / "probe.json"
    runner = FakeRunner()
    parent_env = {
        "PATH": "safe-path",
        "SystemRoot": "safe-system-root",
        "USERPROFILE": "safe-home",
        "TEMP": "safe-temp",
        "XAI_API_KEY": "must-not-cross-boundary",
        "UNRELATED_SECRET": "must-not-cross-boundary",
    }

    assert main(
        ["probe", "--output", str(output)],
        runner=runner,
        locator=lambda: str(executable),
        environ=parent_env,
    ) == 0

    resolved = str(executable.resolve())
    assert [call[0] for call in runner.calls] == [
        (resolved, *suffix) for suffix in AUTHORIZED_ARGV
    ]
    assert parent_env["XAI_API_KEY"] == "must-not-cross-boundary"
    for _, child_env in runner.calls:
        assert child_env["GROK_DISABLE_AUTOUPDATER"] == "1"
        assert set(child_env) == {
            "PATH", "SYSTEMROOT", "USERPROFILE", "TEMP", "GROK_DISABLE_AUTOUPDATER"
        }
        assert "XAI_API_KEY" not in child_env
        assert "UNRELATED_SECRET" not in child_env

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) == EXPECTED_INSTALLED_KEYS
    assert payload == {
        "schema_version": 1,
        "pair_state": "observed",
        "version_state": "recognized",
        "help_state": "recognized",
        "catalog_state": "available",
        "extensions_discovered": "none",
        "provider_key_environment_omitted": True,
        "cached_native_login": "not_exposed",
        "no_extra_spend": "not_exposed",
        "builtin_tool_inventory": "not_exposed",
        "provider_readiness": "not_authorized",
    }
    serialized = output.read_text(encoding="utf-8")
    assert serialized == json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert resolved not in serialized
    assert "must-not-cross-boundary" not in serialized
    for forbidden in (
        "executable_sha256", "pair_key", "mcp_count", "hook_count", "plugin_count",
        "builtin_tool_names", "grok 1.2.3", "raw", "stdout", "stderr", "account",
    ):
        assert forbidden not in serialized.casefold()


def test_model_catalog_accepts_public_single_id_lines_but_not_headers():
    assert parse_model_catalog("grok-4-fast\ngrok-5\tGrok 5\n") == (
        {"value": "grok-4-fast", "label": "grok-4-fast"},
        {"value": "grok-5", "label": "Grok 5"},
    )
    assert parse_model_catalog("Models\ngrok-4\n") == ()


@pytest.mark.parametrize(
    ("behavior", "degraded_field"),
    [
        ({("--no-auto-update", "models"): {"timed_out": True}}, "catalog_state"),
        ({("--no-auto-update", "inspect", "--json"): {"returncode": 3}}, "extensions_discovered"),
        ({("--no-auto-update", "--help"): {"overflow": True}}, "help_state"),
        ({("--no-auto-update", "models"): {"stdout": "x" * (1024 * 1024 + 1)}}, "catalog_state"),
    ],
)
def test_probe_runs_all_six_commands_once_and_categorizes_runner_failures(
    tmp_path: Path, behavior, degraded_field: str
):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    output = tmp_path / "blocked.json"
    runner = FakeRunner(behavior)

    assert main(
        ["probe", "--output", str(output)],
        runner=runner,
        locator=lambda: str(executable),
        environ={"PATH": "safe"},
    ) == 0

    assert [call[0][1:] for call in runner.calls] == list(AUTHORIZED_ARGV)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) == EXPECTED_INSTALLED_KEYS
    assert payload[degraded_field] == "unavailable"
    assert payload["provider_readiness"] == "needs_canary"
    assert "stderr" not in output.read_text(encoding="utf-8").casefold()


def test_probe_categorizes_locator_path_drift_without_emitting_either_path(tmp_path: Path):
    first = tmp_path / "first" / "grok.exe"
    second = tmp_path / "second" / "grok.exe"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    locations = iter((str(first), str(second)))
    output = tmp_path / "drift.json"

    assert main(
        ["probe", "--output", str(output)],
        runner=FakeRunner(),
        locator=lambda: next(locations),
        environ={"PATH": "safe"},
    ) == 0
    text = output.read_text(encoding="utf-8")
    assert json.loads(text)["pair_state"] == "drifted"
    assert str(first.resolve()) not in text
    assert str(second.resolve()) not in text


def test_probe_categorizes_executable_content_drift_after_all_commands(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"before")

    def mutate(call_count, _suffix):
        if call_count == 6:
            executable.write_bytes(b"after")

    runner = FakeRunner(on_call=mutate)
    output = tmp_path / "drift.json"
    assert main(
        ["probe", "--output", str(output)],
        runner=runner,
        locator=lambda: str(executable),
        environ={"PATH": "safe"},
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["pair_state"] == "drifted"


def test_probe_discards_unknown_inspect_structure_values_and_counts(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    raw_value = "C:/Users/private/AppData/secret"
    runner = FakeRunner({
        ("--no-auto-update", "inspect", "--json"): {
            "stdout": json.dumps({"settings": {"private_path": raw_value}, "hooks": []})
        }
    })
    output = tmp_path / "blocked.json"

    assert main(
        ["probe", "--output", str(output)],
        runner=runner,
        locator=lambda: str(executable),
        environ={"PATH": "safe"},
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["extensions_discovered"] == "unavailable"
    assert payload["provider_readiness"] == "needs_canary"
    serialized = output.read_text(encoding="utf-8")
    assert raw_value not in serialized
    assert "Users" not in serialized
    assert "AppData" not in serialized


@pytest.mark.parametrize(("servers", "hooks", "plugins", "state"), [
    ([], [], [], "none"),
    ([{"private": "value"}], [{"private": "value"}], [{"private": "value"}], "present"),
])
def test_public_inspect_persists_only_extension_category(
    tmp_path: Path, servers, hooks, plugins, state: str
):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    public_inspect = {
        "mcpServers": servers,
        "hooks": hooks,
        "plugins": plugins,
        "permissions": {"mode": "dontAsk"},
        "loginPolicy": {"method": "not-an-attestation"},
    }
    output = tmp_path / "blocked.json"

    assert main(
        ["probe", "--output", str(output)],
        runner=FakeRunner({
            ("--no-auto-update", "inspect", "--json"): {"stdout": json.dumps(public_inspect)}
        }),
        locator=lambda: str(executable),
        environ={"PATH": "safe", "XAI_API_KEY": "must-be-omitted"},
    ) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["extensions_discovered"] == state
    assert payload["builtin_tool_inventory"] == "not_exposed"
    assert payload["provider_readiness"] == (
        "not_authorized" if state == "none" else "needs_canary"
    )
    encoded = output.read_text(encoding="utf-8")
    assert "not-an-attestation" not in encoded
    assert "must-be-omitted" not in encoded


def test_probe_does_not_promote_synthetic_login_field_to_installed_auth_evidence(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    inspect = json.loads(_read("documented-inspect.json"))
    inspect["cached_native_login"] = False
    output = tmp_path / "blocked.json"

    assert main(
        ["probe", "--output", str(output)],
        runner=FakeRunner({("--no-auto-update", "inspect", "--json"): {"stdout": json.dumps(inspect)}}),
        locator=lambda: str(executable),
        environ={"PATH": "safe", "XAI_API_KEY": "omitted"},
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["cached_native_login"] == "not_exposed"
    assert payload["no_extra_spend"] == "not_exposed"
    assert payload["provider_key_environment_omitted"] is True


def test_probe_blocks_malformed_catalog_without_persisting_it(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    output = tmp_path / "blocked.json"
    secret_like_raw = "Models\nC:/Users/private/AppData/catalog\n"

    assert main(
        ["probe", "--output", str(output)],
        runner=FakeRunner({("--no-auto-update", "models"): {"stdout": secret_like_raw}}),
        locator=lambda: str(executable),
        environ={"PATH": "safe"},
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["catalog_state"] == "unavailable"
    assert payload["provider_readiness"] == "needs_canary"
    assert "private" not in output.read_text(encoding="utf-8")


def test_categorical_validation_rejects_extra_or_non_categorical_values(tmp_path: Path):
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    output = tmp_path / "candidate.json"
    assert main(
        ["probe", "--output", str(output)],
        runner=FakeRunner(),
        locator=lambda: str(executable),
        environ={"PATH": "safe"},
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="schema"):
        validate_sanitized_output({**payload, "extra": True})
    modified = {**payload, "pair_state": "C:/Users/name/private"}
    with pytest.raises(ValueError, match="schema"):
        validate_sanitized_output(modified)


def test_probe_json_write_is_atomic_on_replace_failure(tmp_path: Path, monkeypatch):
    from spikes.phase0a import core

    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"binary")
    output = tmp_path / "candidate.json"
    output.write_text("original", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(core.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        main(
            ["probe", "--output", str(output)],
            runner=FakeRunner(),
            locator=lambda: str(executable),
            environ={"PATH": "safe"},
        )
    assert output.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob("candidate.json.*.tmp")) == []


def _observation(*, help_text: str | None = None, inspect=None, models=None):
    return GrokCliObservation(
        version=parse_version(_read("documented-version.txt")),
        help_contract=parse_help_contract(help_text or _read("documented-help.txt")),
        inspect_summary=parse_inspect_summary(
            json.loads(_read("documented-inspect.json")) if inspect is None else inspect
        ),
        models=parse_model_catalog(_read("documented-models.txt") if models is None else models),
    )


def test_documented_synthetic_contract_adjudicates_candidate_writer():
    observation = _observation()
    assert observation.version == "grok 1.2.3 (abcdef0123)"
    assert observation.help_contract == GrokHelpContract(*(True,) * 10)
    assert observation.inspect_summary == {
        "mcp_count": 0,
        "hook_count": 0,
        "plugin_count": 0,
        "cached_native_login": True,
        "api_key_override": False,
        "builtin_tool_names": ("edit", "read", "write"),
    }
    assert observation.models == (
        {"value": "grok-4", "label": "Grok 4"},
        {"value": "grok-future:experimental@2027.0", "label": "Future opaque model"},
    )
    assert adjudicate_no_model_contract(observation) == {
        "read_review": "pass",
        "bounded_writer": "candidate",
    }


@pytest.mark.parametrize(
    "removed",
    [
        "  agent        Run Grok without the interactive UI",
        "  stdio     Run the agent over stdio",
        "  --no-leader             Disable leader mode.",
        "    --no-subagents          Disable nested subagents.",
        "   --disable-web-search    Disable web search.",
        "  --deny <TOOL>            Deny a tool.",
        "     --disallowed-tools <TOOLS>  Disallow tools.",
        "  --permission-mode <MODE> Set the permission mode.",
        "  -m, --model <MODEL>     Select a model.",
        "    --reasoning-effort <EFFORT>  Set reasoning effort.",
        "   --cwd <CWD>             Set the working directory.",
    ],
)
def test_help_contract_fails_closed_when_an_exact_surface_is_missing(removed: str):
    assert parse_help_contract(_read("documented-help.txt").replace(removed, "")) != GrokHelpContract(*(True,) * 10)


def test_help_contract_requires_separate_agent_and_stdio_tokens_and_rejects_oversized_input():
    assert parse_help_contract(_read("documented-help.txt")).agent_stdio is True
    assert parse_help_contract("x" * (1024 * 1024 + 1)) == GrokHelpContract()


def test_help_contract_does_not_infer_options_from_prose_or_interactive_only_permission():
    prose = "This prose mentions --deny, agent, and stdio, but exposes no help token.\n"
    interactive = "  --interactive\n"
    contract = parse_help_contract(prose + interactive)
    assert contract.deny is False
    assert contract.agent_stdio is False
    assert contract.permission_mode is False


def test_help_contract_rejects_all_tokens_when_they_are_on_the_wrong_surface():
    help_text = _read("documented-help.txt")
    help_text = help_text.replace("  --no-leader             Disable leader mode.\n", "")
    help_text = help_text.replace(
        "    --no-subagents          Disable nested subagents.\n",
        "  --no-leader             Disable leader mode.\n    --no-subagents          Disable nested subagents.\n",
    )
    assert parse_help_contract(help_text) != GrokHelpContract(*(True,) * 10)


def test_help_contract_requires_the_exact_stdio_usage_surface():
    help_text = _read("documented-help.txt").replace(
        "Usage: grok agent stdio [OPTIONS]", "Usage: grok agent [OPTIONS] [COMMAND]"
    )
    assert parse_help_contract(help_text) != GrokHelpContract(*(True,) * 10)


def test_help_contract_rejects_ambiguous_usage_identity():
    help_text = _read("documented-help.txt").replace(
        "Usage: grok [OPTIONS] [PROMPT] [COMMAND]",
        "Usage: wrong\nUsage: grok [OPTIONS] [PROMPT] [COMMAND]",
    )
    assert parse_help_contract(help_text) != GrokHelpContract(*(True,) * 10)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bad",
        "1.2.3 (Grok Build)",
        "grok 1.2.3 (abcdef0123)\x00",
        pytest.param("x" * (1024 * 1024 + 1), id="oversized"),
    ],
)
def test_version_rejects_empty_malformed_control_or_oversized_input(value: str):
    assert parse_version(value) == ""


def test_version_enforces_public_byte_and_component_boundaries():
    assert parse_version(f"grok {'9' * 16}.{'8' * 16}.{'7' * 16} ({'a' * 64})")
    assert parse_version(f"grok {'9' * 17}.2.3 (abcdef0)") == ""
    assert parse_version(f"grok 1.2.3 ({'a' * 65})") == ""
    assert parse_version("grok 1.2.3 (abcdef0)\u0085") == ""


def test_lone_surrogate_and_deep_json_fail_closed_without_throwing():
    assert parse_version("\ud800") == ""
    assert parse_help_contract("\ud800") == GrokHelpContract()
    assert parse_model_catalog("grok-4\tLabel\ud800") == ()
    nested = []
    for _ in range(1100):
        nested = [nested]
    assert parse_inspect_summary(nested) == {}


def test_inspect_summary_rejects_malformed_and_sensitive_context():
    assert parse_inspect_summary("{") == {}
    assert parse_inspect_summary("x" * (1024 * 1024 + 1)) == {}
    documented = json.loads(_read("documented-inspect.json"))
    for key, value in (
        ("credential", "not-a-secret"),
        ("private_path", "C:/private"),
        ("raw_env", {"NAME": "value"}),
    ):
        candidate = {**documented, key: value}
        assert parse_inspect_summary(candidate) == {}


@pytest.mark.parametrize(
    "missing",
    ["mcp_count", "hook_count", "plugin_count", "builtin_tool_names_complete"],
)
def test_inspect_summary_requires_exact_empty_context_and_complete_tools(missing: str):
    documented = json.loads(_read("documented-inspect.json"))
    documented.pop(missing)
    assert parse_inspect_summary(documented) == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mcp_count", 1),
        ("hook_count", 1),
        ("plugin_count", 1),
        ("mcp_count", False),
        ("mcp_count", 0.0),
        ("builtin_tool_names_complete", False),
    ],
)
def test_inspect_summary_rejects_nonempty_context_or_incomplete_tools(field: str, value):
    documented = json.loads(_read("documented-inspect.json"))
    documented[field] = value
    assert parse_inspect_summary(documented) == {}


def test_inspect_summary_rejects_unsorted_or_renamed_surface():
    documented = json.loads(_read("documented-inspect.json"))
    documented["builtin_tool_names"] = ["write", "read"]
    assert parse_inspect_summary(documented) == {}
    documented = json.loads(_read("documented-inspect.json"))
    documented["native_login"] = documented.pop("cached_native_login")
    assert parse_inspect_summary(documented) == {}


@pytest.mark.parametrize(
    "catalog",
    [
        "grok-4\tOne\ngrok-4\tTwo\n",
        "grok-4\x00\tOne\n",
        "Models\n",
        pytest.param("x" * (1024 * 1024 + 1), id="oversized"),
    ],
)
def test_model_catalog_rejects_duplicates_control_malformed_or_oversized_input(catalog: str):
    assert parse_model_catalog(catalog) == ()


def test_model_catalog_preserves_future_opaque_ids_without_allowlist():
    assert parse_model_catalog("future/model:2028@preview\tFuture\n") == (
        {"value": "future/model:2028@preview", "label": "Future"},
    )


def test_model_catalog_enforces_utf8_bounds_and_unicode_controls():
    assert parse_model_catalog(f"{'x' * 256}\t{'y' * 256}\n") == (
        {"value": "x" * 256, "label": "y" * 256},
    )
    assert parse_model_catalog(f"grok-4\t{'😀' * 256}\n") == ()
    assert parse_model_catalog("grok-4\tLabel\u0085\n") == ()


def test_parsed_evidence_mappings_are_immutable_and_keep_their_adjudication():
    observation = _observation()
    with pytest.raises(TypeError):
        observation.inspect_summary["mcp_count"] = 1
    with pytest.raises(TypeError):
        observation.models[0]["label"] = "changed"
    assert adjudicate_no_model_contract(observation) == {
        "read_review": "pass",
        "bounded_writer": "candidate",
    }


def test_malformed_direct_observations_fail_closed_without_throwing():
    valid = _observation()
    bad_inspect = GrokCliObservation(
        valid.version, valid.help_contract, [], valid.models
    )
    bad_models = GrokCliObservation(
        valid.version, valid.help_contract, valid.inspect_summary, (None,)
    )
    assert adjudicate_no_model_contract(bad_inspect) == {
        "read_review": "blocked",
        "bounded_writer": "blocked",
    }
    assert adjudicate_no_model_contract(bad_models) == {
        "read_review": "blocked",
        "bounded_writer": "blocked",
    }


def test_model_catalog_rejects_more_than_128_native_ids():
    catalog = "".join(f"grok-{number}\tModel {number}\n" for number in range(129))
    assert parse_model_catalog(catalog) == ()


def test_unknown_observation_blocks_all_decisions():
    assert GrokCliObservation.unknown() == GrokCliObservation("", GrokHelpContract(), {}, ())
    assert adjudicate_no_model_contract(GrokCliObservation.unknown()) == {
        "read_review": "blocked",
        "bounded_writer": "blocked",
    }


def test_invalid_inspect_or_model_contract_blocks_all_decisions():
    assert adjudicate_no_model_contract(_observation(inspect={})) == {
        "read_review": "blocked",
        "bounded_writer": "blocked",
    }
    assert adjudicate_no_model_contract(_observation(models="")) == {
        "read_review": "blocked",
        "bounded_writer": "blocked",
    }
