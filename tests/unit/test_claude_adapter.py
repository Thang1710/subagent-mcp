from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from subagent_harness_mcp.adapters import (
    AdapterContextRequest,
    AdapterSpawnRequest,
    CanaryRequest,
)
from subagent_harness_mcp.adapters.claude_code import (
    CONTROLLER_RESULT_MAX_CHARS,
    CREDENTIAL_OVERRIDE_NAMES,
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ClaudeCodeAdapter,
    CommandResult,
    _bounded_controller_result,
    _result_error,
    _spawn_prompt,
    _subscription_oauth_source,
)
from subagent_harness_mcp.contracts import ServiceError, TaskPacket


@pytest.fixture(autouse=True)
def _clean_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_OVERRIDE_NAMES:
        monkeypatch.delenv(name, raising=False)


class _Runner:
    def __call__(self, argv: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        del timeout_seconds
        if argv[-1] == "--version":
            return CommandResult(0, "2.1.224 (Claude Code)\n", "")
        assert argv[-3:] == ("auth", "status", "--json")
        return CommandResult(
            0,
            json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
            "",
        )


def _pair(base_pair_key: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "base_pair_key": base_pair_key,
                "model": "vendor/future-model",
                "reasoning": {"effort": "xhigh"},
                "transport": "managed-sdk",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def test_manifest_publishes_exact_model_suggestions_and_reasoning_efforts() -> None:
    manifest = ClaudeCodeAdapter().manifest

    choices = manifest.model_schema["anyOf"]
    assert [choice.get("const") for choice in choices[:-1]] == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
    ]
    assert [choice["title"] for choice in choices[:-1]] == [
        "Opus 5",
        "Sonnet 5",
        "Fable 5",
    ]
    assert choices[-1] == {
        "type": "string",
        "minLength": 1,
        "title": "Custom exact model ID",
    }
    assert manifest.reasoning_schema["properties"]["effort"]["enum"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_docs_only_release_keeps_claude_adapter_compatibility_identity(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        sdk_version="0.2.142",
        bundled_cli_paths=(),
    )

    probe = asyncio.run(adapter.probe())

    assert adapter.manifest.adapter_version == "1.0.0"
    assert probe.details["adapter_version"] == "1.0.0"
    pair_payload = {
        "adapter_version": "1.0.0",
        "sdk_version": probe.details["sdk_version"],
        "cli_path": os.path.normcase(probe.details["cli_path"]),
        "cli_version": probe.details["cli_version"],
        "cli_sha256": probe.details["cli_sha256"],
        "cli_file_id": probe.details["cli_file_id"],
        "transport": "managed-sdk-default",
    }
    assert probe.details["pair_key"] == hashlib.sha256(
        json.dumps(pair_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_setup_timeout_is_bounded_but_terminal_turn_has_no_default_deadline() -> None:
    adapter = ClaudeCodeAdapter()

    assert DEFAULT_PROVIDER_TIMEOUT_SECONDS == 180.0
    assert adapter._canary_timeout == DEFAULT_PROVIDER_TIMEOUT_SECONDS
    assert adapter._turn_timeout is None


@pytest.mark.parametrize("source", ["none", "oauth"])
def test_only_subscription_oauth_init_sources_are_accepted(source: str) -> None:
    assert _subscription_oauth_source({"apiKeySource": source}) is True


@pytest.mark.parametrize("source", [None, "user", "project", "org", "temporary"])
def test_non_subscription_init_sources_are_rejected(source: str | None) -> None:
    assert _subscription_oauth_source({"apiKeySource": source}) is False


def test_durable_result_has_large_bound_and_explicit_truncation() -> None:
    result = _bounded_controller_result(["x" * (CONTROLLER_RESULT_MAX_CHARS * 2)])

    assert CONTROLLER_RESULT_MAX_CHARS == 65_536
    assert len(result) == CONTROLLER_RESULT_MAX_CHARS
    assert result.endswith("\n[truncated by Subagent MCP]")


def test_task_prompt_requests_capsule_plus_complete_detail_without_word_cap() -> None:
    prompt = _spawn_prompt(
        SimpleNamespace(
            task=TaskPacket(
                "Review",
                "Inspect the change.",
                ("Return evidence.",),
                "sub-agent",
            )
        )
    )

    assert "CAPSULE:" in prompt
    assert "DETAILS:" in prompt
    assert "500 words" not in prompt


@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_BASE_URL",
    ],
)
def test_probe_rejects_every_higher_precedence_auth_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    monkeypatch.setenv(name, "present")
    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        sdk_version="0.2.142",
        bundled_cli_paths=(),
    )

    result = asyncio.run(adapter.probe())

    assert name in CREDENTIAL_OVERRIDE_NAMES
    assert result.state == "incompatible"
    assert result.details == {"code": "CREDENTIAL_OVERRIDE"}


class _UnsafeClient:
    def __init__(self, options) -> None:
        self.options = options
        self.connected_with = object()
        self.query_calls = 0
        self.interrupt_calls = 0
        self.disconnected = False

    async def connect(self, prompt=None) -> None:
        self.connected_with = prompt

    async def receive_messages(self):
        while not self.query_calls:
            await asyncio.sleep(0)
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "session-1",
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status=None,
                raw={"isUsingOverage": True},
            ),
            uuid="rate-1",
            session_id="session-1",
        )

    async def query(self, prompt: str) -> None:
        del prompt
        self.query_calls += 1

    async def disconnect(self) -> None:
        self.disconnected = True

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


