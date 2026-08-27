from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

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


def _binding(tmp_path: Path, *, name: str = "grok.exe", marker: str = "") -> GrokBinding:
    executable = tmp_path / name
    if not executable.exists():
        executable.write_bytes(b"synthetic-grok")
    binding = locate_grok_binding(
        executable_resolver=lambda requested: str(executable) if requested == "grok" else None,
        contract_reader=lambda resolved: _contract(marker=marker),
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
    return GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: catalog,
        inspect_reader=lambda _binding, _workspace: inspect
        or _inspect(binding, workspace),
        platform="win32",
        environment=environment or {},
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


def test_manifest_is_exact_and_contains_no_model_ids(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, _binding(tmp_path))

    assert adapter.manifest.to_dict() == {
        "adapter_api_version": ADAPTER_API_VERSION,
        "runtime_id": "grok-build",
        "provider_id": "xai",
        "harness_id": "grok-build",
        "display_name": "Grok Build",
        "adapter_version": "1.0.0",
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
    not_called = lambda: (_ for _ in ()).throw(AssertionError("locator called"))
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
        "PATH": "synthetic-path",
        "SYSTEMROOT": "C:\\Windows",
        "USERNAME": "Example",
        "USERPROFILE": "C:\\Users\\Example",
        "SSL_CERT_FILE": "C:\\certs\\root.pem",
        "XAI_API_KEY": "must-not-leak",
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
    assert launch.permission_mode == "dontAsk"
    assert launch.write_roots == ()
    assert launch.argv == (
        str(binding.executable_path),
        "--no-auto-update",
        "--cwd",
        str(workspace.resolve()),
        "--model",
        "future/model:opaque@1",
        "--reasoning-effort",
        "highest-native",
        "--permission-mode",
        "dontAsk",
        "--disable-web-search",
        "--no-subagents",
        "agent",
        "--no-leader",
        "stdio",
    )
    assert dict(launch.env) == {
        "GROK_DISABLE_AUTOUPDATER": "1",
        "PATH": "synthetic-path",
        "SSL_CERT_FILE": "C:\\certs\\root.pem",
        "SYSTEMROOT": "C:\\Windows",
        "USERNAME": "Example",
        "USERPROFILE": "C:\\Users\\Example",
    }
    assert first.attestation["cached_native_login"] == "not_exposed"
    assert first.attestation["no_extra_spend"] == "not_exposed"
    assert first.attestation["builtin_tool_inventory"] == "not_exposed"
    assert first.attestation["provider_readiness"] == "needs_canary"
    assert first.attestation["quota_state"] == "unknown"
    assert "terminal" in first.capability_gaps
    assert "resume_after_restart" in first.capability_gaps
    assert "windows_os_sandbox" in first.capability_gaps


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

    def drift_then_inspect(
        current: GrokBinding, current_workspace: str
    ) -> GrokInspectObservation:
        executable.write_bytes(b"replaced-grok!")
        os.utime(executable, ns=(timestamp, timestamp))
        return _inspect(current, Path(current_workspace))

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=drift_then_inspect,
        platform="win32",
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as rejected:
        asyncio.run(adapter.resolve_context(_request(workspace)))

    assert rejected.value.code in {"CAPABILITY_MISSING", "CONTEXT_DRIFT"}


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


def test_context_inspect_is_off_loop_records_extensions_and_never_promotes_permissions_to_tools(
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
    )
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))

    assert reader_threads and reader_threads[0] != caller_thread
    assert context.attestation["discovered_extensions"] == (
        ("compatibility_mcp", "cursor-mcp"),
        ("hook", "project-hook"),
        ("mcp", "project-mcp"),
        ("plugin", "user-plugin"),
    )
    assert context.attestation["inspect_permission_keys"] == (
        "Shell",
        "Terminal",
        "allow",
    )
    assert "builtin_tool_names" not in context.attestation
    assert context.attestation["builtin_tool_inventory"] == "not_exposed"
    assert context.attestation["cached_native_login"] == "not_exposed"


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
        builtin_tool_names=("read_file", "search_files")
        if mode == "review"
        else ("read_file", "write_file"),
        permission_routes=(
            (("read_file", "repo_read"), ("search_files", "repo_read"))
            if mode == "review"
            else (
                ("read_file", "repo_read"),
                ("write_file", "workspace_write_bridge"),
            )
        ),
        workspace_path=workspace_path,
        effective_model=effective_model,
        reasoning_effort=effort,
        auth_method="cached-native",
        api_key_override=False,
        custom_paid_route=False,
        no_extra_spend=True,
        loaded_executable_extensions=(),
        disabled_executable_extensions=(
            ("compatibility_mcp", "cursor-mcp"),
            ("hook", "project-hook"),
            ("mcp", "project-mcp"),
            ("plugin", "user-plugin"),
        ),
        web_search_enabled=False,
        nested_agents_enabled=False,
        terminal_enabled=False,
        quota_state="unknown",
    )


