from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib

import pytest

import subagent_harness_mcp.adapters.grok_build as grok_module

from subagent_harness_mcp.adapters.acp_stdio import AcpProcessError, AcpStdioProcess
from subagent_harness_mcp.adapters.base import (
    AdapterContextRequest,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSpawnRequest,
    CanaryAdapter,
    CanaryRequest,
)
from subagent_harness_mcp.adapters.grok_build import (
    GrokBinding,
    GrokBindingIncompatible,
    GrokBuildAdapter,
    GrokCliContract,
    GrokFilesystemBridge,
    GrokInspectObservation,
    GrokPermissionError,
    GrokSessionToolAttestation,
    locate_grok_binding,
)
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.contracts import (
    ADAPTER_API_VERSION,
    ActionRequest,
    ServiceError,
    SendRequest,
    SpawnRequest,
    TaskPacket,
    WaitRequest,
    WaitTarget,
)
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore


FAKE_ACP = (
    Path(__file__).parents[1] / "fixtures" / "fake_grok_acp.py"
).resolve(strict=True)
_LIFECYCLE_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_LEGACY_ISOLATION_CONFIG = b"""[compat.cursor]
skills = false
rules = false
agents = false
mcps = false
hooks = false
sessions = false

[compat.claude]
skills = false
rules = false
agents = false
mcps = false
hooks = false
sessions = false

[compat.codex]
sessions = false

[claude_compat]
imported = true

[skills]
ignore = ["~/.agents"]
"""


def _isolation_config(home: Path) -> bytes:
    return grok_module._render_isolation_config(home.resolve())
_COMPATIBILITY_CELLS = (
    *(('cursor', surface) for surface in ('skills', 'rules', 'agents', 'mcps', 'hooks', 'sessions')),
    *(('claude', surface) for surface in ('skills', 'rules', 'agents', 'mcps', 'hooks', 'sessions')),
    ('codex', 'sessions'),
)


def _help_text(marker: str = "") -> str:
    return "\n".join(
        (
            "Usage: grok [OPTIONS] [PROMPT] [COMMAND]",
            "--no-auto-update",
            "--cwd <PATH>",
            "--model <MODEL>",
            "--reasoning-effort <EFFORT>",
            "--permission-mode <MODE>",
            "--disable-web-search",
            "--no-subagents",
            "Usage: grok agent [OPTIONS] [COMMAND]",
            "--no-leader",
            "Usage: grok agent stdio [OPTIONS]",
            marker,
        )
    )


def _contract(*, version: str = "grok 1.2.3 (abcdef0)", marker: str = "") -> GrokCliContract:
    return GrokCliContract(version=version, help_text=_help_text(marker))


def _binding(
    tmp_path: Path,
    *,
    name: str = "grok.exe",
    marker: str = "",
    version: str = "grok 1.2.3 (abcdef0)",
) -> GrokBinding:
    executable = tmp_path / name
    if not executable.exists():
        executable.write_bytes(b"synthetic-grok")
    binding = locate_grok_binding(
        executable_resolver=lambda requested: str(executable) if requested == "grok" else None,
        contract_reader=lambda resolved: _contract(version=version, marker=marker),
    )
    assert binding is not None
    return binding


def _binding_at(executable: Path) -> GrokBinding:
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"synthetic-grok")
    binding = locate_grok_binding(
        executable_resolver=lambda _requested: str(executable),
        contract_reader=lambda _resolved: _contract(),
    )
    assert binding is not None
    return binding


def _inspect(
    binding: GrokBinding,
    workspace: Path,
    *,
    mcp_servers: tuple[str, ...] = (),
    hooks: tuple[str, ...] = (),
    plugins: tuple[str, ...] = (),
    compatibility_mcp_servers: tuple[str, ...] = (),
    permission_keys: tuple[str, ...] = ("allow", "deny"),
) -> GrokInspectObservation:
    config_path = (
        binding.executable_path.parent
        / "local"
        / "SubagentMCP"
        / "grok-build"
        / "home"
        / "config.toml"
    )
    return GrokInspectObservation(
        pair_key=binding.pair_key,
        workspace_path=str(workspace.resolve()),
        mcp_servers=mcp_servers,
        hooks=hooks,
        plugins=plugins,
        compatibility_mcp_servers=compatibility_mcp_servers,
        builtin_tool_inventory="not_exposed",
        permission_keys=permission_keys,
        permission_rules=("workspace",),
        permission_modes=("dontAsk",),
        api_key_auth_disabled=True,
        config_source_layer_count=1,
        config_source_path=str(config_path),
        compatibility_isolated=True,
        permission_sources_isolated=True,
        external_surfaces_empty=True,
        builtin_agent_count=3,
        project_trusted=True,
        project_root=None,
    )


def _request(workspace: Path, **overrides: object) -> AdapterContextRequest:
    values: dict[str, object] = {
        "runtime_id": "grok-build",
        "variant_id": "configured",
        "model": "future/model:opaque@1",
        "reasoning": {"effort": "highest-native"},
        "workspace_path": str(workspace.resolve()),
        "workspace_key": "workspace-key-1",
        "transport": "native-acp",
        "permissions": ("repo_read",),
        "context_policy_id": "declared-native",
        "permission_policy_id": "bounded",
        "write_set": (),
    }
    values.update(overrides)
    return AdapterContextRequest(**values)  # type: ignore[arg-type]


def _adapter(
    tmp_path: Path,
    binding: GrokBinding,
    *,
    catalog: tuple[dict[str, str], ...] = (),
    inspect: GrokInspectObservation | None = None,
    environment: dict[str, str] | None = None,
) -> GrokBuildAdapter:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    source = {
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "USERPROFILE": str(tmp_path / "user"),
        **(environment or {}),
    }
    return GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: catalog,
        inspect_reader=lambda _binding, _workspace: inspect
        or _inspect(binding, workspace),
        platform="win32",
        environment=source,
        data_root=tmp_path / "local" / "SubagentMCP",
    )


def test_binding_uses_dynamic_canonical_identity_and_content_sha_not_mtime(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "bin" / "grok.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"AAAAAAAA")
    timestamp = executable.stat().st_mtime_ns
    requested: list[str] = []

    def resolve(name: str) -> str | None:
        requested.append(name)
        return str(executable.parent / "." / executable.name)

    first = locate_grok_binding(
        executable_resolver=resolve,
        contract_reader=lambda _path: _contract(),
    )
    executable.write_bytes(b"BBBBBBBB")
    os.utime(executable, ns=(timestamp, timestamp))
    second = locate_grok_binding(
        executable_resolver=resolve,
        contract_reader=lambda _path: _contract(),
    )

    assert first is not None and second is not None
    assert requested == ["grok", "grok"]
    assert first.executable_path == executable.resolve()
    assert first.executable_sha256 != second.executable_sha256
    assert first.pair_key != second.pair_key


def test_binding_pair_changes_for_path_version_and_help_drift(tmp_path: Path) -> None:
    first_executable = tmp_path / "one" / "grok.exe"
    second_executable = tmp_path / "two" / "grok.exe"
    first_executable.parent.mkdir()
    second_executable.parent.mkdir()
    first_executable.write_bytes(b"same")
    second_executable.write_bytes(b"same")

    def bind(path: Path, contract: GrokCliContract) -> GrokBinding:
        result = locate_grok_binding(
            executable_resolver=lambda _name: str(path),
            contract_reader=lambda _path: contract,
        )
        assert result is not None
        return result

    baseline = bind(first_executable, _contract())
    path_drift = bind(second_executable, _contract())
    version_drift = bind(first_executable, _contract(version="grok 1.2.4 (abcdef1)"))
    help_drift = bind(first_executable, _contract(marker="new documented option"))

    assert len(
        {
            baseline.pair_key,
            path_drift.pair_key,
            version_drift.pair_key,
            help_drift.pair_key,
        }
    ) == 4
    assert baseline.capability_hash != help_drift.capability_hash


def test_binding_and_probe_accept_official_release_channel_without_normalizing_identity(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"native")

    def bind(version: str) -> GrokBinding:
        binding = locate_grok_binding(
            executable_resolver=lambda _name: str(executable),
            contract_reader=lambda _path: _contract(version=version),
        )
        assert binding is not None
        return binding

    plain = bind("grok 1.0.5 (5115b46bc9)")
    stable = bind("grok 1.0.5 (5115b46bc9) [stable]")
    nightly = bind("grok 1.0.5 (5115b46bc9) [nightly]")

    assert stable.version == "grok 1.0.5 (5115b46bc9) [stable]"
    assert len({plain.pair_key, stable.pair_key, nightly.pair_key}) == 3
    observed = asyncio.run(_adapter(tmp_path, stable).probe())
    assert observed.state == "needs_canary"
    assert observed.details["harness_version"] == stable.version
    assert observed.details["pair_key"] == stable.pair_key


@pytest.mark.parametrize(
    "version",
    (
        "grok 1.0.5 (5115b46bc9)[stable]",
        "grok 1.0.5 (5115b46bc9) []",
        "grok 1.0.5 (5115b46bc9) [Stable]",
        "grok 1.0.5 (5115b46bc9) [-stable]",
        "grok 1.0.5 (5115b46bc9) [stable channel]",
        "grok 1.0.5 (5115b46bc9) [stable\tchannel]",
        "grok 1.0.5 (5115b46bc9) [stable!]",
        f"grok 1.0.5 (5115b46bc9) [{'a' * 33}]",
    ),
)
def test_binding_rejects_malformed_or_unbounded_release_channel(
    tmp_path: Path, version: str
) -> None:
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"native")

    with pytest.raises(GrokBindingIncompatible, match="version output"):
        locate_grok_binding(
            executable_resolver=lambda _name: str(executable),
            contract_reader=lambda _path: _contract(version=version),
        )


def test_binding_accepts_hidden_but_supported_no_auto_update_flag(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"native")
    help_text = _help_text().replace("--no-auto-update\n", "", 1)

    binding = locate_grok_binding(
        executable_resolver=lambda _name: str(executable),
        contract_reader=lambda _path: GrokCliContract(
            version="grok 1.0.5 (5115b46bc9)",
            help_text=help_text,
        ),
    )

    assert binding is not None


def test_binding_rejects_executable_content_drift_during_contract_read(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"AAAAAAAA")
    timestamp = executable.stat().st_mtime_ns

    def mutate_during_read(_path: Path) -> GrokCliContract:
        executable.write_bytes(b"BBBBBBBB")
        os.utime(executable, ns=(timestamp, timestamp))
        return _contract()

    with pytest.raises(GrokBindingIncompatible, match="changed"):
        locate_grok_binding(
            executable_resolver=lambda _name: str(executable),
            contract_reader=mutate_during_read,
        )


@pytest.mark.parametrize("suffix", (".cmd", ".bat", ".ps1", ".py", ""))
def test_binding_rejects_non_native_executable_shims(
    tmp_path: Path, suffix: str
) -> None:
    executable = tmp_path / f"grok{suffix}"
    executable.write_bytes(b"shim")

    with pytest.raises(GrokBindingIncompatible, match="native .exe"):
        locate_grok_binding(
            executable_resolver=lambda _name: str(executable),
            contract_reader=lambda _path: _contract(),
        )


def test_binding_rejects_relative_and_repository_local_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    executable = repository / "grok.exe"
    executable.write_bytes(b"native")
    monkeypatch.chdir(repository)

    with pytest.raises(GrokBindingIncompatible):
        locate_grok_binding(
            executable_resolver=lambda _name: "grok.exe",
            contract_reader=lambda _path: _contract(),
        )
    with pytest.raises(GrokBindingIncompatible, match="repository-local"):
        locate_grok_binding(
            executable_resolver=lambda _name: str(executable.resolve()),
            contract_reader=lambda _path: _contract(),
        )


def test_binding_pair_includes_adapter_and_adapter_api_versions(tmp_path: Path) -> None:
    executable = tmp_path / "grok.exe"
    executable.write_bytes(b"native")
    identity = grok_module._file_identity(executable.resolve())
    capability_hash = "a" * 64

    baseline = grok_module._grok_pair_key(
        executable.resolve(),
        identity,
        "grok 1.2.3 (abcdef0)",
        capability_hash,
        adapter_version="1.0.0",
        adapter_api_version="1.0.0",
    )
    adapter_drift = grok_module._grok_pair_key(
        executable.resolve(),
        identity,
        "grok 1.2.3 (abcdef0)",
        capability_hash,
        adapter_version="1.0.1",
        adapter_api_version="1.0.0",
    )
    api_drift = grok_module._grok_pair_key(
        executable.resolve(),
        identity,
        "grok 1.2.3 (abcdef0)",
        capability_hash,
        adapter_version="1.0.0",
        adapter_api_version="2.0.0",
    )

    assert len({baseline, adapter_drift, api_drift}) == 3


def test_binding_pair_uses_stable_content_identity_across_python_runtimes(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "grok.exe").resolve()
    other_executable = (tmp_path / "other" / "grok.exe").resolve()
    identity = {
        "path": os.path.normcase(str(executable)),
        "device": 11,
        "inode": 22,
        "size": 33,
        "sha256": "a" * 64,
    }

    def pair(
        *,
        path: Path = executable,
        current_identity: Mapping[str, object] = identity,
        version: str = "grok 1.2.3 (abcdef0)",
        capability_hash: str = "b" * 64,
    ) -> str:
        return grok_module._grok_pair_key(
            path,
            current_identity,
            version,
            capability_hash,
        )

    baseline = pair()
    runtime_identity = {**identity, "device": 111, "inode": 222}
    assert pair(current_identity=runtime_identity) == baseline
    assert len(
        {
            baseline,
            pair(path=other_executable),
            pair(current_identity={**identity, "size": 34}),
            pair(current_identity={**identity, "sha256": "c" * 64}),
            pair(version="grok 1.2.4 (abcdef1)"),
            pair(capability_hash="d" * 64),
        }
    ) == 6


def test_manifest_is_exact_and_contains_no_model_ids(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))

    assert adapter.manifest.to_dict() == {
        "adapter_api_version": ADAPTER_API_VERSION,
        "runtime_id": "grok-build",
        "provider_id": "xai",
        "harness_id": "grok-build",
        "display_name": "Grok Build",
        "adapter_version": "1.0.27",
        "supported_platforms": ["win32"],
        "supported_transports": ["native-acp"],
        "capabilities": ["interrupt", "session", "workspace"],
        "semantic_permissions": ["repo_read", "workspace_write"],
        "reasoning_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["effort"],
            "properties": {
                "effort": {"type": "string", "minLength": 1, "maxLength": 64}
            },
        },
        "model_schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "Exact bounded Grok Build model ID; catalog-backed when available.",
        },
        "max_write_roots_per_session": 32,
        "write_root_mode": "path-prefix",
    }


def test_binding_probe_classifies_platform_missing_incompatible_and_unknown_quota(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)

    def not_called() -> None:
        raise AssertionError("locator called")

    incompatible = GrokBuildAdapter(
        binding_locator=not_called,
        catalog_reader=lambda _binding: (),
        inspect_reader=lambda _binding, _workspace: None,
        platform="linux",
    )
    missing = GrokBuildAdapter(
        binding_locator=lambda: None,
        catalog_reader=lambda _binding: (),
        inspect_reader=lambda _binding, _workspace: None,
        platform="win32",
    )
    present = _adapter(tmp_path, binding)

    assert asyncio.run(incompatible.probe()).state == "incompatible"
    assert asyncio.run(missing.probe()).state == "not_installed"
    observed = asyncio.run(present.probe())
    assert observed.state == "needs_canary"
    assert observed.details == {
        "pair_key": binding.pair_key,
        "harness_version": binding.version,
        "transport": "native-acp",
        "cached_native_login": "not_exposed",
        "no_extra_spend": "not_exposed",
        "builtin_tool_inventory": "not_exposed",
        "provider_readiness": "needs_canary",
        "quota_state": "unknown",
    }


def test_binding_probe_timeout_keeps_event_loop_responsive(tmp_path: Path) -> None:
    release = threading.Event()

    def blocked_locator() -> GrokBinding | None:
        release.wait(timeout=1)
        return _binding(tmp_path)

    async def scenario() -> None:
        heartbeat = asyncio.Event()

        async def beat() -> None:
            await asyncio.sleep(0)
            heartbeat.set()

        async def release_after_guard() -> None:
            await asyncio.sleep(0.02)
            release.set()

        adapter = GrokBuildAdapter(
            binding_locator=blocked_locator,
            catalog_reader=lambda _binding: (),
            inspect_reader=lambda _binding, _workspace: None,
            platform="win32",
            binding_probe_timeout_seconds=0.01,
        )
        beat_task = asyncio.create_task(beat())
        release_task = asyncio.create_task(release_after_guard())
        result = await adapter.probe()
        await beat_task
        await release_task
        assert heartbeat.is_set()
        assert result.state == "recovery_required"
        assert result.details == {"code": "BINDING_PROBE_TIMEOUT"}

    asyncio.run(scenario())


class _FakePipe:
    def __init__(self, chunks: tuple[bytes, ...], stop: threading.Event) -> None:
        self._chunks = list(chunks)
        self._stop = stop
        self.closed = False

    def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        self._stop.wait(timeout=1)
        return b""

    def close(self) -> None:
        self.closed = True
        self._stop.set()


class _FakeOwnedProcess:
    def __init__(self, stdout_chunks: tuple[bytes, ...], stderr_chunks: tuple[bytes, ...]) -> None:
        self._stop = threading.Event()
        self.stdout = _FakePipe(stdout_chunks, self._stop)
        self.stderr = _FakePipe(stderr_chunks, self._stop)
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.returncode is None and not self._stop.wait(timeout=timeout):
            raise subprocess.TimeoutExpired("fake-grok", timeout)
        return 0 if self.returncode is None else self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15
        self._stop.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._stop.set()


def test_binding_owned_probe_runner_caps_combined_output_and_reaps_spewer() -> None:
    process = _FakeOwnedProcess(
        (b"x" * (768 * 1024),),
        (b"y" * (768 * 1024),),
    )

    result = grok_module._run_owned_command(
        ("C:\\tools\\grok.exe", "--no-auto-update", "--version"),
        env={},
        timeout_seconds=1,
        cleanup_timeout_seconds=0.1,
        process_factory=lambda *_args, **_kwargs: process,
    )

    assert result.overflow is True
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls >= 1
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize("cancelled", (False, True))
def test_binding_owned_probe_runner_reaps_blocker_on_timeout_or_cancel(
    cancelled: bool,
) -> None:
    process = _FakeOwnedProcess((), ())
    cancel = threading.Event()
    if cancelled:
        cancel.set()

    result = grok_module._run_owned_command(
        ("C:\\tools\\grok.exe", "--no-auto-update", "--version"),
        env={},
        timeout_seconds=0.01,
        cleanup_timeout_seconds=0.1,
        cancel_event=cancel,
        process_factory=lambda *_args, **_kwargs: process,
    )

    assert result.cancelled is cancelled
    assert result.timed_out is (not cancelled)
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls >= 1


def test_catalog_is_bounded_cached_by_pair_and_never_reused_across_drift(
    tmp_path: Path,
) -> None:
    first_binding = _binding(tmp_path, name="grok-one.exe")
    second_binding = _binding(tmp_path, name="grok-two.exe")
    current = [first_binding]
    responses: list[object] = [
        (
            {"value": "provider/future:model@1", "label": "Future One"},
            {"value": "strange+opaque/next", "label": "Next"},
        ),
        (),
        (),
    ]
    calls: list[str] = []

    def read_catalog(binding: GrokBinding) -> object:
        calls.append(binding.pair_key)
        return responses.pop(0)

    adapter = GrokBuildAdapter(
        binding_locator=lambda: current[0],
        catalog_reader=read_catalog,
        inspect_reader=lambda binding, workspace: _inspect(binding, Path(workspace)),
        platform="win32",
    )
    first = asyncio.run(adapter.model_catalog())
    cached = asyncio.run(adapter.model_catalog())
    unavailable_refresh = asyncio.run(adapter.model_catalog(refresh=True))
    current[0] = second_binding
    drifted = asyncio.run(adapter.model_catalog())

    assert [row["value"] for row in first] == [
        "provider/future:model@1",
        "strange+opaque/next",
    ]
    assert cached == unavailable_refresh == first
    assert drifted == ()
    assert calls == [first_binding.pair_key, first_binding.pair_key, second_binding.pair_key]


def test_catalog_reader_runs_off_event_loop(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    caller_thread = threading.get_ident()
    reader_threads: list[int] = []

    def reader(_binding: GrokBinding) -> tuple[dict[str, str], ...]:
        reader_threads.append(threading.get_ident())
        return ({"value": "opaque", "label": "Opaque"},)

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=reader,
        inspect_reader=lambda current, workspace: _inspect(current, Path(workspace)),
        platform="win32",
    )

    assert asyncio.run(adapter.model_catalog()) == ({"value": "opaque", "label": "Opaque"},)
    assert reader_threads and reader_threads[0] != caller_thread


def test_catalog_discards_output_and_cache_when_bound_executable_drifts(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "grok.exe"
    binding = _binding_at(executable)
    timestamp = executable.stat().st_mtime_ns

    def drift_then_return(_binding: GrokBinding) -> tuple[dict[str, str], ...]:
        executable.write_bytes(b"replaced-grok!")
        os.utime(executable, ns=(timestamp, timestamp))
        return ({"value": "must-not-cache", "label": "Unsafe"},)

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=drift_then_return,
        inspect_reader=lambda current, workspace: _inspect(current, Path(workspace)),
        platform="win32",
    )

    assert asyncio.run(adapter.model_catalog()) == ()
    assert adapter._catalog_cache == ()
    assert adapter._catalog_authoritative is False


