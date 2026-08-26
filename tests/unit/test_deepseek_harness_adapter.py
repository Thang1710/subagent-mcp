from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

import subagent_harness_mcp.adapters.deepseek_harness as deepseek_module

from subagent_harness_mcp.adapters import (
    AdapterContextRequest,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSpawnRequest,
)
from subagent_harness_mcp.adapters.deepseek_harness import (
    CONTROLLER_RESULT_MAX_CHARS,
    DEFAULT_TURN_TIMEOUT_SECONDS,
    DeepSeekHarnessAdapter,
    DshBinding,
    DshLaunch,
    _StdioAcpClient,
    _bounded_result,
    _dsh_pair_key,
    _windows_process_inventory,
    render_dsh_config,
    _node_path,
    _dsh_env,
    _file_identity,
    _source_root,
)
from subagent_harness_mcp.contracts import ServiceError, TaskPacket


def test_durable_result_has_large_bound_and_explicit_truncation() -> None:
    result = _bounded_result("x" * (CONTROLLER_RESULT_MAX_CHARS * 2))

    assert CONTROLLER_RESULT_MAX_CHARS == 65_536
    assert len(result) == CONTROLLER_RESULT_MAX_CHARS
    assert result.endswith("\n[truncated by Subagent MCP]")


def test_product_default_has_no_elapsed_turn_deadline(tmp_path: Path) -> None:
    adapter = DeepSeekHarnessAdapter(
        binding_locator=lambda: _binding(tmp_path),
        client_factory=lambda launch: _FakeAcpClient(launch),
        data_root=tmp_path / "data",
    )

    assert DEFAULT_TURN_TIMEOUT_SECONDS is None
    assert adapter._turn_timeout is None


def test_binding_probe_timeout_keeps_event_loop_responsive(tmp_path: Path) -> None:
    release = threading.Event()

    def blocked_locator() -> DshBinding | None:
        release.wait(timeout=1)
        return _binding(tmp_path)

    async def scenario() -> None:
        heartbeat = asyncio.Event()

        async def beat() -> None:
            await asyncio.sleep(0)
            heartbeat.set()

        adapter = DeepSeekHarnessAdapter(
            binding_locator=blocked_locator,
            client_factory=lambda launch: _FakeAcpClient(launch),
            data_root=tmp_path / "data",
            binding_probe_timeout_seconds=0.01,
        )
        beat_task = asyncio.create_task(beat())
        try:
            probe = await adapter.probe()
        finally:
            release.set()
        await beat_task

        assert heartbeat.is_set()
        assert probe.state == "recovery_required"
        assert probe.details == {"code": "BINDING_PROBE_TIMEOUT"}

    asyncio.run(scenario())


def test_model_catalog_binding_timeout_keeps_event_loop_responsive(
    tmp_path: Path,
) -> None:
    release = threading.Event()

    def blocked_locator() -> DshBinding | None:
        release.wait(timeout=1)
        return _binding(tmp_path)

    async def scenario() -> None:
        adapter = DeepSeekHarnessAdapter(
            binding_locator=blocked_locator,
            catalog_reader=lambda *_args: asyncio.sleep(0, result=({"value": "fresh"},)),
            data_root=tmp_path / "data",
            binding_probe_timeout_seconds=0.01,
        )
        adapter._catalog_cache = ({"value": "cached"},)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        try:
            catalog = await adapter.model_catalog()
        finally:
            release.set()

        assert loop.time() - started_at < 0.2
        assert catalog == ({"value": "cached"},)

    asyncio.run(scenario())


def test_orphan_cleanup_binding_timeout_keeps_event_loop_responsive(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    calls = 0
    binding = _binding(tmp_path)

    def locate_then_block() -> DshBinding | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return binding
        release.wait(timeout=1)
        return binding

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        adapter = DeepSeekHarnessAdapter(
            binding_locator=locate_then_block,
            process_inventory=lambda: (),
            data_root=tmp_path / "data",
            binding_probe_timeout_seconds=0.1,
        )
        assert (await adapter.probe()).state == "ready"
        context = await adapter.resolve_context(_context_request(workspace))
        request = AdapterSessionRequest(
            "conversation-timeout",
            "execution-timeout",
            "dsh-session-timeout",
            "execution-timeout",
        )
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        try:
            confirmed = await adapter.orphan_cleanup_confirmed(request, context)
        finally:
            release.set()

        assert loop.time() - started_at < 0.5
        assert confirmed is False

    asyncio.run(scenario())


def test_probe_binding_is_reused_until_launch_revalidation(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        binding = _binding(tmp_path)
        calls = 0

        def locate_once() -> DshBinding:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise AssertionError("binding discovery repeated before native launch")
            return binding

        adapter = DeepSeekHarnessAdapter(
            binding_locator=locate_once,
            client_factory=lambda launch: _FakeAcpClient(launch),
            data_root=tmp_path / "data",
            timeout_seconds=1,
        )
        probe = await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-binding-cache",
                "execution-binding-cache",
                TaskPacket("Review", "Review only.", ("Return.",), "reviewer"),
                context,
            )
        )

        assert probe.state == "ready"
        assert started.execution_state == "running"
        assert calls == 1

    asyncio.run(scenario())


class _FakeAcpClient:
    def __init__(self, launch: DshLaunch) -> None:
        self.launch = launch
        self.started = False
        self.closed = False
        self.cancelled: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.responses: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.session_cwd: str | None = None

    async def start(self) -> None:
        self.started = True

    async def new_session(self, cwd: str) -> str:
        assert self.started
        self.session_cwd = cwd
        assert cwd == (self.launch.write_root_path or self.launch.workspace_path)
        return "dsh-session-1"

    async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
        self.prompts.append((session_id, prompt))
        return await self.responses.get()

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)
        self.responses.put_nowait(("cancelled", ""))

    async def close(self) -> None:
        self.closed = True