class _PostQueryUnsafeClient(_UnsafeClient):
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
                "session_id": "session-1",
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="rate-1",
            session_id="session-1",
        )
        while not self.query_calls:
            await asyncio.sleep(0)
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status=None,
                raw={"isUsingOverage": True},
            ),
            uuid="rate-2",
            session_id="session-1",
        )

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


class _SafeQuotaClient(_UnsafeClient):
    async def receive_messages(self):
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="quota-rate-1",
            session_id="quota-session-1",
        )
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "quota-session-1",
            },
        )


class _StartupUnsafeClient(_UnsafeClient):
    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "quota-session-unsafe",
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status=None,
                raw={"isUsingOverage": True},
            ),
            uuid="quota-rate-unsafe",
            session_id="quota-session-unsafe",
        )


class _RateFirstUnsafeClient(_StartupUnsafeClient):
    async def receive_messages(self):
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status=None,
                raw={"isUsingOverage": True},
            ),
            uuid="quota-rate-unsafe",
            session_id="quota-session-unsafe",
        )
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "quota-session-unsafe",
            },
        )


class _PreflightOrderingClient(_UnsafeClient):
    rate_first = False
    assistant_session_id: str | None = "preflight-session"
    terminal_delay_seconds = 0.0

    def __init__(self, options) -> None:
        super().__init__(options)
        self.init_delivered = False
        self.query_started_before_init = False

    async def query(self, prompt: str) -> None:
        self.query_started_before_init = not self.init_delivered
        await super().query(prompt)

    async def receive_messages(self):
        init = SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "preflight-session",
                "cwd": str(self.options.cwd),
            },
        )
        rate = RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="preflight-rate",
            session_id="preflight-session",
        )
        if self.rate_first:
            yield rate
        self.init_delivered = True
        yield init
        while not self.query_calls:
            await asyncio.sleep(0)
        if not self.rate_first:
            yield rate
        if self.terminal_delay_seconds:
            await asyncio.sleep(self.terminal_delay_seconds)
        yield AssistantMessage(
            content=[TextBlock("provider-authorized result")],
            model="vendor/future-model",
            session_id=self.assistant_session_id,
        )
        # Informational status may arrive after output. It must still be checked,
        # but its ordering cannot invalidate already-authorized output.
        yield rate
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="preflight-session",
            result="provider-authorized result",
            terminal_reason="completed",
            uuid="preflight-result",
        )