def test_context_is_exact_hash_bound_and_builds_exact_argv_and_safe_env(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    environment = {
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "PATH": "synthetic-path",
        "SYSTEMROOT": "C:\\Windows",
        "USERNAME": "Example",
        "USERPROFILE": "C:\\Users\\Example",
        "SSL_CERT_FILE": "C:\\certs\\root.pem",
        "XAI_API_KEY": "must-not-leak",
        "GROK_API_KEY": "must-not-leak",
        "OPENROUTER_API_KEY": "must-not-leak",
        "XAI_AUTH_TOKEN": "must-not-leak",
        "GROK_BEARER_TOKEN": "must-not-leak",
        "GROK_FOLDER_TRUST": "0",
        "AWS_SECRET_ACCESS_KEY": "must-not-leak",
        "UNRELATED_SECRET": "must-not-leak",
    }
    adapter = _adapter(tmp_path, binding, environment=environment)
    assert asyncio.run(adapter.probe()).state == "needs_canary"

    request = _request(workspace)
    first = asyncio.run(adapter.resolve_context(request))
    second = asyncio.run(adapter.resolve_context(request))
    changed = asyncio.run(
        adapter.resolve_context(_request(workspace, reasoning={"effort": "other-native"}))
    )
    launch = adapter.launch_for(first)

    assert first == second
    assert first.context_hash != changed.context_hash
    assert first.requested_model == first.effective_model == "future/model:opaque@1"
    assert first.requested_reasoning == first.effective_reasoning == {
        "effort": "highest-native"
    }
    assert launch.binding == binding
    assert launch.workspace_path == str(workspace.resolve())
    assert launch.model == "future/model:opaque@1"
    assert launch.reasoning_effort == "highest-native"
    assert launch.permission_mode == "bypassPermissions"
    assert launch.write_roots == ()
    expected_profile = {
        "name": "subagent-mcp-review",
        "description": "Bounded Subagent MCP review profile.",
        "permissionMode": "bypassPermissions",
        "discoverSkills": False,
        "inheritSkills": False,
        "agentsMd": False,
        "injectDefaultTools": False,
        "tools": ["read_file"],
        "disallowedTools": ["search_tool", "use_tool"],
        "skills": [],
        "mcpServers": [],
        "promptMode": "extend",
        "promptBody": "Follow the caller's requested final-output format exactly.",
    }
    assert json.loads(launch.agent_profile_json) == expected_profile
    assert launch.agent_profile_sha256 == hashlib.sha256(
        launch.agent_profile_json.encode("utf-8")
    ).hexdigest()
    assert launch.argv == (
        str(binding.executable_path),
        "--no-auto-update",
        "--cwd",
        str(workspace.resolve()),
        "--model",
        "future/model:opaque@1",
        "--reasoning-effort",
        "highest-native",
        "--disable-web-search",
        "--no-subagents",
        "agent",
        "--no-leader",
        "stdio",
    )
    assert dict(launch.env) == {
        "APPDATA": str(tmp_path / "roaming"),
        "GROK_AUTH_PATH": "C:\\Users\\Example\\.grok\\auth.json",
        "GROK_CAMPAIGNS": "0",
        "GROK_DISABLE_API_KEY_AUTH": "1",
        "GROK_DISABLE_AUTOUPDATER": "1",
        "GROK_FOLDER_TRUST": "1",
        "GROK_HOME": str(
            tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
        ),
        "GROK_MANAGED_CONFIG": "0",
        "LOCALAPPDATA": str(tmp_path / "local"),
        "PATH": "synthetic-path",
        "SSL_CERT_FILE": "C:\\certs\\root.pem",
        "SYSTEMROOT": "C:\\Windows",
        "USERNAME": "Example",
        "USERPROFILE": "C:\\Users\\Example",
    }
    assert "auth.json" not in repr(first)
    assert first.attestation["cached_native_login"] == "not_exposed"
    assert first.attestation["no_extra_spend"] == "not_exposed"
    assert first.attestation["builtin_tool_inventory"] == "not_exposed"
    assert first.attestation["provider_readiness"] == "needs_canary"
    assert first.attestation["quota_state"] == "unknown"
    assert json.loads(first.attestation["requested_agent_profile_json"]) == (
        expected_profile
    )
    assert first.attestation["requested_agent_profile_sha256"] == (
        launch.agent_profile_sha256
    )
    assert first.attestation["agent_profile_binding"] == (
        "session/new._meta.agentProfile"
    )
    assert first.attestation["required_agent_type"] == "grok-build"
    assert first.attestation["agent_type_evidence_source"] == (
        "_x.ai/models/list.availableModels._meta.agentType"
    )
    assert "tool_allowlist" not in first.attestation
    assert "disallowed_tools" not in first.attestation
    assert first.attestation["acp_fs_transport"] == (
        "read_text_file",
        "write_text_file",
    )
    assert first.attestation["acp_terminal_transport"] is True
    assert first.attestation["terminal_authorized"] is False
    assert "terminal" in first.capability_gaps
    assert "resume_after_restart" in first.capability_gaps
    assert "windows_os_sandbox" in first.capability_gaps


@pytest.mark.parametrize(
    ("auth_overrides", "expected"),
    (
        ({"GROK_AUTH_PATH": "D:\\native\\login.json"}, "D:\\native\\login.json"),
        ({"GROK_HOME": "D:\\native-home"}, "D:\\native-home\\auth.json"),
        ({}, "C:\\Users\\Example\\.grok\\auth.json"),
    ),
)
def test_context_derives_auth_path_without_exposing_it(
    tmp_path: Path,
    auth_overrides: dict[str, str],
    expected: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = {
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "USERPROFILE": "C:\\Users\\Example",
        **auth_overrides,
    }
    adapter = _adapter(
        tmp_path,
        _binding(tmp_path),
        environment=environment,
    )
    asyncio.run(adapter.probe())

    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    launch = adapter.launch_for(context)

    assert launch.env["GROK_AUTH_PATH"] == expected
    assert launch.env["GROK_HOME"] == str(
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
    )
    assert expected not in repr(context)


@pytest.mark.parametrize(
    "unsafe_auth_path",
    ("relative-auth.json", "D:\\native\nbad.json"),
)
def test_context_rejects_malformed_explicit_auth_path_without_fallback(
    tmp_path: Path,
    unsafe_auth_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _adapter(
        tmp_path,
        _binding(tmp_path),
        environment={
            "APPDATA": str(tmp_path / "roaming"),
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": "C:\\Users\\Example",
            "GROK_AUTH_PATH": unsafe_auth_path,
        },
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert rejected.value.retryable is False


@pytest.mark.parametrize("reparse_component", ("grok-build", "home"))
def test_runtime_home_reparse_escape_is_rejected_before_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reparse_component: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_root = (tmp_path / "local" / "SubagentMCP").resolve()
    runtime_root = data_root / "grok-build"
    blocked = runtime_root if reparse_component == "grok-build" else runtime_root / "home"
    blocked.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged\n", "utf-8")
    real_is_reparse = grok_module._is_reparse_point

    def synthetic_reparse(path: Path) -> bool:
        return path == blocked or real_is_reparse(path)

    monkeypatch.setattr(grok_module, "_is_reparse_point", synthetic_reparse)
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    try:
        context = _lifecycle_context(adapter, workspace)
    except ServiceError as rejected:
        assert rejected.code == "CAPABILITY_MISSING"
        assert rejected.retryable is False
        context = None

    async def scenario() -> None:
        assert context is not None
        try:
            started = await adapter.spawn(_lifecycle_spawn_request(context))
        except ServiceError as rejected:
            assert rejected.code == "CAPABILITY_MISSING"
            assert rejected.retryable is False
            return
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await adapter.close(request)
        pytest.fail("reparse-backed Grok runtime home reached ACP child launch")

    if context is not None:
        asyncio.run(scenario())
    assert children == []
    assert _trace_records(trace_path) == []
    assert outside.read_text("utf-8") == "unchanged\n"


def test_runtime_home_normal_creation_stays_under_product_data_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, _children = _lifecycle_adapter(tmp_path, binding)
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, request)
        await adapter.close(request)

    asyncio.run(scenario())
    data_root = (tmp_path / "local" / "SubagentMCP").resolve()
    runtime_home = data_root / "grok-build" / "home"
    assert runtime_home.is_dir()
    assert runtime_home.is_relative_to(data_root)


def test_isolation_config_is_created_exactly_and_existing_exact_is_accepted(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    config_path = (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home" / "config.toml"
    )

    adapter._prepare_runtime_home()
    assert config_path.read_bytes() == _isolation_config(config_path.parent)
    timestamp = config_path.stat().st_mtime_ns
    adapter._prepare_runtime_home()

    assert config_path.read_bytes() == _isolation_config(config_path.parent)
    assert config_path.stat().st_mtime_ns == timestamp


def test_isolation_config_ignores_exact_product_bundled_root(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    runtime_home = (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
    ).resolve()

    adapter._prepare_runtime_home()

    config = tomllib.loads((runtime_home / "config.toml").read_text("utf-8"))
    assert config["skills"]["ignore"] == [
        "~/.agents",
        str(runtime_home / "bundled"),
    ]


def test_isolation_config_renders_arbitrary_windows_path_as_valid_toml() -> None:
    home = Path(r"C:\Users\Name #1\Grok [isolated]\技能")

    rendered = grok_module._render_isolation_config(home)

    config = tomllib.loads(rendered.decode("utf-8"))
    assert config["skills"]["ignore"] == [
        "~/.agents",
        str(home / "bundled"),
    ]


@pytest.mark.parametrize("legacy_kind", ("static", "bundled-skills"))
def test_exact_readonly_legacy_isolation_config_migrates_once(
    tmp_path: Path,
    legacy_kind: str,
) -> None:
    data_root = tmp_path / "local" / "SubagentMCP"
    runtime_home = data_root / "grok-build" / "home"
    config_path = runtime_home / "config.toml"
    runtime_home.mkdir(parents=True)
    legacy = (
        _LEGACY_ISOLATION_CONFIG
        if legacy_kind == "static"
        else grok_module._render_legacy_skills_isolation_config(
            runtime_home.resolve()
        )
    )
    config_path.write_bytes(legacy)
    config_path.chmod(stat.S_IREAD)
    adapter = _adapter(tmp_path, _binding(tmp_path))

    adapter._prepare_runtime_home()

    migrated = config_path.read_bytes()
    parsed = tomllib.loads(migrated.decode("utf-8"))
    assert parsed["skills"]["ignore"] == [
        "~/.agents",
        str(runtime_home.resolve() / "bundled"),
    ]
    assert migrated != legacy
    assert grok_module._is_read_only_file(config_path.stat())
    assert tuple(runtime_home.glob(".config.toml.subagent-mcp-*.tmp")) == ()
    identity = grok_module._isolation_file_identity(config_path.stat())

    adapter._prepare_runtime_home()

    assert config_path.read_bytes() == migrated
    assert grok_module._isolation_file_identity(config_path.stat()) == identity
    assert tuple(runtime_home.glob(".config.toml.subagent-mcp-*.tmp")) == ()


@pytest.mark.parametrize("legacy_kind", ("static", "bundled-skills"))
def test_exact_writable_legacy_isolation_config_is_recovered_before_use(
    tmp_path: Path,
    legacy_kind: str,
) -> None:
    data_root = tmp_path / "local" / "SubagentMCP"
    config_path = data_root / "grok-build" / "home" / "config.toml"
    config_path.parent.mkdir(parents=True)
    legacy = (
        _LEGACY_ISOLATION_CONFIG
        if legacy_kind == "static"
        else grok_module._render_legacy_skills_isolation_config(
            config_path.parent.resolve()
        )
    )
    config_path.write_bytes(legacy)
    adapter = _adapter(tmp_path, _binding(tmp_path))

    adapter._prepare_runtime_home()

    assert config_path.read_bytes() == _isolation_config(config_path.parent)
    assert grok_module._is_read_only_file(config_path.stat())


@pytest.mark.parametrize("fault", ("open", "write", "fsync", "replace"))
def test_legacy_isolation_migration_fault_preserves_recoverable_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    data_root = tmp_path / "local" / "SubagentMCP"
    runtime_home = data_root / "grok-build" / "home"
    config_path = runtime_home / "config.toml"
    runtime_home.mkdir(parents=True)
    config_path.write_bytes(_LEGACY_ISOLATION_CONFIG)
    config_path.chmod(stat.S_IREAD)
    adapter = _adapter(tmp_path, _binding(tmp_path))

    if fault == "open":
        original = tempfile.mkstemp
        monkeypatch.setattr(
            grok_module.tempfile,
            "mkstemp",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("staging open failed")),
        )
    elif fault == "write":
        original = os.write
        monkeypatch.setattr(
            grok_module.os,
            "write",
            lambda _descriptor, _content: (_ for _ in ()).throw(
                OSError("staging write failed")
            ),
        )
    elif fault == "fsync":
        original = os.fsync
        monkeypatch.setattr(
            grok_module.os,
            "fsync",
            lambda _descriptor: (_ for _ in ()).throw(OSError("staging fsync failed")),
        )
    else:
        original = os.replace

        def reject_replace(source: object, target: object) -> None:
            staged = Path(source)
            assert staged.parent == runtime_home
            assert grok_module._is_read_only_file(staged.stat())
            assert Path(target) == config_path
            raise OSError("atomic replace failed")

        monkeypatch.setattr(grok_module.os, "replace", reject_replace)

    with pytest.raises(ServiceError) as rejected:
        adapter._prepare_runtime_home()

    assert rejected.value.code == "CAPABILITY_MISSING"
    if fault == "open":
        monkeypatch.setattr(grok_module.tempfile, "mkstemp", original)
    elif fault == "write":
        monkeypatch.setattr(grok_module.os, "write", original)
    elif fault == "fsync":
        monkeypatch.setattr(grok_module.os, "fsync", original)
    else:
        monkeypatch.setattr(grok_module.os, "replace", original)
    assert config_path.read_bytes() == _LEGACY_ISOLATION_CONFIG
    assert grok_module._is_read_only_file(config_path.stat())
    assert tuple(runtime_home.glob(".config.toml.subagent-mcp-*.tmp")) == ()

    adapter._prepare_runtime_home()

    assert config_path.read_bytes() == _isolation_config(runtime_home)
    assert grok_module._is_read_only_file(config_path.stat())
    assert tuple(runtime_home.glob(".config.toml.subagent-mcp-*.tmp")) == ()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows read-only attribute is the Grok release boundary",
)
def test_isolation_config_blocks_cli_rewrite_and_repeated_prepare_stays_usable(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    config_path = (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home" / "config.toml"
    )

    adapter._prepare_runtime_home()
    attempted_rewrite = subprocess.run(
        (
            sys.executable,
            "-I",
            "-c",
            (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).write_bytes(b'[marketplaces]\\n')"
            ),
            str(config_path),
        ),
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert attempted_rewrite.returncode != 0
    assert config_path.read_bytes() == _isolation_config(config_path.parent)
    assert config_path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    adapter._prepare_runtime_home()
    assert config_path.read_bytes() == _isolation_config(config_path.parent)
    assert config_path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY


def test_runtime_isolation_config_replacement_with_exact_readonly_file_fails_closed(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    config_path = (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home" / "config.toml"
    )
    replacement = config_path.with_name("replacement.toml")
    adapter._prepare_runtime_home()
    original_identity = grok_module._isolation_file_identity(config_path.lstat())
    replacement.write_bytes(_isolation_config(config_path.parent))
    replacement_identity = grok_module._isolation_file_identity(replacement.lstat())
    assert replacement_identity != original_identity
    config_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    os.replace(replacement, config_path)
    config_path.chmod(stat.S_IREAD)

    with pytest.raises(ServiceError) as rejected:
        adapter._prepare_runtime_home()

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert config_path.read_bytes() == _isolation_config(config_path.parent)
    config_path.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_runtime_isolation_config_readonly_removal_fails_without_relocking(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    config_path = (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home" / "config.toml"
    )
    adapter._prepare_runtime_home()
    config_path.chmod(stat.S_IWRITE | stat.S_IREAD)

    with pytest.raises(ServiceError) as rejected:
        adapter._prepare_runtime_home()

    assert rejected.value.code == "CAPABILITY_MISSING"
    if sys.platform == "win32":
        assert not (
            config_path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        )


def test_isolation_config_rejects_differing_existing_content_without_overwrite(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "local" / "SubagentMCP"
    config_path = data_root / "grok-build" / "home" / "config.toml"
    config_path.parent.mkdir(parents=True)
    different = b"[compat.cursor]\nskills = true\n"
    config_path.write_bytes(different)
    adapter = _adapter(tmp_path, _binding(tmp_path))

    with pytest.raises(ServiceError) as rejected:
        adapter._prepare_runtime_home()

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert config_path.read_bytes() == different


@pytest.mark.parametrize("ambiguous", ("reparse", "directory"))
def test_isolation_config_rejects_ambiguous_existing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous: str,
) -> None:
    config_path = (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home" / "config.toml"
    )
    config_path.parent.mkdir(parents=True)
    if ambiguous == "directory":
        config_path.mkdir()
    else:
        config_path.write_bytes(_isolation_config(config_path.parent))
        real_is_reparse = grok_module._is_reparse_point
        monkeypatch.setattr(
            grok_module,
            "_is_reparse_point",
            lambda path: path == config_path or real_is_reparse(path),
        )
    adapter = _adapter(tmp_path, _binding(tmp_path))

    with pytest.raises(ServiceError) as rejected:
        adapter._prepare_runtime_home()

    assert rejected.value.code == "CAPABILITY_MISSING"


def test_disposable_guard_home_contains_exact_isolation_config_before_use(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))

    guard_home = adapter._new_billing_guard_home()
    try:
        assert (guard_home / "config.toml").read_bytes() == _isolation_config(
            guard_home
        )
    finally:
        adapter._remove_billing_guard_home(guard_home)

    assert not guard_home.exists()


def test_disposable_guard_homes_ignore_only_their_own_bundled_root(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    first = adapter._new_billing_guard_home()
    second = adapter._new_billing_guard_home()
    try:
        first_config = tomllib.loads((first / "config.toml").read_text("utf-8"))
        second_config = tomllib.loads((second / "config.toml").read_text("utf-8"))
        assert first_config["skills"]["ignore"] == [
            "~/.agents",
            str(first / "bundled"),
        ]
        assert second_config["skills"]["ignore"] == [
            "~/.agents",
            str(second / "bundled"),
        ]
        assert first_config != second_config
    finally:
        adapter._remove_billing_guard_home(second)
        adapter._remove_billing_guard_home(first)

    assert not first.exists()
    assert not second.exists()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows read-only attribute is the Grok release boundary",
)
def test_billing_guard_cleanup_clears_only_owned_config_readonly_attribute(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged\n", "utf-8")
    outside.chmod(stat.S_IREAD)
    guard_home = adapter._new_billing_guard_home()
    config_path = guard_home / "config.toml"

    assert config_path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    adapter._remove_billing_guard_home(guard_home)

    assert not guard_home.exists()
    assert outside.read_text("utf-8") == "unchanged\n"
    assert outside.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    outside.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_billing_guard_cleanup_rejects_config_content_drift_without_delete(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    guard_home = adapter._new_billing_guard_home()
    config_path = guard_home / "config.toml"
    config_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    config_path.write_bytes(b"[marketplaces]\n")

    with pytest.raises(ServiceError) as rejected:
        adapter._remove_billing_guard_home(guard_home)

    assert rejected.value.code == "RECOVERY_REQUIRED"
    assert guard_home.is_dir()
    assert config_path.read_bytes() == b"[marketplaces]\n"
    config_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(guard_home)


def test_billing_guard_cleanup_rejects_same_name_directory_replacement(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    guard_home = adapter._new_billing_guard_home()
    original = guard_home.with_name(f"original-{guard_home.name}")
    guard_home.rename(original)
    guard_home.mkdir()
    replacement_config = guard_home / "config.toml"
    replacement_config.write_bytes(_isolation_config(guard_home))

    with pytest.raises(ServiceError) as rejected:
        adapter._remove_billing_guard_home(guard_home)

    assert rejected.value.code == "RECOVERY_REQUIRED"
    assert guard_home.is_dir()
    assert original.is_dir()
    replacement_config.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(guard_home)
    original_config = original / "config.toml"
    original_config.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(original)


def test_billing_guard_cleanup_rejects_exact_readonly_config_replacement(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    guard_home = adapter._new_billing_guard_home()
    config_path = guard_home / "config.toml"
    replacement = guard_home / "replacement.toml"
    original_identity = grok_module._isolation_file_identity(config_path.lstat())
    replacement.write_bytes(_isolation_config(guard_home))
    replacement_identity = grok_module._isolation_file_identity(replacement.lstat())
    assert replacement_identity != original_identity
    config_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    os.replace(replacement, config_path)
    config_path.chmod(stat.S_IREAD)

    with pytest.raises(ServiceError) as rejected:
        adapter._remove_billing_guard_home(guard_home)

    assert rejected.value.code == "RECOVERY_REQUIRED"
    assert guard_home.is_dir()
    assert config_path.read_bytes() == _isolation_config(guard_home)
    config_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(guard_home)


def test_isolation_config_descriptor_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    real_fstat = os.fstat
    calls = 0

    def drifting_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        details = real_fstat(descriptor)
        calls += 1
        if calls == 2:
            values = list(details)
            values[6] = details.st_size + 1
            return os.stat_result(values)
        return details

    monkeypatch.setattr(grok_module.os, "fstat", drifting_fstat)

    with pytest.raises(ServiceError) as rejected:
        adapter._prepare_runtime_home()

    assert rejected.value.code == "CAPABILITY_MISSING"


def test_failed_guard_config_preparation_removes_new_guard_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))
    guard_root = tmp_path / "local" / "SubagentMCP" / "grok-build" / "billing-guards"
    monkeypatch.setattr(
        grok_module,
        "_ensure_isolation_config",
        lambda *_args: (_ for _ in ()).throw(
            GrokPermissionError("synthetic config preparation failure")
        ),
    )

    with pytest.raises(ServiceError) as rejected:
        adapter._new_billing_guard_home()

    assert rejected.value.code == "RECOVERY_REQUIRED"
    assert guard_root.is_dir()
    assert tuple(guard_root.iterdir()) == ()


def _native_inspect_payload(*, config_path: Path) -> dict[str, object]:
    return {
        "projectRoot": None,
        "projectTrusted": True,
        "mcpServers": [],
        "hooks": [],
        "plugins": [],
        "projectInstructions": [],
        "skills": [],
        "lspServers": [],
        "marketplaces": [],
        "agents": [
            {
                "name": f"builtin-{index}",
                "description": f"Built-in agent {index}",
                "source": {"type": "builtin"},
            }
            for index in range(3)
        ],
        "externalCompat": {
            "remoteSettingsLoaded": False,
            "cells": [
                {
                    "vendor": vendor,
                    "surface": surface,
                    "enabled": False,
                    "source": "config",
                }
                for vendor, surface in _COMPATIBILITY_CELLS
            ],
        },
        "permissions": {
            "sources": [],
            "loaded": 0,
            "skipped": [],
            "mcpServerAllowlist": [],
            "marketplaceAllowlist": [],
            "managedSettingsExists": False,
            "managedSettingsActive": False,
        },
        "loginPolicy": {"apiKeyAuthDisabled": True},
        "configSources": {
            "layers": [
                {"role": "user", "path": str(config_path), "note": None}
            ]
        },
    }


def _add_effectively_disabled_discovery(
    payload: dict[str, object],
    *,
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "private-plugin-root"
    plugin_names = ["private-review-kit"] + [
        f"private-review-kit-{index}" for index in range(1, 10)
    ]
    payload["plugins"] = [
        {
            "name": name,
            "scope": "user" if index % 2 == 0 else "project",
            "path": str(plugin_root / name),
            # Grok 1.0.5 reports discovered plugins as enabled even though its
            # live registry has not enabled their components.
            "enabled": True,
            "provides": {
                "skills": 4,
                "agents": 1,
                "hooks": index < 5,
                "mcpServers": 0,
            },
        }
        for index, name in enumerate(plugin_names)
    ]
    payload["hooks"] = [
        {
            "event": "(plugin)",
            "hookType": "file",
            "target": str(plugin_root / name / "hooks" / "hooks.json"),
            "source": {
                "type": "plugin",
                "plugin_name": name,
                "path": str(plugin_root / name),
            },
            "matcher": None,
        }
        for name in plugin_names[:5]
    ]
    skill_names = ["private-user-skill"] + [
        f"private-user-skill-{index}" for index in range(1, 23)
    ]
    payload["skills"] = [
        {
            "name": name,
            "description": "Disabled external discovery row",
            "source": {
                "type": "user",
                "path": str(tmp_path / name / "SKILL.md"),
            },
            "userInvocable": True,
            "disabled": True,
        }
        for name in skill_names
    ]
    payload["projectInstructions"] = [
        {
            "path": str(tmp_path / "private-compat" / "CLAUDE.md"),
            "scope": "global",
            "fileType": "agents_md",
            "sizeBytes": 17,
            "approxTokens": 5,
            "vendor": "claude",
            "disabled": True,
            "compatibilityStatus": "disabled",
        }
    ]
    payload["configWarnings"] = [
        {
            "target": "configKey",
            "path": "claude_compat",
            "kind": "unknown-field",
            "reason": "unrecognized config key",
        }
    ]


def _add_project_instruction(payload: dict[str, object], path: str, size: int) -> None:
    payload["projectInstructions"] = [
        {
            "path": path,
            "scope": "project",
            "fileType": "agents_md",
            "sizeBytes": size,
            "approxTokens": max(1, size // 4),
        }
    ]


def _install_dynamic_git_manifest(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace / ".git").mkdir(exist_ok=True)
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: git,
    )

    def run(_argv: tuple[str, ...], **_kwargs: object) -> object:
        if "--version" in _argv:
            return grok_module._CommandResult(
                0,
                b"git version 2.51.0.windows.1\n",
                b"",
            )
        if "rev-parse" in _argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        if not any(token.startswith(":(icase") for token in _argv):
            return grok_module._CommandResult(0, b"", b"")
        rows = sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file()
            and ".git" not in {part.casefold() for part in path.parts}
            and path.name.casefold() in {"agent.md", "agents.md", "claude.md"}
        )
        return grok_module._CommandResult(
            0,
            b"".join(row.encode("utf-8") + b"\0" for row in rows),
            b"",
        )

    monkeypatch.setattr(grok_module, "_run_owned_command", run)


def test_json_roundtripped_context_retains_git_instructions_root_and_write_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "docs").mkdir()
    instruction = workspace / "AGENTS.md"
    instruction.write_text("bounded authority\n", "utf-8")
    binding = _binding(tmp_path)
    _install_dynamic_git_manifest(tmp_path, workspace, monkeypatch)
    adapter = _adapter(
        tmp_path,
        binding,
        inspect=replace(
            _inspect(binding, workspace),
            project_root=str(workspace.resolve()),
        ),
    )
    assert asyncio.run(adapter.probe()).state == "needs_canary"
    context = asyncio.run(
        adapter.resolve_context(
            _request(
                workspace,
                permissions=("repo_read", "workspace_write"),
                write_set=("src", "docs"),
            )
        )
    )
    roundtripped = replace(
        context,
        attestation=json.loads(json.dumps(context.attestation)),
    )

    launch = adapter.launch_for(roundtripped)
    observed = adapter._assert_context_current_sync(roundtripped)

    assert launch.write_roots == ("src", "docs")
    assert (
        grok_module._reattest_context_workspace_root(roundtripped)
        == workspace.resolve()
    )
    assert [
        row.relative_path
        for row in grok_module._context_project_instructions(roundtripped)
    ] == ["AGENTS.md"]
    assert grok_module._context_git_attestation(roundtripped) == observed.git_attestation

    instruction.write_text("drifted authority\n", "utf-8")
    with pytest.raises(ServiceError) as rejected:
        adapter._assert_context_current_sync(roundtripped)
    assert rejected.value.code == "CONTEXT_DRIFT"


def test_native_inspect_attests_bounded_project_agents_without_raw_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instruction = workspace / "AGENTS.md"
    content = b"# Project authority\n"
    instruction.write_bytes(content)
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_project_instruction(payload, str(instruction), len(content))
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )

    attestations = getattr(observed, "project_instructions", ())
    assert len(attestations) == 1
    assert attestations[0].relative_path == "AGENTS.md"
    assert attestations[0].sha256 == hashlib.sha256(content).hexdigest()
    assert attestations[0].size == len(content)
    assert str(instruction) not in repr(attestations)
    assert "Project authority" not in repr(attestations)


def test_git_instruction_manifest_skips_ignored_inaccessible_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    root_instruction = workspace / "AGENTS.md"
    root_instruction.write_bytes(b"root\n")
    inaccessible = workspace / ".preview" / "ignored-cache"
    inaccessible.mkdir(parents=True)
    (inaccessible / "Claude.md").write_bytes(b"ignored\n")
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: git,
        raising=False,
    )
    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        output = b"AGENTS.md\0" if any(
            token.startswith(":(icase") for token in argv
        ) else b""
        return grok_module._CommandResult(0, output, b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)
    def guarded_scandir(path: object) -> object:
        raise PermissionError(f"Git-mode scan must not traverse ignored cache: {path}")

    monkeypatch.setattr(grok_module.os, "scandir", guarded_scandir)

    observed = grok_module._scan_grok_instruction_manifest(
        workspace.resolve(),
        str(workspace.resolve()),
    )

    assert [row.relative_path for row in observed] == ["AGENTS.md"]


def test_git_instruction_manifest_binds_tracked_and_untracked_nonignored_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / ".git").mkdir()
    (workspace / "AGENTS.md").write_bytes(b"root\n")
    (workspace / "src" / "Claude.md").write_bytes(b"nested\n")
    (workspace / "src" / "AGENT.md").write_bytes(b"ignored-but-present\n")
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: git,
        raising=False,
    )
    captured: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        captured.append(argv)
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        output = b"AGENTS.md\0src/Claude.md\0" if any(
            token.startswith(":(icase") for token in argv
        ) else b""
        return grok_module._CommandResult(0, output, b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)

    observed = grok_module._scan_grok_instruction_manifest(
        workspace.resolve(),
        str(workspace.resolve()),
    )

    assert [row.relative_path for row in observed] == [
        "AGENTS.md",
        "src/Claude.md",
    ]
    listing = next(argv for argv in captured if "--exclude-standard" in argv)
    assert "safe.directory=" + workspace.resolve().as_posix() in listing
    assert f"--git-dir={workspace.resolve() / '.git'}" in listing
    assert f"--work-tree={workspace.resolve()}" in listing


def _git_for_nested_manifest(cwd: Path, *args: str) -> str:
    git = shutil.which("git.exe") or shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable")
    completed = subprocess.run(
        (git, "-c", "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null", *args),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


@pytest.mark.parametrize("nested_kind", ("tracked-submodule", "untracked-embedded"))
def test_git_instruction_manifest_includes_nested_repository_rules_and_changes(
    tmp_path: Path,
    nested_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git_for_nested_manifest(workspace, "init", "--quiet")
    _git_for_nested_manifest(nested, "init", "--quiet")
    _git_for_nested_manifest(nested, "config", "user.name", "Fixture")
    _git_for_nested_manifest(nested, "config", "user.email", "fixture@example.invalid")
    rule = nested / "AGENTS.md"
    rule.write_text("nested-one\n", "utf-8")
    _git_for_nested_manifest(nested, "add", "AGENTS.md")
    _git_for_nested_manifest(nested, "commit", "--quiet", "-m", "fixture")
    if nested_kind == "tracked-submodule":
        head = _git_for_nested_manifest(nested, "rev-parse", "HEAD")
        (workspace / ".gitmodules").write_text(
            '[submodule "vendor/nested"]\n\tpath = vendor/nested\n'
            "\turl = ./vendor/nested\n",
            "utf-8",
        )
        _git_for_nested_manifest(workspace, "add", ".gitmodules")
        _git_for_nested_manifest(
            workspace,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head},vendor/nested",
        )

    root = workspace.resolve()
    git_attestation = grok_module._attest_git_executable(root)
    first = grok_module._scan_grok_instruction_manifest(
        root,
        str(root),
        git_attestation,
    )
    assert [row.relative_path for row in first] == ["vendor/nested/AGENTS.md"]

    rule.write_text("nested-two\n", "utf-8")
    second = grok_module._scan_grok_instruction_manifest(
        root,
        str(root),
        git_attestation,
    )
    assert second != first
    added_rule = nested / "Claude.md"
    added_rule.write_text("late-add\n", "utf-8")
    added = grok_module._scan_grok_instruction_manifest(
        root,
        str(root),
        git_attestation,
    )
    assert [row.relative_path for row in added] == [
        "vendor/nested/AGENTS.md",
        "vendor/nested/Claude.md",
    ]
    rule.unlink()
    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(
            root,
            str(root),
            git_attestation,
        )


def test_git_instruction_manifest_allows_top_level_linked_worktree_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    worktree = tmp_path / "linked-worktree"
    repository.mkdir()
    _git_for_nested_manifest(repository, "init", "--quiet")
    _git_for_nested_manifest(repository, "config", "user.name", "Fixture")
    _git_for_nested_manifest(
        repository,
        "config",
        "user.email",
        "fixture@example.invalid",
    )
    (repository / "AGENTS.md").write_text("root-worktree-rule\n", "utf-8")
    _git_for_nested_manifest(repository, "add", "AGENTS.md")
    _git_for_nested_manifest(repository, "commit", "--quiet", "-m", "fixture")
    _git_for_nested_manifest(
        repository,
        "worktree",
        "add",
        "--quiet",
        "--detach",
        str(worktree),
        "HEAD",
    )

    root = worktree.resolve()
    observed = grok_module._scan_grok_instruction_manifest(
        root,
        str(root),
        grok_module._attest_git_executable(root),
    )

    assert [row.relative_path for row in observed] == ["AGENTS.md"]


def test_git_instruction_manifest_allows_tracked_submodule_metadata_under_root_git_dir(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source = tmp_path / "submodule-source"
    nested = repository / "vendor" / "nested"
    repository.mkdir()
    source.mkdir()
    _git_for_nested_manifest(repository, "init", "--quiet")
    _git_for_nested_manifest(source, "init", "--quiet")
    _git_for_nested_manifest(source, "config", "user.name", "Fixture")
    _git_for_nested_manifest(source, "config", "user.email", "fixture@example.invalid")
    (source / "AGENTS.md").write_text("submodule-rule\n", "utf-8")
    _git_for_nested_manifest(source, "add", "AGENTS.md")
    _git_for_nested_manifest(source, "commit", "--quiet", "-m", "fixture")
    _git_for_nested_manifest(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(source),
        "vendor/nested",
    )
    assert (nested / ".git").is_file()

    root = repository.resolve()
    observed = grok_module._scan_grok_instruction_manifest(
        root,
        str(root),
        grok_module._attest_git_executable(root),
    )

    assert [row.relative_path for row in observed] == ["vendor/nested/AGENTS.md"]


@pytest.mark.parametrize(
    "redirect",
    ("core-worktree", "worktree-config", "core-excludes-file"),
)
def test_git_instruction_manifest_rejects_local_discovery_redirects(
    tmp_path: Path,
    redirect: str,
) -> None:
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    _git_for_nested_manifest(repository, "init", "--quiet")
    (repository / "AGENTS.md").write_text("workspace-rule\n", "utf-8")
    if redirect == "core-worktree":
        _git_for_nested_manifest(repository, "config", "core.worktree", str(outside))
    elif redirect == "worktree-config":
        _git_for_nested_manifest(
            repository,
            "config",
            "extensions.worktreeConfig",
            "true",
        )
        _git_for_nested_manifest(
            repository,
            "config",
            "--worktree",
            "core.worktree",
            str(outside),
        )
    else:
        excludes = outside / "global-ignore"
        excludes.write_text("AGENTS.md\n", "utf-8")
        _git_for_nested_manifest(
            repository,
            "config",
            "core.excludesFile",
            str(excludes),
        )

    root = repository.resolve()
    observed = grok_module._scan_grok_instruction_manifest(
        root,
        str(root),
        grok_module._attest_git_executable(root),
    )

    assert [row.relative_path for row in observed] == ["AGENTS.md"]


def test_git_instruction_manifest_rejects_mismatched_bound_show_toplevel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "AGENTS.md").write_text("workspace-rule\n", "utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(grok_module, "_resolve_git_executable", lambda _root: git)
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        commands.append(argv)
        if "--version" in argv:
            return grok_module._CommandResult(
                0,
                b"git version 2.51.0.windows.1\n",
                b"",
            )
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{outside.resolve()}\n".encode("utf-8"),
                b"",
            )
        if any(token.startswith(":(icase") for token in argv):
            return grok_module._CommandResult(0, b"AGENTS.md\0", b"")
        return grok_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)
    root = workspace.resolve()

    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(
            root,
            str(root),
            grok_module._attest_git_executable(root),
        )

    assert any("rev-parse" in argv for argv in commands)


def _tracked_submodule_context(
    tmp_path: Path,
    *,
    prompt_write_started: asyncio.Event | None = None,
    prompt_write_release: asyncio.Event | None = None,
) -> tuple[Path, Path, GrokBuildAdapter, object]:
    repository = tmp_path / "workspace"
    source = tmp_path / "submodule-source"
    nested = repository / "vendor" / "nested"
    repository.mkdir()
    source.mkdir()
    _git_for_nested_manifest(repository, "init", "--quiet")
    _git_for_nested_manifest(source, "init", "--quiet")
    _git_for_nested_manifest(source, "config", "user.name", "Fixture")
    _git_for_nested_manifest(source, "config", "user.email", "fixture@example.invalid")
    (source / "AGENTS.md").write_text("submodule-rule\n", "utf-8")
    _git_for_nested_manifest(source, "add", "AGENTS.md")
    _git_for_nested_manifest(source, "commit", "--quiet", "-m", "fixture")
    _git_for_nested_manifest(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(source),
        "vendor/nested",
    )
    binding = _binding(tmp_path)
    adapter, _trace, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=replace(
            _inspect(binding, repository),
            project_root=str(repository.resolve()),
        ),
        prompt_write_started=prompt_write_started,
        prompt_write_release=prompt_write_release,
    )
    return repository.resolve(), nested.resolve(), adapter, _lifecycle_context(
        adapter,
        repository,
    )


def _nested_git_dir_from_marker(marker: Path) -> Path:
    prefix = "gitdir: "
    text = marker.read_text("utf-8").strip()
    assert text.startswith(prefix)
    return (marker.parent / text[len(prefix) :]).resolve(strict=True)


def test_git_context_hash_binds_tracked_nested_git_dir_path(
    tmp_path: Path,
) -> None:
    repository, nested, adapter, first = _tracked_submodule_context(tmp_path)
    marker = nested / ".git"
    original_git_dir = _nested_git_dir_from_marker(marker)
    alternate_git_dir = repository / ".git" / "modules" / "alternate" / "nested"
    shutil.copytree(original_git_dir, alternate_git_dir)
    relative = os.path.relpath(alternate_git_dir, nested)
    marker.unlink()
    marker.write_text(f"gitdir: {relative}\n", "utf-8")

    second = asyncio.run(adapter.resolve_context(_request(repository)))
    first_git = first.attestation["git_attestation"]  # type: ignore[attr-defined,index]
    second_git = second.attestation["git_attestation"]
    assert first.attestation["project_instructions"] == second.attestation[  # type: ignore[attr-defined,index]
        "project_instructions"
    ]
    assert first_git["nested_repository_boundaries"] != second_git[
        "nested_repository_boundaries"
    ]
    assert first.context_hash != second.context_hash  # type: ignore[attr-defined]


@pytest.mark.parametrize("mutation", ("marker-identity", "git-dir-path"))
def test_git_context_nested_metadata_drift_blocks_before_inspect_or_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    repository, nested, adapter, context = _tracked_submodule_context(tmp_path)
    marker = nested / ".git"
    if mutation == "marker-identity":
        marker_bytes = marker.read_bytes()
        marker.unlink()
        marker.write_bytes(marker_bytes)
    else:
        original_git_dir = _nested_git_dir_from_marker(marker)
        alternate_git_dir = repository / ".git" / "modules" / "alternate" / "nested"
        shutil.copytree(original_git_dir, alternate_git_dir)
        marker.unlink()
        marker.write_text(
            f"gitdir: {os.path.relpath(alternate_git_dir, nested)}\n",
            "utf-8",
        )
    inspect_calls = 0
    original_reader = adapter._inspect_reader
    commands: list[tuple[str, ...]] = []
    original_command = grok_module._run_owned_command

    def counted_reader(*args: object) -> object:
        nonlocal inspect_calls
        inspect_calls += 1
        return original_reader(*args)  # type: ignore[arg-type]

    def record_command(argv: tuple[str, ...], **kwargs: object) -> object:
        commands.append(argv)
        return original_command(argv, **kwargs)

    adapter._inspect_reader = counted_reader  # type: ignore[assignment]
    monkeypatch.setattr(grok_module, "_run_owned_command", record_command)

    with pytest.raises(ServiceError) as rejected:
        adapter._assert_context_current_sync(context)  # type: ignore[arg-type]

    assert rejected.value.code == "CONTEXT_DRIFT"
    assert inspect_calls == 0
    assert commands == []


def test_git_context_nested_metadata_drift_blocks_before_billing_or_child(
    tmp_path: Path,
) -> None:
    _repository, nested, adapter, context = _tracked_submodule_context(tmp_path)
    marker = nested / ".git"
    marker_bytes = marker.read_bytes()
    marker.unlink()
    marker.write_bytes(marker_bytes)

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.spawn(_lifecycle_spawn_request(context)))  # type: ignore[arg-type]

    assert rejected.value.code == "CONTEXT_DRIFT"
    assert adapter._sessions == {}


