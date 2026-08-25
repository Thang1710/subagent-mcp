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
    CLIJSONDecodeError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from subagent_harness_mcp.adapters import (
    AdapterContextRequest,
    AdapterSessionRequest,
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
    _unsafe_rate,
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


def test_rate_guard_accepts_optional_missing_overage_status_when_not_in_use() -> None:
    info = RateLimitInfo(
        status="allowed_warning",
        overage_status=None,
        raw={"isUsingOverage": False},
    )

    assert _unsafe_rate(info) is None


def test_rate_guard_rejects_available_overage_even_when_not_yet_in_use() -> None:
    info = RateLimitInfo(
        status="allowed",
        overage_status="allowed",
        raw={"isUsingOverage": False},
    )

    failure = _unsafe_rate(info)

    assert failure is not None
    assert failure.code == "USAGE_CREDITS_FORBIDDEN"


def test_rate_guard_waits_for_terminal_result_after_rejected_plan_status() -> None:
    info = RateLimitInfo(
        status="rejected",
        overage_status="rejected",
        raw={"isUsingOverage": False},
    )

    assert _unsafe_rate(info) is None


def test_rate_limit_envelope_alone_does_not_pause_plan_backed_execution() -> None:
    info = RateLimitInfo(
        status="rejected",
        rate_limit_type="seven_day_opus",
        overage_status="rejected",
        overage_disabled_reason="out_of_credits",
        raw={"isUsingOverage": False},
    )

    assert _unsafe_rate(info) is None


def test_rate_guard_reports_missing_no_overage_boolean_as_unknown() -> None:
    info = RateLimitInfo(
        status="allowed",
        overage_status=None,
        raw={},
    )

    failure = _unsafe_rate(info)

    assert failure is not None
    assert failure.code == "CAPABILITY_MISSING"
    assert failure.message == "Claude no-overage evidence is unavailable"


def test_manifest_publishes_exact_model_suggestions_and_reasoning_efforts() -> None:
    manifest = ClaudeCodeAdapter().manifest

    assert manifest.max_write_roots_per_session == 32

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


def test_claude_adapter_version_changes_its_pair_identity(
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

    assert adapter.manifest.adapter_version == "1.0.3"
    assert probe.details["adapter_version"] == "1.0.3"
    pair_payload = {
        "adapter_version": "1.0.3",
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


def test_task_prompt_publishes_controller_verified_input_hash() -> None:
    digest = "a" * 64
    prompt = _spawn_prompt(
        SimpleNamespace(
            task=TaskPacket(
                "Review exact input",
                "Inspect the change.",
                ("Bind the decision to the trusted hash.",),
                "reviewer",
            ),
            context=SimpleNamespace(
                workspace_path=r"C:\workspace",
                attestation={
                    "input_attestations": [
                        {
                            "path": "docs/specs/review.md",
                            "sha256": digest,
                            "byte_count": 42,
                            "source": "subagent-mcp-read-only-sha256",
                        }
                    ]
                },
            ),
        )
    )

    assert "Trusted input attestations" in prompt
    assert "docs/specs/review.md" in prompt
    assert digest in prompt
    assert "42 bytes" in prompt


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


class _RejectedEnvelopeThenSuccessClient(_UnsafeClient):
    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "rejected-envelope-session",
                "cwd": str(self.options.cwd),
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="rejected-envelope-startup-rate",
            session_id="rejected-envelope-session",
        )
        while not self.query_calls:
            await asyncio.sleep(0)
        yield AssistantMessage(
            content=[TextBlock("completed despite an informational envelope")],
            model="vendor/future-model",
            session_id="rejected-envelope-session",
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected",
                rate_limit_type="seven_day_opus",
                overage_status="rejected",
                overage_disabled_reason="out_of_credits",
                raw={"isUsingOverage": False},
            ),
            uuid="rejected-envelope-terminal-rate",
            session_id="rejected-envelope-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="rejected-envelope-session",
            result="completed despite an informational envelope",
            terminal_reason="completed",
            uuid="rejected-envelope-result",
        )


class _SyntheticRateLimitClient(_UnsafeClient):
    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "synthetic-rate-limit-session",
                "cwd": str(self.options.cwd),
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected",
                rate_limit_type="five_hour",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="synthetic-rate-limit-envelope",
            session_id="synthetic-rate-limit-session",
        )
        while not self.query_calls:
            await asyncio.sleep(0)
        yield AssistantMessage(
            content=[],
            model="<synthetic>",
            error="rate_limit",
            session_id="synthetic-rate-limit-session",
        )


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


