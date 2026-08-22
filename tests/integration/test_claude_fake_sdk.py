from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
)
from subagent_harness_mcp.adapters.claude_code import (
    CREDENTIAL_OVERRIDE_NAMES,
    ClaudeCodeAdapter,
    CommandResult,
)
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.contracts import (
    ActionRequest,
    SendRequest,
    ServiceError,
    SpawnRequest,
    TaskPacket,
)
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore, VerifiedCleanupReceipt


@pytest.fixture(autouse=True)
def _clean_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_OVERRIDE_NAMES:
        monkeypatch.delenv(name, raising=False)


class _Ids:
    def __init__(self) -> None:
        self._values = itertools.count(1)

    def __call__(self, prefix: str) -> str:
        return f"{prefix}-{next(self._values)}"


class _Runner:
    def __call__(self, argv: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        del timeout_seconds
        if argv[-1] == "--version":
            return CommandResult(0, "2.1.224 (Claude Code)\n", "")
        return CommandResult(
            0,
            json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
            "",
        )


class _PassingClient:
    def __init__(
        self,
        options,
        *,
        session_id: str = "session-1",
        text: str = "review complete",
        fail_disconnect: bool = False,
    ) -> None:
        self.options = options
        self.session_id = options.resume or session_id
        self.text = text
        self.fail_disconnect = fail_disconnect
        self.queried = False
        self.query_prompt: str | None = None
        self.disconnected = False

    async def connect(self, prompt=None) -> None:
        assert prompt is None

    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": self.session_id,
                "cwd": None if self.options.cwd is None else str(self.options.cwd),
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                overage_status="rejected",
                overage_disabled_reason="out_of_credits",
                raw={"isUsingOverage": False},
            ),
            uuid="rate-1",
            session_id=self.session_id,
        )
        while not self.queried:
            await asyncio.sleep(0)
        yield AssistantMessage(
            content=[
                ThinkingBlock("must never persist", "signature"),
                TextBlock(self.text),
            ],
            model="vendor/future-model",
            session_id=self.session_id,
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self.session_id,
            result="private provider result",
            terminal_reason="completed",
            uuid="provider-turn-1",
        )

    async def query(self, prompt: str) -> None:
        assert prompt
        self.query_prompt = prompt
        self.queried = True

    async def disconnect(self) -> None:
        self.disconnected = True
        if self.fail_disconnect:
            raise RuntimeError("simulated disconnect ambiguity")


class _QuotaClient(_PassingClient):
    def __init__(self, options) -> None:
        super().__init__(options)
        self.interrupt_calls = 0

    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": self.session_id,
                "cwd": None if self.options.cwd is None else str(self.options.cwd),
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                overage_status="rejected",
                overage_disabled_reason="out_of_credits",
                raw={"isUsingOverage": False},
            ),
            uuid="rate-safe",
            session_id=self.session_id,
        )
        while not self.queried:
            await asyncio.sleep(0)
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status=None,
                raw={"isUsingOverage": True},
            ),
            uuid="rate-unsafe",
            session_id=self.session_id,
        )

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


class _QuotaStatusClient(_PassingClient):
    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": self.session_id,
                "cwd": None if self.options.cwd is None else str(self.options.cwd),
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="rate-quota-status",
            session_id=self.session_id,
        )