def _binding(tmp_path: Path) -> DshBinding:
    node = tmp_path / "node.exe"
    binary = tmp_path / "bin.js"
    if not node.exists():
        node.write_bytes(b"node")
    if not binary.exists():
        binary.write_bytes(b"acp")
    plugins: dict[str, Path] = {}
    for name in (
        "settings-file",
        "credentials-local",
        "llm",
        "llm-deepseek",
        "llm-pi-ai",
        "sandbox-local",
        "sandbox-policy",
        "subprocess-local",
        "pwsh-sandbox",
        "shell-env",
        "user-approval",
        "acp-demo",
        "token-meter",
        "compaction-basic",
        "fs-sandbox",
        "fs-observation-policy",
        "tool-fs",
        "tool-pwsh",
    ):
        plugin = tmp_path / "plugins" / name / "lib" / "index.js"
        plugin.parent.mkdir(parents=True, exist_ok=True)
        if not plugin.exists():
            plugin.write_bytes(name.encode())
        plugins[name] = plugin
    version = "0.1.1-rc.2"
    pair_key = _dsh_pair_key(node, binary, plugins, version)
    return DshBinding(node, binary, plugins, version, pair_key)


def _context_request(workspace: Path, **overrides: object) -> AdapterContextRequest:
    values: dict[str, object] = {
        "runtime_id": "deepseek-harness",
        "variant_id": "ox-alpha",
        "model": "ox-provider::stealth/ox-alpha",
        "reasoning": {},
        "workspace_path": str(workspace.resolve()),
        "workspace_key": str(workspace.resolve()),
        "transport": "native-acp",
        "permissions": ("repo_read", "git_read", "run_tests", "workspace_write"),
        "context_policy_id": "declared-native",
        "permission_policy_id": "default",
        "write_set": (".",),
    }
    values.update(overrides)
    return AdapterContextRequest(**values)  # type: ignore[arg-type]


def test_manifest_and_context_keep_provider_model_opaque(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    adapter = DeepSeekHarnessAdapter(
        binding_locator=lambda: binding,
        data_root=tmp_path / "data",
    )

    probe = asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_context_request(workspace)))

    assert probe.state == "ready"
    assert probe.details == {
        "pair_key": binding.pair_key,
        "harness_version": "0.1.1-rc.2",
        "transport": "native-acp",
    }
    assert adapter.manifest.runtime_id == "deepseek-harness"
    assert adapter.manifest.supported_transports == ("native-acp",)
    assert adapter.manifest.max_write_roots_per_session == 1
    assert adapter.manifest.write_root_mode == "existing-directory"
    assert "prepaid balance" in adapter.manifest.model_schema["description"]
    assert "never buys or reloads credits" in adapter.manifest.model_schema["description"]
    assert adapter.manifest.capabilities == frozenset(
        {"session", "interrupt", "workspace"}
    )
    assert context.effective_model == "ox-provider::stealth/ox-alpha"
    assert context.effective_reasoning == {}
    assert context.attestation["provider"] == "ox-provider"
    assert context.attestation["model"] == "stealth/ox-alpha"
    assert context.attestation["permission_mode"] == "workspace-write"
    assert "resume_after_restart" in context.capability_gaps
    assert "provider_quota_evidence" in context.capability_gaps
    assert "exact_auto_compaction_trigger" in context.capability_gaps


def test_context_rejects_unimplemented_context_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = DeepSeekHarnessAdapter(
        binding_locator=lambda: _binding(tmp_path),
        data_root=tmp_path / "data",
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            adapter.resolve_context(
                _context_request(
                    workspace,
                    context_policy_id="full-native",
                )
            )
        )

    assert captured.value.code == "CAPABILITY_MISSING"
    assert captured.value.category == "capability"
    assert captured.value.retryable is False
    assert "declared-native" in (captured.value.next_action or "")


def test_write_scope_becomes_native_session_cwd_and_multi_root_fails_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        write_root = workspace / "src" / "context"
        write_root.mkdir(parents=True)
        clients: list[_FakeAcpClient] = []

        def factory(launch: DshLaunch) -> _FakeAcpClient:
            client = _FakeAcpClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
        )
        await adapter.probe()
        context = await adapter.resolve_context(
            _context_request(workspace, write_set=("src/context",))
        )
        await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-scope",
                "execution-scope",
                TaskPacket("Implement", "Change context.", ("Done",), "writer"),
                context,
            )
        )
        await asyncio.sleep(0)

        assert clients[0].launch.write_root_path == str(write_root.resolve())
        assert clients[0].session_cwd == str(write_root.resolve())
        assert str(workspace.resolve()) in clients[0].prompts[0][1]
        with pytest.raises(ServiceError) as captured:
            await adapter.resolve_context(
                _context_request(
                    workspace,
                    write_set=("src/context", "docs/status"),
                )
            )
        assert captured.value.code == "CAPABILITY_MISSING"

    asyncio.run(scenario())


