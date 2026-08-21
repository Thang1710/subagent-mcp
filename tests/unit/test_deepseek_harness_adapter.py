from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from subagent_harness_mcp.adapters import (
    AdapterContextRequest,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSpawnRequest,
)
from subagent_harness_mcp.adapters.deepseek_harness import (
    CONTROLLER_RESULT_MAX_CHARS,
    DeepSeekHarnessAdapter,
    DshBinding,
    DshLaunch,
    _StdioAcpClient,
    _bounded_result,
    render_dsh_config,
    _node_path,
    _dsh_env,
    _source_root,
)
from subagent_harness_mcp.contracts import ServiceError, TaskPacket


def test_durable_result_has_large_bound_and_explicit_truncation() -> None:
    result = _bounded_result("x" * (CONTROLLER_RESULT_MAX_CHARS * 2))

    assert CONTROLLER_RESULT_MAX_CHARS == 65_536
    assert len(result) == CONTROLLER_RESULT_MAX_CHARS
    assert result.endswith("\n[truncated by Subagent MCP]")


class _FakeAcpClient:
    def __init__(self, launch: DshLaunch) -> None:
        self.launch = launch
        self.started = False
        self.closed = False
        self.cancelled: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.responses: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def start(self) -> None:
        self.started = True

    async def new_session(self, cwd: str) -> str:
        assert self.started
        assert cwd == self.launch.workspace_path
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
    node.write_bytes(b"node")
    binary.write_bytes(b"acp")
    plugins: dict[str, Path] = {}
    for name in (
        "settings-file",
        "credentials-local",
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
        plugin.write_bytes(name.encode())
        plugins[name] = plugin
    return DshBinding(node, binary, plugins, "0.1.1-rc.2", "pair-1")


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
    }
    values.update(overrides)
    return AdapterContextRequest(**values)  # type: ignore[arg-type]


def test_manifest_and_context_keep_provider_model_opaque(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = DeepSeekHarnessAdapter(
        binding_locator=lambda: _binding(tmp_path),
        data_root=tmp_path / "data",
    )

    probe = asyncio.run(adapter.probe())
    context = asyncio.run(adapter.resolve_context(_context_request(workspace)))

    assert probe.state == "ready"
    assert probe.details == {
        "pair_key": "pair-1",
        "harness_version": "0.1.1-rc.2",
        "transport": "native-acp",
    }
    assert adapter.manifest.runtime_id == "deepseek-harness"
    assert adapter.manifest.supported_transports == ("native-acp",)
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
        assert clients[0].cancelled == ["dsh-session-1"]
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
                "turn failed: insufficient_quota; account credits exhausted"
            )

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        adapter = DeepSeekHarnessAdapter(
            binding_locator=lambda: _binding(tmp_path),
            client_factory=QuotaAcpClient,
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


def test_native_dsh_auth_environment_is_inherited_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-by-user")
    monkeypatch.setenv("OX_PROVIDER_API_KEY", "configured-by-user")

    env = _dsh_env("read-only")

    assert env["DEEPSEEK_API_KEY"] == "configured-by-user"
    assert env["OX_PROVIDER_API_KEY"] == "configured-by-user"
    assert env["DSH_PERMISSION_MODE"] == "read-only"
    assert env["DSH_TELEMETRY_DISABLED"] == "1"