class _QuotaDisconnectFailsClient(_UnsafeClient):
    async def disconnect(self) -> None:
        raise RuntimeError("control connection did not close")


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


class _TerminalDecodeFailureClient(_PreflightOrderingClient):
    original_error: Exception = ValueError("managed frame exceeded its ceiling")
    offending_line = "SENSITIVE_PROVIDER_FRAME"

    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "decode-failure-session",
                "cwd": str(self.options.cwd),
            },
        )
        while not self.query_calls:
            await asyncio.sleep(0)
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="decode-failure-rate",
            session_id="decode-failure-session",
        )
        raise CLIJSONDecodeError(self.offending_line, self.original_error)


class _OutputBeforeRateClient(_UnsafeClient):
    unsafe_rate = False

    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "early-output-session",
                "cwd": str(self.options.cwd),
            },
        )
        while not self.query_calls:
            await asyncio.sleep(0)
        yield AssistantMessage(
            content=[TextBlock("early provider result")],
            model="vendor/future-model",
            session_id="early-output-session",
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                overage_status="allowed" if self.unsafe_rate else "rejected",
                raw={"isUsingOverage": self.unsafe_rate},
            ),
            uuid="early-output-rate",
            session_id="early-output-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="early-output-session",
            result="early provider result",
            terminal_reason="completed",
            uuid="early-output-result",
        )


class _ControlledLifecycleClient(_UnsafeClient):
    def __init__(self, options) -> None:
        super().__init__(options)
        self.rate_session_id = "controlled-session"
        self.rate_release = asyncio.Event()
        self.terminal_release = asyncio.Event()
        self.disconnect_started = asyncio.Event()
        self.disconnect_release = asyncio.Event()
        self.interrupt_started = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self.hold_disconnect = False
        self.hold_interrupt = False
        self.complete_during_interrupt = False
        self.disconnect_calls = 0
        self.disconnect_failures = 0

    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "controlled-session",
                "cwd": str(self.options.cwd),
            },
        )
        await self.rate_release.wait()
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="controlled-rate",
            session_id=self.rate_session_id,
        )
        await self.terminal_release.wait()
        if self.interrupt_calls and not self.complete_during_interrupt:
            yield ResultMessage(
                subtype="error_during_execution",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id="controlled-session",
                result="interrupted",
                terminal_reason="aborted_streaming",
                uuid="controlled-interrupted",
            )
            return
        yield AssistantMessage(
            content=[TextBlock("controlled result")],
            model="vendor/future-model",
            session_id="controlled-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="controlled-session",
            result="controlled result",
            terminal_reason="completed",
            uuid="controlled-result",
        )

    async def interrupt(self) -> None:
        await super().interrupt()
        self.terminal_release.set()
        if self.hold_interrupt:
            self.interrupt_started.set()
            await self.interrupt_release.wait()

    async def disconnect(self) -> None:
        if self.hold_disconnect:
            self.disconnect_started.set()
            await self.disconnect_release.wait()
        self.disconnect_calls += 1
        if self.disconnect_failures:
            self.disconnect_failures -= 1
            raise RuntimeError("disconnect failed")
        await super().disconnect()


class _NoRateLifecycleClient(_ControlledLifecycleClient):
    async def receive_messages(self):
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "apiKeySource": "none",
                "session_id": "controlled-session",
                "cwd": str(self.options.cwd),
            },
        )
        await self.terminal_release.wait()
        if self.interrupt_calls:
            yield ResultMessage(
                subtype="error_during_execution",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id="controlled-session",
                result="interrupted without rate evidence",
                terminal_reason="aborted_streaming",
                uuid="controlled-interrupted-without-rate",
            )
            return
        yield AssistantMessage(
            content=[TextBlock("must not be accepted")],
            model="vendor/future-model",
            session_id="controlled-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="controlled-session",
            result="must not be accepted",
            terminal_reason="completed",
            uuid="controlled-result-without-rate",
        )