def test_git_context_nested_metadata_drift_fails_terminal_acceptance(
    tmp_path: Path,
) -> None:
    prompt_started = asyncio.Event()
    prompt_release = asyncio.Event()
    _repository, nested, adapter, context = _tracked_submodule_context(
        tmp_path,
        prompt_write_started=prompt_started,
        prompt_write_release=prompt_release,
    )

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))  # type: ignore[arg-type]
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await asyncio.wait_for(prompt_started.wait(), timeout=1)
        marker = nested / ".git"
        marker_bytes = marker.read_bytes()
        marker.unlink()
        marker.write_bytes(marker_bytes)
        prompt_release.set()
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "failed"
        assert terminal.error is not None
        assert terminal.error.code == "CONTEXT_DRIFT"
        await adapter.close(request)

    asyncio.run(scenario())


def test_git_context_binds_root_gitmodules_content_and_identity(
    tmp_path: Path,
) -> None:
    _repository, _nested, adapter, context = _tracked_submodule_context(tmp_path)
    serialized = context.attestation["git_attestation"]  # type: ignore[attr-defined,index]
    assert serialized["root_gitmodules_identity"] is not None
    assert serialized["root_gitmodules_sha256"] == hashlib.sha256(
        (tmp_path / "workspace" / ".gitmodules").read_bytes()
    ).hexdigest()
    restored = grok_module._context_git_attestation(context)  # type: ignore[arg-type]
    assert restored is not None
    assert serialized == grok_module._serialize_git_attestation(restored)

    gitmodules = tmp_path / "workspace" / ".gitmodules"
    content = gitmodules.read_bytes()
    gitmodules.unlink()
    gitmodules.write_bytes(content)
    with pytest.raises(ServiceError) as rejected:
        adapter._assert_context_current_sync(context)  # type: ignore[arg-type]
    assert rejected.value.code == "CONTEXT_DRIFT"


def test_git_instruction_manifest_rejects_untracked_nested_pointer_outside_root_authority_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    nested = repository / "nested"
    outside = tmp_path / "unrelated"
    nested.mkdir(parents=True)
    outside.mkdir()
    _git_for_nested_manifest(repository, "init", "--quiet")
    _git_for_nested_manifest(outside, "init", "--quiet")
    (outside / "AGENTS.md").write_text("must-not-be-read\n", "utf-8")
    pointer = os.path.relpath(outside / ".git", nested)
    (nested / ".git").write_text(f"gitdir: {pointer}\n", "utf-8")
    root = repository.resolve()
    nested_root = nested.resolve()
    commands: list[tuple[str, ...]] = []
    original = grok_module._run_owned_command

    def record(argv: tuple[str, ...], **kwargs: object) -> object:
        commands.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(grok_module, "_run_owned_command", record)

    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(
            root,
            str(root),
            grok_module._attest_git_executable(root),
        )

    assert not any(
        "-C" in argv and argv[argv.index("-C") + 1] == str(nested_root)
        for argv in commands
    )


def test_git_instruction_manifest_rejects_root_metadata_identity_drift(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    worktree = tmp_path / "linked-worktree"
    repository.mkdir()
    _git_for_nested_manifest(repository, "init", "--quiet")
    _git_for_nested_manifest(repository, "config", "user.name", "Fixture")
    _git_for_nested_manifest(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "AGENTS.md").write_text("root-worktree-rule\n", "utf-8")
    _git_for_nested_manifest(repository, "add", "AGENTS.md")
    _git_for_nested_manifest(repository, "commit", "--quiet", "-m", "fixture")
    _git_for_nested_manifest(
        repository,
        "worktree",
        "add",
        "--quiet",
        "--detach",
        str(worktree),
        "HEAD",
    )
    root = worktree.resolve()
    bound = grok_module._bind_git_root_attestation(
        root,
        grok_module._attest_git_executable(root),
    )
    marker = root / ".git"
    marker_bytes = marker.read_bytes()
    marker.unlink()
    marker.write_bytes(marker_bytes)

    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(root, str(root), bound)


def test_git_instruction_manifest_does_not_serialize_the_whole_tracked_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "AGENTS.md").write_bytes(b"root\n")
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(grok_module, "_resolve_git_executable", lambda _root: git)
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        commands.append(argv)
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        if "--stage" in argv and "--" not in argv:
            raise AssertionError("whole-index stage listing is forbidden")
        if "--cached" in argv and ":(icase,glob)AGENTS.md" in argv:
            return grok_module._CommandResult(0, b"AGENTS.md\0", b"")
        return grok_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)

    observed = grok_module._scan_grok_instruction_manifest(
        workspace.resolve(),
        str(workspace.resolve()),
    )

    assert [row.relative_path for row in observed] == ["AGENTS.md"]
    assert not any("--stage" in command and "--" not in command for command in commands)


def test_git_instruction_manifest_rejects_undeclared_gitlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    _git_for_nested_manifest(workspace, "init", "--quiet")
    _git_for_nested_manifest(nested, "init", "--quiet")
    _git_for_nested_manifest(nested, "config", "user.name", "Fixture")
    _git_for_nested_manifest(nested, "config", "user.email", "fixture@example.invalid")
    (nested / "AGENTS.md").write_text("must-not-be-omitted\n", "utf-8")
    _git_for_nested_manifest(nested, "add", "AGENTS.md")
    _git_for_nested_manifest(nested, "commit", "--quiet", "-m", "fixture")
    head = _git_for_nested_manifest(nested, "rev-parse", "HEAD")
    _git_for_nested_manifest(
        workspace,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{head},nested",
    )
    root = workspace.resolve()

    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(
            root,
            str(root),
            grok_module._attest_git_executable(root),
        )


def test_git_instruction_manifest_rejects_local_config_include(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_for_nested_manifest(workspace, "init", "--quiet")
    outside_config = tmp_path / "outside.gitconfig"
    outside_config.write_text("[core]\n\tignorecase = false\n", "utf-8")
    _git_for_nested_manifest(
        workspace,
        "config",
        "--local",
        "include.path",
        str(outside_config),
    )
    root = workspace.resolve()

    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(
            root,
            str(root),
            grok_module._attest_git_executable(root),
        )


@pytest.mark.parametrize(
    "output",
    (
        b"../AGENT.md\0",
        b"AGENTS.md\0AGENTS.md\0",
        b"C:/outside/AGENT.md\0",
        b"AGENTS.md",
        b"bad-\xff.md\0",
    ),
)
def test_git_instruction_manifest_rejects_malicious_or_malformed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_bytes(b"root\n")
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: git,
        raising=False,
    )
    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        payload = output if any(token.startswith(":(icase") for token in argv) else b""
        return grok_module._CommandResult(0, payload, b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)

    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(
            workspace.resolve(),
            str(workspace.resolve()),
        )


def test_non_git_instruction_manifest_reads_root_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (workspace / "Agents.md").write_bytes(b"root\n")
    (nested / "Claude.md").write_bytes(b"nested\n")

    observed = grok_module._scan_grok_instruction_manifest(
        workspace.resolve(),
        None,
    )

    assert [row.relative_path for row in observed] == ["Agents.md"]


def test_native_inspect_rejects_project_root_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    payload["projectRoot"] = str(tmp_path.resolve())
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


def test_git_context_missing_executable_is_capability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    observed = replace(
        _inspect(binding, workspace),
        project_root=str(workspace.resolve()),
    )
    adapter, _trace, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=observed,
    )
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: (_ for _ in ()).throw(
            GrokBindingIncompatible("Git unavailable")
        ),
    )

    with pytest.raises(ServiceError) as caught:
        _lifecycle_context(adapter, workspace)

    assert caught.value.code == "CAPABILITY_MISSING"


def test_git_attestation_rejects_malformed_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: git,
    )
    monkeypatch.setattr(
        grok_module,
        "_run_owned_command",
        lambda *_args, **_kwargs: grok_module._CommandResult(
            0,
            b"git version unsafe value\n",
            b"",
        ),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._attest_git_executable(workspace.resolve())


def test_git_manifest_revalidates_bound_binary_not_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "AGENTS.md").write_bytes(b"rule\n")
    first = tmp_path / "first-git.exe"
    second = tmp_path / "second-git.exe"
    first.write_bytes(b"git-one")
    second.write_bytes(b"git-two")
    selected = first
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: selected,
    )

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        output = b"AGENTS.md\0" if any(
            token.startswith(":(icase") for token in argv
        ) else b""
        return grok_module._CommandResult(0, output, b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)
    attestation = grok_module._attest_git_executable(workspace.resolve())
    selected = second

    rows = grok_module._scan_grok_instruction_manifest(
        workspace.resolve(),
        str(workspace.resolve()),
        attestation,
    )

    assert [row.relative_path for row in rows] == ["AGENTS.md"]
    assert attestation.executable_path == str(first.resolve())


def test_git_manifest_rejects_same_size_same_mtime_binary_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    git = tmp_path / "git.exe"
    git.write_bytes(b"git-one")
    timestamp = git.stat().st_mtime_ns
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: git,
    )

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        return grok_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)
    attestation = grok_module._attest_git_executable(workspace.resolve())
    git.write_bytes(b"git-two")
    os.utime(git, ns=(timestamp, timestamp))

    with pytest.raises(GrokBindingIncompatible):
        grok_module._scan_grok_instruction_manifest(
            workspace.resolve(),
            str(workspace.resolve()),
            attestation,
        )


def test_git_attestation_context_serialization_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    binding = _binding(tmp_path)
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    monkeypatch.setattr(
        grok_module,
        "_resolve_git_executable",
        lambda _workspace: git,
    )

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        return grok_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)
    observed = replace(
        _inspect(binding, workspace),
        project_root=str(workspace.resolve()),
    )
    adapter, _trace, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=observed,
    )
    context = _lifecycle_context(adapter, workspace)

    serialized = context.attestation.get("git_attestation")
    restored = grok_module._context_git_attestation(context)
    assert isinstance(serialized, Mapping)
    assert restored is not None
    assert serialized["sha256"] == restored.sha256
    assert serialized["version"] == "git version 2.51.0.windows.1"
    assert serialized["root_marker_identity"] == restored.root_marker_identity
    assert serialized["root_git_dir_path"] == restored.root_git_dir_path
    assert serialized["root_git_dir_identity"] == restored.root_git_dir_identity
    assert serialized["root_common_dir_path"] == restored.root_common_dir_path
    assert serialized["root_common_dir_identity"] == restored.root_common_dir_identity
    assert serialized["root_gitmodules_identity"] == restored.root_gitmodules_identity
    assert serialized["root_gitmodules_sha256"] == restored.root_gitmodules_sha256
    assert serialized["repository_context_bound"] is True
    assert serialized["nested_repository_boundaries"] == ()


def test_git_context_root_metadata_drift_blocks_before_inspect_or_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / ".git"
    marker.mkdir()
    binding = _binding(tmp_path)
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(grok_module, "_resolve_git_executable", lambda _root: git)

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        commands.append(argv)
        if "--version" in argv:
            return grok_module._CommandResult(0, b"git version 2.51.0.windows.1\n", b"")
        if "rev-parse" in argv:
            return grok_module._CommandResult(
                0,
                f"{workspace.resolve()}\n".encode("utf-8"),
                b"",
            )
        return grok_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(grok_module, "_run_owned_command", run)
    observed = replace(
        _inspect(binding, workspace),
        project_root=str(workspace.resolve()),
    )
    adapter, _trace, children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=observed,
    )
    context = _lifecycle_context(adapter, workspace)
    inspect_calls = 0
    original_reader = adapter._inspect_reader

    def counted_reader(*args: object) -> object:
        nonlocal inspect_calls
        inspect_calls += 1
        return original_reader(*args)  # type: ignore[arg-type]

    adapter._inspect_reader = counted_reader  # type: ignore[assignment]
    commands.clear()
    marker.rmdir()
    marker.mkdir()

    with pytest.raises(ServiceError) as sync_rejected:
        adapter._assert_context_current_sync(context)
    with pytest.raises(ServiceError) as async_rejected:
        asyncio.run(adapter._assert_context_current(context))

    assert sync_rejected.value.code == "CONTEXT_DRIFT"
    assert async_rejected.value.code == "CONTEXT_DRIFT"
    assert inspect_calls == 0
    assert commands == []
    assert children == []


