from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from claude_agent_sdk import RateLimitEvent, RateLimitInfo, SystemMessage

from subagent_harness_mcp.adapters import AdapterContextRequest, CanaryRequest
from subagent_harness_mcp.adapters.claude_code import (
    ClaudeCodeAdapter,
    CommandResult,
)


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
                "session_id": "session-1",
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status=None,
                raw={"isUsingOverage": False},
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
                raw={"isUsingOverage": False},
            ),
            uuid="rate-2",
            session_id="session-1",
        )

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


class _SafeQuotaClient(_UnsafeClient):
    async def receive_messages(self):
        while not self.query_calls:
            await asyncio.sleep(0)
        yield SystemMessage(
            subtype="init",
            data={
                "model": "vendor/future-model",
                "effort": "xhigh",
                "mcp_servers": [],
                "session_id": "quota-session-1",
            },
        )
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                overage_status="rejected",
                raw={"isUsingOverage": False},
            ),
            uuid="quota-rate-1",
            session_id="quota-session-1",
        )


def test_quota_probe_accepts_safe_warning_before_model_output(tmp_path: Path) -> None:
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

    assert result.passed is True
    assert result.details == {
        "model": "vendor/future-model",
        "effort": "xhigh",
        "is_using_overage": False,
        "overage_blocked": True,
        "cleanup_confirmed": True,
    }
    assert clients[0].query_calls == 1
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_quota_probe_fails_closed_on_warning_rate(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_UnsafeClient] = []

    def factory(options):
        client = _UnsafeClient(options)
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
    assert result.error is not None and result.error.code == "QUOTA_PAUSED"
    assert clients[0].interrupt_calls == 1
    assert clients[0].disconnected is True


def test_canary_stops_before_model_output_on_any_non_allowed_primary_rate(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"standalone-cli")
    clients: list[_UnsafeClient] = []

    def factory(options):
        client = _UnsafeClient(options)
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
        "context_policy_id": "declared-native",
        "permission_policy_id": "default",
    }
    assert "workspace_write" in adapter.manifest.semantic_permissions
    assert "background_lifecycle" in context.capability_gaps
    assert "declared_mcp" in context.capability_gaps
    assert "project_local_context_and_hooks" in context.capability_gaps