async def _controlled_context(
    adapter: ClaudeCodeAdapter,
    workspace: Path,
):
    await adapter.probe()
    return await adapter.resolve_context(
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


async def _terminal_adapter_snapshot(
    adapter: ClaudeCodeAdapter,
    request: AdapterSessionRequest,
):
    while True:
        snapshot = await adapter.snapshot(request)
        if snapshot.execution_state != "running":
            return snapshot
        await asyncio.sleep(0)


def test_orphan_cleanup_requires_absence_of_exact_managed_process(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = SimpleNamespace(
        name=cli.name,
        executable_path=str(cli.resolve()),
        command_line="\x00".join(
            (
                str(cli.resolve()),
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--strict-mcp-config",
            )
        ),
        cwd=str(workspace.resolve()),
    )
    unrelated = SimpleNamespace(
        name="notepad.exe",
        executable_path=None,
        command_line=None,
        cwd=None,
    )
    inaccessible_candidate = SimpleNamespace(
        name="node.exe",
        executable_path=None,
        command_line=None,
        cwd=None,
    )

    async def run(processes):
        adapter = ClaudeCodeAdapter(
            cli_path=cli,
            command_runner=_Runner(),
            sdk_version="0.2.142",
            bundled_cli_paths=(),
            process_inventory=lambda: processes,
        )
        context = await _controlled_context(adapter, workspace)
        request = AdapterSessionRequest(
            "conversation-orphan",
            "execution-orphan",
            "session-orphan",
            "external-orphan",
        )
        return await adapter.orphan_cleanup_confirmed(request, context)

    assert asyncio.run(run(())) is True
    assert asyncio.run(run((unrelated,))) is True
    assert asyncio.run(run((process,))) is False
    assert asyncio.run(run((inaccessible_candidate,))) is False


def test_orphan_cleanup_rejects_changed_runtime_binding(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = ClaudeCodeAdapter(
        cli_path=cli,
        command_runner=_Runner(),
        sdk_version="0.2.142",
        bundled_cli_paths=(),
        process_inventory=lambda: (),
    )

    async def run():
        context = await _controlled_context(adapter, workspace)
        cli.write_bytes(b"changed-cli")
        request = AdapterSessionRequest(
            "conversation-orphan",
            "execution-orphan",
            "session-orphan",
            "external-orphan",
        )
        return await adapter.orphan_cleanup_confirmed(request, context)

    assert asyncio.run(run()) is False


def test_spawn_returns_running_after_init_before_rate_and_finishes_in_background(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_ControlledLifecycleClient] = []

    def factory(options):
        client = _ControlledLifecycleClient(options)
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        spawn_task = asyncio.create_task(
            adapter.spawn(
                AdapterSpawnRequest(
                    "conversation-controlled",
                    "execution-controlled",
                    TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                    context,
                )
            )
        )
        while not clients:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            started = await asyncio.wait_for(asyncio.shield(spawn_task), timeout=0.1)
        except (TimeoutError, asyncio.TimeoutError):
            clients[0].rate_release.set()
            clients[0].terminal_release.set()
            await spawn_task
            pytest.fail("Claude spawn waited for rate evidence after native init")
        assert started.execution_state == "running"
        assert started.evidence["rate_evidence_seen"] is False
        assert "is_using_overage" not in started.evidence
        assert "overage_blocked" not in started.evidence
        assert clients[0].disconnected is False
        request = AdapterSessionRequest(
            "conversation-controlled",
            "execution-controlled",
            started.external_session_id,
            started.external_execution_id,
        )
        clients[0].rate_release.set()
        clients[0].terminal_release.set()
        finished = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        return started, finished

    started, finished = asyncio.run(run())

    assert started.external_session_id == "controlled-session"
    assert finished.execution_state == "succeeded"
    assert finished.result_text == "controlled result"
    assert clients[0].disconnected is True


def test_terminal_result_without_rate_evidence_is_rejected_after_running_start(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_NoRateLifecycleClient] = []

    def factory(options):
        client = _NoRateLifecycleClient(options)
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-no-rate",
                "execution-no-rate",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-no-rate",
            "execution-no-rate",
            started.external_session_id,
            started.external_execution_id,
        )
        clients[0].terminal_release.set()
        finished = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        return started, finished

    started, finished = asyncio.run(run())

    assert started.execution_state == "running"
    assert finished.execution_state == "failed"
    assert finished.result_text is None
    assert finished.error is not None
    assert finished.error.code == "CAPABILITY_MISSING"
    assert clients[0].disconnected is True


def test_late_rate_event_must_match_the_initialized_native_session(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_ControlledLifecycleClient] = []

    def factory(options):
        client = _ControlledLifecycleClient(options)
        client.rate_session_id = "different-session"
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-rate-drift",
                "execution-rate-drift",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-rate-drift",
            "execution-rate-drift",
            started.external_session_id,
            started.external_execution_id,
        )
        clients[0].rate_release.set()
        finished = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        return started, finished

    started, finished = asyncio.run(run())

    assert started.execution_state == "running"
    assert finished.execution_state == "failed"
    assert finished.result_text is None
    assert finished.error is not None
    assert finished.error.code == "CONTEXT_DRIFT"
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_interrupt_stops_a_running_background_claude_turn(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_ControlledLifecycleClient] = []

    def factory(options):
        client = _ControlledLifecycleClient(options)
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        spawn_task = asyncio.create_task(
            adapter.spawn(
                AdapterSpawnRequest(
                    "conversation-interrupt",
                    "execution-interrupt",
                    TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                    context,
                )
            )
        )
        while not clients:
            await asyncio.sleep(0)
        clients[0].rate_release.set()
        try:
            started = await asyncio.wait_for(asyncio.shield(spawn_task), timeout=0.1)
        except (TimeoutError, asyncio.TimeoutError):
            clients[0].terminal_release.set()
            await spawn_task
            pytest.fail("Claude spawn waited for the terminal model result")
        request = AdapterSessionRequest(
            "conversation-interrupt",
            "execution-interrupt",
            started.external_session_id,
            started.external_execution_id,
        )
        return await adapter.interrupt(request)

    interrupted = asyncio.run(run())

    assert interrupted.execution_state == "interrupted"
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_interrupt_before_rate_does_not_fabricate_safe_quota_evidence(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_NoRateLifecycleClient] = []

    def factory(options):
        client = _NoRateLifecycleClient(options)
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-interrupt-no-rate",
                "execution-interrupt-no-rate",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        return await adapter.interrupt(
            AdapterSessionRequest(
                "conversation-interrupt-no-rate",
                "execution-interrupt-no-rate",
                started.external_session_id,
                started.external_execution_id,
            )
        )

    interrupted = asyncio.run(run())

    assert interrupted.execution_state == "interrupted"
    assert interrupted.evidence["rate_evidence_seen"] is False
    assert "is_using_overage" not in interrupted.evidence
    assert "overage_blocked" not in interrupted.evidence
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_late_interrupt_preserves_result_while_cleanup_finishes(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_ControlledLifecycleClient] = []

    def factory(options):
        client = _ControlledLifecycleClient(options)
        client.hold_disconnect = True
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        spawn_task = asyncio.create_task(
            adapter.spawn(
                AdapterSpawnRequest(
                    "conversation-late-interrupt",
                    "execution-late-interrupt",
                    TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                    context,
                )
            )
        )
        while not clients:
            await asyncio.sleep(0)
        clients[0].rate_release.set()
        started = await asyncio.wait_for(spawn_task, timeout=0.1)
        request = AdapterSessionRequest(
            "conversation-late-interrupt",
            "execution-late-interrupt",
            started.external_session_id,
            started.external_execution_id,
        )
        clients[0].terminal_release.set()
        await asyncio.wait_for(clients[0].disconnect_started.wait(), timeout=1)
        late_interrupt = asyncio.create_task(adapter.interrupt(request))
        await asyncio.sleep(0)
        assert clients[0].interrupt_calls == 0
        clients[0].disconnect_release.set()
        return await asyncio.wait_for(late_interrupt, timeout=1)

    finished = asyncio.run(run())

    assert finished.execution_state == "succeeded"
    assert finished.result_text == "controlled result"
    assert clients[0].interrupt_calls == 0
    assert clients[0].disconnected is True


def test_result_wins_when_completion_arrives_during_interrupt_call(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_ControlledLifecycleClient] = []

    def factory(options):
        client = _ControlledLifecycleClient(options)
        client.hold_interrupt = True
        client.complete_during_interrupt = True
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        spawn_task = asyncio.create_task(
            adapter.spawn(
                AdapterSpawnRequest(
                    "conversation-mid-interrupt",
                    "execution-mid-interrupt",
                    TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                    context,
                )
            )
        )
        while not clients:
            await asyncio.sleep(0)
        clients[0].rate_release.set()
        started = await asyncio.wait_for(spawn_task, timeout=0.1)
        request = AdapterSessionRequest(
            "conversation-mid-interrupt",
            "execution-mid-interrupt",
            started.external_session_id,
            started.external_execution_id,
        )
        interrupt_task = asyncio.create_task(adapter.interrupt(request))
        await asyncio.wait_for(clients[0].interrupt_started.wait(), timeout=1)
        await asyncio.sleep(0)
        clients[0].interrupt_release.set()
        return await asyncio.wait_for(interrupt_task, timeout=1)

    finished = asyncio.run(run())

    assert finished.execution_state == "succeeded"
    assert finished.result_text == "controlled result"
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_close_retries_ambiguous_terminal_cleanup(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_ControlledLifecycleClient] = []

    def factory(options):
        client = _ControlledLifecycleClient(options)
        client.disconnect_failures = 1
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        spawn_task = asyncio.create_task(
            adapter.spawn(
                AdapterSpawnRequest(
                    "conversation-cleanup-retry",
                    "execution-cleanup-retry",
                    TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                    context,
                )
            )
        )
        while not clients:
            await asyncio.sleep(0)
        clients[0].rate_release.set()
        started = await asyncio.wait_for(spawn_task, timeout=0.1)
        request = AdapterSessionRequest(
            "conversation-cleanup-retry",
            "execution-cleanup-retry",
            started.external_session_id,
            started.external_execution_id,
        )
        clients[0].terminal_release.set()
        ambiguous = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        closed = await adapter.close(request)
        return ambiguous, closed

    ambiguous, closed = asyncio.run(run())

    assert ambiguous.execution_state == "failed"
    assert ambiguous.result_text == "controlled result"
    assert ambiguous.evidence["cleanup_confirmed"] is False
    assert closed.conversation_state == "closed"
    assert closed.result_text == "controlled result"
    assert closed.evidence["cleanup_confirmed"] is True
    assert clients[0].disconnect_calls == 2


def test_output_before_safe_rate_is_buffered_then_preserved(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_OutputBeforeRateClient] = []

    def factory(options):
        client = _OutputBeforeRateClient(options)
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-early-output",
                "execution-early-output",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-early-output",
            "execution-early-output",
            started.external_session_id,
            started.external_execution_id,
        )
        finished = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        return started, finished

    started, finished = asyncio.run(run())

    assert started.execution_state == "running"
    assert finished.execution_state == "succeeded"
    assert finished.result_text == "early provider result"
    assert clients[0].interrupt_calls == 0
    assert clients[0].disconnected is True