def test_native_model_catalog_is_cached_by_binding_and_settings_identity(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    settings = tmp_path / "settings.yaml"
    settings.write_text("llm-pi-ai: {}\n", encoding="utf-8")
    calls: list[tuple[DshBinding, Path]] = []

    async def catalog_reader(
        current: DshBinding,
        settings_path: Path,
    ) -> tuple[dict[str, str], ...]:
        calls.append((current, settings_path))
        return (
            {
                "value": "deepseek-official::deepseek-v4-flash",
                "label": "DeepSeek-V4-Flash",
                "provider": "deepseek-official",
                "model": "deepseek-v4-flash",
            },
            {
                "value": "ox-provider::stealth/ox-alpha",
                "label": "OX Alpha - OpenRouter",
                "provider": "ox-provider",
                "model": "stealth/ox-alpha",
            },
        )

    adapter = DeepSeekHarnessAdapter(
        binding_locator=lambda: binding,
        catalog_reader=catalog_reader,
        settings_path_locator=lambda: settings,
        data_root=tmp_path / "data",
    )

    first = asyncio.run(adapter.model_catalog())
    second = asyncio.run(adapter.model_catalog())
    settings.write_text("llm-pi-ai:\n  providers: {}\n", encoding="utf-8")
    third = asyncio.run(adapter.model_catalog())

    assert [item["value"] for item in first] == [
        "deepseek-official::deepseek-v4-flash",
        "ox-provider::stealth/ox-alpha",
    ]
    assert second == first == third
    assert len(calls) == 2
    assert calls[0][1] == settings


def test_generated_acp_config_mounts_every_catalog_provider_adapter(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)

    rendered = render_dsh_config(
        binding,
        provider="deepseek-official",
        model="deepseek-v4-flash",
        workspace_path=str(tmp_path),
        persistence_root=tmp_path / "sessions",
        permission_mode="read-only",
    )

    assert binding.plugins["llm-deepseek"].resolve().as_uri() in rendered
    assert binding.plugins["llm-pi-ai"].resolve().as_uri() in rendered
    assert rendered.index('id: llm-deepseek') < rendered.index('id: acp-agent')


@pytest.mark.parametrize(
    ("model", "reasoning"),
    [
        ("missing-provider", {}),
        ("::missing-provider", {}),
        ("provider::", {}),
        ("provider::model", {"effort": "high"}),
    ],
)
def test_context_rejects_ambiguous_native_selection(
    tmp_path: Path,
    model: str,
    reasoning: dict[str, str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = DeepSeekHarnessAdapter(
        binding_locator=lambda: _binding(tmp_path),
        data_root=tmp_path / "data",
    )
    asyncio.run(adapter.probe())

    with pytest.raises(ServiceError) as error:
        asyncio.run(
            adapter.resolve_context(
                _context_request(workspace, model=model, reasoning=reasoning)
            )
        )

    assert error.value.code == "POLICY_REJECTED"


def _orphan_confirmation(
    tmp_path: Path,
    processes: object,
    *,
    drift_binding: bool = False,
    legacy_attestation: bool = False,
) -> bool:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = _binding(tmp_path)
    current_binding = [binding]
    adapter = DeepSeekHarnessAdapter(
        binding_locator=lambda: current_binding[0],
        process_inventory=lambda: processes,
        data_root=tmp_path / "data",
    )
    asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_context_request(workspace)))
    if legacy_attestation:
        context = replace(
            context,
            attestation={
                key: context.attestation[key]
                for key in (
                    "source",
                    "variant_id",
                    "permissions",
                    "context_policy_id",
                    "permission_policy_id",
                )
            },
        )
    if drift_binding:
        current_binding[0] = replace(binding, pair_key="pair-2")
    request = AdapterSessionRequest(
        "conversation-1",
        "execution-1",
        "dsh-session-1",
        "execution-1",
    )
    return asyncio.run(adapter.orphan_cleanup_confirmed(request, context))


def test_orphan_cleanup_is_unconfirmed_while_exact_acp_process_exists(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    config = tmp_path / "data" / "deepseek-harness" / "conversation-1" / "cordis.yml"
    process = SimpleNamespace(
        name="node.exe",
        executable_path=str(binding.node_path),
        command_line=(
            f'"{binding.node_path}" "{binding.acp_bin_path}" --config "{config}"'
        ),
    )

    assert _orphan_confirmation(tmp_path, [process]) is False


def test_orphan_cleanup_is_confirmed_when_only_visible_unrelated_node_exists(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    process = SimpleNamespace(
        name="node.exe",
        executable_path=str(binding.node_path),
        command_line=f'"{binding.node_path}" unrelated.js',
    )

    assert _orphan_confirmation(tmp_path, [process]) is True


def test_orphan_cleanup_fails_closed_for_opaque_matching_node(
    tmp_path: Path,
) -> None:
    process = SimpleNamespace(
        name="node.exe",
        executable_path=None,
        command_line=None,
    )

    assert _orphan_confirmation(tmp_path, [process]) is False


def test_orphan_cleanup_fails_closed_after_harness_binding_drift(
    tmp_path: Path,
) -> None:
    assert _orphan_confirmation(tmp_path, [], drift_binding=True) is False


def test_orphan_cleanup_fails_closed_when_process_inventory_is_unavailable(
    tmp_path: Path,
) -> None:
    class UnavailableInventory:
        def __iter__(self):
            raise OSError("process inventory unavailable")

    assert _orphan_confirmation(tmp_path, UnavailableInventory()) is False


def test_orphan_cleanup_accepts_legacy_context_hash_without_persisted_pair_key(
    tmp_path: Path,
) -> None:
    assert _orphan_confirmation(tmp_path, [], legacy_attestation=True) is True


def test_windows_inventory_uses_process_api_without_wmi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(
        info={
            "name": "node.exe",
            "exe": r"C:\Program Files\nodejs\node.exe",
            "cmdline": [
                r"C:\Program Files\nodejs\node.exe",
                r"D:\DeepSeek Harness\acp.js",
            ],
        }
    )
    fake_psutil = SimpleNamespace(
        process_iter=lambda attrs, ad_value: (
            process
            if attrs == ["name", "exe", "cmdline"] and ad_value is None
            else None
            for _ in range(1)
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    observed = _windows_process_inventory()

    assert len(observed) == 1
    assert observed[0].name == "node.exe"
    assert observed[0].executable_path == r"C:\Program Files\nodejs\node.exe"
    assert r"D:\DeepSeek Harness\acp.js" in str(observed[0].command_line)


def test_background_lifecycle_reuses_native_session_and_interrupts(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[_FakeAcpClient] = []

        def factory(launch: DshLaunch) -> _FakeAcpClient:
            client = _FakeAcpClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        task = TaskPacket(
            title="Review the adapter",
            prompt="Find one concrete issue.",
            acceptance_criteria=("Return a concise answer",),
            role="reviewer",
        )
        spawned = await adapter.spawn(
            AdapterSpawnRequest("conversation-1", "execution-1", task, context)
        )
        assert spawned.execution_state == "running"
        assert len(clients) == 1
        assert clients[0].launch.provider == "ox-provider"
        assert clients[0].launch.model == "stealth/ox-alpha"
        assert clients[0].launch.persistence_root.is_relative_to(tmp_path / "data")

        clients[0].responses.put_nowait(("end_turn", "DeepSeek review complete."))
        for _ in range(20):
            done = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-1", "execution-1", "dsh-session-1", "execution-1"
                )
            )
            if done.execution_state != "running":
                break
            await asyncio.sleep(0)
        assert done.execution_state == "succeeded"
        assert done.result_text == "DeepSeek review complete."
        assert "Return only the final result" in clients[0].prompts[0][1]
        assert "CAPSULE:" in clients[0].prompts[0][1]
        assert "DETAILS:" in clients[0].prompts[0][1]
        assert "500 words" not in clients[0].prompts[0][1]
        late_interrupt = await adapter.interrupt(
            AdapterSessionRequest(
                "conversation-1", "execution-1", "dsh-session-1", "execution-1"
            )
        )
        assert late_interrupt.execution_state == "succeeded"
        assert clients[0].cancelled == []

        sent = await adapter.send(
            AdapterSendRequest(
                "conversation-1",
                "execution-2",
                "dsh-session-1",
                "Check the fix.",
                None,
                {},
                context,
            )
        )
        assert sent.execution_state == "running"
        interrupted = await adapter.interrupt(
            AdapterSessionRequest(
                "conversation-1", "execution-2", "dsh-session-1", "execution-2"
            )
        )
        assert interrupted.execution_state == "interrupted"
        assert clients[0].cancelled == ["dsh-session-1"]

        closed = await adapter.close(
            AdapterSessionRequest(
                "conversation-1", "execution-2", "dsh-session-1", "execution-2"
            )
        )
        assert closed.conversation_state == "closed"
        assert clients[0].closed is True

    asyncio.run(scenario())


def test_acp_wire_error_preserves_terminal_quota_detail_for_classification(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        launch = DshLaunch(
            _binding(tmp_path),
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            tmp_path / "persistence",
            tmp_path / "config.yml",
        )
        client = _StdioAcpClient(launch, timeout_seconds=1)
        future = asyncio.get_running_loop().create_future()
        client._pending[7] = future

        client._handle_response(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "error": {
                    "code": -32603,
                    "message": "turn failed: insufficient_quota; account credits exhausted",
                },
            }
        )

        with pytest.raises(RuntimeError, match="credits exhausted"):
            await future

    asyncio.run(scenario())


def test_acp_wire_error_preserves_sdk_wrapped_details_for_rate_classification(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        launch = DshLaunch(
            _binding(tmp_path),
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            tmp_path / "persistence",
            tmp_path / "config.yml",
        )
        client = _StdioAcpClient(launch, timeout_seconds=1)
        future = asyncio.get_running_loop().create_future()
        client._pending[8] = future

        client._handle_response(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": {
                        "details": (
                            "turn failed: 429: stealth/ox-alpha is temporarily "
                            "rate-limited upstream; "
                            "limit_source=upstream_provider_shared_pool"
                        )
                    },
                },
            }
        )

        with pytest.raises(RuntimeError, match="upstream_provider_shared_pool"):
            await future

    asyncio.run(scenario())


def test_acp_wire_error_preserves_structured_provider_diagnostics(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        launch = DshLaunch(
            _binding(tmp_path),
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            tmp_path / "persistence",
            tmp_path / "config.yml",
        )
        client = _StdioAcpClient(launch, timeout_seconds=1)
        future = asyncio.get_running_loop().create_future()
        client._pending[9] = future

        client._handle_response(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "error": {
                    "code": -32603,
                    "message": "turn failed: Provider returned error: PI_AI_ERROR",
                    "data": {
                        "raw_frame": {"authorization": "Bearer do-not-copy"},
                    },
                },
            }
        )

        with pytest.raises(RuntimeError) as caught:
            await future

        error = caught.value
        assert type(error).__name__ == "_AcpResponseError"
        assert error.rpc_code == -32603  # type: ignore[attr-defined]
        assert error.provider_code == "PI_AI_ERROR"  # type: ignore[attr-defined]
        assert error.detail == (  # type: ignore[attr-defined]
            "turn failed: Provider returned error: PI_AI_ERROR"
        )
        assert "do-not-copy" not in str(error)

    asyncio.run(scenario())


def test_acp_close_retries_after_partial_teardown_failure(tmp_path: Path) -> None:
    class _Stdin:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        async def wait_closed(self) -> None:
            return None

    class _Process:
        def __init__(self) -> None:
            self.returncode = None
            self.stdin = _Stdin()
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise RuntimeError("process wait failed")
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            raise AssertionError("terminate should not be needed")

        def kill(self) -> None:
            raise AssertionError("kill should not be needed")

    async def scenario() -> None:
        launch = DshLaunch(
            _binding(tmp_path),
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            tmp_path / "persistence",
            tmp_path / "config.yml",
        )
        client = _StdioAcpClient(launch, timeout_seconds=1)
        process = _Process()
        client._process = process

        with pytest.raises(RuntimeError, match="process wait failed"):
            await client.close()
        assert client._closed is False

        await client.close()

        assert client._closed is True
        assert process.wait_calls == 2
        assert process.stdin.close_calls == 2

    asyncio.run(scenario())


def test_acp_prompt_uses_the_long_turn_budget_not_the_operation_budget(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        launch = DshLaunch(
            _binding(tmp_path),
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            tmp_path / "persistence",
            tmp_path / "config.yml",
        )
        client = _StdioAcpClient(
            launch,
            timeout_seconds=0.01,
            turn_timeout_seconds=0.2,
        )

        async def delayed_request(method: str, params: object) -> object:
            assert method == "session/prompt"
            assert params is not None
            await asyncio.sleep(0.05)
            return {"stopReason": "end_turn"}

        client._request = delayed_request  # type: ignore[method-assign]

        assert await client.prompt("session-1", "Review") == ("end_turn", "")

    asyncio.run(scenario())


def test_adapter_keeps_a_turn_alive_past_the_short_operation_budget(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[_FakeAcpClient] = []

        def factory(launch: DshLaunch) -> _FakeAcpClient:
            client = _FakeAcpClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=0.01,
            turn_timeout_seconds=0.2,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        task = TaskPacket(
            title="Long review",
            prompt="Keep working beyond the operation budget.",
            acceptance_criteria=("Return a result",),
            role="reviewer",
        )
        await adapter.spawn(
            AdapterSpawnRequest("conversation-1", "execution-1", task, context)
        )

        await asyncio.sleep(0.05)
        still_running = await adapter.snapshot(
            AdapterSessionRequest(
                "conversation-1", "execution-1", "dsh-session-1", "execution-1"
            )
        )
        assert still_running.execution_state == "running"

        clients[0].responses.put_nowait(("end_turn", "Long review complete."))
        for _ in range(20):
            completed = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-1", "execution-1", "dsh-session-1", "execution-1"
                )
            )
            if completed.execution_state != "running":
                break
            await asyncio.sleep(0)
        assert completed.execution_state == "succeeded"

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_adapter_closes_provider_process_after_turn_timeout(
    tmp_path: Path, cancel_fails: bool
) -> None:
    class TimeoutAcpClient(_FakeAcpClient):
        async def cancel(self, session_id: str) -> None:
            self.cancelled.append(session_id)
            if cancel_fails:
                raise RuntimeError("ACP cancel failed")

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[TimeoutAcpClient] = []

        def factory(launch: DshLaunch) -> TimeoutAcpClient:
            client = TimeoutAcpClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=0.1,
            turn_timeout_seconds=0.01,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        task = TaskPacket(
            title="Timeout review",
            prompt="Keep working until the local deadline.",
            acceptance_criteria=("Stop at the deadline",),
            role="reviewer",
        )
        await adapter.spawn(
            AdapterSpawnRequest("conversation-1", "execution-1", task, context)
        )

        for _ in range(50):
            snapshot = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-1", "execution-1", "dsh-session-1", "execution-1"
                )
            )
            if snapshot.execution_state != "running":
                break
            await asyncio.sleep(0.005)

        assert snapshot.execution_state == "failed"
        assert snapshot.error is not None
        assert snapshot.error.code == "RECOVERY_REQUIRED"
        assert snapshot.evidence["cleanup_confirmed"] is True
        assert clients[0].cancelled == ["dsh-session-1"]
        assert clients[0].closed is True

    asyncio.run(scenario())