def test_native_inspect_rejects_malformed_project_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    payload["projectTrusted"] = "true"
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


def test_filesystem_bridge_runs_context_guard_before_and_after_read(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "visible.txt").write_text("safe", "utf-8")
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ServiceError(
                "CONTEXT_DRIFT",
                "synthetic manifest drift",
                category="context",
            )

    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
        context_guard=guard,
    )
    with pytest.raises(GrokPermissionError):
        asyncio.run(
            bridge.read_text_file(
                {
                    "sessionId": "native-session-1",
                    "path": str((workspace / "visible.txt").resolve()),
                }
            )
        )
    assert calls == 2


def test_filesystem_bridge_full_context_guard_blocks_late_nested_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    _install_dynamic_git_manifest(tmp_path, workspace, monkeypatch)
    adapter._inspect_reader = lambda current, current_workspace: replace(
        _inspect(current, Path(current_workspace)),
        project_root=str(workspace.resolve()),
    )
    context = _lifecycle_context(adapter, workspace)
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
        context_guard=adapter._context_guard(context),
    )
    (nested / "AGENTS.md").write_text("late\n", "utf-8")

    with pytest.raises(GrokPermissionError):
        asyncio.run(
            bridge.write_text_file(
                {
                    "sessionId": "native-session-1",
                    "path": str((workspace / "allowed.txt").resolve()),
                    "content": "safe",
                }
            )
        )
    assert not (workspace / "allowed.txt").exists()


@pytest.mark.parametrize(
    "reserved",
    (
        "nested/Claude.md",
        ".git/config",
        "nested/.GiT/index",
        ".gitmodules",
        "nested/.GITMODULES",
        ".grok/config.toml",
        ".agents/agent.toml",
        ".cursor/rules.md",
        ".claude/settings.json",
        ".mcp.json",
        ".envrc",
    ),
)
def test_filesystem_bridge_denies_reserved_instruction_and_native_surfaces(
    tmp_path: Path,
    reserved: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace.joinpath(*PureWindowsPath(reserved).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )
    bridge.bind_session("native-session-1")

    with pytest.raises(GrokPermissionError):
        asyncio.run(
            bridge.write_text_file(
                {
                    "sessionId": "native-session-1",
                    "path": str(target.resolve(strict=False)),
                    "content": "unsafe",
                }
            )
        )
    assert not target.exists()
    assert not tuple(workspace.rglob("*.subagent-mcp-*.tmp"))


def test_project_instruction_attestation_enters_context_and_is_rechecked_at_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instruction = workspace / "AGENTS.md"
    instruction.write_bytes(b"authority-one\n")
    runtime_home = tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_project_instruction(payload, str(instruction), instruction.stat().st_size)
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )
    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=observed,
    )
    context = _lifecycle_context(adapter, workspace)
    serialized = context.attestation.get("project_instructions")

    assert context.attestation.get("project_instruction_count") == 1
    assert isinstance(serialized, tuple) and len(serialized) == 1
    assert serialized[0]["path"] == "AGENTS.md"
    assert serialized[0]["sha256"] == hashlib.sha256(b"authority-one\n").hexdigest()
    assert str(instruction) not in repr(serialized)
    instruction.write_bytes(b"authority-two\n")

    async def scenario() -> None:
        with pytest.raises(ServiceError) as rejected:
            await adapter.spawn(_lifecycle_spawn_request(context))
        assert rejected.value.code == "CONTEXT_DRIFT"

    asyncio.run(scenario())
    assert children == []
    assert _trace_records(trace_path) == []


@pytest.mark.parametrize("unsafe_kind", ("hardlink", "reparse", "directory"))
def test_native_inspect_rejects_unsafe_project_instruction_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instruction = workspace / "AGENTS.md"
    if unsafe_kind == "directory":
        instruction.mkdir()
        size = 0
    elif unsafe_kind == "hardlink":
        outside = tmp_path / "outside.md"
        outside.write_bytes(b"shared\n")
        os.link(outside, instruction)
        size = instruction.stat().st_size
    else:
        instruction.write_bytes(b"linked\n")
        size = instruction.stat().st_size
        real_is_reparse = grok_module._is_reparse_point
        monkeypatch.setattr(
            grok_module,
            "_is_reparse_point",
            lambda path: path == instruction or real_is_reparse(path),
        )
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_project_instruction(payload, str(instruction), size)
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


@pytest.mark.parametrize(
    "hostile",
    (
        r"\\server\share\config.toml",
        r"\\?\C:\runtime\config.toml",
        r"C:\runtime\config.toml:ads",
        r"runtime\config.toml",
        r"C:\runtime\..\config.toml",
        "C:\\runtime\\config.toml. ",
        "C:\\runtime\\bad\nconfig.toml",
    ),
)
def test_native_inspect_rejects_hostile_config_path_before_filesystem_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostile: str,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = Path(r"C:\runtime")
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    payload["configSources"]["layers"][0]["path"] = hostile  # type: ignore[index]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        calls.append("filesystem")
        raise AssertionError("hostile raw path reached filesystem")

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "resolve", forbidden)
        guarded.setattr(Path, "stat", forbidden)
        guarded.setattr(Path, "open", forbidden)
        guarded.setattr(grok_module.os, "open", forbidden)
        with pytest.raises(GrokBindingIncompatible):
            grok_module._read_grok_inspect(
                binding,
                str(workspace),
                {"GROK_HOME": str(runtime_home)},
            )
    assert calls == []


@pytest.mark.parametrize(
    "hostile",
    (
        r"\\server\share\AGENTS.md",
        r"\\?\C:\workspace\AGENTS.md",
        r"C:\workspace\AGENTS.md:ads",
        r"AGENTS.md",
        r"C:\workspace\..\AGENTS.md",
        "C:\\workspace\\AGENTS.md. ",
        "C:\\workspace\\bad\nAGENTS.md",
    ),
)
def test_native_inspect_rejects_hostile_instruction_path_before_filesystem_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostile: str,
) -> None:
    binding = _binding(tmp_path)
    workspace = Path(r"C:\workspace")
    runtime_home = Path(r"C:\runtime")
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_project_instruction(payload, hostile, 1)
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        calls.append("filesystem")
        raise AssertionError("hostile raw path reached filesystem")

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "resolve", forbidden)
        guarded.setattr(Path, "stat", forbidden)
        guarded.setattr(Path, "open", forbidden)
        guarded.setattr(grok_module.os, "open", forbidden)
        with pytest.raises(GrokBindingIncompatible):
            grok_module._read_grok_inspect(
                binding,
                str(workspace),
                {"GROK_HOME": str(runtime_home)},
            )
    assert calls == []


def test_native_inspect_retains_only_isolation_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    config_path = runtime_home / "config.toml"
    payload = _native_inspect_payload(config_path=config_path)
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )

    assert getattr(observed, "api_key_auth_disabled", None) is True
    assert getattr(observed, "config_source_layer_count", None) == 1
    assert getattr(observed, "config_source_path", None) == str(config_path)
    assert getattr(observed, "compatibility_isolated", None) is True
    assert getattr(observed, "permission_sources_isolated", None) is True
    assert getattr(observed, "external_surfaces_empty", None) is True
    assert getattr(observed, "builtin_agent_count", None) == 3
    assert "login.json" not in repr(observed)


def test_native_inspect_accepts_order_independent_cells_and_builtin_only_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    payload["externalCompat"]["cells"].reverse()  # type: ignore[index,union-attr]
    payload["configSources"]["layers"][0].pop("note")  # type: ignore[index]
    payload["agents"] = [
        {
            "name": f"arbitrary-builtin-{index}",
            "description": "Native built-in",
            "source": {"type": "builtin"},
        }
        for index in range(5)
    ]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )

    assert observed.compatibility_isolated is True
    assert observed.builtin_agent_count == 5


def test_native_inspect_accepts_no_builtin_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    payload["agents"] = []
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )

    assert observed.builtin_agent_count == 0


def test_native_inspect_accepts_only_effectively_disabled_discovery_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path, version="grok 1.0.5 (5115b46bc9)")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_effectively_disabled_discovery(payload, tmp_path=tmp_path)
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )
    adapter = _adapter(tmp_path, binding, inspect=observed)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))

    assert observed.external_surfaces_empty is True
    assert observed.hooks == ()
    assert observed.plugins == ()
    assert observed.project_instructions == ()
    assert context.attestation["discovered_extensions"] == ()
    assert "private-review-kit" not in repr(observed)
    assert "private-user-skill" not in repr(observed)
    assert "private-plugin-root" not in repr(observed)


def test_native_inspect_accepts_inert_plugin_hook_target_with_dot_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path, version="grok 1.0.5 (5115b46bc9) [stable]")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_effectively_disabled_discovery(payload, tmp_path=tmp_path)
    hooks = payload["hooks"]
    assert isinstance(hooks, list)
    source = hooks[0]["source"]  # type: ignore[index]
    plugin_root = source["path"]
    hooks[0]["target"] = f"{plugin_root}\\hooks\\.\\hooks.json"  # type: ignore[index]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )

    assert observed.hooks == ()


def test_native_inspect_rejects_dotted_alias_of_duplicate_plugin_hook_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path, version="grok 1.0.5 (5115b46bc9)")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_effectively_disabled_discovery(payload, tmp_path=tmp_path)
    hooks = payload["hooks"]
    assert isinstance(hooks, list)
    source = hooks[0]["source"]  # type: ignore[index]
    plugin_root = source["path"]
    hooks[0]["target"] = f"{plugin_root}\\hooks\\.\\hooks.json"  # type: ignore[index]
    duplicate = dict(hooks[0])
    duplicate["target"] = f"{plugin_root}\\hooks\\hooks.json"
    hooks.append(duplicate)
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible, match="duplicated"):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


@pytest.mark.parametrize(
    "target_template",
    (
        r"{root}\hooks\..\outside.json",
        r".\{root}\hooks\hooks.json",
        r"\\server\share\hooks.json",
        r"\\?\C:\plugin\hooks.json",
        r"C:hooks\hooks.json",
        r"{root}\CON\hooks.json",
        "{root}\\hooks\\bad\nname.json",
        r"{root}\hooks:ads\hooks.json",
        r"{root}\hooks.\hooks.json",
        r"{root}\hooks \hooks.json",
    ),
)
def test_native_inspect_dot_compat_keeps_unsafe_plugin_hook_targets_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_template: str,
) -> None:
    binding = _binding(tmp_path, version="grok 1.0.5 (5115b46bc9)")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_effectively_disabled_discovery(payload, tmp_path=tmp_path)
    hooks = payload["hooks"]
    assert isinstance(hooks, list)
    source = hooks[0]["source"]  # type: ignore[index]
    hooks[0]["target"] = target_template.format(root=source["path"])  # type: ignore[index]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


def test_native_inspect_accepts_disabled_plugin_skill_below_discovery_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path, version="grok 1.0.5 (5115b46bc9) [stable]")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_effectively_disabled_discovery(payload, tmp_path=tmp_path)
    hooks = payload["hooks"]
    skills = payload["skills"]
    assert isinstance(hooks, list) and isinstance(skills, list)
    source = dict(hooks[0]["source"])  # type: ignore[index]
    source["path"] = str(Path(source["path"]) / "skills" / "review" / "SKILL.md")
    skills[0]["source"] = source  # type: ignore[index]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    observed = grok_module._read_grok_inspect(
        binding,
        str(workspace.resolve()),
        {"GROK_HOME": str(runtime_home)},
    )

    assert observed.plugins == ()


@pytest.mark.parametrize(
    "unapproved_version",
    (
        "grok 1.0.6 (5115b46bc9)",
        "grok 1.0.5 (5115b46bca)",
        "grok 1.0.5 (5115b46bc9) [nightly]",
    ),
)
def test_native_inspect_rejects_inert_discovery_from_unapproved_grok_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unapproved_version: str,
) -> None:
    binding = _binding(tmp_path, version=unapproved_version)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_effectively_disabled_discovery(payload, tmp_path=tmp_path)
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "active-skill",
        "active-bundled-skill",
        "active-plugin-skill",
        "disabled-plugin-skill-at-root",
        "disabled-plugin-skill-escape",
        "non-plugin-hook",
        "uncorrelated-plugin-hook",
        "camelcase-plugin-source",
        "config-plugin",
        "cli-plugin",
        "unknown-warning",
        "extra-warning",
        "malformed-disabled-instruction",
        "active-compat-instruction",
        "malformed-plugin-row",
    ),
)
def test_native_inspect_rejects_unsafe_effective_discovery_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    binding = _binding(tmp_path, version="grok 1.0.5 (5115b46bc9)")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    _add_effectively_disabled_discovery(payload, tmp_path=tmp_path)
    skills = payload["skills"]
    hooks = payload["hooks"]
    plugins = payload["plugins"]
    instructions = payload["projectInstructions"]
    warnings = payload["configWarnings"]
    assert all(isinstance(rows, list) for rows in (
        skills,
        hooks,
        plugins,
        instructions,
        warnings,
    ))
    if mutation == "active-skill":
        skills[0]["disabled"] = False  # type: ignore[index]
    elif mutation == "active-bundled-skill":
        skills[0]["disabled"] = False  # type: ignore[index]
        skills[0]["source"] = {  # type: ignore[index]
            "type": "bundled",
            "path": str(runtime_home / "bundled" / "skills" / "remote" / "SKILL.md"),
        }
    elif mutation == "active-plugin-skill":
        skills[0]["disabled"] = False  # type: ignore[index]
        skills[0]["source"] = dict(hooks[0]["source"])  # type: ignore[index]
    elif mutation == "disabled-plugin-skill-at-root":
        skills[0]["source"] = dict(hooks[0]["source"])  # type: ignore[index]
    elif mutation == "disabled-plugin-skill-escape":
        source = dict(hooks[0]["source"])  # type: ignore[index]
        source["path"] = str(tmp_path / "another-plugin" / "SKILL.md")
        skills[0]["source"] = source  # type: ignore[index]
    elif mutation == "non-plugin-hook":
        hooks[0]["source"] = {  # type: ignore[index]
            "type": "user",
            "path": str(tmp_path / "hooks.json"),
        }
    elif mutation == "uncorrelated-plugin-hook":
        hooks[0]["source"]["plugin_name"] = "another-plugin"  # type: ignore[index]
    elif mutation == "camelcase-plugin-source":
        source = hooks[0]["source"]  # type: ignore[index]
        source["pluginName"] = source.pop("plugin_name")
    elif mutation == "config-plugin":
        plugins[0]["scope"] = "config"  # type: ignore[index]
    elif mutation == "cli-plugin":
        plugins[0]["scope"] = "cli"  # type: ignore[index]
    elif mutation == "unknown-warning":
        warnings[0]["path"] = "another_key"  # type: ignore[index]
    elif mutation == "extra-warning":
        warnings.append(dict(warnings[0]))  # type: ignore[arg-type,index]
    elif mutation == "malformed-disabled-instruction":
        instructions[0]["sizeBytes"] = "17"  # type: ignore[index]
    elif mutation == "active-compat-instruction":
        instructions[0]["disabled"] = False  # type: ignore[index]
    elif mutation == "malformed-plugin-row":
        plugins[0]["provides"]["hooks"] = 1  # type: ignore[index]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "api-key-auth",
        "remote-settings",
        "duplicate-cell",
        "missing-cell",
        "enabled-cell",
        "wrong-cell-source",
        "wrong-config-path",
        "warning",
        "mcp-problem",
        "permission-source",
        "permission-loaded",
        "permission-allowlist",
        "managed-permission",
        "hook-without-name",
        "external-agent",
        "duplicate-agent",
    ),
)
def test_native_inspect_rejects_unisolated_or_malformed_public_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    config_path = runtime_home / "config.toml"
    payload = _native_inspect_payload(config_path=config_path)
    if mutation == "api-key-auth":
        payload["loginPolicy"] = {"apiKeyAuthDisabled": False}
    elif mutation == "remote-settings":
        payload["externalCompat"]["remoteSettingsLoaded"] = True  # type: ignore[index]
    elif mutation == "duplicate-cell":
        payload["externalCompat"]["cells"].append(  # type: ignore[index,union-attr]
            dict(payload["externalCompat"]["cells"][0])  # type: ignore[index]
        )
    elif mutation == "missing-cell":
        payload["externalCompat"]["cells"].pop()  # type: ignore[index,union-attr]
    elif mutation == "enabled-cell":
        payload["externalCompat"]["cells"][0]["enabled"] = True  # type: ignore[index]
    elif mutation == "wrong-cell-source":
        payload["externalCompat"]["cells"][0]["source"] = "user"  # type: ignore[index]
    elif mutation == "wrong-config-path":
        payload["configSources"]["layers"][0]["path"] = str(tmp_path / "other.toml")  # type: ignore[index]
    elif mutation == "warning":
        payload["configWarnings"] = []
    elif mutation == "mcp-problem":
        payload["mcpConfigProblems"] = []
    elif mutation == "permission-source":
        payload["permissions"]["sources"] = ["user"]  # type: ignore[index]
    elif mutation == "permission-loaded":
        payload["permissions"]["loaded"] = 1  # type: ignore[index]
    elif mutation == "permission-allowlist":
        payload["permissions"]["mcpServerAllowlist"] = ["external"]  # type: ignore[index]
    elif mutation == "managed-permission":
        payload["permissions"]["managedSettingsExists"] = True  # type: ignore[index]
    elif mutation == "hook-without-name":
        payload["hooks"] = [
            {
                "event": "BeforeTool",
                "hookType": "command",
                "target": "external",
                "source": {"type": "compat"},
            }
        ]
    elif mutation == "external-agent":
        payload["agents"][0]["source"] = {"type": "config"}  # type: ignore[index]
    elif mutation == "duplicate-agent":
        payload["agents"].append(dict(payload["agents"][0]))  # type: ignore[union-attr,index]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


@pytest.mark.parametrize(
    "surface",
    (
        "hooks",
        "mcpServers",
        "plugins",
        "projectInstructions",
        "skills",
        "lspServers",
        "marketplaces",
        "compatibilityMcpServers",
        "permissionRules",
        "permissionModes",
    ),
)
def test_native_inspect_rejects_every_nonempty_external_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_home = tmp_path / "runtime-home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    payload[surface] = [{}]
    monkeypatch.setattr(
        grok_module,
        "_run_grok",
        lambda *_args, **_kwargs: json.dumps(payload),
    )

    with pytest.raises(GrokBindingIncompatible):
        grok_module._read_grok_inspect(
            binding,
            str(workspace.resolve()),
            {"GROK_HOME": str(runtime_home)},
        )