def test_canary_is_single_launch_idempotent_and_gates_spawn(tmp_path: Path) -> None:
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )
    config = ConfigStore(paths)
    config.save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "claude-code": {
                    "enabled": True,
                    "selection_mode": "lead-selects",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "future-deep",
                            "model": "vendor/future-model",
                            "reasoning": {"effort": "xhigh"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_PassingClient] = []

    def factory(options):
        client = _PassingClient(options)
        clients.append(client)
        return client

    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=factory,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=1,
    )
    store = StateStore.open(paths)
    service = SubagentMcpService(
        config=config,
        store=store,
        registry=AdapterRegistry(builtin_factories=(lambda: adapter,)),
        id_factory=_Ids(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spawn = SpawnRequest(
        request_id="spawn-1",
        runtime_id="claude-code",
        variant_id="future-deep",
        task=TaskPacket("Review", "Review only.", ("Report.",), "sub-agent"),
        cwd=str(workspace),
        mode="review",
        transport="managed-sdk",
        permissions=("repo_read",),
    )

    check = asyncio.run(service.runtime_check("claude-code"))
    with pytest.raises(ServiceError) as blocked:
        asyncio.run(service.agent_spawn(spawn))
    payload = {
        "request_id": "canary-1",
        "runtime_id": "claude-code",
        "variant_id": "future-deep",
        "transport": "managed-sdk",
    }
    first = asyncio.run(service.runtime_canary(payload))
    replay = asyncio.run(service.runtime_canary(payload))

    assert check["state"] == "needs_canary"
    assert blocked.value.code == "CAPABILITY_MISSING"
    assert first == replay
    assert first["state"] == "ready"
    assert first["attestation"]["overage_blocked"] is True
    assert len(clients) == 1
    assert clients[0].disconnected is True
    assert "private provider result" not in json.dumps(first)
    assert "must never persist" not in paths.database_file.read_bytes().decode(
        "utf-8", errors="ignore"
    )
    started = asyncio.run(service.agent_spawn(spawn))
    assert started.execution_state == "succeeded"
    assert started.external_session_id == "session-1"
    assert started.result == {"text": "review complete"}
    assert len(clients) == 2
    assert clients[1].options.resume is None
    assert clients[1].options.model == "vendor/future-model"
    assert clients[1].options.cwd == workspace.resolve()
    assert clients[1].options.cli_path == cli.resolve()
    assert clients[1].options.fallback_model is None
    assert clients[1].options.strict_mcp_config is True
    assert clients[1].options.mcp_servers == {}
    assert clients[1].options.setting_sources == ["user"]
    assert clients[1].options.skills == "all"
    assert clients[1].options.tools == ["Read", "Glob", "Grep"]
    assert clients[1].options.env == {
        "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
        "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
        "DISABLE_EXTRA_USAGE_COMMAND": "1",
    }
    assert clients[1].disconnected is True

    changed = config.load()
    changed["runtimes"]["claude-code"]["variants"][0]["model"] = (
        "vendor/future-model-v2"
    )
    changed["runtimes"]["claude-code"]["variants"][0]["reasoning"] = {
        "effort": "max"
    }
    config.save(changed, expected_revision=1)
    drifted = asyncio.run(service.runtime_check("claude-code"))
    with pytest.raises(ServiceError) as stale_request:
        asyncio.run(service.runtime_canary(payload))

    assert drifted["state"] == "needs_canary"
    assert stale_request.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(clients) == 2


def test_fresh_adapter_send_resumes_exact_session_and_permission_context(
    tmp_path: Path,
) -> None:
    paths = resolve_paths({"SUBAGENT_MCP_HOME": str(tmp_path / "home")}, os_name="nt")
    config = ConfigStore(paths)
    config.save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "claude-code": {
                    "enabled": True,
                    "selection_mode": "lead-selects",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "future-deep",
                            "model": "vendor/future-model",
                            "reasoning": {"effort": "xhigh"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_clients: list[_PassingClient] = []

    def first_factory(options):
        client = _PassingClient(options)
        first_clients.append(client)
        return client

    first_adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=first_factory,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=1,
    )
    service = SubagentMcpService(
        config=config,
        store=StateStore.open(paths),
        registry=AdapterRegistry(builtin_factories=(lambda: first_adapter,)),
        id_factory=_Ids(),
    )
    asyncio.run(
        service.runtime_canary(
            {
                "request_id": "canary-1",
                "runtime_id": "claude-code",
                "variant_id": "future-deep",
                "transport": "managed-sdk",
            }
        )
    )
    started = asyncio.run(
        service.agent_spawn(
            SpawnRequest(
                request_id="spawn-1",
                runtime_id="claude-code",
                variant_id="future-deep",
                task=TaskPacket("Implement", "Create the bounded change.", ("Done",), "sub-agent"),
                cwd=str(workspace),
                mode="implement",
                transport="managed-sdk",
                permissions=("repo_read", "workspace_write"),
            )
        )
    )

    resumed_clients: list[_PassingClient] = []

    def resumed_factory(options):
        client = _PassingClient(options, text="follow-up complete")
        resumed_clients.append(client)
        return client

    fresh_adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=resumed_factory,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=1,
    )
    fresh_service = SubagentMcpService(
        config=config,
        store=StateStore.open(paths),
        registry=AdapterRegistry(builtin_factories=(lambda: fresh_adapter,)),
        id_factory=_Ids(),
    )
    followed = asyncio.run(
        fresh_service.agent_send(
            SendRequest("send-1", started.conversation_id, "Now finish the follow-up.")
        )
    )
    closed = asyncio.run(
        fresh_service.agent_close(ActionRequest("close-1", started.conversation_id))
    )

    assert followed.execution_state == "succeeded"
    assert followed.external_session_id == started.external_session_id == "session-1"
    assert followed.result == {"text": "follow-up complete"}
    assert len(resumed_clients) == 1
    assert resumed_clients[0].options.resume == "session-1"
    assert resumed_clients[0].options.cwd == workspace.resolve()
    assert resumed_clients[0].options.tools == ["Read", "Glob", "Grep", "Edit", "Write"]
    assert resumed_clients[0].options.allowed_tools == [
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
    ]
    assert closed.conversation_state == "closed"


def test_disconnect_ambiguity_is_persisted_and_blocks_fresh_adapter_reuse(
    tmp_path: Path,
) -> None:
    paths = resolve_paths({"SUBAGENT_MCP_HOME": str(tmp_path / "home")}, os_name="nt")
    config = ConfigStore(paths)
    config.save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "claude-code": {
                    "enabled": True,
                    "selection_mode": "lead-selects",
                    "fallback": False,
                    "variants": [{"id": "future-deep", "model": "vendor/future-model", "reasoning": {"effort": "xhigh"}}],
                }
            },
        },
        expected_revision=0,
    )
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created: list[_PassingClient] = []

    def factory(options):
        client = _PassingClient(options, fail_disconnect=len(created) == 1)
        created.append(client)
        return client

    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=factory,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=1,
    )
    service = SubagentMcpService(
        config=config,
        store=StateStore.open(paths),
        registry=AdapterRegistry(builtin_factories=(lambda: adapter,)),
        id_factory=_Ids(),
    )
    asyncio.run(service.runtime_canary({"request_id": "canary-1", "runtime_id": "claude-code", "variant_id": "future-deep", "transport": "managed-sdk"}))
    with pytest.raises(ServiceError) as failed:
        asyncio.run(
            service.agent_spawn(
                SpawnRequest(
                    request_id="spawn-1",
                    runtime_id="claude-code",
                    variant_id="future-deep",
                    task=TaskPacket("Review", "Review.", ("Done",), "sub-agent"),
                    cwd=str(workspace),
                    mode="review",
                    transport="managed-sdk",
                    permissions=("repo_read",),
                )
            )
        )
    fresh_calls: list[object] = []
    fresh_adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=lambda options: fresh_calls.append(options),
        sdk_version="0.2.142",
        bundled_cli_paths=(),
    )
    fresh_service = SubagentMcpService(
        config=config,
        store=StateStore.open(paths),
        registry=AdapterRegistry(builtin_factories=(lambda: fresh_adapter,)),
        id_factory=_Ids(),
    )
    with pytest.raises(ServiceError) as blocked:
        asyncio.run(
            fresh_service.agent_send(
                SendRequest("send-1", "conversation-1", "Do not run this.")
            )
        )

    assert failed.value.code == "RECOVERY_REQUIRED"
    assert blocked.value.code == "RECOVERY_REQUIRED"
    assert fresh_calls == []