def test_turn_timeout_marks_cleanup_unconfirmed_when_close_fails(
    tmp_path: Path,
) -> None:
    class CloseFailsAcpClient(_FakeAcpClient):
        def __init__(self, launch: DshLaunch) -> None:
            super().__init__(launch)
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("ACP close failed")
            await super().close()

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[CloseFailsAcpClient] = []

        def factory(launch: DshLaunch) -> CloseFailsAcpClient:
            client = CloseFailsAcpClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=0.1,
            turn_timeout_seconds=0.01,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-close-fails",
                "execution-close-fails",
                TaskPacket(
                    title="Timeout review",
                    prompt="Keep working until the local deadline.",
                    acceptance_criteria=("Stop safely",),
                    role="reviewer",
                ),
                context,
            )
        )

        for _ in range(50):
            snapshot = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-close-fails",
                    "execution-close-fails",
                    "dsh-session-1",
                    "execution-close-fails",
                )
            )
            if snapshot.execution_state != "running":
                break
            await asyncio.sleep(0.005)

        assert snapshot.execution_state == "failed"
        assert snapshot.error is not None
        assert snapshot.error.code == "RECOVERY_REQUIRED"
        assert snapshot.evidence["cleanup_confirmed"] is False
        assert clients[0].closed is False
        closed = await adapter.close(
            AdapterSessionRequest(
                "conversation-close-fails",
                "execution-close-fails",
                "dsh-session-1",
                "execution-close-fails",
            )
        )
        assert closed.conversation_state == "closed"
        assert closed.evidence["process_closed"] is True
        assert closed.evidence["cleanup_confirmed"] is True
        assert clients[0].close_calls == 2
        assert clients[0].closed is True

    asyncio.run(scenario())


