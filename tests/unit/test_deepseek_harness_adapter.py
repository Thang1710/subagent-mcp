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
    DeepSeekHarnessAdapter,
    DshBinding,
    DshLaunch,
    render_dsh_config,
    _node_path,
    _dsh_env,
    _source_root,
)
from subagent_harness_mcp.contracts import ServiceError, TaskPacket


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