def test_context_handshake_accepts_exact_cached_native_isolated_review_and_unknown_quota(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    inspect = _inspect(
        binding,
        workspace,
        mcp_servers=("project-mcp",),
        hooks=("project-hook",),
        plugins=("user-plugin",),
        compatibility_mcp_servers=("cursor-mcp",),
        permission_keys=("Shell", "Terminal"),
    )
    adapter = _adapter(tmp_path, binding, inspect=inspect)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    attestation = _valid_session_attestation(context, binding)

    adapter.validate_session_attestation(context, attestation)

    assert attestation.quota_state == "unknown"
    assert attestation.builtin_tool_names == ("read_file", "search_files")


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


def test_context_handshake_requires_explicit_extension_inventories_even_when_empty(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    missing = GrokSessionToolAttestation(
        pair_key=binding.pair_key,
        external_session_id="native-session-1",
        workspace_key=context.workspace_key,
        mode="review",
        builtin_tool_names=("read_file",),
        permission_routes=(("read_file", "repo_read"),),
        workspace_path=context.workspace_path,
        effective_model=context.requested_model,
        reasoning_effort=context.requested_reasoning["effort"],
        auth_method="cached-native",
        api_key_override=False,
        custom_paid_route=False,
        no_extra_spend=True,
        web_search_enabled=False,
        nested_agents_enabled=False,
        terminal_enabled=False,
        quota_state="unknown",
    )

    with pytest.raises(ServiceError) as rejected:
        adapter.validate_session_attestation(context, missing)

    assert rejected.value.code == "CAPABILITY_MISSING"


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("workspace_path", None),
        ("workspace_key", ["unhashable"]),
        ("quota_state", []),
        ("loaded_executable_extensions", ((["mcp"], "project-mcp"),)),
        ("disabled_executable_extensions", (("mcp", ["project-mcp"]),)),
        ("builtin_tool_names", (["read_file"],)),
        ("permission_routes", ((["read_file"], "repo_read"),)),
    ],
)
def test_context_handshake_malformed_public_values_return_capability_error(
    tmp_path: Path,
    field: str,
    malformed: object,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = _adapter(tmp_path, binding)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    changes: dict[str, object] = {
        "loaded_executable_extensions": (),
        "disabled_executable_extensions": (),
        field: malformed,
    }
    attestation = replace(_valid_session_attestation(context, binding), **changes)

    with pytest.raises(ServiceError) as rejected:
        adapter.validate_session_attestation(context, attestation)

    assert rejected.value.code == "CAPABILITY_MISSING"


def test_context_handshake_writer_requires_read_and_bounded_write_routes(
    tmp_path: Path,
) -> None:
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
    missing_read = replace(
        _valid_session_attestation(context, binding, mode="writer"),
        builtin_tool_names=("write_file",),
        permission_routes=(("write_file", "workspace_write_bridge"),),
    )

    with pytest.raises(ServiceError) as rejected:
        adapter.validate_session_attestation(context, missing_read)

    assert rejected.value.code == "CAPABILITY_MISSING"


@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("pair_key", "f" * 64),
        ("external_session_id", ""),
        ("workspace_key", "wrong-workspace"),
        ("workspace_path", "C:\\outside"),
        ("mode", "writer"),
        ("effective_model", "silent-fallback"),
        ("reasoning_effort", "silent-downgrade"),
        ("auth_method", "not_exposed"),
        ("auth_method", "api-key"),
        ("api_key_override", True),
        ("custom_paid_route", True),
        ("no_extra_spend", False),
        ("no_extra_spend", None),
        ("loaded_executable_extensions", (("mcp", "project-mcp"),)),
        ("disabled_executable_extensions", ()),
        ("web_search_enabled", True),
        ("nested_agents_enabled", True),
        ("terminal_enabled", True),
        ("quota_state", "exhausted"),
        ("builtin_tool_names", ("read_file", "shell")),
        ("permission_routes", (("read_file", "repo_read"), ("shell", "shell"))),
    ],
)
def test_context_handshake_fails_closed_on_missing_mismatched_or_unsafe_evidence(
    tmp_path: Path,
    field: str,
    unsafe: object,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    inspect = _inspect(binding, workspace, mcp_servers=("project-mcp",))
    adapter = _adapter(tmp_path, binding, inspect=inspect)
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_request(workspace)))
    changes: dict[str, object] = {
        "disabled_executable_extensions": (("mcp", "project-mcp"),),
        field: unsafe,
    }
    attestation = replace(_valid_session_attestation(context, binding), **changes)

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