def test_catalog_inspect_and_acp_launch_share_one_isolated_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed_environments: list[dict[str, str]] = []

    def run_public(
        _executable: Path,
        suffix: tuple[str, ...],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        observed_environments.append(dict(environment or {}))
        if suffix == ("models",):
            return "future/model:opaque@1\tOpaque"
        return json.dumps(
            _native_inspect_payload(
                config_path=(
                    tmp_path
                    / "local"
                    / "SubagentMCP"
                    / "grok-build"
                    / "home"
                    / "config.toml"
                )
            )
        )

    monkeypatch.setattr(grok_module, "_run_grok", run_public)
    environment = {
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "USERPROFILE": "C:\\Users\\Example",
        "XAI_API_KEY": "must-not-leak",
        "GROK_MODELS_BASE_URL": "https://paid.invalid",
    }
    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        platform="win32",
        environment=environment,
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    assert asyncio.run(adapter.model_catalog()) == (
        {"value": "future/model:opaque@1", "label": "Opaque"},
    )
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    launch_environment = dict(adapter.launch_for(context).env)

    assert observed_environments == [launch_environment, launch_environment]
    assert "XAI_API_KEY" not in launch_environment
    assert "GROK_MODELS_BASE_URL" not in launch_environment


def test_context_writer_attests_canonical_write_set_for_service_contract(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _adapter(tmp_path, _binding(tmp_path))
    asyncio.run(adapter.probe())

    context = asyncio.run(
        adapter.resolve_context(
            _request(
                workspace,
                permissions=("repo_read", "workspace_write"),
                write_set=("src/exact.py",),
            )
        )
    )

    assert context.attestation["write_set"] == ("src/exact.py",)
    assert "write_roots" not in context.attestation


def test_context_rejects_binding_inside_requested_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding_at(workspace / "tools" / "grok.exe")
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code == "CAPABILITY_MISSING"


def test_context_inspect_discards_evidence_when_bound_executable_drifts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "grok.exe"
    binding = _binding_at(executable)
    timestamp = executable.stat().st_mtime_ns
    calls = 0

    def drift_then_inspect(
        current: GrokBinding, current_workspace: str
    ) -> GrokInspectObservation:
        nonlocal calls
        calls += 1
        executable.write_bytes(b"replaced-grok!")
        os.utime(executable, ns=(timestamp, timestamp))
        return _inspect(current, Path(current_workspace))

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=drift_then_inspect,
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code in {"CAPABILITY_MISSING", "CONTEXT_DRIFT"}
    assert calls == 1


def test_context_inspect_retries_one_transient_timeout_then_passes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    calls = 0

    def inspect_reader(
        current: GrokBinding,
        current_workspace: str,
    ) -> GrokInspectObservation:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise grok_module.GrokBindingTimeout("synthetic inspect timeout")
        return _inspect(current, Path(current_workspace))

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=inspect_reader,
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    context = asyncio.run(adapter.resolve_context(_request(workspace)))

    assert context.workspace_path == str(workspace.resolve())
    assert calls == 2


def test_context_inspect_retries_one_command_availability_failure_then_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    runtime_home = tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
    payload = _native_inspect_payload(config_path=runtime_home / "config.toml")
    calls = 0

    def run(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError("synthetic command availability failure")
        return json.dumps(payload)

    monkeypatch.setattr(grok_module, "_run_grok", run)
    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    context = asyncio.run(adapter.resolve_context(_request(workspace)))

    assert context.workspace_path == str(workspace.resolve())
    assert calls == 2


def test_context_inspect_stops_after_three_transient_failures(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    calls = 0

    def inspect_reader(
        _current: GrokBinding,
        _current_workspace: str,
    ) -> GrokInspectObservation:
        nonlocal calls
        calls += 1
        raise grok_module.GrokBindingTimeout("synthetic inspect timeout")

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=inspect_reader,
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert rejected.value.retryable is False
    assert calls == 3


def test_context_inspect_malformed_json_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    calls = 0

    def run(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return "{not-json"

    monkeypatch.setattr(grok_module, "_run_grok", run)
    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert calls == 1


def test_context_inspect_security_failure_is_not_retried(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    calls = 0

    def inspect_reader(
        current: GrokBinding,
        current_workspace: str,
    ) -> GrokInspectObservation:
        nonlocal calls
        calls += 1
        return _inspect(
            current,
            Path(current_workspace),
            mcp_servers=("unsafe-project-mcp",),
        )

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=inspect_reader,
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert calls == 1


def test_context_inspect_cancellation_propagates_without_retry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    started = threading.Event()
    calls = 0

    def inspect_reader(
        _current: GrokBinding,
        _current_workspace: str,
    ) -> GrokInspectObservation:
        nonlocal calls
        calls += 1
        cancel = grok_module._COMMAND_CANCEL_EVENT.get()
        assert cancel is not None
        started.set()
        cancel.wait(timeout=2)
        raise grok_module.GrokBindingTimeout("synthetic inspect cancellation")

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=inspect_reader,
        platform="win32",
        environment={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())

    async def scenario() -> None:
        task = asyncio.create_task(adapter.resolve_context(_request(workspace)))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls == 1


def test_context_accepts_exact_file_roots_and_thirty_two_path_prefixes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact_file = workspace / "README.md"
    exact_file.write_text("read me", encoding="utf-8")
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    roots = ("README.md", *(f"lane-{index}" for index in range(31)))

    context = asyncio.run(
        adapter.resolve_context(
            _request(
                workspace,
                permissions=("repo_read", "workspace_write"),
                write_set=roots,
            )
        )
    )
    launch = adapter.launch_for(context)

    assert launch.write_roots == roots
    assert json.loads(launch.agent_profile_json) == {
        "name": "subagent-mcp-writer",
        "description": "Bounded Subagent MCP writer profile.",
        "permissionMode": "bypassPermissions",
        "discoverSkills": False,
        "inheritSkills": False,
        "agentsMd": False,
        "injectDefaultTools": False,
        "tools": ["read_file", "search_replace"],
        "disallowedTools": ["search_tool", "use_tool"],
        "skills": [],
        "mcpServers": [],
        "promptMode": "extend",
        "promptBody": "Follow the caller's requested final-output format exactly.",
    }
    assert launch.argv[-3:] == ("agent", "--no-leader", "stdio")
    assert "--tools" not in launch.argv
    assert "--disallowed-tools" not in launch.argv
    assert "--permission-mode" not in launch.argv
    assert context.attestation["mode"] == "writer"
    with pytest.raises(ServiceError) as too_many:
        asyncio.run(
            adapter.resolve_context(
                _request(
                    workspace,
                    permissions=("repo_read", "workspace_write"),
                    write_set=(*roots, "lane-overflow"),
                )
            )
        )
    assert too_many.value.code == "CAPABILITY_MISSING"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("requested_agent_profile_json", "{}"),
        ("requested_agent_profile_sha256", "0" * 64),
        ("agent_profile_binding", "unbound"),
        ("required_agent_type", "codex"),
        ("agent_type_evidence_source", "unbound"),
        ("acp_fs_transport", ("read_text_file",)),
        ("acp_terminal_transport", False),
        ("terminal_authorized", True),
    ),
)
def test_launch_rejects_transport_or_agent_profile_attestation_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _adapter(tmp_path, _binding(tmp_path))
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    drifted = replace(
        context,
        attestation={**context.attestation, field: value},
    )

    with pytest.raises(ServiceError) as rejected:
        adapter.launch_for(drifted)

    assert rejected.value.code == "CONTEXT_DRIFT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"runtime_id": "other"},
        {"transport": "managed-sdk"},
        {"context_policy_id": "full-native"},
        {"permissions": ("repo_read", "network")},
        {"permissions": ("workspace_write",), "write_set": ("src",)},
        {"permissions": ("repo_read", "workspace_write"), "write_set": ()},
        {"permissions": ("repo_read",), "write_set": ("src",)},
        {"reasoning": {}},
        {"reasoning": {"effort": "max", "extra": True}},
        {"reasoning": {"effort": "\ud800"}},
        {"model": ""},
        {"write_set": ("../escape",), "permissions": ("repo_read", "workspace_write")},
        {"write_set": ("\ud800",), "permissions": ("repo_read", "workspace_write")},
    ],
)
def test_context_rejects_unsupported_policy_or_ambiguous_authority(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _adapter(tmp_path, _binding(tmp_path))
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError):
        asyncio.run(adapter.resolve_context(_request(workspace, **overrides)))


def test_catalog_authority_rejects_unknown_model_but_unavailable_catalog_does_not(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    available = _adapter(
        tmp_path,
        binding,
        catalog=({"value": "only/native", "label": "Only Native"},),
    )
    unavailable = _adapter(tmp_path, binding, catalog=())
    asyncio.run(available.probe())
    asyncio.run(unavailable.probe())
    asyncio.run(available.model_catalog())
    asyncio.run(unavailable.model_catalog())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(available.resolve_context(_request(workspace)))
    accepted = asyncio.run(unavailable.resolve_context(_request(workspace)))

    assert rejected.value.code == "POLICY_REJECTED"
    assert accepted.requested_model == "future/model:opaque@1"


def test_context_inspect_is_off_loop_and_rejects_executable_extensions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    caller_thread = threading.get_ident()
    reader_threads: list[int] = []

    def inspect_reader(current: GrokBinding, current_workspace: str) -> GrokInspectObservation:
        reader_threads.append(threading.get_ident())
        return _inspect(
            current,
            Path(current_workspace),
            mcp_servers=("project-mcp",),
            hooks=("project-hook",),
            plugins=("user-plugin",),
            compatibility_mcp_servers=("cursor-mcp",),
            permission_keys=("Shell", "Terminal", "allow"),
        )

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=inspect_reader,
        platform="win32",
        data_root=tmp_path / "local" / "SubagentMCP",
    )
    asyncio.run(adapter.probe())
    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert reader_threads and reader_threads[0] != caller_thread
    assert rejected.value.code == "CAPABILITY_MISSING"


def _valid_session_attestation(
    context: object,
    binding: GrokBinding,
    *,
    mode: str = "review",
) -> GrokSessionToolAttestation:
    workspace_path = getattr(context, "workspace_path")
    workspace_key = getattr(context, "workspace_key")
    effective_model = getattr(context, "requested_model")
    effort = getattr(context, "requested_reasoning")["effort"]
    return GrokSessionToolAttestation(
        pair_key=binding.pair_key,
        external_session_id="native-session-1",
        workspace_key=workspace_key,
        mode=mode,
        workspace_path=workspace_path,
        effective_model=effective_model,
        reasoning_effort=effort,
        auth_method="cached_token",
        provider_no_spend_safe=True,
        quota_state="unknown",
        effective_agent_type="grok-build",
        agent_type_source="_x.ai/models/list.availableModels._meta.agentType",
    )


def test_context_handshake_accepts_exact_cached_token_review_and_unknown_quota(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    attestation = _valid_session_attestation(context, binding)

    adapter.validate_session_attestation(context, attestation)

    assert attestation.quota_state == "unknown"


def test_context_handshake_accepts_only_bounded_writer_route(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    context = asyncio.run(
        adapter.resolve_context(
            _request(
                workspace,
                permissions=("repo_read", "workspace_write"),
                write_set=("src/exact.py",),
            )
        )
    )

    adapter.validate_session_attestation(
        context,
        _valid_session_attestation(context, binding, mode="writer"),
    )


@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("workspace_path", None),
        ("workspace_key", ["unhashable"]),
        ("quota_state", []),
        ("pair_key", "f" * 64),
        ("external_session_id", ""),
        ("workspace_key", "wrong-workspace"),
        ("workspace_path", "C:\\outside"),
        ("mode", "writer"),
        ("effective_model", "silent-fallback"),
        ("reasoning_effort", "silent-downgrade"),
        ("auth_method", "not_exposed"),
        ("auth_method", "api-key"),
        ("provider_no_spend_safe", False),
        ("quota_state", "exhausted"),
        ("effective_agent_type", "codex"),
        ("agent_type_source", "unbound"),
    ],
)
def test_context_handshake_fails_closed_on_mismatched_native_evidence(
    tmp_path: Path,
    field: str,
    unsafe: object,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    attestation = replace(_valid_session_attestation(context, binding), **{field: unsafe})

    with pytest.raises(ServiceError) as rejected:
        adapter.validate_session_attestation(context, attestation)

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert rejected.value.retryable is False


def _filesystem_client(
    workspace: Path,
    scenario: str,
    bridge: GrokFilesystemBridge,
) -> AcpStdioProcess:
    names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT")
    return AcpStdioProcess(
        argv=(sys.executable, "-I", str(FAKE_ACP), scenario),
        cwd=workspace,
        env={name: os.environ[name] for name in names if name in os.environ},
        request_handler=bridge.handle_reverse_request,
        startup_timeout_seconds=1.0,
        request_timeout_seconds=1.0,
        close_timeout_seconds=0.2,
        max_line_bytes=1_048_576,
    )


_FILESYSTEM_SESSION_ID = "native-session-1"


def _bound_filesystem_bridge(**kwargs: object) -> GrokFilesystemBridge:
    bridge = GrokFilesystemBridge(**kwargs)  # type: ignore[arg-type]
    bridge.bind_session(_FILESYSTEM_SESSION_ID)
    return bridge


def _filesystem_read_request(
    workspace: Path,
    relative_path: str,
    **optional: object,
) -> dict[str, object]:
    return {
        "sessionId": _FILESYSTEM_SESSION_ID,
        "path": str(workspace.joinpath(*PureWindowsPath(relative_path).parts).resolve(strict=False)),
        **optional,
    }


def _filesystem_write_request(
    workspace: Path,
    relative_path: str,
    content: str,
    **optional: object,
) -> dict[str, object]:
    return {
        **_filesystem_read_request(workspace, relative_path, **optional),
        "content": content,
    }


def test_filesystem_review_reads_utf8_inside_workspace_and_denies_all_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "README.md"
    source.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )

    async def scenario() -> None:
        assert await bridge.read_text_file(_filesystem_read_request(workspace, "README.md")) == {
            "content": "before\n"
        }
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "README.md", "changed\n")
            )
        for method in (
            "fs/delete",
            "fs/move",
            "terminal/create",
            "process/spawn",
            "network/request",
            "browser/open",
            "mcp/call",
            "session/request_permission",
        ):
            with pytest.raises(GrokPermissionError):
                await bridge.handle_reverse_request(method, {})

    asyncio.run(scenario())
    assert source.read_text(encoding="utf-8") == "before\n"


def test_managed_acp_forces_reverse_io_transport_but_denies_terminal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )

    assert grok_module._initialize_params() == {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        },
        "clientInfo": {
            "name": "subagent-mcp",
            "version": grok_module.__version__,
        },
    }

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.handle_reverse_request("terminal/create", {})
        with pytest.raises(GrokPermissionError):
            await bridge.handle_reverse_request(
                "fs/write_text_file",
                _filesystem_write_request(workspace, "denied.txt", "denied\n"),
            )

    asyncio.run(scenario())
    assert not (workspace / "denied.txt").exists()


def test_reverse_io_attestation_saturates_without_retaining_request_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "PRIVATE.txt").write_text("SECRET\n", encoding="utf-8")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    monkeypatch.setattr(grok_module, "_MAX_REVERSE_IO_COUNT", 1)

    async def scenario() -> None:
        request = _filesystem_read_request(workspace, "PRIVATE.txt")
        await bridge.handle_reverse_request("fs/read_text_file", request)
        await bridge.handle_reverse_request("fs/read_text_file", request)

    asyncio.run(scenario())
    evidence = bridge._reverse_io_attestation()
    assert evidence == {
        "scope": "native-session-cumulative",
        "read_attempts": 1,
        "read_successes": 1,
        "write_attempts": 0,
        "write_successes": 0,
        "terminal_attempts": 0,
        "terminal_denials": 0,
        "saturated": True,
    }
    assert "PRIVATE" not in repr(evidence)
    assert "SECRET" not in repr(evidence)


def test_acp_filesystem_wire_requires_bound_session_absolute_path_and_read_slice(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "README.md"
    source.write_bytes(b"one\ntwo\nthree\n")
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    absolute = str(source.resolve())

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.handle_reverse_request(
                "fs/read_text_file",
                {"sessionId": "native-session-1", "path": absolute},
            )
        bridge.bind_session("native-session-1")
        with pytest.raises(GrokPermissionError):
            await bridge.handle_reverse_request(
                "fs/read_text_file",
                {"sessionId": "wrong-session", "path": absolute},
            )
        assert await bridge.handle_reverse_request(
            "fs/read_text_file",
            {
                "sessionId": "native-session-1",
                "path": absolute,
                "line": 2,
                "limit": 1,
                "_meta": {"source": "fixture"},
            },
        ) == {"content": "two\n"}
        assert await bridge.handle_reverse_request(
            "fs/read_text_file",
            {
                "sessionId": "native-session-1",
                "path": absolute,
                "line": None,
                "limit": 0,
                "_meta": None,
            },
        ) == {"content": ""}
        for invalid in (
            {"sessionId": "native-session-1", "path": "README.md"},
            {"sessionId": "native-session-1", "path": absolute, "line": True},
            {"sessionId": "native-session-1", "path": absolute, "_meta": []},
            {"sessionId": "native-session-1", "path": absolute, "extra": 1},
        ):
            with pytest.raises(GrokPermissionError):
                await bridge.handle_reverse_request("fs/read_text_file", invalid)

    asyncio.run(scenario())


def test_workspace_root_reparse_is_rejected_before_outside_read_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical_workspace = tmp_path / "workspace-link"
    lexical_workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE-SECRET", "utf-8")
    real_resolve = Path.resolve

    def synthetic_resolve(path: Path, strict: bool = False) -> Path:
        if path == lexical_workspace:
            return outside
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", synthetic_resolve)
    monkeypatch.setattr(
        grok_module,
        "_is_reparse_point",
        lambda path: path == lexical_workspace,
    )
    outside_accesses: list[Path] = []
    real_path_open = Path.open
    real_os_open = grok_module.os.open

    def guarded_path_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == outside or outside in path.parents:
            outside_accesses.append(path)
        return real_path_open(path, *args, **kwargs)

    def guarded_os_open(path: object, *args: object, **kwargs: object) -> int:
        candidate = Path(path)
        if candidate == outside or outside in candidate.parents:
            outside_accesses.append(candidate)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(grok_module.os, "open", guarded_os_open)

    with pytest.raises(GrokPermissionError):
        GrokFilesystemBridge(
            workspace=lexical_workspace,
            permission_mode="workspace-write",
            write_roots=(".",),
        )
    assert outside_accesses == []
    monkeypatch.undo()
    assert (outside / "secret.txt").read_text("utf-8") == "OUTSIDE-SECRET"
    assert not (outside / "must-not-land.txt").exists()


def test_filesystem_root_identity_drift_blocks_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "README.md"
    source.write_text("inside\n", "utf-8")
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    original_attest = grok_module._attest_workspace_root

    def drifted_attest(value: object) -> tuple[Path, tuple[int, int]]:
        path, identity = original_attest(value)
        return path, (identity[0], identity[1] + 1)

    monkeypatch.setattr(grok_module, "_attest_workspace_root", drifted_attest)
    bridge.bind_session("native-session-1")
    with pytest.raises(GrokPermissionError):
        asyncio.run(
            bridge.handle_reverse_request(
                "fs/read_text_file",
                {
                    "sessionId": "native-session-1",
                    "path": str(source.resolve()),
                },
            )
        )


def test_context_attests_workspace_root_identity_in_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())

    context = asyncio.run(adapter.resolve_context(_request(workspace)))

    identity = context.attestation.get("workspace_root_identity")
    assert isinstance(identity, tuple)
    assert len(identity) == 2
    payload = {
        "workspace_path": context.workspace_path,
        "workspace_root_identity": identity,
    }
    assert json.dumps(payload, sort_keys=True)


def test_context_rejects_lexical_root_reparse_before_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    inspect_calls = 0
    original_reader = adapter._inspect_reader

    def counted_reader(*args: object) -> object:
        nonlocal inspect_calls
        inspect_calls += 1
        return original_reader(*args)  # type: ignore[arg-type]

    adapter._inspect_reader = counted_reader  # type: ignore[assignment]
    monkeypatch.setattr(
        grok_module,
        "_is_reparse_point",
        lambda path: path == workspace,
    )

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert inspect_calls == 0


def test_context_sync_and_async_guards_reject_root_identity_drift_before_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    inspect_calls = 0
    original_reader = adapter._inspect_reader

    def counted_reader(*args: object) -> object:
        nonlocal inspect_calls
        inspect_calls += 1
        return original_reader(*args)  # type: ignore[arg-type]

    adapter._inspect_reader = counted_reader  # type: ignore[assignment]
    original_attest = grok_module._attest_workspace_root

    def drifted_attest(value: object) -> tuple[Path, tuple[int, int]]:
        path, identity = original_attest(value)
        return path, (identity[0], identity[1] + 1)

    monkeypatch.setattr(grok_module, "_attest_workspace_root", drifted_attest)

    with pytest.raises(ServiceError) as sync_rejected:
        adapter._assert_context_current_sync(context)
    with pytest.raises(ServiceError) as async_rejected:
        asyncio.run(adapter._assert_context_current(context))

    assert sync_rejected.value.code == "CONTEXT_DRIFT"
    assert async_rejected.value.code == "CONTEXT_DRIFT"
    assert inspect_calls == 0


def test_filesystem_writer_updates_and_creates_only_exact_file_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    exact = source / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("src/exact.py", "src/new.py"),
    )

    async def scenario() -> None:
        assert await bridge.read_text_file(_filesystem_read_request(workspace, "src/exact.py")) == {
            "content": "before\n"
        }
        assert await bridge.write_text_file(
            _filesystem_write_request(workspace, "src/exact.py", "after\n")
        ) == {}
        assert await bridge.write_text_file(
            _filesystem_write_request(workspace, "src/new.py", "new\n")
        ) == {}
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "src/other.py", "forbidden\n")
            )

    asyncio.run(scenario())
    assert exact.read_text(encoding="utf-8") == "after\n"
    assert (source / "new.py").read_text(encoding="utf-8") == "new\n"
    assert not (source / "other.py").exists()
    assert not tuple(workspace.rglob("*.subagent-mcp-*.tmp"))


def test_filesystem_writer_normalizes_case_and_separators_for_multiple_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "Docs" / "Specs").mkdir(parents=True)
    (workspace / "SRC" / "nested").mkdir(parents=True)
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("docs\\specs", "src"),
    )

    async def scenario() -> None:
        await bridge.write_text_file(
            _filesystem_write_request(workspace, "DOCS\\SPECS\\one.md", "one\n")
        )
        await bridge.write_text_file(
            _filesystem_write_request(workspace, "src/nested/two.py", "two\n")
        )
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "Docs/other.md", "outside\n")
            )

    asyncio.run(scenario())
    assert (workspace / "Docs" / "Specs" / "one.md").read_text("utf-8") == "one\n"
    assert (workspace / "SRC" / "nested" / "two.py").read_text("utf-8") == "two\n"


def test_filesystem_windows_case_matching_does_not_overfold_distinct_names(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    sharp_s = "\u00df"
    (workspace / "ss").mkdir(parents=True)
    (workspace / sharp_s).mkdir()
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("ss",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, f"{sharp_s}/outside.txt", "outside\n")
            )

    asyncio.run(scenario())
    assert not (workspace / sharp_s / "outside.txt").exists()


@pytest.mark.parametrize(
    "path",
    (
        "../escape.txt",
        "C:\\outside.txt",
        "D:\\alternate.txt",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "\\\\?\\C:\\device.txt",
        "\\\\.\\NUL",
        "src/file.txt:stream",
        "NUL",
        "CLOCK$",
        "CONIN$",
        "CONOUT$.txt",
        "COM\u00b9",
        "com\u00b2.txt",
        "COM\u00b3...",
        "LPT\u00b9",
        "lpt\u00b2.log",
        "LPT\u00b3   ",
        "src/COM1.txt",
        "src/control\x01.txt",
        "src/" + ("x" * 4097),
    ),
)
def test_filesystem_rejects_ambiguous_windows_paths_before_access(
    tmp_path: Path,
    path: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("allowed.txt",),
    )

    with pytest.raises(GrokPermissionError):
        grok_module._windows_relative_parts(path)

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(
                {"sessionId": _FILESYSTEM_SESSION_ID, "path": path}
            )
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"sessionId": _FILESYSTEM_SESSION_ID, "path": path, "content": "x"}
            )

    asyncio.run(scenario())
    assert tuple(workspace.iterdir()) == ()


def test_filesystem_rejects_symlink_escape_without_mutating_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("outside\n", encoding="utf-8")
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(_filesystem_read_request(workspace, "linked/secret.txt"))
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "linked/secret.txt", "changed\n")
            )

    asyncio.run(scenario())
    assert secret.read_text(encoding="utf-8") == "outside\n"


def test_filesystem_detects_authorized_parent_replacement_before_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authorized = workspace / "docs"
    authorized.mkdir(parents=True)
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("docs",),
    )
    replaced = workspace / "docs-original"
    authorized.rename(replaced)
    authorized.mkdir()

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "docs/new.md", "must not land\n")
            )

    asyncio.run(scenario())
    assert not (authorized / "new.md").exists()
    assert not (replaced / "new.md").exists()


def test_filesystem_enforces_utf8_and_file_size_bounds(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invalid.txt").write_bytes(b"\xff")
    (workspace / "large.txt").write_bytes(b"x" * (1_048_576 + 1))
    (workspace / "json-expanded.txt").write_bytes(b'"' * 600_000)
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("output.txt",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(_filesystem_read_request(workspace, "invalid.txt"))
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(_filesystem_read_request(workspace, "large.txt"))
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(_filesystem_read_request(workspace, "json-expanded.txt"))
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "output.txt", "x" * (1_048_576 + 1))
            )
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "output.txt", "\ud800")
            )
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(
                {**_filesystem_read_request(workspace, "README.md"), "extra": True}
            )

    asyncio.run(scenario())
    assert not (workspace / "output.txt").exists()


def test_filesystem_reverse_requests_use_real_bridge_through_acp_stdio(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_bytes(b"bridge-read\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )

    async def scenario() -> None:
        client = _filesystem_client(workspace, "filesystem-read", bridge)
        await client.start()
        try:
            result = await client.request("trigger/filesystem", {})
            assert result["reverseResponse"] == {
                "jsonrpc": "2.0",
                "id": "filesystem-1",
                "result": {"content": "bridge-read\n"},
            }
        finally:
            await client.close()

    asyncio.run(scenario())


def test_filesystem_unknown_reverse_method_is_method_not_found_and_session_survives(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )

    async def scenario() -> None:
        client = _filesystem_client(workspace, "filesystem-unknown", bridge)
        await client.start()
        try:
            result = await client.request("trigger/filesystem", {})
            assert result["reverseResponse"] == {
                "jsonrpc": "2.0",
                "id": "filesystem-1",
                "error": {"code": -32601, "message": "Method not found"},
            }
            assert (await client.request("still/alive", {}))["method"] == "still/alive"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_filesystem_writer_routes_allowed_and_denied_writes_through_acp_stdio(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )

    async def scenario() -> None:
        allowed = _filesystem_client(workspace, "filesystem-write", bridge)
        await allowed.start()
        try:
            result = await allowed.request("trigger/filesystem", {})
            assert result["reverseResponse"]["result"] == {}
        finally:
            await allowed.close()

        denied = _filesystem_client(workspace, "filesystem-write-denied", bridge)
        await denied.start()
        try:
            result = await denied.request("trigger/filesystem", {})
            assert result["reverseResponse"]["error"] == {
                "code": -32603,
                "message": "Internal error",
            }
        finally:
            await denied.close()

    asyncio.run(scenario())
    assert exact.read_bytes() == b"written-through-acp\n"
    assert not (workspace / "other.py").exists()


def test_filesystem_acp_denies_git_metadata_write_without_mutation_or_temp_residue(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    git_metadata = workspace / ".git"
    git_metadata.mkdir(parents=True)
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )

    async def scenario() -> None:
        client = _filesystem_client(git_metadata, "filesystem-write", bridge)
        await client.start()
        try:
            result = await client.request("trigger/filesystem", {})
            assert result["reverseResponse"]["error"] == {
                "code": -32603,
                "message": "Internal error",
            }
        finally:
            await client.close()

    asyncio.run(scenario())
    assert not (git_metadata / "exact.py").exists()
    assert not tuple(workspace.rglob("*.subagent-mcp-*.tmp"))


def test_filesystem_rejects_overlapping_write_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "nested").mkdir(parents=True)

    with pytest.raises(GrokPermissionError):
        GrokFilesystemBridge(
            workspace=workspace,
            permission_mode="workspace-write",
            write_roots=("src", "SRC/nested"),
        )


def test_filesystem_explicit_workspace_root_remains_workspace_bounded(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )

    async def scenario() -> None:
        await bridge.write_text_file(_filesystem_write_request(workspace, "inside.txt", "inside\n"))
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {
                    "sessionId": _FILESYSTEM_SESSION_ID,
                    "path": str(workspace.parent / "outside.txt"),
                    "content": "outside\n",
                }
            )

    asyncio.run(scenario())
    assert (workspace / "inside.txt").read_bytes() == b"inside\n"
    assert not (tmp_path / "outside.txt").exists()


def test_filesystem_synthetic_reparse_component_is_denied_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    linked = workspace / "linked"
    linked.mkdir(parents=True)
    (linked / "secret.txt").write_bytes(b"bounded\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    real_check = grok_module._is_reparse_point
    monkeypatch.setattr(
        grok_module,
        "_is_reparse_point",
        lambda path: path.name.casefold() == "linked" or real_check(path),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(_filesystem_read_request(workspace, "linked/secret.txt"))

    asyncio.run(scenario())


def test_filesystem_detects_target_replacement_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )
    real_fsync = grok_module.os.fsync
    raced = False

    def replace_target_during_temp_flush(descriptor: int) -> None:
        nonlocal raced
        real_fsync(descriptor)
        if not raced:
            raced = True
            replacement = workspace / "replacement.py"
            replacement.write_bytes(b"raced\n")
            os.replace(replacement, exact)

    monkeypatch.setattr(grok_module.os, "fsync", replace_target_during_temp_flush)

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "exact.py", "must-not-overwrite-race\n")
            )

    asyncio.run(scenario())
    assert exact.read_bytes() == b"raced\n"
    assert not tuple(workspace.glob("*.subagent-mcp-*.tmp"))


def test_filesystem_cleans_owned_temp_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )

    def reject_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic replacement failure")

    monkeypatch.setattr(grok_module.os, "replace", reject_replace)

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "exact.py", "must-not-land\n")
            )

    asyncio.run(scenario())
    assert exact.read_bytes() == b"before\n"
    assert not tuple(workspace.glob("*.subagent-mcp-*.tmp"))


def test_filesystem_rejects_workspace_hardlink_to_outside_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    linked = workspace / "linked.txt"
    try:
        os.link(outside, linked)
    except OSError as exc:
        pytest.skip(f"hardlink creation unavailable: {exc}")
    reader = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    writer = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await reader.read_text_file(_filesystem_read_request(workspace, "linked.txt"))
        with pytest.raises(GrokPermissionError):
            await writer.write_text_file(
                _filesystem_write_request(workspace, "linked.txt", "must-not-land\n")
            )

    asyncio.run(scenario())
    assert outside.read_bytes() == b"outside\n"
    assert linked.read_bytes() == b"outside\n"