@pytest.mark.parametrize(
    ("rate_first", "assistant_session_id"),
    [
        (False, "preflight-session"),
        (True, "preflight-session"),
        (False, None),
        (True, None),
    ],
)
def test_lifecycle_queries_after_control_connect_and_uses_response_attestation(
    tmp_path: Path,
    rate_first: bool,
    assistant_session_id: str | None,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_PreflightOrderingClient] = []

    def factory(options):
        client = _PreflightOrderingClient(options)
        client.rate_first = rate_first
        client.assistant_session_id = assistant_session_id
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
    asyncio.run(adapter.probe())
    context = asyncio.run(
        adapter.resolve_context(
            AdapterContextRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                workspace_path=str(workspace.resolve()),
                workspace_key="workspace-1",
                transport="managed-sdk",
                permissions=("repo_read",),
                context_policy_id="context-1",
                permission_policy_id="permission-1",
            )
        )
    )

    snapshot = asyncio.run(
        adapter.spawn(
            AdapterSpawnRequest(
                "conversation-1",
                "execution-1",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
    )

    assert snapshot.execution_state == "succeeded"
    assert snapshot.result_text == "provider-authorized result"
    assert clients[0].query_started_before_init is True
    assert clients[0].query_calls == 1
    assert clients[0].options.max_turns is None
    assert snapshot.evidence["rate_evidence_seen"] is True
    assert snapshot.evidence["is_using_overage"] is False
    assert snapshot.evidence["cleanup_confirmed"] is True


def test_max_turn_result_keeps_the_terminal_reason_and_count() -> None:
    failure = _result_error(
        SimpleNamespace(
            api_error_status=None,
            subtype="error_max_turns",
            terminal_reason="max_turns",
            num_turns=32,
        )
    )

    assert failure.code == "CAPABILITY_MISSING"
    assert failure.message == "Claude task reached its turn limit after 32 turns"


def test_terminal_turn_has_no_product_completion_deadline(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_PreflightOrderingClient] = []

    def factory(options):
        client = _PreflightOrderingClient(options)
        client.terminal_delay_seconds = 0.05
        clients.append(client)
        return client

    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=factory,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=0.01,
    )
    asyncio.run(adapter.probe())
    context = asyncio.run(
        adapter.resolve_context(
            AdapterContextRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                workspace_path=str(workspace.resolve()),
                workspace_key="workspace-1",
                transport="managed-sdk",
                permissions=("repo_read",),
                context_policy_id="context-1",
                permission_policy_id="permission-1",
            )
        )
    )

    snapshot = asyncio.run(
        adapter.spawn(
            AdapterSpawnRequest(
                "conversation-slow",
                "execution-slow",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
    )

    assert snapshot.execution_state == "succeeded"
    assert clients[0].disconnected is True


def test_canary_rejects_unsafe_task_rate_without_accepting_output(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_StartupUnsafeClient] = []

    def factory(options):
        client = _StartupUnsafeClient(options)
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
    probe = asyncio.run(adapter.probe())
    base_pair_key = str(probe.details["pair_key"])

    result = asyncio.run(
        adapter.runtime_canary(
            CanaryRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                transport="managed-sdk",
                base_pair_key=base_pair_key,
                pair_key=_pair(base_pair_key),
            )
        )
    )

    assert result.passed is False
    assert result.error is not None and result.error.code == "QUOTA_PAUSED"
    assert clients[0].query_calls == 1
    assert clients[0].disconnected is True


def test_quota_probe_reports_unknown_without_reading_response_only_status(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_SafeQuotaClient] = []

    def factory(options):
        client = _SafeQuotaClient(options)
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
    probe = asyncio.run(adapter.probe())
    base_pair_key = str(probe.details["pair_key"])

    result = asyncio.run(
        adapter.quota_probe(
            CanaryRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                transport="managed-sdk",
                base_pair_key=base_pair_key,
                pair_key=_pair(base_pair_key),
            )
        )
    )

    assert result.passed is False
    assert result.error is not None and result.error.code == "CAPABILITY_MISSING"
    assert clients[0].query_calls == 0
    assert clients[0].interrupt_calls == 0
    assert clients[0].disconnected is True


def test_quota_probe_never_waits_for_stream_init_or_queries(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    class _InitOnlyClient(_UnsafeClient):
        async def receive_messages(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    clients: list[_InitOnlyClient] = []

    def factory(options):
        client = _InitOnlyClient(options)
        clients.append(client)
        return client

    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        client_factory=factory,
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        canary_timeout_seconds=5,
    )
    probe = asyncio.run(adapter.probe())
    base_pair_key = str(probe.details["pair_key"])

    result = asyncio.run(
        asyncio.wait_for(
            adapter.quota_probe(
                CanaryRequest(
                    runtime_id="claude-code",
                    variant_id="future-deep",
                    model="vendor/future-model",
                    reasoning={"effort": "xhigh"},
                    transport="managed-sdk",
                    base_pair_key=base_pair_key,
                    pair_key=_pair(base_pair_key),
                )
            )
            ,
            timeout=0.2,
        )
    )

    assert result.passed is False
    assert result.error is not None and result.error.code == "CAPABILITY_MISSING"
    assert clients[0].query_calls == 0
    assert clients[0].interrupt_calls == 0
    assert clients[0].disconnected is True


def test_quota_probe_does_not_treat_unrequested_buffer_as_status(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_RateFirstUnsafeClient] = []

    def factory(options):
        client = _RateFirstUnsafeClient(options)
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
    probe = asyncio.run(adapter.probe())
    base_pair_key = str(probe.details["pair_key"])

    result = asyncio.run(
        adapter.quota_probe(
            CanaryRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                transport="managed-sdk",
                base_pair_key=base_pair_key,
                pair_key=_pair(base_pair_key),
            )
        )
    )

    assert result.passed is False
    assert result.error is not None and result.error.code == "CAPABILITY_MISSING"
    assert clients[0].query_calls == 0
    assert clients[0].interrupt_calls == 0
    assert clients[0].disconnected is True