def test_output_before_unsafe_rate_is_never_accepted(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_OutputBeforeRateClient] = []

    def factory(options):
        client = _OutputBeforeRateClient(options)
        client.unsafe_rate = True
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-unsafe-early-output",
                "execution-unsafe-early-output",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-unsafe-early-output",
            "execution-unsafe-early-output",
            started.external_session_id,
            started.external_execution_id,
        )
        finished = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        return started, finished

    started, finished = asyncio.run(run())

    assert started.execution_state == "running"
    assert started.evidence["rate_evidence_seen"] is False
    assert finished.execution_state == "failed"
    assert finished.result_text is None
    assert finished.error is not None
    assert finished.error.code == "USAGE_CREDITS_FORBIDDEN"
    assert finished.evidence["rate_evidence_seen"] is True
    assert "is_using_overage" not in finished.evidence
    assert "overage_blocked" not in finished.evidence
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_rejected_rate_envelope_before_success_is_informational(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_RejectedEnvelopeThenSuccessClient] = []

    def factory(options):
        client = _RejectedEnvelopeThenSuccessClient(options)
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-rejected-envelope",
                "execution-rejected-envelope",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-rejected-envelope",
            "execution-rejected-envelope",
            started.external_session_id,
            started.external_execution_id,
        )
        return await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )

    finished = asyncio.run(run())

    assert finished.execution_state == "succeeded"
    assert finished.result_text == "completed despite an informational envelope"
    assert clients[0].interrupt_calls == 0
    assert clients[0].disconnected is True