def test_filesystem_rejects_link_count_change_after_read_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_bytes(b"bounded\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    real_fstat = grok_module.os.fstat

    class ChangedLinkCount:
        def __init__(self, observed: object) -> None:
            self._observed = observed
            self.st_nlink = 2

        def __getattr__(self, name: str) -> object:
            return getattr(self._observed, name)

    monkeypatch.setattr(
        grok_module.os,
        "fstat",
        lambda descriptor: ChangedLinkCount(real_fstat(descriptor)),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(_filesystem_read_request(workspace, "source.txt"))

    asyncio.run(scenario())


def test_filesystem_rejects_link_count_change_before_read_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_bytes(b"bounded\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    real_stat = Path.stat

    class ChangedLinkCount:
        def __init__(self, observed: object) -> None:
            self._observed = observed
            self.st_nlink = 2

        def __getattr__(self, name: str) -> object:
            return getattr(self._observed, name)

    def changed_source_stat(path: Path, *args: object, **kwargs: object) -> object:
        observed = real_stat(path, *args, **kwargs)
        if path == source:
            return ChangedLinkCount(observed)
        return observed

    monkeypatch.setattr(Path, "stat", changed_source_stat)

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file(_filesystem_read_request(workspace, "source.txt"))

    asyncio.run(scenario())


def test_filesystem_cancel_waits_for_worker_and_prevents_late_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )
    entered = threading.Event()
    release = threading.Event()
    real_fsync = grok_module.os.fsync

    def blocking_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(grok_module.os, "fsync", blocking_fsync)

    async def scenario() -> None:
        task = asyncio.create_task(
            bridge.write_text_file(_filesystem_write_request(workspace, "exact.py", "late\n"))
        )
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        try:
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exact.read_bytes() == b"before\n"
        assert bridge._write_worker is None
        assert not bridge._write_lock.locked()
        await asyncio.sleep(0.05)
        assert exact.read_bytes() == b"before\n"

    asyncio.run(scenario())


def test_filesystem_acp_close_surfaces_ambiguity_until_write_worker_settles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )
    entered = threading.Event()
    release = threading.Event()
    real_fsync = grok_module.os.fsync

    def blocking_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(grok_module.os, "fsync", blocking_fsync)

    async def scenario() -> None:
        client = _filesystem_client(workspace, "filesystem-write", bridge)
        client._close_timeout = 0.05
        await client.start()
        request = asyncio.create_task(client.request("trigger/filesystem", {}))
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            with pytest.raises(AcpProcessError, match="callback cleanup timed out"):
                await client.close()
            assert exact.read_bytes() == b"before\n"
        finally:
            release.set()
        await asyncio.gather(request, return_exceptions=True)
        for _ in range(100):
            if bridge._write_worker is None:
                break
            await asyncio.sleep(0.01)
        assert bridge._write_worker is None
        assert not bridge._write_lock.locked()
        assert exact.read_bytes() == b"before\n"

    asyncio.run(scenario())


def test_filesystem_cleanup_failure_surfaces_typed_ambiguity_and_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )
    replace_error = OSError("synthetic replace failure")
    unlink_error = OSError("synthetic unlink failure")
    real_unlink = Path.unlink

    monkeypatch.setattr(
        grok_module.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(replace_error),
    )

    def fail_owned_temp_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if ".subagent-mcp-" in path.name:
            raise unlink_error
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_owned_temp_unlink)

    async def scenario() -> None:
        with pytest.raises(grok_module.GrokFilesystemCleanupError) as caught:
            await bridge.write_text_file(
                _filesystem_write_request(workspace, "exact.py", "must-not-land\n")
            )
        assert caught.value.original_error is replace_error
        assert caught.value.cleanup_error is unlink_error

    asyncio.run(scenario())
    assert exact.read_bytes() == b"before\n"
    leftovers = tuple(workspace.glob("*.subagent-mcp-*.tmp"))
    assert len(leftovers) == 1
    real_unlink(leftovers[0])


def test_filesystem_acp_cleanup_ambiguity_is_terminal_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )
    real_unlink = Path.unlink
    replace_error = OSError(f"replace exposed {workspace}")
    unlink_error = OSError("unlink exposed SECRET_DETAIL")
    monkeypatch.setattr(
        grok_module.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(replace_error),
    )

    def fail_owned_temp_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if ".subagent-mcp-" in path.name:
            raise unlink_error
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_owned_temp_unlink)

    async def scenario() -> None:
        client = _filesystem_client(workspace, "filesystem-write", bridge)
        await client.start()
        with pytest.raises(AcpProcessError, match="cleanup ambiguity") as pending:
            await client.request("trigger/filesystem", {})
        with pytest.raises(AcpProcessError, match="cleanup ambiguity") as closed:
            await client.close()
        for error in (pending.value, closed.value):
            assert os.fspath(workspace) not in str(error)
            assert "SECRET_DETAIL" not in str(error)

    asyncio.run(scenario())
    assert exact.read_bytes() == b"before\n"
    leftovers = tuple(workspace.glob("*.subagent-mcp-*.tmp"))
    assert len(leftovers) == 1
    real_unlink(leftovers[0])


def test_filesystem_acp_cancel_cleanup_ambiguity_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )
    entered = threading.Event()
    release = threading.Event()
    real_fsync = grok_module.os.fsync
    real_unlink = Path.unlink

    def blocking_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        entered.set()
        release.wait(timeout=2)

    def fail_owned_temp_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if ".subagent-mcp-" in path.name:
            raise OSError("SECRET_CANCEL_CLEANUP")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(grok_module.os, "fsync", blocking_fsync)
    monkeypatch.setattr(Path, "unlink", fail_owned_temp_unlink)

    async def scenario() -> None:
        client = _filesystem_client(workspace, "filesystem-write-hang", bridge)
        client._close_timeout = 0.05
        await client.start()
        request = asyncio.create_task(client.request("trigger/filesystem", {}))
        assert await asyncio.to_thread(entered.wait, 1)
        close = asyncio.create_task(client.close())
        for _ in range(100):
            if bridge._write_cancel is not None and bridge._write_cancel.is_set():
                break
            await asyncio.sleep(0.01)
        assert bridge._write_cancel is not None and bridge._write_cancel.is_set()
        release.set()
        with pytest.raises(AcpProcessError, match="cleanup ambiguity") as pending:
            await request
        with pytest.raises(AcpProcessError, match="cleanup ambiguity") as closed:
            await close
        assert "SECRET_CANCEL_CLEANUP" not in str(pending.value)
        assert "SECRET_CANCEL_CLEANUP" not in str(closed.value)

    asyncio.run(scenario())
    assert exact.read_bytes() == b"before\n"
    leftovers = tuple(workspace.glob("*.subagent-mcp-*.tmp"))
    assert len(leftovers) == 1
    real_unlink(leftovers[0])


def test_filesystem_acp_retains_cleanup_fatal_after_eof_wins_terminal_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = _bound_filesystem_bridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )
    entered = threading.Event()
    release = threading.Event()
    real_unlink = Path.unlink

    def blocked_replace(_source: object, _target: object) -> None:
        entered.set()
        release.wait(timeout=2)
        raise OSError("SECRET_LATE_REPLACE")

    def fail_owned_temp_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if ".subagent-mcp-" in path.name:
            raise OSError("SECRET_LATE_UNLINK")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(grok_module.os, "replace", blocked_replace)
    monkeypatch.setattr(Path, "unlink", fail_owned_temp_unlink)

    async def scenario() -> None:
        client = _filesystem_client(workspace, "filesystem-write-eof", bridge)
        await client.start()
        request = asyncio.create_task(client.request("trigger/filesystem", {}))
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(AcpProcessError) as pending:
            await request
        assert "cleanup ambiguity" not in str(pending.value)
        release.set()
        for _ in range(100):
            if bridge._write_worker is None and not client._reverse_tasks:
                break
            await asyncio.sleep(0.01)
        assert bridge._write_worker is None
        assert not client._reverse_tasks
        with pytest.raises(AcpProcessError, match="cleanup ambiguity") as closed:
            await client.close()
        assert "SECRET_LATE_REPLACE" not in str(closed.value)
        assert "SECRET_LATE_UNLINK" not in str(closed.value)

    asyncio.run(scenario())
    assert exact.read_bytes() == b"before\n"
    leftovers = tuple(workspace.glob("*.subagent-mcp-*.tmp"))
    assert len(leftovers) == 1
    real_unlink(leftovers[0])


def _lifecycle_adapter(
    tmp_path: Path,
    binding: GrokBinding,
    *,
    scenario: str = "happy",
    mutation: str | None = None,
    error_code: str | None = None,
    error_retryable: bool = False,
    error_detail: str | None = None,
    stop_reason: str | None = None,
    inspect: GrokInspectObservation | None = None,
    inspect_sequence: tuple[GrokInspectObservation, ...] = (),
    billing_mutation: str | None = None,
    disabled_extensions: tuple[tuple[str, str], ...] = (),
    handshake_delay: float = 0.0,
    cancel_timeout: float = 0.5,
    close_delay: float = 0.0,
    close_error: bool = False,
    rpc_code: int | str | None = None,
    rpc_message: str | None = None,
    rpc_data: Mapping[str, object] | None = None,
    handshake_rpc_method: str | None = None,
    prompt_write_started: asyncio.Event | None = None,
    prompt_write_release: asyncio.Event | None = None,
    prompt_write_error: bool = False,
    write_order: list[str] | None = None,
    lifecycle_events: list[str] | None = None,
    materialize_bundled_skill_at: str | None = None,
    model_state_mutation: str | None = None,
    session_model_state_mutation: str | None = None,
) -> tuple[GrokBuildAdapter, Path, list[AcpStdioProcess]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    trace_path = tmp_path / f"trace-{scenario}-{mutation or 'none'}.jsonl"
    children: list[AcpStdioProcess] = []
    inspect_calls = 0
    billing_guard_count = 0
    persistent_home = (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
    ).resolve()
    bundled_root = persistent_home / "bundled"
    bundled_skill = bundled_root / "skills" / "remote" / "SKILL.md"

    def inspect_reader(
        current: GrokBinding, current_workspace: str
    ) -> GrokInspectObservation:
        nonlocal inspect_calls
        if inspect_sequence:
            current_inspect = inspect_sequence[min(inspect_calls, len(inspect_sequence) - 1)]
            inspect_calls += 1
            return current_inspect
        current_inspect = inspect or _inspect(current, Path(current_workspace))
        if bundled_skill.exists():
            config = tomllib.loads(
                (persistent_home / "config.toml").read_text("utf-8")
            )
            if str(bundled_root) not in config["skills"]["ignore"]:
                return replace(current_inspect, plugins=("active-bundled-skill",))
        return current_inspect

    def process_factory(
        launch: object,
        request_handler: object,
        notification_handler: object,
    ) -> AcpStdioProcess:
        nonlocal billing_guard_count
        launch_env = dict(getattr(launch, "env"))
        observed_home = Path(launch_env["GROK_HOME"])
        child_role = (
            "session" if observed_home == persistent_home else "billing-guard"
        )
        if child_role == "billing-guard":
            billing_guard_count += 1
        if lifecycle_events is not None:
            lifecycle_events.append(f"{child_role}:create")
        config: dict[str, object] = {
            "scenario": scenario,
            "pair_key": binding.pair_key,
            "workspace_key": getattr(launch, "workspace_key"),
            "workspace_path": str(Path(getattr(launch, "workspace_path")).resolve()),
            "session_id": "123e4567-e89b-12d3-a456-426614174000",
            "mode": "writer" if getattr(launch, "write_roots") else "review",
            "model": getattr(launch, "model"),
            "reasoning_effort": getattr(launch, "reasoning_effort"),
            "quota_state": "unknown",
            "disabled_extensions": [list(item) for item in disabled_extensions],
            "handshake_delay": handshake_delay,
            "child_role": child_role,
        }
        if mutation is not None:
            config["mutation"] = mutation
        if model_state_mutation is not None:
            config["model_state_mutation"] = model_state_mutation
        if session_model_state_mutation is not None:
            config["session_model_state_mutation"] = session_model_state_mutation
        if billing_mutation is not None:
            config["billing_mutation"] = (
                "auto-topup-enabled"
                if billing_mutation == "second-auto-topup-enabled"
                and child_role == "billing-guard"
                and billing_guard_count > 1
                else billing_mutation
            )
        if error_code is not None:
            config["error_code"] = error_code
            config["error_retryable"] = error_retryable
        if error_detail is not None:
            config["error_detail"] = error_detail
        if stop_reason is not None:
            config["stop_reason"] = stop_reason
        if rpc_code is not None:
            config["rpc_code"] = rpc_code
        if rpc_message is not None:
            config["rpc_message"] = rpc_message
        if rpc_data is not None:
            config["rpc_data"] = dict(rpc_data)
        if handshake_rpc_method is not None:
            config["handshake_rpc_method"] = handshake_rpc_method
        names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT")
        class LifecycleProcess(AcpStdioProcess):
            async def _write(self, message: Mapping[str, object]) -> None:
                method = message.get("method")
                if lifecycle_events is not None and isinstance(method, str):
                    lifecycle_events.append(f"{child_role}:{method}")
                if method == "session/prompt" and prompt_write_release is not None:
                    assert prompt_write_started is not None
                    prompt_write_started.set()
                    await prompt_write_release.wait()
                    if prompt_write_error:
                        raise AcpProcessError("synthetic prompt write failure")
                await super()._write(message)
                if (
                    child_role == "session"
                    and method == materialize_bundled_skill_at
                ):
                    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
                    bundled_skill.write_text("remote bundled skill\n", "utf-8")
                if write_order is not None and method in {
                    "session/prompt",
                    "session/cancel",
                }:
                    write_order.append(str(method))

            async def close(self) -> None:
                if lifecycle_events is not None:
                    lifecycle_events.append(f"{child_role}:close-start")
                if close_delay:
                    await asyncio.sleep(close_delay)
                await super().close()
                if lifecycle_events is not None:
                    lifecycle_events.append(f"{child_role}:close-done")
                if close_error:
                    raise AcpProcessError("synthetic cleanup ambiguity")

        child = LifecycleProcess(
            argv=(
                sys.executable,
                "-I",
                str(FAKE_ACP),
                "grok-lifecycle",
                json.dumps(config, separators=(",", ":"), sort_keys=True),
                str(trace_path),
            ),
            cwd=workspace,
            env={
                **{name: os.environ[name] for name in names if name in os.environ},
                **{
                    name: value
                    for name, value in launch_env.items()
                    if name.startswith("GROK_")
                },
            },
            request_handler=request_handler,  # type: ignore[arg-type]
            notification_handler=notification_handler,  # type: ignore[arg-type]
            startup_timeout_seconds=1.0,
            request_timeout_seconds=float("inf"),
            close_timeout_seconds=_LIFECYCLE_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            max_line_bytes=1_048_576,
        )
        children.append(child)
        return child

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=inspect_reader,
        platform="win32",
        environment={
            "APPDATA": str(tmp_path / "roaming"),
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "user"),
        },
        data_root=tmp_path / "local" / "SubagentMCP",
        acp_process_factory=process_factory,
        handshake_timeout_seconds=0.5,
        cancel_timeout_seconds=cancel_timeout,
    )
    return adapter, trace_path, children


def _lifecycle_context(
    adapter: GrokBuildAdapter,
    workspace: Path,
    *,
    writer: bool = False,
) -> object:
    assert asyncio.run(adapter.probe()).state == "needs_canary"
    request = (
        _request(
            workspace,
            permissions=("repo_read", "workspace_write"),
            write_set=("allowed.txt",),
        )
        if writer
        else _request(workspace)
    )
    return asyncio.run(adapter.resolve_context(request))


def _lifecycle_spawn_request(context: object) -> AdapterSpawnRequest:
    return AdapterSpawnRequest(
        "conversation-grok",
        "execution-grok-1",
        TaskPacket(
            "Review",
            "Review one file.",
            ("Return findings.",),
            "reviewer",
        ),
        context,  # type: ignore[arg-type]
    )


def _trace_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


async def _lifecycle_wait_terminal(
    adapter: GrokBuildAdapter,
    request: AdapterSessionRequest,
) -> object:
    for _ in range(200):
        snapshot = await adapter.snapshot(request)
        if snapshot.execution_state != "running":
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("fake Grok lifecycle did not reach terminal state")


@pytest.mark.parametrize("auth_mutation", (None, "auth-meta"))
def test_lifecycle_spawn_handshake_returns_running_then_succeeds_with_public_text_only(
    tmp_path: Path,
    auth_mutation: str | None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path, binding, mutation=auth_mutation
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        assert started.execution_state == "running"
        assert started.conversation_state == "active"
        observed_evidence = {
            key: started.evidence.get(key)
            for key in (
                "auth_method",
                "auth_evidence_source",
                "route_isolation",
                "route_isolation_source",
                "client_terminal_enabled",
                "reverse_terminal_authorized",
                "agent_profile_request_sha256",
                "agent_profile_request_source",
                "effective_agent_type",
                "agent_type_evidence_source",
                "web_search_enabled",
                "nested_agents_enabled",
                "mcp_servers",
                "provider_no_spend_safe",
                "quota_state",
            )
        }
        expected_evidence = {
            "auth_method": "cached_token",
            "auth_evidence_source": "initialize._meta.defaultAuthMethodId",
            "route_isolation": "verified",
            "route_isolation_source": "isolated-home-native-inspect",
            "client_terminal_enabled": True,
            "reverse_terminal_authorized": False,
            "agent_profile_request_sha256": context.attestation[
                "requested_agent_profile_sha256"
            ],
            "agent_profile_request_source": "session/new._meta.agentProfile",
            "effective_agent_type": "grok-build",
            "agent_type_evidence_source": (
                "_x.ai/models/list.availableModels._meta.agentType"
            ),
            "web_search_enabled": False,
            "nested_agents_enabled": False,
            "mcp_servers": [],
            "provider_no_spend_safe": True,
            "quota_state": "unknown",
        }
        serialized_evidence = repr(started.evidence)
        for private_field in (
            "creditUsagePercent",
            "prepaidBalance",
            "onDemandCap",
            "isUnifiedBillingUser",
            "onDemandEnabled",
            "account",
            "tier",
            "email",
        ):
            assert private_field not in serialized_evidence
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        before_snapshot = len(_trace_records(trace_path))
        local = await adapter.snapshot(request)
        assert local.external_execution_id == started.external_execution_id
        assert "post_handshake_attestation" not in local.evidence
        assert len(_trace_records(trace_path)) == before_snapshot
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "succeeded"
        assert terminal.result_text == "APPROVED"
        serialized = repr(terminal)
        assert "PRIVATE_REASONING" not in serialized
        assert "PRIVATE_TOOL_FRAME" not in serialized
        assert "PRIVATE_UNKNOWN_FRAME" not in serialized
        assert "Review one file" not in serialized
        assert started.evidence["post_handshake_attestation"] == {
            "reasoning_source": "grok-build-native-acp-session",
            "reasoning_binding": [
                context.attestation["pair_key"],
                started.external_session_id,
                context.effective_model,
                dict(context.effective_reasoning),
                context.context_hash,
            ],
            "reasoning_provider_reported": True,
        }
        closed = await adapter.close(request)
        assert closed.evidence["effective_agent_type"] == "grok-build"
        assert closed.evidence["agent_type_evidence_source"] == (
            "_x.ai/models/list.availableModels._meta.agentType"
        )
        assert observed_evidence == expected_evidence

    asyncio.run(scenario())
    assert len(children) == 2
    assert all(child.closed is True for child in children)
    records = _trace_records(trace_path)
    guard_records = [
        record for record in records if record.get("childRole") == "billing-guard"
    ]
    session_records = [
        record for record in records if record.get("childRole") == "session"
    ]
    assert [record["method"] for record in guard_records] == [
        "initialize",
        "initialized",
        "authenticate",
        "_x.ai/billing",
        "_x.ai/auto-topup-rule",
    ]
    assert not any(
        record["method"] in {"x.ai/billing", "x.ai/auto-topup-rule"}
        for record in records
    )
    assert [record["method"] for record in session_records] == [
        "initialize",
        "initialized",
        "authenticate",
        "_x.ai/models/list",
        "session/new",
        "session/prompt",
    ]
    assert guard_records[0]["params"] == {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        },
        "clientInfo": {
            "name": "subagent-mcp",
            "version": grok_module.__version__,
        },
    }
    assert session_records[0]["params"] == guard_records[0]["params"]
    assert guard_records[2]["params"] == {
        "methodId": "cached_token",
        "_meta": {"headless": True},
    }
    assert guard_records[3]["params"] == {}
    assert guard_records[4]["params"] == {}
    assert session_records[3]["params"] == {}
    assert session_records[4]["params"] == {
        "cwd": str(workspace.resolve()),
        "mcpServers": [],
        "_meta": {
            "agentProfile": {
                "name": "subagent-mcp-review",
                "description": "Bounded Subagent MCP review profile.",
                "permissionMode": "bypassPermissions",
                "discoverSkills": False,
                "inheritSkills": False,
                "agentsMd": False,
                "injectDefaultTools": False,
                "tools": ["read_file"],
                "disallowedTools": ["search_tool", "use_tool"],
                "skills": [],
                "mcpServers": [],
                "promptMode": "extend",
                "promptBody": "Follow the caller's requested final-output format exactly.",
            }
        },
    }
    guard_home = Path(children[0]._env["GROK_HOME"])
    session_home = Path(children[1]._env["GROK_HOME"])
    assert not guard_home.exists()
    assert session_home == (
        tmp_path / "local" / "SubagentMCP" / "grok-build" / "home"
    ).resolve()
    assert session_home.is_dir()
    assert children[0]._env["GROK_AUTH_PATH"] == children[1]._env["GROK_AUTH_PATH"]


def test_lifecycle_fake_exercises_canonical_acp_filesystem_wire(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_bytes(b"one\ntwo\nthree\n")
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="filesystem-wire",
    )
    context = _lifecycle_context(adapter, workspace, writer=True)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "succeeded"
        assert terminal.evidence["reverse_io"] == {
            "scope": "native-session-cumulative",
            "read_attempts": 1,
            "read_successes": 1,
            "write_attempts": 1,
            "write_successes": 1,
            "terminal_attempts": 0,
            "terminal_denials": 0,
            "saturated": False,
        }
        closed = await adapter.close(request)
        assert closed.evidence["reverse_io"] == terminal.evidence["reverse_io"]

    asyncio.run(scenario())
    assert (workspace / "allowed.txt").read_text("utf-8") == (
        "written-through-real-acp-wire\n"
    )
    session_new = next(
        row
        for row in _trace_records(trace_path)
        if row.get("childRole") == "session" and row.get("method") == "session/new"
    )
    assert session_new["params"] == {
        "cwd": str(workspace.resolve()),
        "mcpServers": [],
        "_meta": {
            "agentProfile": {
                "name": "subagent-mcp-writer",
                "description": "Bounded Subagent MCP writer profile.",
                "permissionMode": "bypassPermissions",
                "discoverSkills": False,
                "inheritSkills": False,
                "agentsMd": False,
                "injectDefaultTools": False,
                "tools": ["read_file", "search_replace"],
                "disallowedTools": ["search_tool", "use_tool"],
                "skills": [],
                "mcpServers": [],
                "promptMode": "extend",
                "promptBody": "Follow the caller's requested final-output format exactly.",
            }
        },
    }
    assert all(child.closed for child in children)


@pytest.mark.parametrize(
    "model_state_mutation",
    ("agent-type-missing", "agent-type-strict", "agent-type-mismatch"),
)
def test_model_agent_type_fails_closed_before_session_new_or_prompt(
    tmp_path: Path,
    model_state_mutation: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        model_state_mutation=model_state_mutation,
    )
    context = _lifecycle_context(adapter, workspace)

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.spawn(_lifecycle_spawn_request(context)))

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert rejected.value.retryable is False
    session_methods = [
        row["method"]
        for row in _trace_records(trace_path)
        if row.get("childRole") == "session"
    ]
    assert session_methods == [
        "initialize",
        "initialized",
        "authenticate",
        "_x.ai/models/list",
    ]
    assert "session/new" not in session_methods
    assert "session/prompt" not in session_methods
    assert len(children) == 2 and all(child.closed for child in children)


@pytest.mark.parametrize(
    "session_model_state_mutation",
    ("agent-type-missing", "agent-type-strict", "agent-type-mismatch"),
)
def test_session_new_revalidates_model_agent_type_before_prompt(
    tmp_path: Path,
    session_model_state_mutation: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        session_model_state_mutation=session_model_state_mutation,
    )
    context = _lifecycle_context(adapter, workspace)

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.spawn(_lifecycle_spawn_request(context)))

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert rejected.value.retryable is False
    session_methods = [
        row["method"]
        for row in _trace_records(trace_path)
        if row.get("childRole") == "session"
    ]
    assert session_methods == [
        "initialize",
        "initialized",
        "authenticate",
        "_x.ai/models/list",
        "session/new",
    ]
    assert "session/prompt" not in session_methods
    assert len(children) == 2 and all(child.closed for child in children)