def test_turn_timeout_wins_interrupt_race_and_blocks_reuse_of_closed_client(
    tmp_path: Path,
) -> None:
    class RaceAcpClient(_FakeAcpClient):
        def __init__(self, launch: DshLaunch) -> None:
            super().__init__(launch)
            self.close_started = asyncio.Event()
            self.allow_close = asyncio.Event()

        async def close(self) -> None:
            self.close_started.set()
            await self.allow_close.wait()
            self.closed = True

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[RaceAcpClient] = []

        def factory(launch: DshLaunch) -> RaceAcpClient:
            client = RaceAcpClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=0.2,
            turn_timeout_seconds=0.01,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        task = TaskPacket(
            title="Timeout race",
            prompt="Keep working until interrupted.",
            acceptance_criteria=("Stop safely",),
            role="sub-agent",
        )
        await adapter.spawn(
            AdapterSpawnRequest("conversation-1", "execution-1", task, context)
        )
        await asyncio.wait_for(clients[0].close_started.wait(), timeout=1)

        interrupted_task = asyncio.create_task(
            adapter.interrupt(
                AdapterSessionRequest(
                    "conversation-1", "execution-1", "dsh-session-1", "execution-1"
                )
            )
        )
        for _ in range(50):
            if len(clients[0].cancelled) >= 2:
                break
            await asyncio.sleep(0)
        assert clients[0].cancelled == ["dsh-session-1", "dsh-session-1"]
        clients[0].allow_close.set()

        interrupted = await asyncio.wait_for(interrupted_task, timeout=1)
        assert interrupted.execution_state == "failed"
        assert interrupted.error is not None
        assert interrupted.error.code == "RECOVERY_REQUIRED"
        with pytest.raises(ServiceError) as captured:
            await adapter.send(
                AdapterSendRequest(
                    "conversation-1",
                    "execution-2",
                    "dsh-session-1",
                    "Do not reuse the dead client.",
                    None,
                    {},
                    context,
                )
            )
        assert captured.value.code == "RECOVERY_REQUIRED"

    asyncio.run(scenario())