def test_orphan_probing_requires_verified_cleanup_receipt_before_reset(
    tmp_path: Path,
) -> None:
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )
    config = ConfigStore(paths)
    config.save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "claude-code": {
                    "enabled": True,
                    "selection_mode": "lead-selects",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "future-deep",
                            "model": "vendor/future-model",
                            "reasoning": {"effort": "xhigh"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")

    def explode(_options):
        raise RuntimeError("simulated host crash after circuit claim")

    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=explode,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=1,
    )
    store = StateStore.open(paths)
    verifier_calls: list[dict[str, object]] = []

    def verifier(raw, circuit):
        verifier_calls.append(dict(raw))
        if raw.get("receipt_id") != "cleanup-1":
            return None
        return VerifiedCleanupReceipt(
            receipt_id="cleanup-1",
            pair_key=circuit.pair_key,
            verifier_id="deterministic-fake-verifier",
            process_identity="fake-process-1",
            evidence={"source": "deterministic-fake"},
        )

    service = SubagentMcpService(
        config=config,
        store=store,
        registry=AdapterRegistry(builtin_factories=(lambda: adapter,)),
        id_factory=_Ids(),
        canary_cleanup_verifier=verifier,
    )
    base = {
        "runtime_id": "claude-code",
        "variant_id": "future-deep",
        "transport": "managed-sdk",
    }

    with pytest.raises(RuntimeError, match="simulated host crash"):
        asyncio.run(service.runtime_canary(base | {"request_id": "canary-crash"}))
    with pytest.raises(ServiceError) as orphan:
        asyncio.run(service.runtime_canary(base | {"request_id": "canary-replay"}))
    recovered = asyncio.run(
        service.runtime_canary(
            base
            | {
                "request_id": "canary-recover",
                "cleanup_receipt": {"receipt_id": "cleanup-1"},
            }
        )
    )

    assert orphan.value.code == "RECOVERY_REQUIRED"
    assert recovered["state"] == "needs_canary"
    assert recovered["recovered"] is True
    assert verifier_calls == [{"receipt_id": "cleanup-1"}]
    assert store.load_circuit("claude-code", "future-deep").state == "needs_canary"


def test_spawn_and_send_auto_resume_only_after_safe_task_response(
    tmp_path: Path,
) -> None:
    paths = resolve_paths({"SUBAGENT_MCP_HOME": str(tmp_path / "home")}, os_name="nt")
    config = ConfigStore(paths)
    config.save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "claude-code": {
                    "enabled": True,
                    "selection_mode": "lead-selects",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "future-deep",
                            "model": "vendor/future-model",
                            "reasoning": {"effort": "xhigh"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    modes = iter(
        (
            "pass",
            "quota_turn",
            "pass",
            "quota_turn",
            "pass",
        )
    )
    clients: list[_PassingClient] = []

    def factory(options):
        mode = next(modes)
        client = (
            _QuotaStatusClient(options)
            if mode == "quota_status"
            else _QuotaClient(options)
            if mode == "quota_turn"
            else _PassingClient(options)
        )
        clients.append(client)
        return client

    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=factory,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=1,
    )
    store = StateStore.open(paths)
    service = SubagentMcpService(
        config=config,
        store=store,
        registry=AdapterRegistry(builtin_factories=(lambda: adapter,)),
        id_factory=_Ids(),
    )

    def spawn(request_id: str):
        return service.agent_spawn(
            SpawnRequest(
                request_id=request_id,
                runtime_id="claude-code",
                variant_id="future-deep",
                task=TaskPacket("Review", "Review only.", ("Report.",), "sub-agent"),
                cwd=str(workspace),
                mode="review",
                transport="managed-sdk",
                permissions=("repo_read",),
            )
        )

    canary = {
        "runtime_id": "claude-code",
        "variant_id": "future-deep",
        "transport": "managed-sdk",
    }
    asyncio.run(service.runtime_canary(canary | {"request_id": "canary-initial"}))
    with pytest.raises(ServiceError) as spawn_quota:
        asyncio.run(spawn("spawn-quota"))
    assert spawn_quota.value.code == "QUOTA_PAUSED"
    assert store.load_circuit("claude-code", "future-deep").state == "auto_paused"
    assert isinstance(clients[-1], _QuotaClient)
    assert len(clients) == 2

    started = asyncio.run(spawn("spawn-auto-resume"))
    assert started.execution_state == "succeeded"
    assert store.load_circuit("claude-code", "future-deep").state == "ready"
    assert len(clients) == 3

    with pytest.raises(ServiceError) as send_quota:
        asyncio.run(
            service.agent_send(
                SendRequest("send-quota", started.conversation_id, "Continue safely.")
            )
        )
    assert send_quota.value.code == "QUOTA_PAUSED"
    assert store.load_circuit("claude-code", "future-deep").state == "auto_paused"
    assert isinstance(clients[-1], _QuotaClient) and clients[-1].interrupt_calls == 1
    assert len(clients) == 4

    resumed_send = asyncio.run(
        service.agent_send(
            SendRequest("send-auto-resume", started.conversation_id, "Continue after status.")
        )
    )
    assert resumed_send.execution_state == "succeeded"
    assert store.load_circuit("claude-code", "future-deep").state == "ready"
    assert len(clients) == 5