def test_filesystem_review_reads_utf8_inside_workspace_and_denies_all_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "README.md"
    source.write_bytes(b"before\n")
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )

    async def scenario() -> None:
        assert await bridge.read_text_file({"path": "README.md"}) == {
            "content": "before\n"
        }
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": "README.md", "content": "changed\n"}
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


def test_filesystem_writer_updates_and_creates_only_exact_file_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    exact = source / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("src/exact.py", "src/new.py"),
    )

    async def scenario() -> None:
        assert await bridge.read_text_file({"path": "src/exact.py"}) == {
            "content": "before\n"
        }
        assert await bridge.write_text_file(
            {"path": "src/exact.py", "content": "after\n"}
        ) == {"written": True}
        assert await bridge.write_text_file(
            {"path": "src/new.py", "content": "new\n"}
        ) == {"written": True}
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": "src/other.py", "content": "forbidden\n"}
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
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("docs\\specs", "src"),
    )

    async def scenario() -> None:
        await bridge.write_text_file(
            {"path": "DOCS\\SPECS\\one.md", "content": "one\n"}
        )
        await bridge.write_text_file(
            {"path": "src/nested/two.py", "content": "two\n"}
        )
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": "Docs/other.md", "content": "outside\n"}
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
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("ss",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": f"{sharp_s}/outside.txt", "content": "outside\n"}
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
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("allowed.txt",),
    )

    with pytest.raises(GrokPermissionError):
        grok_module._windows_relative_parts(path)

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file({"path": path})
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file({"path": path, "content": "x"})

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
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file({"path": "linked/secret.txt"})
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": "linked/secret.txt", "content": "changed\n"}
            )

    asyncio.run(scenario())
    assert secret.read_text(encoding="utf-8") == "outside\n"


def test_filesystem_detects_authorized_parent_replacement_before_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authorized = workspace / "docs"
    authorized.mkdir(parents=True)
    bridge = GrokFilesystemBridge(
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
                {"path": "docs/new.md", "content": "must not land\n"}
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
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("output.txt",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file({"path": "invalid.txt"})
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file({"path": "large.txt"})
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file({"path": "json-expanded.txt"})
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": "output.txt", "content": "x" * (1_048_576 + 1)}
            )
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": "output.txt", "content": "\ud800"}
            )
        with pytest.raises(GrokPermissionError):
            await bridge.read_text_file({"path": "README.md", "extra": True})

    asyncio.run(scenario())
    assert not (workspace / "output.txt").exists()