def test_provider_quota_exhaustion_is_reported_without_ambiguous_retry(
    tmp_path: Path,
) -> None:
    class QuotaAcpClient(_FakeAcpClient):
        async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
            self.prompts.append((session_id, prompt))
            raise RuntimeError(
                "turn failed: 429: insufficient_quota; account credits exhausted; "
                "retry shortly"
            )

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[QuotaAcpClient] = []

        def factory(launch: DshLaunch) -> QuotaAcpClient:
            client = QuotaAcpClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        task = TaskPacket(
            title="Review quota handling",
            prompt="Return a result.",
            acceptance_criteria=("Report terminal quota",),
            role="reviewer",
        )
        await adapter.spawn(
            AdapterSpawnRequest("conversation-1", "execution-1", task, context)
        )
        for _ in range(20):
            snapshot = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-1", "execution-1", "dsh-session-1", "execution-1"
                )
            )
            if snapshot.execution_state != "running":
                break
            await asyncio.sleep(0)

        assert snapshot.execution_state == "failed"
        assert snapshot.error is not None
        assert snapshot.error.code == "QUOTA_PAUSED"
        assert "credit" in snapshot.error.message.lower()
        assert len(clients[0].prompts) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("permissions", "write_set", "expected_retryable"),
    [
        (("repo_read",), (), True),
        (("repo_read", "workspace_write"), (".",), False),
    ],
)
def test_generic_provider_error_has_permission_safe_recovery_guidance(
    tmp_path: Path,
    permissions: tuple[str, ...],
    write_set: tuple[str, ...],
    expected_retryable: bool,
) -> None:
    class GenericProviderErrorClient(_FakeAcpClient):
        async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
            self.prompts.append((session_id, prompt))
            raise RuntimeError("Provider returned error: PI_AI_ERROR")

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[GenericProviderErrorClient] = []

        def factory(launch: DshLaunch) -> GenericProviderErrorClient:
            client = GenericProviderErrorClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
        )
        await adapter.probe()
        context = await adapter.resolve_context(
            _context_request(
                workspace,
                permissions=permissions,
                write_set=write_set,
            )
        )
        await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-provider-error",
                "execution-provider-error",
                TaskPacket("Review", "Return a result.", ("Report",), "reviewer"),
                context,
            )
        )
        for _ in range(30):
            snapshot = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-provider-error",
                    "execution-provider-error",
                    "dsh-session-1",
                    "execution-provider-error",
                )
            )
            if snapshot.execution_state != "running":
                break
            await asyncio.sleep(0)

        assert snapshot.execution_state == "failed"
        assert snapshot.error is not None
        assert snapshot.error.code == "PROVIDER_ERROR"
        assert snapshot.error.retryable is expected_retryable
        assert snapshot.error.next_action is not None
        if expected_retryable:
            assert "new read-only conversation" in snapshot.error.next_action
            assert "new request_id" in snapshot.error.next_action
            assert "three total attempts" in snapshot.error.next_action
            assert "usage credits" in snapshot.error.next_action
        else:
            assert "Reconcile the declared write set" in snapshot.error.next_action
            assert "do not retry automatically" in snapshot.error.next_action
        assert len(clients[0].prompts) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("detail", "provider_code", "expected_message"),
    (
        (
            "turn failed: provider route failed",
            "PI_AI_ERROR",
            "DeepSeek ACP provider error (PI_AI_ERROR; RPC -32603): "
            "turn failed: provider route failed",
        ),
        (
            "turn failed: quota=unknown; error: QUOTA",
            "QUOTA",
            "DeepSeek ACP provider error (QUOTA; RPC -32603): "
            "turn failed: quota=unknown; error: QUOTA",
        ),
    ),
)
def test_structured_acp_provider_error_reaches_terminal_snapshot(
    tmp_path: Path,
    detail: str,
    provider_code: str,
    expected_message: str,
) -> None:
    class StructuredProviderErrorClient(_FakeAcpClient):
        async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
            self.prompts.append((session_id, prompt))
            raise deepseek_module._AcpResponseError(
                rpc_code=-32603,
                detail=detail,
                provider_code=provider_code,
            )

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[StructuredProviderErrorClient] = []

        def factory(launch: DshLaunch) -> StructuredProviderErrorClient:
            client = StructuredProviderErrorClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
        )
        await adapter.probe()
        context = await adapter.resolve_context(
            _context_request(
                workspace,
                permissions=("repo_read",),
                write_set=(),
            )
        )
        await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-structured-provider-error",
                "execution-structured-provider-error",
                TaskPacket("Review", "Return a result.", ("Report",), "reviewer"),
                context,
            )
        )
        for _ in range(30):
            snapshot = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-structured-provider-error",
                    "execution-structured-provider-error",
                    "dsh-session-1",
                    "execution-structured-provider-error",
                )
            )
            if snapshot.execution_state != "running":
                break
            await asyncio.sleep(0)

        assert snapshot.execution_state == "failed"
        assert snapshot.error is not None
        assert snapshot.error.code == "PROVIDER_ERROR"
        assert snapshot.error.category == "provider"
        assert snapshot.error.retryable is True
        assert snapshot.error.message == expected_message
        assert snapshot.error.next_action is not None
        assert "new read-only conversation" in snapshot.error.next_action
        assert snapshot.evidence["provider_error"] == {
            "source": "native-acp",
            "rpc_code": -32603,
            "provider_code": provider_code,
            "detail": detail,
        }
        assert len(clients[0].prompts) == 1

    asyncio.run(scenario())


def test_transient_upstream_rate_limit_retries_three_total_attempts(
    tmp_path: Path,
) -> None:
    class RateLimitedThenReadyClient(_FakeAcpClient):
        async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
            self.prompts.append((session_id, prompt))
            if len(self.prompts) < 3:
                raise RuntimeError(
                    "turn failed: 429: stealth/ox-alpha is temporarily rate-limited "
                    "upstream; limit_source=upstream_provider_shared_pool; retry shortly"
                )
            return "end_turn", "CAPSULE: OX_READY"

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[RateLimitedThenReadyClient] = []

        def factory(launch: DshLaunch) -> RateLimitedThenReadyClient:
            client = RateLimitedThenReadyClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
            transient_retry_delay_seconds=0,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-rate-limit",
                "execution-rate-limit",
                TaskPacket("Review", "Return ready.", ("Ready",), "reviewer"),
                context,
            )
        )
        for _ in range(50):
            snapshot = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-rate-limit",
                    "execution-rate-limit",
                    "dsh-session-1",
                    "execution-rate-limit",
                )
            )
            if snapshot.execution_state != "running":
                break
            await asyncio.sleep(0)

        assert snapshot.execution_state == "succeeded"
        assert snapshot.result_text == "CAPSULE: OX_READY"
        assert len(clients[0].prompts) == 3

    asyncio.run(scenario())