def test_lifecycle_review_uses_reverse_read_and_denies_write_and_terminal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_bytes(b"one\ntwo\nthree\n")
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="filesystem-review-boundary",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "succeeded"
        assert terminal.evidence["reverse_io"] == {
            "scope": "native-session-cumulative",
            "read_attempts": 1,
            "read_successes": 1,
            "write_attempts": 1,
            "write_successes": 0,
            "terminal_attempts": 1,
            "terminal_denials": 1,
            "saturated": False,
        }
        closed = await adapter.close(request)
        assert closed.evidence["reverse_io"] == terminal.evidence["reverse_io"]

    asyncio.run(scenario())
    session_new = next(
        row
        for row in _trace_records(trace_path)
        if row.get("childRole") == "session" and row.get("method") == "session/new"
    )
    assert session_new["params"] == {
        "cwd": str(workspace.resolve()),
        "mcpServers": [],
        "_meta": {
            "agentProfile": {
                "name": "subagent-mcp-review",
                "description": "Bounded Subagent MCP review profile.",
                "permissionMode": "bypassPermissions",
                "discoverSkills": False,
                "inheritSkills": False,
                "agentsMd": False,
                "injectDefaultTools": False,
                "tools": ["read_file"],
                "disallowedTools": ["search_tool", "use_tool"],
                "skills": [],
                "mcpServers": [],
                "promptMode": "extend",
                "promptBody": "Follow the caller's requested final-output format exactly.",
            }
        },
    }
    assert not (workspace / "denied.txt").exists()
    assert all(child.closed for child in children)


def test_startup_locks_cover_inspect_guard_and_session_handshake_without_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    events: list[str] = []
    adapter, _trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        lifecycle_events=events,
    )
    original_inspect = adapter._inspect_reader

    def inspect_with_trace(
        current: GrokBinding,
        current_workspace: str,
    ) -> GrokInspectObservation:
        events.append("inspect")
        result = original_inspect(current, current_workspace)
        assert isinstance(result, GrokInspectObservation)
        return result

    adapter._inspect_reader = inspect_with_trace

    @contextmanager
    def traced_lock(paths: tuple[Path, ...]) -> object:
        role = (
            "billing-guard"
            if any("billing-guards" in str(path) for path in paths)
            else "session"
        )
        events.append(f"{role}:lock-enter")
        yield
        events.append(f"{role}:lock-exit")

    monkeypatch.setattr(grok_module, "_locked_grok_startup", traced_lock)
    context = _lifecycle_context(adapter, workspace)
    events.clear()

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, request)
        await adapter.close(request)

    asyncio.run(scenario())

    assert events.index("session:lock-enter") < events.index("inspect")
    assert events.index("inspect") < events.index("billing-guard:lock-enter")
    assert events.index("billing-guard:_x.ai/auto-topup-rule") < events.index(
        "billing-guard:lock-exit"
    )
    assert events.index("billing-guard:lock-exit") < events.index("session:create")
    assert events.index("session:session/new") < events.index("session:lock-exit")
    assert events.index("session:lock-exit") < events.index("session:session/prompt")
    assert len(children) == 2


def test_startup_rejects_workspace_native_extension_directories_before_child(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".grok").mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        with pytest.raises(ServiceError) as rejected:
            await adapter.spawn(_lifecycle_spawn_request(context))
        assert rejected.value.code in {"CAPABILITY_MISSING", "CONTEXT_DRIFT"}

    asyncio.run(scenario())
    assert children == []
    assert _trace_records(trace_path) == []


def test_post_session_inspect_catches_surface_race_before_prompt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    clean = _inspect(binding, workspace)
    drift = replace(clean, hooks=("late-hook",))
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect_sequence=(clean, clean, drift),
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        with pytest.raises(ServiceError) as rejected:
            await adapter.spawn(_lifecycle_spawn_request(context))
        assert rejected.value.code in {"CAPABILITY_MISSING", "CONTEXT_DRIFT"}

    asyncio.run(scenario())
    methods = [row["method"] for row in _trace_records(trace_path)]
    assert "session/new" in methods
    assert "session/prompt" not in methods
    assert all(child.closed is True for child in children)


def test_followup_surface_drift_blocks_before_new_guard_or_prompt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    clean = _inspect(binding, workspace)
    drift = replace(clean, plugins=("late-plugin",))
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect_sequence=(clean, clean, clean, clean, drift),
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        first = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, first)
        followup = AdapterSendRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            "Must not reach the provider.",
            None,
            {},
            context,  # type: ignore[arg-type]
        )
        with pytest.raises(ServiceError) as rejected:
            await adapter.send(followup)
        assert rejected.value.code in {"CAPABILITY_MISSING", "CONTEXT_DRIFT"}
        await adapter.close(first)

    asyncio.run(scenario())
    records = _trace_records(trace_path)
    assert [row["method"] for row in records].count("session/prompt") == 1
    assert len([child for child in children if "billing-" in child._env["GROK_HOME"]]) == 1


def test_terminal_nested_instruction_addition_becomes_context_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    binding = _binding(tmp_path)
    _install_dynamic_git_manifest(tmp_path, workspace, monkeypatch)
    prompt_started = asyncio.Event()
    prompt_release = asyncio.Event()
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=replace(
            _inspect(binding, workspace),
            project_root=str(workspace.resolve()),
        ),
        prompt_write_started=prompt_started,
        prompt_write_release=prompt_release,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await asyncio.wait_for(prompt_started.wait(), timeout=1)
        (nested / "AGENT.md").write_text("late rule\n", "utf-8")
        prompt_release.set()
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "failed"
        assert terminal.error is not None
        assert terminal.error.code == "CONTEXT_DRIFT"
        await adapter.close(request)

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ("modify", "remove"))
def test_nested_instruction_mutation_or_removal_blocks_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    instruction = nested / "Claude.md"
    instruction.write_text("original\n", "utf-8")
    binding = _binding(tmp_path)
    _install_dynamic_git_manifest(tmp_path, workspace, monkeypatch)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=replace(
            _inspect(binding, workspace),
            project_root=str(workspace.resolve()),
        ),
    )
    context = _lifecycle_context(adapter, workspace)
    if mutation == "modify":
        instruction.write_text("changed!\n", "utf-8")
    else:
        instruction.unlink()

    async def scenario() -> None:
        with pytest.raises(ServiceError) as rejected:
            await adapter.spawn(_lifecycle_spawn_request(context))
        assert rejected.value.code == "CONTEXT_DRIFT"

    asyncio.run(scenario())
    assert children == []
    assert _trace_records(trace_path) == []


@pytest.mark.parametrize(
    ("provider_code", "retryable", "expected_code", "expected_category"),
    (
        ("auth_required", False, "AUTH_REQUIRED", "authentication"),
        ("permission_denied", False, "POLICY_REJECTED", "policy"),
        ("quota_exhausted", False, "QUOTA_PAUSED", "quota"),
        ("upstream_unavailable", False, "PROVIDER_ERROR", "provider"),
        ("upstream_unavailable", True, "PROVIDER_ERROR", "provider"),
    ),
)
def test_billing_guard_preserves_provider_rpc_taxonomy(
    tmp_path: Path,
    provider_code: str,
    retryable: bool,
    expected_code: str,
    expected_category: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        handshake_rpc_method="_x.ai/billing",
        rpc_data={"providerCode": provider_code, "retryable": retryable},
    )
    context = _lifecycle_context(adapter, workspace)

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.spawn(_lifecycle_spawn_request(context)))

    assert rejected.value.code == expected_code
    assert rejected.value.category == expected_category
    expected_retryable = retryable if expected_code == "PROVIDER_ERROR" else False
    assert rejected.value.retryable is expected_retryable
    assert len(children) == 1 and children[0].closed is True
    records = _trace_records(trace_path)
    assert {row.get("childRole") for row in records} == {"billing-guard"}
    assert not any(row["method"] in {"session/new", "session/prompt"} for row in records)


@pytest.mark.parametrize(
    "billing_mutation",
    ("billing-process-exit", "billing-invalid-result", "billing-timeout"),
)
def test_billing_guard_transport_ambiguity_requires_recovery(
    tmp_path: Path,
    billing_mutation: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        billing_mutation=billing_mutation,
    )
    context = _lifecycle_context(adapter, workspace)

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.spawn(_lifecycle_spawn_request(context)))

    assert rejected.value.code == "RECOVERY_REQUIRED"
    assert rejected.value.category == "adapter"
    assert rejected.value.retryable is False
    assert len(children) == 1 and children[0].closed is True
    assert not any(
        row["method"] in {"session/new", "session/prompt"}
        for row in _trace_records(trace_path)
    )


def test_billing_guard_cleanup_completes_before_session_child_and_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_home = tmp_path / "user" / ".grok"
    user_home.mkdir(parents=True)
    sentinel = user_home / "owned.txt"
    sentinel.write_text("unchanged\n", "utf-8")
    events: list[str] = []
    real_rmtree = grok_module.shutil.rmtree

    def tracked_rmtree(path: object, *args: object, **kwargs: object) -> None:
        events.append("billing-guard:home-cleanup")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(grok_module.shutil, "rmtree", tracked_rmtree)
    binding = _binding(tmp_path)
    adapter, _trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        lifecycle_events=events,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, request)
        await adapter.close(request)

    asyncio.run(scenario())
    assert len(children) == 2
    assert events.index("billing-guard:close-done") < events.index(
        "billing-guard:home-cleanup"
    )
    assert events.index("billing-guard:home-cleanup") < events.index(
        "session:create"
    )
    assert events.index("session:create") < events.index("session:session/prompt")
    assert sentinel.read_text("utf-8") == "unchanged\n"
    assert tuple(user_home.iterdir()) == (sentinel,)


def test_billing_guard_cleanup_ambiguity_blocks_session_and_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    context = _lifecycle_context(adapter, workspace)

    def fail_guard_cleanup(path: object, *args: object, **kwargs: object) -> None:
        raise OSError("PRIVATE_GUARD_CLEANUP_DETAIL")

    monkeypatch.setattr(grok_module.shutil, "rmtree", fail_guard_cleanup)
    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.spawn(_lifecycle_spawn_request(context)))

    assert rejected.value.code == "RECOVERY_REQUIRED"
    assert "PRIVATE_GUARD_CLEANUP_DETAIL" not in str(rejected.value)
    assert len(children) == 1 and children[0].closed is True
    assert not any(
        row["method"] in {"session/new", "session/prompt"}
        for row in _trace_records(trace_path)
    )


def test_billing_guard_cancellation_cleans_owned_child_and_home(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        billing_mutation="billing-timeout",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        pending = asyncio.create_task(adapter.spawn(_lifecycle_spawn_request(context)))
        for _ in range(100):
            if any(
                row["method"] == "_x.ai/billing"
                for row in _trace_records(trace_path)
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("billing guard did not reach the billing request")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())
    assert len(children) == 1 and children[0].closed is True
    assert not Path(children[0]._env["GROK_HOME"]).exists()
    assert not any(
        row["method"] in {"session/new", "session/prompt"}
        for row in _trace_records(trace_path)
    )
@pytest.mark.parametrize(
    ("billing_mutation", "expected_code"),
    (
        ("billing-method-missing", "CAPABILITY_MISSING"),
        ("billing-explicit-exhaustion", "QUOTA_PAUSED"),
        ("billing-missing-config", "CAPABILITY_MISSING"),
        ("included-invalid", "CAPABILITY_MISSING"),
        ("included-exhausted", "QUOTA_PAUSED"),
        ("prepaid-nonzero", "POLICY_REJECTED"),
        ("prepaid-unknown", "CAPABILITY_MISSING"),
        ("on-demand-cap-nonzero", "POLICY_REJECTED"),
        ("on-demand-enabled", "POLICY_REJECTED"),
        ("not-unified-billing", "POLICY_REJECTED"),
        ("auto-topup-enabled", "POLICY_REJECTED"),
        ("auto-topup-malformed", "CAPABILITY_MISSING"),
    ),
)
def test_lifecycle_spawn_billing_gate_fails_closed_before_session_or_prompt(
    tmp_path: Path,
    billing_mutation: str,
    expected_code: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        billing_mutation=billing_mutation,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        try:
            started = await adapter.spawn(_lifecycle_spawn_request(context))
        except ServiceError as rejected:
            assert rejected.code == expected_code
            assert rejected.retryable is False
            if billing_mutation == "billing-method-missing":
                assert rejected.next_action is not None
            return
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, request)
        await adapter.close(request)
        pytest.fail("unsafe Grok billing state reached session/new")

    asyncio.run(scenario())
    assert len(children) == 1 and children[0].closed is True
    methods = {record["method"] for record in _trace_records(trace_path)}
    assert "session/new" not in methods
    assert "session/prompt" not in methods


def test_provider_no_spend_accepts_exact_grok_105_nested_billing_shape() -> None:
    billing = {
        "config": {
            "prepaidBalance": {"val": 0},
            "onDemandCap": {"val": 0},
            "isUnifiedBillingUser": True,
        }
    }

    assert grok_module._validate_provider_no_spend(billing, {}) is True


@pytest.mark.parametrize("auto_topup", ({"rule": None}, {"rule": {}}))
def test_provider_no_spend_accepts_official_serde_default_zero_fields(
    auto_topup: dict[str, object],
) -> None:
    billing = {
        "config": {
            "prepaidBalance": {},
            "onDemandCap": {},
            "isUnifiedBillingUser": True,
        }
    }

    assert grok_module._validate_provider_no_spend(billing, auto_topup) is True


def test_lifecycle_spawn_accepts_official_serde_default_billing_shape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        billing_mutation="serde-defaults",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, request)
        await adapter.close(request)

    asyncio.run(scenario())
    assert [row["method"] for row in _trace_records(trace_path)].count(
        "session/prompt"
    ) == 1