def test_filesystem_reverse_requests_use_real_bridge_through_acp_stdio(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_bytes(b"bridge-read\n")
    bridge = GrokFilesystemBridge(
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
    bridge = GrokFilesystemBridge(
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
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=("exact.py",),
    )

    async def scenario() -> None:
        allowed = _filesystem_client(workspace, "filesystem-write", bridge)
        await allowed.start()
        try:
            result = await allowed.request("trigger/filesystem", {})
            assert result["reverseResponse"]["result"] == {"written": True}
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
    bridge = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )

    async def scenario() -> None:
        await bridge.write_text_file({"path": "inside.txt", "content": "inside\n"})
        with pytest.raises(GrokPermissionError):
            await bridge.write_text_file(
                {"path": "../outside.txt", "content": "outside\n"}
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
    bridge = GrokFilesystemBridge(
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
            await bridge.read_text_file({"path": "linked/secret.txt"})

    asyncio.run(scenario())


def test_filesystem_detects_target_replacement_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = GrokFilesystemBridge(
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
                {"path": "exact.py", "content": "must-not-overwrite-race\n"}
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
    bridge = GrokFilesystemBridge(
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
                {"path": "exact.py", "content": "must-not-land\n"}
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
    reader = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="repo-read",
        write_roots=(),
    )
    writer = GrokFilesystemBridge(
        workspace=workspace,
        permission_mode="workspace-write",
        write_roots=(".",),
    )

    async def scenario() -> None:
        with pytest.raises(GrokPermissionError):
            await reader.read_text_file({"path": "linked.txt"})
        with pytest.raises(GrokPermissionError):
            await writer.write_text_file(
                {"path": "linked.txt", "content": "must-not-land\n"}
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
    bridge = GrokFilesystemBridge(
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
            await bridge.read_text_file({"path": "source.txt"})

    asyncio.run(scenario())


def test_filesystem_rejects_link_count_change_before_read_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_bytes(b"bounded\n")
    bridge = GrokFilesystemBridge(
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
            await bridge.read_text_file({"path": "source.txt"})

    asyncio.run(scenario())


def test_filesystem_cancel_waits_for_worker_and_prevents_late_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exact = workspace / "exact.py"
    exact.write_bytes(b"before\n")
    bridge = GrokFilesystemBridge(
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
            bridge.write_text_file({"path": "exact.py", "content": "late\n"})
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
    bridge = GrokFilesystemBridge(
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
    bridge = GrokFilesystemBridge(
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
                {"path": "exact.py", "content": "must-not-land\n"}
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
    bridge = GrokFilesystemBridge(
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
    bridge = GrokFilesystemBridge(
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
    bridge = GrokFilesystemBridge(
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
    inspect: GrokInspectObservation | None = None,
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
) -> tuple[GrokBuildAdapter, Path, list[AcpStdioProcess]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    trace_path = tmp_path / f"trace-{scenario}-{mutation or 'none'}.jsonl"
    children: list[AcpStdioProcess] = []

    def process_factory(
        launch: object,
        request_handler: object,
        notification_handler: object,
    ) -> AcpStdioProcess:
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
        }
        if mutation is not None:
            config["mutation"] = mutation
        if error_code is not None:
            config["error_code"] = error_code
            config["error_retryable"] = error_retryable
        if error_detail is not None:
            config["error_detail"] = error_detail
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
                if method == "session/prompt" and prompt_write_release is not None:
                    assert prompt_write_started is not None
                    prompt_write_started.set()
                    await prompt_write_release.wait()
                    if prompt_write_error:
                        raise AcpProcessError("synthetic prompt write failure")
                await super()._write(message)
                if write_order is not None and method in {
                    "session/prompt",
                    "session/cancel",
                }:
                    write_order.append(str(method))

            async def close(self) -> None:
                if close_delay:
                    await asyncio.sleep(close_delay)
                await super().close()
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
            env={name: os.environ[name] for name in names if name in os.environ},
            request_handler=request_handler,  # type: ignore[arg-type]
            notification_handler=notification_handler,  # type: ignore[arg-type]
            startup_timeout_seconds=1.0,
            request_timeout_seconds=float("inf"),
            close_timeout_seconds=0.2,
            max_line_bytes=1_048_576,
        )
        children.append(child)
        return child

    adapter = GrokBuildAdapter(
        binding_locator=lambda: binding,
        catalog_reader=lambda _binding: (),
        inspect_reader=lambda current, current_workspace: inspect
        or _inspect(current, Path(current_workspace)),
        platform="win32",
        environment={},
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


def test_lifecycle_spawn_handshake_returns_running_then_succeeds_with_public_text_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter, trace_path, children = _lifecycle_adapter(tmp_path, binding)
    context = _lifecycle_context(adapter, workspace)

    async def scenario() -> None:
        started = await adapter.spawn(_lifecycle_spawn_request(context))
        assert started.execution_state == "running"
        assert started.conversation_state == "active"
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
        await adapter.close(request)

    asyncio.run(scenario())
    assert len(children) == 1
    assert children[0].closed is True
    records = _trace_records(trace_path)
    assert [record["method"] for record in records] == [
        "initialize",
        "initialized",
        "authenticate",
        "session/new",
        "session/prompt",
    ]
    assert records[0]["params"] == {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": False}
        },
        "clientInfo": {
            "name": "subagent-mcp",
            "version": grok_module.__version__,
        },
    }
    assert records[2]["params"] == {"methodId": "cached_token"}
    assert records[3]["params"] == {
        "cwd": str(workspace.resolve()),
        "mcpServers": [],
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-auth",
        "api-key",
        "custom-paid",
        "pair-mismatch",
        "unsafe-route",
        "missing-isolation",
        "loaded-extension",
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
    assert len(children) == 1
    assert children[0].closed is True
    assert "session/prompt" not in {
        record["method"] for record in _trace_records(trace_path)
    }


def test_lifecycle_spawn_accepts_nonzero_discovery_only_when_disabled_is_bound(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    observed = _inspect(binding, workspace, mcp_servers=("project-mcp",))
    adapter, trace_path, _children = _lifecycle_adapter(
        tmp_path,
        binding,
        inspect=observed,
        disabled_extensions=(("mcp", "project-mcp"),),
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
        await adapter.close(request)

    asyncio.run(scenario())
    initialize = _trace_records(trace_path)[0]
    assert initialize["params"]["clientCapabilities"]["fs"] == {
        "readTextFile": True,
        "writeTextFile": True,
    }


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
    assert len(children) == 1


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
    assert len(children) == 1
    assert [record["method"] for record in _trace_records(trace_path)].count(
        "session/prompt"
    ) == 2


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
    assert len(children) == 1
    assert children[0].closed is True


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
        await asyncio.sleep(0.03)
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
        "is_using_overage": False,
        "overage_blocked": True,
        "cleanup_confirmed": True,
    }
    assert len(children) == 1 and children[0].closed is True
    records = _trace_records(trace_path)
    assert [record["method"] for record in records] == [
        "initialize",
        "initialized",
        "authenticate",
        "session/new",
    ]
    canary_cwd = Path(str(records[-1]["params"]["cwd"]))
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
    assert len(children) == 2