def test_successful_first_turn_followup_retries_transient_429_to_terminal_result(
    tmp_path: Path,
) -> None:
    class FollowupRateLimitedThenReadyClient(_FakeAcpClient):
        async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
            self.prompts.append((session_id, prompt))
            if len(self.prompts) == 1:
                return "end_turn", "CAPSULE: ROUND_1_READY"
            if len(self.prompts) == 2:
                raise RuntimeError(
                    "Internal error: turn failed: 429: stealth/ox-alpha is "
                    "temporarily rate-limited upstream; "
                    "limit_source=upstream_provider_shared_pool; retry shortly"
                )
            return "end_turn", "CAPSULE: FOLLOWUP_READY"

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[FollowupRateLimitedThenReadyClient] = []

        def factory(launch: DshLaunch) -> FollowupRateLimitedThenReadyClient:
            client = FollowupRateLimitedThenReadyClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
            transient_retry_delay_seconds=0,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-followup",
                "execution-round-1",
                TaskPacket("Review", "Round one.", ("Ready",), "reviewer"),
                context,
            )
        )
        for _ in range(30):
            first = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-followup",
                    "execution-round-1",
                    "dsh-session-1",
                    "execution-round-1",
                )
            )
            if first.execution_state != "running":
                break
            await asyncio.sleep(0)
        assert first.execution_state == "succeeded"

        await adapter.send(
            AdapterSendRequest(
                "conversation-followup",
                "execution-followup",
                "dsh-session-1",
                "Review round two.",
                None,
                {},
                context,
            )
        )
        for _ in range(30):
            followup = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-followup",
                    "execution-followup",
                    "dsh-session-1",
                    "execution-followup",
                )
            )
            if followup.execution_state != "running":
                break
            await asyncio.sleep(0)

        assert followup.execution_state == "succeeded"
        assert followup.result_text == "CAPSULE: FOLLOWUP_READY"
        assert len(clients[0].prompts) == 3

    asyncio.run(scenario())


def test_transient_upstream_rate_limit_is_retryable_after_three_attempts(
    tmp_path: Path,
) -> None:
    class RateLimitedClient(_FakeAcpClient):
        async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
            self.prompts.append((session_id, prompt))
            raise RuntimeError(
                "turn failed: 429: stealth/ox-alpha is temporarily rate-limited "
                "upstream; limit_source=upstream_provider_shared_pool; retry shortly"
            )

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        clients: list[RateLimitedClient] = []

        def factory(launch: DshLaunch) -> RateLimitedClient:
            client = RateLimitedClient(launch)
            clients.append(client)
            return client

        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=factory,
            data_root=tmp_path / "data",
            timeout_seconds=1,
            transient_retry_delay_seconds=0,
        )
        await adapter.probe()
        context = await adapter.resolve_context(_context_request(workspace))
        await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-rate-limit",
                "execution-rate-limit",
                TaskPacket("Review", "Return ready.", ("Ready",), "reviewer"),
                context,
            )
        )
        for _ in range(50):
            snapshot = await adapter.snapshot(
                AdapterSessionRequest(
                    "conversation-rate-limit",
                    "execution-rate-limit",
                    "dsh-session-1",
                    "execution-rate-limit",
                )
            )
            if snapshot.execution_state != "running":
                break
            await asyncio.sleep(0)

        assert snapshot.execution_state == "failed"
        assert snapshot.error is not None
        assert snapshot.error.code == "RATE_LIMITED"
        assert snapshot.error.retryable is True
        assert snapshot.error.message == (
            "DeepSeek provider is temporarily rate-limited after 3 attempts"
        )
        assert snapshot.error.next_action == (
            "Continue the same live conversation after provider availability changes; "
            "start a new conversation only after a controller or package restart."
        )
        assert len(clients[0].prompts) == 3

    asyncio.run(scenario())