def test_snapshot_does_not_claim_unreported_spend_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    context = _lifecycle_context(adapter, workspace)

    snapshot = grok_module._grok_snapshot(
        context,
        session_id="native-session",
        execution_id="execution",
        execution_state="running",
        quota_state="unknown",
        reverse_io={
            "scope": "native-session-cumulative",
            "read_attempts": 0,
            "read_successes": 0,
            "write_attempts": 0,
            "write_successes": 0,
            "terminal_attempts": 0,
            "terminal_denials": 0,
            "saturated": False,
            "private_path": "C:\\PRIVATE\\README.md",
        },
    )

    assert snapshot.evidence["quota_state"] == "unknown"
    assert snapshot.evidence["reverse_io"] == {
        "scope": "native-session-cumulative",
        "read_attempts": 0,
        "read_successes": 0,
        "write_attempts": 0,
        "write_successes": 0,
        "terminal_attempts": 0,
        "terminal_denials": 0,
        "saturated": False,
    }
    assert "PRIVATE" not in repr(snapshot.evidence["reverse_io"])
    for key in (
        "api_key_override",
        "custom_paid_route",
        "fallback_configured",
        "no_extra_spend",
        "is_using_overage",
        "overage_blocked",
    ):
        assert key not in snapshot.evidence


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-auth",
        "api-key",
        "interactive-auth",
        "malformed-default-auth",
        "duplicate-auth",
        "protocol-mismatch",
        "malformed-auth-response",
        "missing-session-id",
        "models-current-mismatch",
        "model-mismatch",
        "missing-model",
        "multiple-model",
        "reasoning-mismatch",
        "missing-reasoning",
        "multiple-reasoning",
        "malformed-selected",
        "cwd-mismatch",
        "missing-cwd",
        "missing-session-config",
    ),
)
def test_lifecycle_spawn_unsafe_handshake_closes_child_before_prompt(
    tmp_path: Path,
    mutation: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        mutation=mutation,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        with pytest.raises(ServiceError) as rejected:
            await adapter.spawn(_lifecycle_spawn_request(context))
        assert rejected.value.code == "CAPABILITY_MISSING"
        assert rejected.value.retryable is False

    asyncio.run(scenario())
    guard_handshake_mutations = {
        "missing-auth",
        "api-key",
        "interactive-auth",
        "malformed-default-auth",
        "duplicate-auth",
        "protocol-mismatch",
        "malformed-auth-response",
    }
    expected_children = 1 if mutation in guard_handshake_mutations else 2
    assert len(children) == expected_children
    assert all(child.closed is True for child in children)
    assert "session/prompt" not in {
        record["method"] for record in _trace_records(trace_path)
    }


def test_lifecycle_spawn_unsafe_handshake_cleanup_failure_stays_ambiguous(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        mutation="missing-auth",
        close_error=True,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        with pytest.raises(ServiceError) as rejected:
            await adapter.spawn(_lifecycle_spawn_request(context))
        assert rejected.value.code == "RECOVERY_REQUIRED"
        assert rejected.value.retryable is False

    asyncio.run(scenario())
    assert len(children) == 1
    assert children[0].closed is True
    assert (
        children[0]._close_timeout
        == _LIFECYCLE_PROCESS_CLEANUP_TIMEOUT_SECONDS
    )
    guard_home = Path(children[0]._env["GROK_HOME"])
    guard_config = guard_home / "config.toml"
    assert guard_home.is_dir()
    assert guard_config.read_bytes() == _isolation_config(guard_home)
    if sys.platform == "win32":
        assert guard_config.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    assert "session/prompt" not in {
        record["method"] for record in _trace_records(trace_path)
    }
    guard_config.chmod(stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(guard_home)


def test_context_rejects_executable_extensions_before_starting_acp(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    observed = _inspect(binding, workspace, mcp_servers=("project-mcp",))
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=observed,
    )

    with pytest.raises(ServiceError) as rejected:
        _lifecycle_context(adapter, workspace, writer=True)

    assert rejected.value.code == "CAPABILITY_MISSING"
    assert children == []
    assert _trace_records(trace_path) == []


def test_lifecycle_spawn_rechecks_inspect_and_rejects_extension_drift_before_child(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged\n", "utf-8")
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect_sequence=(
            _inspect(binding, workspace),
            _inspect(binding, workspace, mcp_servers=("late-project-mcp",)),
        ),
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        try:
            started = await adapter.spawn(_lifecycle_spawn_request(context))
        except ServiceError as rejected:
            assert rejected.code == "CAPABILITY_MISSING"
            assert rejected.retryable is False
            return
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await adapter.close(request)
        pytest.fail("Grok inspect drift reached ACP child launch")

    asyncio.run(scenario())
    assert children == []
    assert _trace_records(trace_path) == []
    assert outside.read_text("utf-8") == "unchanged\n"


@pytest.mark.parametrize("materialize_at", ("authenticate", "session/prompt"))
def test_lifecycle_hides_remote_bundled_skills_materialized_during_turn(
    tmp_path: Path,
    materialize_at: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        materialize_bundled_skill_at=materialize_at,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "succeeded"
        await adapter.close(request)

    asyncio.run(scenario())
    assert (
        tmp_path
        / "local"
        / "SubagentMCP"
        / "grok-build"
        / "home"
        / "bundled"
        / "skills"
        / "remote"
        / "SKILL.md"
    ).is_file()


def test_lifecycle_spawn_rejects_concurrent_duplicate_conversation_before_child(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="long",
        handshake_delay=0.1,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        first_task = asyncio.create_task(
            adapter.spawn(_lifecycle_spawn_request(context))
        )
        await asyncio.sleep(0.02)
        with pytest.raises(ServiceError) as duplicate:
            await adapter.spawn(_lifecycle_spawn_request(context))
        assert duplicate.value.code == "SESSION_BUSY"
        started = await first_task
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await adapter.close(request)

    asyncio.run(scenario())
    assert len(children) == 2
    assert all(child.closed is True for child in children)


def test_lifecycle_send_reuses_native_session_and_rejects_concurrent_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        first_request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, first_request)
        followup = AdapterSendRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            "Review the follow-up.",
            None,
            {},
            context,  # type: ignore[arg-type]
        )
        second = await adapter.send(followup)
        assert second.external_session_id == started.external_session_id
        assert second.execution_state == "running"
        with pytest.raises(ServiceError) as busy:
            await adapter.send(replace(followup, execution_id="execution-grok-3"))
        assert busy.value.code == "SESSION_BUSY"
        second_request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            second.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, second_request)
        await adapter.close(second_request)

    asyncio.run(scenario())
    assert len(children) == 3
    assert all(child.closed is True for child in children)
    records = _trace_records(trace_path)
    guard_records = [
        record for record in records if record.get("childRole") == "billing-guard"
    ]
    session_records = [
        record for record in records if record.get("childRole") == "session"
    ]
    assert [record["method"] for record in guard_records].count(
        "_x.ai/billing"
    ) == 2
    assert [record["method"] for record in guard_records].count(
        "_x.ai/auto-topup-rule"
    ) == 2
    assert not any(
        record["method"] in {"_x.ai/billing", "_x.ai/auto-topup-rule"}
        for record in session_records
    )
    assert [record["method"] for record in session_records].count(
        "session/prompt"
    ) == 2
    guard_homes = [Path(child._env["GROK_HOME"]) for child in (children[0], children[2])]
    assert guard_homes[0] != guard_homes[1]
    assert all(not home.exists() for home in guard_homes)


@pytest.mark.parametrize("drift", ("pair_key", "writer_write_set"))
def test_lifecycle_send_rejects_full_resumed_authority_before_guard_or_prompt(
    tmp_path: Path,
    drift: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    if drift == "writer_write_set":
        (workspace / "src").mkdir()
        (workspace / "docs").mkdir()
        assert asyncio.run(adapter.probe()).state == "needs_canary"
        context = asyncio.run(
            adapter.resolve_context(
                _request(
                    workspace,
                    permissions=("repo_read", "workspace_write"),
                    write_set=("src", "docs"),
                )
            )
        )
    else:
        context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        first = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, first)
        before_records = _trace_records(trace_path)
        before_children = len(children)
        attestation = json.loads(json.dumps(context.attestation))
        if drift == "pair_key":
            attestation["pair_key"] = "d" * 64
        else:
            attestation["write_set"] = ["src"]
        drifted = replace(context, attestation=attestation)
        rejected: ServiceError | None = None
        second = None
        try:
            second = await adapter.send(
                AdapterSendRequest(
                    "conversation-grok",
                    "execution-grok-2",
                    started.external_session_id,
                    "This turn must never reach Grok.",
                    None,
                    {},
                    drifted,
                )
            )
        except ServiceError as exc:
            rejected = exc
        if second is None:
            await adapter.close(first)
        else:
            second_request = AdapterSessionRequest(
                "conversation-grok",
                "execution-grok-2",
                second.external_session_id,
                second.external_execution_id,
            )
            await _lifecycle_wait_terminal(adapter, second_request)
            await adapter.close(second_request)

        assert rejected is not None
        assert rejected.code == "CONTEXT_DRIFT"
        assert len(children) == before_children
        assert _trace_records(trace_path) == before_records

    asyncio.run(scenario())


def test_lifecycle_send_rechecks_billing_and_blocks_newly_unsafe_route(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        billing_mutation="second-auto-topup-enabled",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        first_request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, first_request)
        followup = AdapterSendRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            "Review the follow-up.",
            None,
            {},
            context,  # type: ignore[arg-type]
        )
        second = None
        try:
            with pytest.raises(ServiceError) as rejected:
                second = await adapter.send(followup)
            assert rejected.value.code == "POLICY_REJECTED"
            assert rejected.value.retryable is False
        finally:
            close_request = (
                first_request
                if second is None
                else AdapterSessionRequest(
                    "conversation-grok",
                    "execution-grok-2",
                    second.external_session_id,
                    second.external_execution_id,
                )
            )
            await adapter.close(close_request)

    asyncio.run(scenario())
    assert len(children) == 3 and all(child.closed is True for child in children)
    records = _trace_records(trace_path)
    guard_records = [
        record for record in records if record.get("childRole") == "billing-guard"
    ]
    session_records = [
        record for record in records if record.get("childRole") == "session"
    ]
    assert [record["method"] for record in guard_records].count(
        "_x.ai/billing"
    ) == 2
    assert [record["method"] for record in guard_records].count(
        "_x.ai/auto-topup-rule"
    ) == 2
    assert not any(
        record["method"] in {"_x.ai/billing", "_x.ai/auto-topup-rule"}
        for record in session_records
    )
    assert [record["method"] for record in session_records].count(
        "session/prompt"
    ) == 1


def test_lifecycle_long_running_turn_has_no_elapsed_timeout_and_snapshot_is_local(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="long",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await asyncio.sleep(0.7)
        before = len(_trace_records(trace_path))
        observed = await adapter.snapshot(request)
        assert observed.execution_state == "running"
        assert len(_trace_records(trace_path)) == before
        await adapter.close(request)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("fake_scenario", "expected_state", "expected_result"),
    (
        ("cancelled", "interrupted", None),
        ("cancel-late-success", "succeeded", "LATE SUCCESS"),
    ),
)
def test_lifecycle_interrupt_sends_cancel_once_and_reconciles_late_terminal(
    tmp_path: Path,
    fake_scenario: str,
    expected_state: str,
    expected_result: str | None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario=fake_scenario,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        first = await adapter.interrupt(request)
        second = await adapter.interrupt(request)
        assert first.execution_state == second.execution_state == expected_state
        assert first.result_text == second.result_text == expected_result
        await adapter.close(request)

    asyncio.run(scenario())
    assert [record["method"] for record in _trace_records(trace_path)].count(
        "session/cancel"
    ) == 1


def test_lifecycle_interrupt_cannot_overtake_delayed_prompt_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    prompt_write_started = asyncio.Event()
    prompt_write_release = asyncio.Event()
    write_order: list[str] = []
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="cancel-late-success",
        prompt_write_started=prompt_write_started,
        prompt_write_release=prompt_write_release,
        write_order=write_order,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await asyncio.wait_for(prompt_write_started.wait(), timeout=1)
        interrupt = asyncio.create_task(adapter.interrupt(request))
        for _ in range(20):
            if write_order:
                break
            await asyncio.sleep(0)
        prompt_write_release.set()
        interrupted = await asyncio.wait_for(interrupt, timeout=1)
        assert interrupted.execution_state == "succeeded"
        assert write_order[:2] == ["session/prompt", "session/cancel"]
        await adapter.close(request)

    asyncio.run(scenario())


def test_lifecycle_interrupt_does_not_cancel_when_prompt_write_fails_first(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    prompt_write_started = asyncio.Event()
    prompt_write_release = asyncio.Event()
    write_order: list[str] = []
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="long",
        prompt_write_started=prompt_write_started,
        prompt_write_release=prompt_write_release,
        prompt_write_error=True,
        write_order=write_order,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await asyncio.wait_for(prompt_write_started.wait(), timeout=1)
        interrupt = asyncio.create_task(adapter.interrupt(request))
        for _ in range(20):
            if write_order:
                break
            await asyncio.sleep(0)
        prompt_write_release.set()
        interrupted = await asyncio.wait_for(interrupt, timeout=1)
        assert interrupted.execution_state == "failed"
        assert interrupted.error is not None
        assert interrupted.error.code == "RECOVERY_REQUIRED"
        assert "session/cancel" not in write_order
        await adapter.close(request)

    asyncio.run(scenario())


def test_lifecycle_close_active_is_exact_idempotent_and_restart_is_recovery_required(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="long",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        first = await adapter.close(request)
        second = await adapter.close(request)
        assert first == second
        assert first.conversation_state == "closed"
        assert first.evidence["cleanup_confirmed"] is True

        restarted, _trace, _fresh_children = _lifecycle_adapter(tmp_path, binding)
        with pytest.raises(ServiceError) as unavailable:
            await restarted.open_session(request)
        assert unavailable.value.code == "RECOVERY_REQUIRED"

    asyncio.run(scenario())
    assert len(children) == 2
    assert all(child.closed is True for child in children)


def test_lifecycle_close_ambiguity_keeps_closed_child_unusable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="cancel-timeout",
        cancel_timeout=0.05,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        with pytest.raises(ServiceError) as ambiguous:
            await adapter.close(request)
        assert ambiguous.value.code == "RECOVERY_REQUIRED"
        assert children[0].closed is True
        followup = AdapterSendRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            "Must not reach a closed child.",
            None,
            {},
            context,  # type: ignore[arg-type]
        )
        with pytest.raises(ServiceError) as closed:
            await adapter.send(followup)
        assert closed.value.code == "SESSION_CLOSED"

    asyncio.run(scenario())


def test_lifecycle_close_blocks_a_new_turn_while_process_cleanup_runs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        close_delay=0.1,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, request)
        close_task = asyncio.create_task(adapter.close(request))
        await asyncio.sleep(0.02)
        followup = AdapterSendRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            "Must not race process cleanup.",
            None,
            {},
            context,  # type: ignore[arg-type]
        )
        with pytest.raises(ServiceError) as closed:
            await adapter.send(followup)
        assert closed.value.code == "SESSION_CLOSED"
        await close_task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider_code", "retryable", "code", "category", "public_retryable"),
    (
        ("authentication_required", False, "AUTH_REQUIRED", "authentication", False),
        ("permission_denied", False, "POLICY_REJECTED", "policy", False),
        ("model_not_found", False, "CAPABILITY_MISSING", "capability", False),
        ("quota_exhausted", False, "QUOTA_PAUSED", "quota", False),
        ("upstream_busy", True, "PROVIDER_ERROR", "provider", True),
        ("future_unknown", False, "PROVIDER_ERROR", "provider", False),
    ),
)
def test_lifecycle_terminal_error_taxonomy_is_explicit_and_sanitized(
    tmp_path: Path,
    provider_code: str,
    retryable: bool,
    code: str,
    category: str,
    public_retryable: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        error_code=provider_code,
        error_retryable=retryable,
        error_detail="bounded provider detail",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "failed"
        assert terminal.error is not None
        assert terminal.error.code == code
        assert terminal.error.category == category
        assert terminal.error.retryable is public_retryable
        assert "PRIVATE_PROVIDER_DETAIL" not in repr(terminal)
        assert terminal.evidence["provider_error"] == {
            "source": "native-acp",
            "provider_code": provider_code,
            "detail": "bounded provider detail",
        }
        await adapter.close(request)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    (
        "stop_reason",
        "code",
        "category",
        "next_action_fragment",
    ),
    (
        ("max_tokens", "MAX_TOKENS_REACHED", "provider", "bounded follow-up"),
        (
            "max_turn_requests",
            "MAX_TURN_REQUESTS_REACHED",
            "provider",
            "new bounded task",
        ),
        ("refusal", "REQUEST_REFUSED", "policy", "materially revised request"),
    ),
)
def test_lifecycle_standard_non_success_stop_reasons_are_precise_and_actionable(
    tmp_path: Path,
    stop_reason: str,
    code: str,
    category: str,
    next_action_fragment: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        stop_reason=stop_reason,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "failed"
        assert terminal.error is not None
        assert terminal.error.code == code
        assert terminal.error.category == category
        assert terminal.error.retryable is False
        assert terminal.error.next_action is not None
        assert next_action_fragment in terminal.error.next_action
        assert terminal.evidence["stop_reason"] == stop_reason
        assert terminal.evidence["quota_state"] == "unknown"
        assert "provider_error" not in terminal.evidence
        await adapter.close(request)

    asyncio.run(scenario())
    methods = [record["method"] for record in _trace_records(trace_path)]
    assert methods.count("session/prompt") == 1
    assert not any(
        isinstance(method, str)
        and any(term in method.casefold() for term in ("credit", "fallback", "model/set"))
        for method in methods
    )


def test_lifecycle_unknown_stop_reason_remains_fail_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        stop_reason="future_stop_reason",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "failed"
        assert terminal.error is not None
        assert terminal.error.code == "PROVIDER_ERROR"
        assert terminal.error.retryable is False
        assert terminal.error.next_action is None
        assert terminal.evidence["stop_reason"] == "future_stop_reason"
        await adapter.close(request)

    asyncio.run(scenario())


@pytest.mark.parametrize("fake_scenario", ("process-exit", "malformed-terminal"))
def test_lifecycle_process_exit_or_malformed_terminal_is_recovery_required(
    tmp_path: Path,
    fake_scenario: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario=fake_scenario,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "failed"
        assert terminal.error is not None
        assert terminal.error.code == "RECOVERY_REQUIRED"
        assert terminal.error.category == "adapter"
        assert terminal.evidence["cleanup_confirmed"] is False
        closed = await adapter.close(request)
        assert closed.evidence["cleanup_confirmed"] is True

    asyncio.run(scenario())


def test_lifecycle_unknown_quota_success_never_retries_switches_or_uses_credit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(tmp_path, binding)
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.execution_state == "succeeded"
        assert terminal.evidence["quota_state"] == "unknown"
        await adapter.close(request)

    asyncio.run(scenario())
    methods = [record["method"] for record in _trace_records(trace_path)]
    assert methods.count("session/prompt") == 1
    assert not any(
        isinstance(method, str)
        and any(term in method.casefold() for term in ("credit", "fallback", "model/set"))
        for method in methods
    )


def test_lifecycle_prompt_relays_only_controller_attestations_and_write_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "allowed.txt").write_text("ok\n", "utf-8")
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(tmp_path, binding)
    base = _lifecycle_context(adapter, workspace, writer=True)
    attestation = dict(base.attestation)
    attestation["input_attestations"] = (
        {
            "path": "allowed.txt",
            "sha256": "a" * 64,
            "byte_count": 3,
            "source": "controller-only",
            "raw": "SECRET_RAW_METADATA",
        },
    )
    context = replace(base, attestation=attestation)
    task = _lifecycle_spawn_request(context).task
    expected = "\n".join(
        (
            f"Role: {task.role}",
            f"Task: {task.title}",
            task.prompt,
            "Acceptance criteria:",
            "- Return findings.",
            "Trusted input attestations:",
            f"- path=allowed.txt; sha256={'a' * 64}; byte_count=3",
            "Verified write set:",
            f"- {attestation['write_set'][0]}",
        )
    )

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok", "execution-grok-1",
            started.external_session_id, started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, request)
        await adapter.close(request)

    asyncio.run(scenario())
    prompt = next(
        row
        for row in _trace_records(trace_path)
        if row["method"] == "session/prompt"
    )
    assert prompt["params"]["promptBytes"] == len(expected.encode("utf-8"))
    assert prompt["params"]["promptSha256"] == hashlib.sha256(
        expected.encode("utf-8")
    ).hexdigest()
    assert "SECRET_RAW_METADATA" not in repr(prompt)


def test_lifecycle_prompt_rejects_invalid_controller_attestation_before_child(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace_path, children = _lifecycle_adapter(tmp_path, binding)
    base = _lifecycle_context(adapter, workspace)
    context = replace(
        base,
        attestation={
            **dict(base.attestation),
            "input_attestations": (
                {
                    "path": "bad\npath",
                    "sha256": "a" * 64,
                    "byte_count": 1,
                },
            ),
        },
    )
    with pytest.raises(ServiceError, match="input attestation") as rejected:
        asyncio.run(adapter.spawn(_lifecycle_spawn_request(context)))
    assert rejected.value.code == "REQUEST_INVALID"
    assert children == []


def test_lifecycle_rpc_taxonomy_uses_only_structured_provider_fields(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        scenario="rpc-error",
        rpc_code=-32603,
        rpc_message="authentication required; quota exhausted; model not found",
        rpc_data={"retryable": True, "detail": "bounded upstream detail"},
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.error is not None
        assert terminal.error.code == "PROVIDER_ERROR"
        assert terminal.error.retryable is True
        assert terminal.evidence["provider_error"] == {
            "source": "native-acp", "rpc_code": -32603,
            "detail": "bounded upstream detail",
        }
        assert "authentication required" not in repr(terminal)
        await adapter.close(request)

    asyncio.run(scenario())


def test_lifecycle_oversized_structured_error_fields_are_not_persisted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        error_code="x" * 129,
        error_detail="unsafe\ndetail",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        terminal = await _lifecycle_wait_terminal(adapter, request)
        assert terminal.error is not None
        assert terminal.error.code == "PROVIDER_ERROR"
        assert "provider_error" not in terminal.evidence
        assert "unsafe" not in repr(terminal)
        await adapter.close(request)

    asyncio.run(scenario())


def test_lifecycle_actions_reject_wrong_conversation_without_native_effect(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(
        tmp_path, binding, scenario="long"
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        wrong = AdapterSessionRequest(
            "wrong-conversation",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        for action in (adapter.snapshot, adapter.interrupt, adapter.close):
            with pytest.raises(ServiceError) as rejected:
                await action(wrong)
            assert rejected.value.code == "CONTEXT_DRIFT"
        exact = replace(wrong, conversation_id="conversation-grok")
        await adapter.close(exact)

    asyncio.run(scenario())
    assert not any(
        row["method"] == "session/cancel"
        for row in _trace_records(trace_path)[:-1]
    )


def test_lifecycle_stale_interrupt_cannot_cancel_new_turn_after_send_race(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, _children = _lifecycle_adapter(
        tmp_path, binding, scenario="second-long",
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        first = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        await _lifecycle_wait_terminal(adapter, first)
        session = adapter._sessions[started.external_session_id]
        await session.lock.acquire()
        send_task = asyncio.create_task(adapter.send(AdapterSendRequest(
            "conversation-grok", "execution-grok-2", started.external_session_id,
            "Continue safely.", None, {}, context,
        )))
        await asyncio.sleep(0)
        stale_interrupt = asyncio.create_task(adapter.interrupt(first))
        session.lock.release()
        second = await send_task
        assert second.execution_state == "running"
        with pytest.raises(ServiceError) as stale:
            await stale_interrupt
        assert stale.value.code == "CONTEXT_DRIFT"
        second_request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            second.external_execution_id,
        )
        assert (await adapter.snapshot(second_request)).execution_state == "running"
        assert not any(
            row["method"] == "session/cancel"
            for row in _trace_records(trace_path)
        )
        await adapter.close(second_request)

    asyncio.run(scenario())


def test_lifecycle_interrupt_returns_captured_turn_before_queued_send(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, _trace, _children = _lifecycle_adapter(
        tmp_path, binding, scenario="cancel-late-success"
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        first = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-1",
            started.external_session_id,
            started.external_execution_id,
        )
        session = adapter._sessions[started.external_session_id]
        send_tasks: list[asyncio.Task[object]] = []

        def queue_send(_done: asyncio.Task[None]) -> None:
            send_tasks.append(
                asyncio.create_task(
                    adapter.send(
                        AdapterSendRequest(
                            "conversation-grok",
                            "execution-grok-2",
                            started.external_session_id,
                            "Queued follow-up.",
                            None,
                            {},
                            context,
                        )
                    )
                )
            )

        assert session.turn is not None
        session.turn.task.add_done_callback(queue_send)
        interrupted = await adapter.interrupt(first)
        assert interrupted.external_execution_id == "execution-grok-1"
        assert interrupted.result_text == "LATE SUCCESS"
        assert send_tasks
        second = await send_tasks[0]
        assert second.external_execution_id == "execution-grok-2"
        second_request = AdapterSessionRequest(
            "conversation-grok",
            "execution-grok-2",
            started.external_session_id,
            "execution-grok-2",
        )
        await adapter.close(second_request)

    asyncio.run(scenario())


@pytest.mark.parametrize("ambiguous", (False, True))
def test_lifecycle_startup_cancellation_reconciles_owned_child(
    tmp_path: Path,
    ambiguous: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path, binding, handshake_delay=0.2, close_error=ambiguous,
    )
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        task = asyncio.create_task(adapter.spawn(_lifecycle_spawn_request(context)))
        for _ in range(100):
            if children:
                break
            await asyncio.sleep(0.01)
        assert children
        task.cancel()
        if ambiguous:
            with pytest.raises(ServiceError) as error:
                await task
            assert error.value.code == "RECOVERY_REQUIRED"
        else:
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert len(children) == 1 and children[0].closed is True
    assert not any(
        row["method"] == "session/prompt"
        for row in _trace_records(trace_path)
    )


def _canary_pair(
    base_pair_key: str,
    model: str,
    reasoning: Mapping[str, object],
    transport: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "base_pair_key": base_pair_key,
                "model": model,
                "reasoning": reasoning,
                "transport": transport,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_runtime_canary_is_disposable_handshake_only_and_service_compatible(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    assert isinstance(adapter, CanaryAdapter)
    probe = asyncio.run(adapter.probe())
    assert probe.state == "needs_canary"
    reasoning = {"effort": "xhigh"}
    pair = _canary_pair(binding.pair_key, "grok-4", reasoning, "native-acp")
    request = CanaryRequest(
        "grok-build",
        "default",
        "grok-4",
        reasoning,
        "native-acp",
        binding.pair_key,
        pair,
    )

    result = asyncio.run(adapter.runtime_canary(request))

    assert result.passed is True
    assert result.pair_key == pair
    assert result.details == {
        "model": "grok-4",
        "effort": "xhigh",
        "cleanup_confirmed": True,
        "provider_no_spend_safe": True,
        "quota_state": "unknown",
        "effective_agent_type": "grok-build",
        "agent_type_evidence_source": (
            "_x.ai/models/list.availableModels._meta.agentType"
        ),
        "route_isolation": "verified",
        "route_isolation_source": "isolated-home-native-inspect",
    }
    assert len(children) == 2 and all(child.closed is True for child in children)
    records = _trace_records(trace_path)
    guard_records = [
        record for record in records if record.get("childRole") == "billing-guard"
    ]
    session_records = [
        record for record in records if record.get("childRole") == "session"
    ]
    assert [record["method"] for record in guard_records] == [
        "initialize",
        "initialized",
        "authenticate",
        "_x.ai/billing",
        "_x.ai/auto-topup-rule",
    ]
    assert [record["method"] for record in session_records] == [
        "initialize",
        "initialized",
        "authenticate",
        "_x.ai/models/list",
        "session/new",
    ]
    canary_cwd = Path(str(session_records[-1]["params"]["cwd"]))
    assert not canary_cwd.exists()
    assert not canary_cwd.is_relative_to(tmp_path / "workspace")


@pytest.mark.parametrize("drift", ("base", "variant"))
def test_runtime_canary_pair_drift_fails_before_child(
    tmp_path: Path, drift: str
) -> None:
    binding = _binding(tmp_path)
    adapter, _trace_path, children = _lifecycle_adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    result = asyncio.run(
        adapter.runtime_canary(
            CanaryRequest(
                "grok-build",
                "default",
                "grok-4",
                {"effort": "xhigh"},
                "native-acp",
                "f" * 64 if drift == "base" else binding.pair_key,
                "e" * 64,
            )
        )
    )
    assert result.passed is False
    assert result.error is not None
    assert result.error.code == "CONTEXT_DRIFT"
    assert children == []


@pytest.mark.parametrize(
    ("provider_code", "expected_code"),
    (
        ("authentication_required", "AUTH_REQUIRED"),
        ("quota_exhausted", "QUOTA_PAUSED"),
        ("model_not_found", "CAPABILITY_MISSING"),
    ),
)
def test_runtime_canary_maps_only_structured_handshake_failures(
    tmp_path: Path,
    provider_code: str,
    expected_code: str,
) -> None:
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(
        tmp_path,
        binding,
        handshake_rpc_method="authenticate",
        rpc_code=-32603,
        rpc_message="misleading free-form quota auth model words",
        rpc_data={"providerCode": provider_code},
    )
    asyncio.run(adapter.probe())
    reasoning = {"effort": "xhigh"}
    pair = _canary_pair(binding.pair_key, "grok-4", reasoning, "native-acp")
    result = asyncio.run(
        adapter.runtime_canary(
            CanaryRequest(
                "grok-build",
                "default",
                "grok-4",
                reasoning,
                "native-acp",
                binding.pair_key,
                pair,
            )
        )
    )
    assert result.passed is False
    assert result.error is not None and result.error.code == expected_code
    assert len(children) == 1 and children[0].closed is True
    assert "session/prompt" not in {
        record["method"] for record in _trace_records(trace_path)
    }


def test_runtime_canary_cleanup_failure_overrides_a_valid_handshake(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    adapter, _trace_path, children = _lifecycle_adapter(
        tmp_path, binding, close_error=True
    )
    asyncio.run(adapter.probe())
    reasoning = {"effort": "xhigh"}
    pair = _canary_pair(binding.pair_key, "grok-4", reasoning, "native-acp")
    result = asyncio.run(
        adapter.runtime_canary(
            CanaryRequest(
                "grok-build", "default", "grok-4", reasoning,
                "native-acp", binding.pair_key, pair,
            )
        )
    )
    assert result.passed is False
    assert result.error is not None
    assert result.error.code == "RECOVERY_REQUIRED"
    assert len(children) == 1 and children[0].closed is True


def test_service_canary_unblocks_grok_spawn_without_a_canary_prompt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")}, os_name="nt"
    )
    ConfigStore(paths).save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "grok-build": {
                    "enabled": True,
                    "delegation_priority": 100,
                    "selection_mode": "fixed",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "default",
                            "model": "grok-4",
                            "reasoning": {"effort": "xhigh"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    identifiers = iter(range(1, 20))
    service = SubagentMcpService(
        config=ConfigStore(paths),
        store=StateStore.open(paths),
        registry=AdapterRegistry(builtin_factories=(lambda: adapter,)),
        id_factory=lambda prefix: f"{prefix}-{next(identifiers)}",
    )
    spawn = SpawnRequest(
        request_id="grok-before-canary",
        runtime_id="grok-build",
        variant_id="default",
        task=TaskPacket(
            "Review",
            "Review one bounded change.",
            ("Return a verdict.",),
            "reviewer",
        ),
        cwd=str(workspace),
        mode="review",
        transport="native-acp",
        permissions=("repo_read",),
    )
    async def scenario() -> object:
        with pytest.raises(ServiceError):
            await service.agent_spawn(spawn)
        assert children == []
        ready = await service.runtime_canary(
            {
                "request_id": "grok-canary-ready",
                "runtime_id": "grok-build",
                "variant_id": "default",
                "transport": "native-acp",
            }
        )
        assert ready["state"] == "ready"
        started = await service.agent_spawn(
            replace(spawn, request_id="grok-after-canary")
        )
        for _ in range(100):
            statuses = await service.agent_wait(
                WaitRequest((WaitTarget(started.conversation_id),), 0.05)
            )
            if statuses[0].execution_state != "running":
                await service.agent_close(
                    ActionRequest("grok-close", started.conversation_id)
                )
                return statuses[0]
        raise AssertionError("fake Grok service task did not finish")

    terminal = asyncio.run(scenario())
    assert terminal.execution_state == "succeeded"
    records = _trace_records(trace_path)
    assert [row["method"] for row in records].count("session/prompt") == 1
    assert len(children) == 4
    assert all(child.closed is True for child in children)


@pytest.mark.parametrize(
    ("writer", "mutation"),
    (
        (False, None),
        (True, None),
        (False, "pair_key"),
        (True, "write_set"),
    ),
    ids=(
        "reader-roundtrip",
        "two-root-writer-roundtrip",
        "reader-pair-drift",
        "writer-write-set-drift",
    ),
)
def test_service_followup_binds_complete_persisted_grok_authority(
    tmp_path: Path,
    writer: bool,
    mutation: str | None,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    write_set = ("src", "docs") if writer else ()
    for root in write_set:
        (workspace / root).mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")}, os_name="nt"
    )
    ConfigStore(paths).save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "grok-build": {
                    "enabled": True,
                    "delegation_priority": 100,
                    "selection_mode": "fixed",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "default",
                            "model": "grok-4",
                            "reasoning": {"effort": "xhigh"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    store = StateStore.open(paths)
    identifiers = iter(range(1, 50))
    service = SubagentMcpService(
        config=ConfigStore(paths),
        store=store,
        registry=AdapterRegistry(builtin_factories=(lambda: adapter,)),
        id_factory=lambda prefix: f"{prefix}-{next(identifiers)}",
    )
    spawn = SpawnRequest(
        request_id="grok-authority-spawn",
        runtime_id="grok-build",
        variant_id="default",
        task=TaskPacket(
            "Bounded task",
            "Use only the declared authority.",
            ("Return a verdict.",),
            "sub-agent",
        ),
        cwd=str(workspace),
        mode="implement" if writer else "review",
        transport="native-acp",
        permissions=("repo_read", "workspace_write") if writer else ("repo_read",),
        write_set=write_set,
    )

    async def terminal(conversation_id: str) -> object:
        for _ in range(100):
            statuses = await service.agent_wait(
                WaitRequest((WaitTarget(conversation_id),), 0.05)
            )
            if statuses[0].execution_state != "running":
                return statuses[0]
        raise AssertionError("fake Grok service task did not finish")

    async def scenario() -> None:
        ready = await service.runtime_canary(
            {
                "request_id": "grok-authority-canary",
                "runtime_id": "grok-build",
                "variant_id": "default",
                "transport": "native-acp",
            }
        )
        assert ready["state"] == "ready"
        started = await service.agent_spawn(spawn)
        first = await terminal(started.conversation_id)
        assert first.execution_state == "succeeded"
        before_records = _trace_records(trace_path)
        before_children = len(children)

        if mutation is not None:
            previous = store.load_latest_execution(started.conversation_id)
            with store.transaction(write=True) as database:
                raw = database.execute(
                    "SELECT observed_json FROM executions WHERE execution_id = ?",
                    (previous.execution_id,),
                ).fetchone()[0]
                observed = json.loads(raw)
                original_hash = observed["context_hash"]
                if mutation == "pair_key":
                    observed["attestation"]["pair_key"] = "d" * 64
                else:
                    observed["attestation"]["write_set"] = ["src"]
                assert observed["context_hash"] == original_hash
                database.execute(
                    "UPDATE executions SET observed_json = ? WHERE execution_id = ?",
                    (json.dumps(observed, sort_keys=True), previous.execution_id),
                )

        rejected: ServiceError | None = None
        followed = None
        try:
            followed = await service.agent_send(
                SendRequest(
                    "grok-authority-followup",
                    started.conversation_id,
                    "Continue with the exact retained authority.",
                )
            )
        except ServiceError as exc:
            rejected = exc

        if followed is not None:
            second = await terminal(started.conversation_id)
            await service.agent_close(
                ActionRequest("grok-authority-close", started.conversation_id)
            )
        else:
            owned = adapter._sessions[first.external_session_id].snapshot
            await adapter.close(
                AdapterSessionRequest(
                    started.conversation_id,
                    str(owned.external_execution_id),
                    first.external_session_id,
                    owned.external_execution_id,
                )
            )

        if mutation is None:
            assert rejected is None
            assert followed is not None
            assert second.execution_state == "succeeded"
            assert len(children) == before_children + 1
            assert [row["method"] for row in _trace_records(trace_path)].count(
                "session/prompt"
            ) == [row["method"] for row in before_records].count(
                "session/prompt"
            ) + 1
        else:
            assert rejected is not None
            assert rejected.code == "CONTEXT_DRIFT"
            assert len(children) == before_children
            assert _trace_records(trace_path) == before_records

    asyncio.run(scenario())