def test_canary_interrupts_on_any_non_allowed_task_rate(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_StartupUnsafeClient] = []

    def factory(options):
        client = _StartupUnsafeClient(options)
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
    probe = asyncio.run(adapter.probe())
    base_pair_key = str(probe.details["pair_key"])

    result = asyncio.run(
        adapter.runtime_canary(
            CanaryRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                transport="managed-sdk",
                base_pair_key=base_pair_key,
                pair_key=_pair(base_pair_key),
            )
        )
    )

    assert probe.state == "needs_canary"
    assert result.passed is False
    assert result.error is not None
    assert result.error.code == "QUOTA_PAUSED"
    assert len(clients) == 1
    assert clients[0].connected_with is None
    assert clients[0].query_calls == 1
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True
    assert clients[0].options.cli_path == cli.resolve()
    assert clients[0].options.strict_mcp_config is True
    assert clients[0].options.mcp_servers == {}
    assert clients[0].options.fallback_model is None
    assert clients[0].options.env == {
        "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
        "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
        "DISABLE_EXTRA_USAGE_COMMAND": "1",
    }


def test_canary_interrupts_immediately_on_unsafe_rate_after_query(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_PostQueryUnsafeClient] = []

    def factory(options):
        client = _PostQueryUnsafeClient(options)
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
    probe = asyncio.run(adapter.probe())
    base_pair_key = str(probe.details["pair_key"])
    result = asyncio.run(
        adapter.runtime_canary(
            CanaryRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                transport="managed-sdk",
                base_pair_key=base_pair_key,
                pair_key=_pair(base_pair_key),
            )
        )
    )

    assert result.passed is False
    assert result.error is not None and result.error.code == "QUOTA_PAUSED"
    assert clients[0].query_calls == 1
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_resolve_context_binds_opaque_model_workspace_and_resume_policy(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        sdk_version="0.2.142",
        bundled_cli_paths=(),
    )

    probe = asyncio.run(adapter.probe())
    context = asyncio.run(
        adapter.resolve_context(
            AdapterContextRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                workspace_path=str(workspace.resolve()),
                workspace_key=str(workspace.resolve()),
                transport="managed-sdk",
                permissions=("repo_read", "workspace_write"),
                context_policy_id="declared-native",
                permission_policy_id="default",
                write_set=("src/context", "docs/status.md"),
            )
        )
    )

    assert probe.state == "needs_canary"
    assert context.requested_model == context.effective_model == "vendor/future-model"
    assert context.requested_reasoning == context.effective_reasoning == {
        "effort": "xhigh"
    }
    assert context.workspace_path == str(workspace.resolve())
    assert context.transport == "managed-sdk"
    assert context.attestation == {
        "source": "claude-code-managed-sdk",
        "variant_id": "future-deep",
        "permissions": ["repo_read", "workspace_write"],
        "write_set": ["src/context", "docs/status.md"],
        "context_policy_id": "declared-native",
        "permission_policy_id": "default",
    }
    assert "workspace_write" in adapter.manifest.semantic_permissions
    assert "background_lifecycle" in context.capability_gaps
    assert "declared_mcp" in context.capability_gaps
    assert "project_local_context_and_hooks" in context.capability_gaps


def test_claude_pre_tool_hook_denies_write_outside_attested_set(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    allowed_root = workspace / "src" / "context"
    allowed_root.mkdir(parents=True)
    clients: list[_PreflightOrderingClient] = []

    def factory(options):
        client = _PreflightOrderingClient(options)
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
    asyncio.run(adapter.probe())
    context = asyncio.run(
        adapter.resolve_context(
            AdapterContextRequest(
                runtime_id="claude-code",
                variant_id="future-deep",
                model="vendor/future-model",
                reasoning={"effort": "xhigh"},
                workspace_path=str(workspace.resolve()),
                workspace_key=str(workspace.resolve()),
                transport="managed-sdk",
                permissions=("repo_read", "workspace_write"),
                context_policy_id="declared-native",
                permission_policy_id="default",
                write_set=("src/context",),
            )
        )
    )
    asyncio.run(
        adapter.spawn(
            AdapterSpawnRequest(
                "conversation-scope",
                "execution-scope",
                TaskPacket("Implement", "Change context.", ("Done",), "writer"),
                context,
            )
        )
    )
    hook = clients[0].options.hooks["PreToolUse"][0].hooks[0]

    allowed = asyncio.run(
        hook(
            {"tool_name": "Write", "tool_input": {"file_path": "src/context/a.py"}},
            None,
            None,
        )
    )
    denied = asyncio.run(
        hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "src/other.py"}},
            None,
            None,
        )
    )

    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