def test_synthetic_rate_limit_error_beats_model_attestation_in_spawn(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_SyntheticRateLimitClient] = []

    def factory(options):
        client = _SyntheticRateLimitClient(options)
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-synthetic-rate",
                "execution-synthetic-rate",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-synthetic-rate",
            "execution-synthetic-rate",
            started.external_session_id,
            started.external_execution_id,
        )
        return await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )

    finished = asyncio.run(run())

    assert finished.execution_state == "failed"
    assert finished.error is not None and finished.error.code == "QUOTA_PAUSED"
    assert clients[0].disconnected is True


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

    async def run():
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-1",
                "execution-1",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-1",
            "execution-1",
            started.external_session_id,
            started.external_execution_id,
        )
        finished = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        return started, finished

    started, snapshot = asyncio.run(run())

    assert started.execution_state == "running"
    assert snapshot.execution_state == "succeeded"
    assert snapshot.result_text == "provider-authorized result"
    assert clients[0].query_started_before_init is True
    assert clients[0].query_calls == 1
    assert clients[0].options.max_turns is None
    assert clients[0].options.max_buffer_size == 8 * 1024 * 1024
    assert snapshot.evidence["rate_evidence_seen"] is True
    assert snapshot.evidence["is_using_overage"] is False
    assert snapshot.evidence["cleanup_confirmed"] is True


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            ValueError("managed frame exceeded its ceiling"),
            "Claude terminal turn outcome is ambiguous (stdout frame exceeded managed buffer limit)",
        ),
        (
            json.JSONDecodeError("Expecting value", "{", 1),
            "Claude terminal turn outcome is ambiguous (stdout frame was not valid JSON)",
        ),
    ],
)
def test_terminal_decode_failure_is_precise_without_leaking_frame(
    tmp_path: Path,
    cause: Exception,
    expected: str,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clients: list[_TerminalDecodeFailureClient] = []

    def factory(options):
        client = _TerminalDecodeFailureClient(options)
        client.original_error = cause
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

    async def run():
        context = await _controlled_context(adapter, workspace)
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-decode-failure",
                "execution-decode-failure",
                TaskPacket("Review", "Review only.", ("Return.",), "reviewer"),
                context,
            )
        )
        return await asyncio.wait_for(
            _terminal_adapter_snapshot(
                adapter,
                AdapterSessionRequest(
                    "conversation-decode-failure",
                    "execution-decode-failure",
                    started.external_session_id,
                    started.external_execution_id,
                ),
            ),
            timeout=1,
        )

    finished = asyncio.run(run())

    assert finished.execution_state == "failed"
    assert finished.error is not None
    assert finished.error.code == "RECOVERY_REQUIRED"
    assert finished.error.category == "adapter"
    assert finished.error.retryable is False
    assert finished.error.message == expected
    assert _TerminalDecodeFailureClient.offending_line not in finished.error.message
    assert _TerminalDecodeFailureClient.offending_line not in json.dumps(
        finished.evidence
    )
    assert finished.result_text is None
    assert finished.evidence["cleanup_confirmed"] is True
    assert finished.evidence["is_using_overage"] is False
    assert finished.evidence["overage_blocked"] is True


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

    async def run():
        started = await adapter.spawn(
            AdapterSpawnRequest(
                "conversation-slow",
                "execution-slow",
                TaskPacket("Review", "Review only.", ("Return one result.",), "reviewer"),
                context,
            )
        )
        request = AdapterSessionRequest(
            "conversation-slow",
            "execution-slow",
            started.external_session_id,
            started.external_execution_id,
        )
        finished = await asyncio.wait_for(
            _terminal_adapter_snapshot(adapter, request), timeout=1
        )
        return started, finished

    started, snapshot = asyncio.run(run())

    assert started.execution_state == "running"
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
    assert result.error is not None and result.error.code == "USAGE_CREDITS_FORBIDDEN"
    assert clients[0].options.max_buffer_size == 8 * 1024 * 1024
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