def test_generated_config_uses_native_plugins_without_nested_agents(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    workspace = tmp_path / "workspace"
    persistence = tmp_path / "data" / "sessions"
    config = render_dsh_config(
        binding,
        provider="ox-provider",
        model="stealth/ox-alpha",
        workspace_path=str(workspace),
        persistence_root=persistence,
        permission_mode="read-only",
    )

    assert "provider: \"ox-provider\"" in config
    assert "model: \"stealth/ox-alpha\"" in config
    assert "mode: read-only" in config
    assert "file:///" in config
    assert "dsh-acp-demo" not in config
    assert "subagent" not in config.lower()
    assert "jobs: false" not in config
    assert "auto-top-up" not in config.lower()
    assert ".credentials.yaml" not in config


def test_node_locator_uses_explicit_native_runtime_not_codex_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = tmp_path / "node.exe"
    node.write_bytes(b"node")
    monkeypatch.setenv("SUBAGENT_MCP_DSH_NODE", str(node))
    monkeypatch.setattr("shutil.which", lambda _name: None)

    assert _node_path() == node.resolve()


def test_node_locator_falls_back_to_standard_windows_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_files = tmp_path / "Program Files"
    node = program_files / "nodejs" / "node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"node")
    monkeypatch.delenv("SUBAGENT_MCP_DSH_NODE", raising=False)
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setattr("shutil.which", lambda _name: None)

    assert _node_path() == node.resolve()


def test_node_locator_uses_system_drive_when_mcp_client_sanitizes_program_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_drive = tmp_path / "system-drive"
    node = system_drive / "Program Files" / "nodejs" / "node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"node")
    monkeypatch.delenv("SUBAGENT_MCP_DSH_NODE", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.setenv("SystemDrive", str(system_drive))
    monkeypatch.setattr("shutil.which", lambda _name: None)

    assert _node_path() == node.resolve()


def test_source_locator_infers_checkout_from_native_dsh_profile_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    package_link = (
        home
        / ".dsh"
        / "profiles"
        / "node_modules"
        / "@deepseek-ai"
        / "dsh"
    )
    source_root = tmp_path / "deepseek-harness"
    package_target = source_root / "apps" / "cli"
    acp_binary = source_root / "packages" / "examples" / "acp-demo" / "lib" / "bin.js"
    acp_binary.parent.mkdir(parents=True)
    acp_binary.write_bytes(b"acp")
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == package_link:
            return package_target
        return original_resolve(path, strict=strict)

    monkeypatch.delenv("SUBAGENT_MCP_DSH_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("SUBAGENT_MCP_DSH_ACP_BIN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(Path, "resolve", resolve)

    assert _source_root() == source_root.resolve()


def test_native_dsh_child_environment_excludes_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", r"C:\\Windows\\System32")
    monkeypatch.setenv("SystemRoot", r"C:\\Windows")
    monkeypatch.setenv("TEMP", r"C:\\Temp")
    monkeypatch.setenv("USERPROFILE", r"C:\\Users\\operator")
    monkeypatch.setenv("DSH_HOME", r"C:\\Users\\operator\\.dsh")
    monkeypatch.setenv("SSL_CERT_FILE", r"C:\\Trust\\corporate.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy:8080")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-by-user")
    monkeypatch.setenv("OX_PROVIDER_API_KEY", "configured-by-user")
    monkeypatch.setenv("DATABASE_URL", "postgres://private")
    monkeypatch.setenv("SESSION_COOKIE", "private-cookie")
    monkeypatch.setenv("SERVICE_JWT", "private-jwt")
    monkeypatch.setenv("GITHUB_TOKEN", "private-token")
    monkeypatch.setenv("NODE_OPTIONS", "--require=C:\\private\\hook.js")
    monkeypatch.setenv("PSModulePath", r"C:\\private\\modules")
    monkeypatch.setenv("DSH_BUNDLED_SKILL_DIR", r"C:\\private\\skills")
    monkeypatch.setenv("DSH_TEST_SECRET", "private-dsh-secret")
    monkeypatch.setenv("SUBAGENT_MCP_DSH_SECRET", "private-controller-secret")

    env = _dsh_env("read-only")
    normalized = {name.upper(): value for name, value in env.items()}

    assert normalized["PATH"] == r"C:\\Windows\\System32"
    assert normalized["SYSTEMROOT"] == r"C:\\Windows"
    assert normalized["TEMP"] == r"C:\\Temp"
    assert normalized["USERPROFILE"] == r"C:\\Users\\operator"
    assert normalized["DSH_HOME"] == r"C:\\Users\\operator\\.dsh"
    assert normalized["SSL_CERT_FILE"] == r"C:\\Trust\\corporate.pem"
    for name in (
        "DEEPSEEK_API_KEY",
        "OX_PROVIDER_API_KEY",
        "DATABASE_URL",
        "SESSION_COOKIE",
        "SERVICE_JWT",
        "GITHUB_TOKEN",
        "HTTPS_PROXY",
        "NODE_OPTIONS",
        "PSMODULEPATH",
        "DSH_BUNDLED_SKILL_DIR",
        "DSH_TEST_SECRET",
        "SUBAGENT_MCP_DSH_SECRET",
    ):
        assert name not in normalized
    assert normalized["DSH_PERMISSION_MODE"] == "read-only"
    assert normalized["DSH_TELEMETRY_DISABLED"] == "1"


def test_file_identity_changes_for_same_size_same_mtime_content_swap(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "entry.js"
    executable.write_bytes(b"first-entry")
    original = executable.stat()
    before = _file_identity(executable)

    executable.write_bytes(b"other-entry")
    os.utime(executable, ns=(original.st_atime_ns, original.st_mtime_ns))
    after = _file_identity(executable)

    assert before["path"] == after["path"]
    assert before["size"] == after["size"]
    assert before["mtime_ns"] == after["mtime_ns"]
    assert before["sha256"] != after["sha256"]


def test_acp_boots_from_product_state_not_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".env").write_text("DATABASE_URL=private\n", encoding="utf-8")
        state = tmp_path / "product-state"
        state.mkdir()
        launch = DshLaunch(
            _binding(tmp_path),
            "provider",
            "model",
            str(workspace),
            "read-only",
            state / "sessions",
            state / "cordis.yml",
        )
        captured: dict[str, object] = {}

        async def create_process(*args: object, **kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        async def no_io() -> None:
            return None

        async def request(*args: object, **kwargs: object) -> object:
            return {}

        client = _StdioAcpClient(launch, timeout_seconds=1)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
        monkeypatch.setattr(client, "_read_stdout", no_io)
        monkeypatch.setattr(client, "_drain_stderr", no_io)
        monkeypatch.setattr(client, "_request", request)

        await client.start()

        assert Path(str(captured["cwd"])) == state
        assert Path(str(captured["cwd"])) != workspace

    asyncio.run(scenario())


def test_acp_revalidates_binding_before_process_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        binding = _binding(tmp_path)
        original = binding.node_path.stat()
        binding.node_path.write_bytes(b"evil")
        os.utime(
            binding.node_path,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )
        state = tmp_path / "state"
        state.mkdir()
        launch = DshLaunch(
            binding,
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            state / "sessions",
            state / "cordis.yml",
        )
        launched = False

        async def create_process(*args: object, **kwargs: object) -> object:
            nonlocal launched
            launched = True
            return object()

        client = _StdioAcpClient(launch, timeout_seconds=1)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

        with pytest.raises(ServiceError) as captured:
            await client.start()

        assert captured.value.code == "CONTEXT_DRIFT"
        assert launched is False

    asyncio.run(scenario())


def test_launch_file_hashing_keeps_event_loop_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        binding = _binding(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        launch = DshLaunch(
            binding,
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            state / "sessions",
            state / "cordis.yml",
        )
        release = threading.Event()
        process_started = False

        def blocked_pair_key(*_args: object) -> str:
            release.wait(timeout=1)
            return binding.pair_key

        async def create_process(*_args: object, **_kwargs: object) -> object:
            nonlocal process_started
            process_started = True
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(deepseek_module, "_dsh_pair_key", blocked_pair_key)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
        client = _StdioAcpClient(launch, timeout_seconds=1)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        try:
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await asyncio.wait_for(client.start(), timeout=0.01)
        finally:
            release.set()

        assert loop.time() - started_at < 0.2
        assert process_started is False

    asyncio.run(scenario())


def test_acp_holds_launch_files_read_only_through_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        binding = _binding(tmp_path)
        state = tmp_path / "state"
        state.mkdir()
        launch = DshLaunch(
            binding,
            "provider",
            "model",
            str(tmp_path),
            "read-only",
            state / "sessions",
            state / "cordis.yml",
        )
        swap_blocked = False

        async def create_process(*args: object, **kwargs: object) -> object:
            nonlocal swap_blocked
            with pytest.raises(OSError):
                binding.node_path.write_bytes(b"evil")
            swap_blocked = True
            return object()

        async def no_io() -> None:
            return None

        async def request(*args: object, **kwargs: object) -> object:
            return {}

        client = _StdioAcpClient(launch, timeout_seconds=1)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
        monkeypatch.setattr(client, "_read_stdout", no_io)
        monkeypatch.setattr(client, "_drain_stderr", no_io)
        monkeypatch.setattr(client, "_request", request)

        await client.start()

        assert swap_blocked is True

    asyncio.run(scenario())