def test_quota_probe_disconnect_failure_stays_unknown_without_model_task(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_QuotaDisconnectFailsClient] = []

    def factory(options):
        client = _QuotaDisconnectFailsClient(options)
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
    assert result.error is not None
    assert result.error.code == "CAPABILITY_MISSING"
    assert "no model task was sent" in result.error.message
    assert clients[0].query_calls == 0
    assert clients[0].interrupt_calls == 0


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
    assert result.error.code == "USAGE_CREDITS_FORBIDDEN"
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
    assert result.error is not None and result.error.code == "USAGE_CREDITS_FORBIDDEN"
    assert clients[0].query_calls == 1
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_synthetic_rate_limit_error_beats_model_attestation_in_canary(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_SyntheticRateLimitClient] = []

    def factory(options):
        client = _SyntheticRateLimitClient(options)
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
        "reasoning_source": "claude-code-managed-sdk",
        "reasoning_binding": [
            "ClaudeAgentOptions.effort",
            "CLAUDE_CODE_EFFORT_LEVEL",
        ],
        "reasoning_provider_reported": False,
        "variant_id": "future-deep",
        "permissions": ["repo_read", "workspace_write"],
        "write_set": ["src/context", "docs/status.md"],
        "context_policy_id": "declared-native",
        "permission_policy_id": "default",
    }
    assert "workspace_write" in adapter.manifest.semantic_permissions
    assert "background_lifecycle" not in context.capability_gaps
    assert "live_status" not in context.capability_gaps
    assert "live_status_after_restart" in context.capability_gaps
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
