"""Capability-gated managed Claude Code adapter.

The Windows preview deliberately implements only terminal synchronous managed
turns. Background status, needs-input, and promotion remain explicit gaps.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, get_args

from ..contracts import ADAPTER_API_VERSION, AdapterManifest, ServiceError, validate_model_id
from .base import (
    AdapterContextRequest,
    AdapterFailure,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSnapshot,
    AdapterSpawnRequest,
    CanaryRequest,
    CanaryResult,
    ProbeResult,
    ResolvedContext,
)


SDK_VERSION = "0.2.142"
RECURSION_DENIES = (
    "mcp__codex__*",
    "mcp__agent_bridge__*",
    "mcp__subagent_harness_mcp__*",
)
CREDENTIAL_OVERRIDE_NAMES = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_BASE_URL",
)
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROVIDER_SAFETY_ENV = {
    "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
    "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
    "DISABLE_EXTRA_USAGE_COMMAND": "1",
}
QUOTA_EVIDENCE_GRACE_SECONDS = 0.5
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 180.0
DURABLE_RESULT_MAX_CHARS = 65_536
CONTROLLER_RESULT_MAX_CHARS = DURABLE_RESULT_MAX_CHARS
_CONTROLLER_TRUNCATION_MARKER = "\n[truncated by Subagent MCP]"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, argv: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        ...


@dataclass(frozen=True, slots=True)
class _BoundRuntime:
    cli_path: Path
    cli_version: str
    cli_sha256: str
    cli_file_id: str
    sdk_version: str
    pair_key: str

    def details(self) -> dict[str, str]:
        return {
            "pair_key": self.pair_key,
            "adapter_version": "0.1.0a20",
            "sdk_version": self.sdk_version,
            "cli_path": str(self.cli_path),
            "cli_version": self.cli_version,
            "cli_sha256": self.cli_sha256,
            "cli_file_id": self.cli_file_id,
            "transport": "managed-sdk-default",
            "auth_method": "claude.ai",
        }


@dataclass(slots=True)
class _ManagedSession:
    context: ResolvedContext
    snapshot: AdapterSnapshot
    closed: bool = False


class ClaudeCodeAdapter:
    def __init__(
        self,
        *,
        cli_path: Path | None = None,
        command_runner: CommandRunner | None = None,
        client_factory: Callable[[Any], Any] | None = None,
        sdk_version: str | None = None,
        bundled_cli_paths: Sequence[Path] | None = None,
        canary_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self._cli_path = cli_path
        self._command_runner = command_runner or _run_command
        self._client_factory = client_factory or _default_client_factory
        self._sdk_version = sdk_version
        self._bundled_cli_paths = bundled_cli_paths
        self._canary_timeout = canary_timeout_seconds
        self._last_probe_pair: str | None = None
        self._sessions: dict[str, _ManagedSession] = {}
        self._active_clients: dict[str, tuple[Any, ResolvedContext, str]] = {}
        self._manifest = AdapterManifest(
            adapter_api_version=ADAPTER_API_VERSION,
            runtime_id="claude-code",
            provider_id="anthropic",
            harness_id="claude-code",
            display_name="Claude sub-agent",
            adapter_version="0.1.0a20",
            supported_platforms=("win32",),
            supported_transports=("managed-sdk",),
            capabilities=frozenset({"canary", "session", "resume", "workspace"}),
            semantic_permissions=frozenset({"repo_read", "workspace_write"}),
            reasoning_schema={
                "type": "object",
                "required": ["effort"],
                "additionalProperties": False,
                "properties": {"effort": {"enum": list(CLAUDE_EFFORTS)}},
            },
            model_schema={
                "anyOf": [
                    {"const": "claude-opus-5", "title": "Opus 5"},
                    {"const": "claude-sonnet-5", "title": "Sonnet 5"},
                    {"const": "claude-fable-5", "title": "Fable 5"},
                    {
                        "type": "string",
                        "minLength": 1,
                        "title": "Custom exact model ID",
                    },
                ]
            },
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def probe(self) -> ProbeResult:
        state, bound, details = self._bind_no_model()
        if bound is not None:
            self._last_probe_pair = bound.pair_key
            details = bound.details()
        else:
            self._last_probe_pair = None
        return ProbeResult(state, details)

    async def runtime_canary(self, request: CanaryRequest) -> CanaryResult:
        state, bound, details = self._bind_no_model()
        if state != "needs_canary" or bound is None:
            return _failure(
                request.pair_key,
                _probe_error(state),
                details,
            )
        if bound.pair_key != request.base_pair_key:
            return _failure(
                request.pair_key,
                AdapterFailure(
                    "CONTEXT_DRIFT",
                    "adapter",
                    False,
                    "Claude adapter pair changed before canary",
                ),
                {},
            )
        if _variant_pair_key(
            request.base_pair_key,
            request.model,
            request.reasoning,
            request.transport,
        ) != request.pair_key:
            return _failure(
                request.pair_key,
                AdapterFailure(
                    "CONTEXT_DRIFT",
                    "adapter",
                    False,
                    "Claude composite variant identity changed before canary",
                ),
                {},
            )
        try:
            options, effort, context_hash = _build_canary_options(request, bound)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return _failure(
                request.pair_key,
                AdapterFailure("CAPABILITY_MISSING", "adapter", False, str(exc)),
                {},
            )
        client = self._client_factory(options)
        result: CanaryResult
        try:
            result = await self._run_guarded_canary(
                client,
                request=request,
                effort=effort,
                context_hash=context_hash,
            )
        except (TimeoutError, asyncio.TimeoutError):
            result = _failure(
                request.pair_key,
                AdapterFailure(
                    "CAPABILITY_MISSING", "adapter", False, "Claude canary timed out"
                ),
                {},
            )
        except BaseException as exc:
            result = _failure(
                request.pair_key,
                AdapterFailure(
                    "CAPABILITY_MISSING",
                    "adapter",
                    False,
                    f"Claude canary failed ({type(exc).__name__})",
                ),
                {},
            )
        try:
            await asyncio.wait_for(client.disconnect(), timeout=self._canary_timeout)
        except BaseException:
            return _failure(
                request.pair_key,
                AdapterFailure(
                    "RECOVERY_REQUIRED",
                    "adapter",
                    False,
                    "Claude canary cleanup was not confirmed",
                ),
                {},
            )
        if not result.passed:
            return result
        safe = dict(result.details)
        safe["cleanup_confirmed"] = True
        return CanaryResult(True, request.pair_key, safe)

    async def quota_probe(self, request: CanaryRequest) -> CanaryResult:
        state, bound, details = self._bind_no_model()
        if state != "needs_canary" or bound is None:
            return _failure(request.pair_key, _probe_error(state), details)
        if bound.pair_key != request.base_pair_key or _variant_pair_key(
            request.base_pair_key,
            request.model,
            request.reasoning,
            request.transport,
        ) != request.pair_key:
            return _failure(
                request.pair_key,
                AdapterFailure(
                    "CONTEXT_DRIFT",
                    "adapter",
                    False,
                    "Claude quota probe identity changed",
                ),
                {},
            )
        try:
            options, effort, _ = _build_canary_options(request, bound)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return _failure(
                request.pair_key,
                AdapterFailure("CAPABILITY_MISSING", "adapter", False, str(exc)),
                {},
            )
        client = self._client_factory(options)
        try:
            result = await self._run_quota_probe(
                client,
                request=request,
                effort=effort,
            )
        except (TimeoutError, asyncio.TimeoutError):
            result = _failure(
                request.pair_key,
                AdapterFailure("CAPABILITY_MISSING", "adapter", False, "Claude quota probe timed out"),
                {},
            )
        except BaseException as exc:
            result = _failure(
                request.pair_key,
                AdapterFailure(
                    "CAPABILITY_MISSING",
                    "adapter",
                    False,
                    f"Claude quota probe failed ({type(exc).__name__})",
                ),
                {},
            )
        try:
            await asyncio.wait_for(client.disconnect(), timeout=self._canary_timeout)
        except BaseException:
            return _failure(
                request.pair_key,
                AdapterFailure(
                    "RECOVERY_REQUIRED",
                    "adapter",
                    False,
                    "Claude quota probe cleanup was not confirmed",
                ),
                {},
            )
        if not result.passed:
            return result
        safe = dict(result.details)
        safe["cleanup_confirmed"] = True
        return CanaryResult(True, request.pair_key, safe)

    async def _run_quota_probe(
        self,
        client: Any,
        *,
        request: CanaryRequest,
        effort: str,
    ) -> CanaryResult:
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            ResultMessage,
            SystemMessage,
        )

        await asyncio.wait_for(client.connect(None), timeout=self._canary_timeout)
        messages = client.receive_messages().__aiter__()
        while True:
            message = await asyncio.wait_for(
                messages.__anext__(),
                timeout=min(self._canary_timeout, QUOTA_EVIDENCE_GRACE_SECONDS),
            )
            if isinstance(message, (AssistantMessage, ResultMessage)):
                return await self._interrupt_for_guard_failure(
                    client,
                    request.pair_key,
                    AdapterFailure(
                        "USAGE_CREDITS_FORBIDDEN",
                        "quota",
                        False,
                        "Claude produced model output before quota evidence",
                    ),
                )
            if isinstance(message, SystemMessage) and message.subtype == "init":
                data = message.data
                if (
                    data.get("model") != request.model
                    or data.get("mcp_servers") != []
                    or not _subscription_oauth_source(data)
                    or not isinstance(data.get("session_id"), str)
                    or not data["session_id"]
                ):
                    return _failure(
                        request.pair_key,
                        AdapterFailure(
                            "CONTEXT_DRIFT",
                            "adapter",
                            False,
                            "Claude quota probe init identity did not match",
                        ),
                        {},
                    )
            elif isinstance(message, RateLimitEvent):
                unsafe = _unsafe_rate(message.rate_limit_info)
                if unsafe is not None:
                    return _failure(request.pair_key, unsafe, {})
                return CanaryResult(
                    True,
                    request.pair_key,
                    {
                        "model": request.model,
                        "effort": effort,
                        "rate_evidence_seen": True,
                        "is_using_overage": False,
                        "overage_blocked": True,
                    },
                )

    async def _run_guarded_canary(
        self,
        client: Any,
        *,
        request: CanaryRequest,
        effort: str,
        context_hash: str,
    ) -> CanaryResult:
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            ResultMessage,
            SystemMessage,
        )

        await asyncio.wait_for(client.connect(None), timeout=self._canary_timeout)
        messages = client.receive_messages().__aiter__()
        await asyncio.wait_for(
            client.query("Return a short deterministic readiness acknowledgement."),
            timeout=self._canary_timeout,
        )
        init_session: str | None = None
        init_seen = False
        rate_seen = False
        while not (init_seen and rate_seen):
            message = await asyncio.wait_for(
                messages.__anext__(), timeout=self._canary_timeout
            )
            if isinstance(message, AssistantMessage) or isinstance(message, ResultMessage):
                return await self._interrupt_for_guard_failure(
                    client,
                    request.pair_key,
                    AdapterFailure(
                        "USAGE_CREDITS_FORBIDDEN",
                        "quota",
                        False,
                        "Claude produced model output before the no-overage guard",
                    ),
                )
            if isinstance(message, SystemMessage) and message.subtype == "init":
                data = message.data
                if (
                    data.get("model") != request.model
                    or data.get("mcp_servers") != []
                    or not _subscription_oauth_source(data)
                    or not isinstance(data.get("session_id"), str)
                    or not data["session_id"]
                ):
                    return await self._interrupt_for_guard_failure(
                        client,
                        request.pair_key,
                        AdapterFailure(
                            "CONTEXT_DRIFT",
                            "adapter",
                            False,
                            "Claude init attestation did not match the canary",
                        ),
                    )
                init_session = data["session_id"]
                init_seen = True
            elif isinstance(message, RateLimitEvent):
                unsafe = _unsafe_rate(message.rate_limit_info)
                if unsafe is not None:
                    return await self._interrupt_for_guard_failure(
                        client, request.pair_key, unsafe
                    )
                rate_seen = True

        assistant_seen = False
        while True:
            message = await asyncio.wait_for(
                messages.__anext__(), timeout=self._canary_timeout
            )
            if isinstance(message, RateLimitEvent):
                unsafe = _unsafe_rate(message.rate_limit_info)
                if unsafe is not None:
                    return await self._interrupt_for_guard_failure(
                        client,
                        request.pair_key,
                        unsafe,
                    )
                rate_seen = True
            elif isinstance(message, AssistantMessage):
                if message.model != request.model or (
                    message.session_id is not None
                    and message.session_id != init_session
                ):
                    return _failure(
                        request.pair_key,
                        AdapterFailure(
                            "CONTEXT_DRIFT",
                            "adapter",
                            False,
                            "Claude model or session changed during canary",
                        ),
                        {},
                    )
                if message.error is not None:
                    return _failure(
                        request.pair_key,
                        _assistant_error(message.error),
                        {},
                    )
                assistant_seen = True
                # Content is intentionally never inspected; ThinkingBlock and raw
                # provider output cannot enter the service or durable state.
            elif isinstance(message, ResultMessage):
                if (
                    message.is_error is not False
                    or message.session_id != init_session
                    or not assistant_seen
                    or message.terminal_reason in {"aborted_streaming", "aborted_tools"}
                ):
                    return _failure(
                        request.pair_key,
                        _result_error(message),
                        {},
                    )
                return CanaryResult(
                    True,
                    request.pair_key,
                    {
                        "model": request.model,
                        "effort": effort,
                        "session_id": init_session,
                        "context_hash": context_hash,
                        "rate_evidence_seen": rate_seen,
                        "is_using_overage": False,
                        "overage_blocked": True,
                    },
                )

    async def _interrupt_for_guard_failure(
        self,
        client: Any,
        pair_key: str,
        unsafe: AdapterFailure,
    ) -> CanaryResult:
        try:
            await asyncio.wait_for(client.interrupt(), timeout=self._canary_timeout)
        except BaseException:
            return _failure(
                pair_key,
                AdapterFailure(
                    "RECOVERY_REQUIRED",
                    "adapter",
                    False,
                    "Claude interrupt was not confirmed after an unsafe quota event",
                ),
                {},
            )
        return _failure(pair_key, unsafe, {})

    async def resolve_context(self, request: AdapterContextRequest) -> ResolvedContext:
        if request.runtime_id != "claude-code" or request.transport != "managed-sdk":
            raise ServiceError("CAPABILITY_MISSING", "Claude managed-sdk context is required")
        unsupported = set(request.permissions) - self._manifest.semantic_permissions
        if unsupported:
            raise ServiceError("CAPABILITY_MISSING", "Claude permission is unsupported")
        state, bound, _ = self._bind_no_model()
        if state != "needs_canary" or bound is None:
            raise _service_probe_error(state)
        if self._last_probe_pair != bound.pair_key:
            raise ServiceError("CONTEXT_DRIFT", "Claude runtime identity changed after readiness check")
        effort = _validate_reasoning(request.model, request.reasoning)
        attestation = {
            "source": "claude-code-managed-sdk",
            "variant_id": request.variant_id,
            "permissions": list(request.permissions),
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
        }
        context_hash = _lifecycle_context_hash(
            bound,
            runtime_id=request.runtime_id,
            variant_id=request.variant_id,
            model=request.model,
            reasoning={"effort": effort},
            workspace_path=request.workspace_path,
            workspace_key=request.workspace_key,
            transport=request.transport,
            permissions=request.permissions,
            context_policy_id=request.context_policy_id,
            permission_policy_id=request.permission_policy_id,
        )
        return ResolvedContext(
            runtime_id=request.runtime_id,
            requested_model=request.model,
            effective_model=request.model,
            requested_reasoning=dict(request.reasoning),
            effective_reasoning={"effort": effort},
            workspace_path=request.workspace_path,
            workspace_key=request.workspace_key,
            transport=request.transport,
            context_hash=context_hash,
            capability_gaps=(
                "background_lifecycle",
                "live_status",
                "needs_input",
                "declared_mcp",
                "project_local_context_and_hooks",
            ),
            attestation=attestation,
        )

    async def spawn(self, request: AdapterSpawnRequest) -> AdapterSnapshot:
        snapshot = await self._run_terminal_turn(
            context=request.context,
            prompt=_spawn_prompt(request),
            external_execution_id=request.execution_id,
            expected_session_id=None,
        )
        self._sessions[snapshot.external_session_id] = _ManagedSession(
            request.context,
            snapshot,
        )
        return snapshot

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        existing = self._sessions.get(request.external_session_id)
        if existing is not None and existing.context.context_hash != request.context.context_hash:
            raise ServiceError("CONTEXT_DRIFT", "Claude resumed context changed")
        snapshot = await self._run_terminal_turn(
            context=request.context,
            prompt=_send_prompt(request),
            external_execution_id=request.execution_id,
            expected_session_id=request.external_session_id,
        )
        self._sessions[request.external_session_id] = _ManagedSession(
            request.context,
            snapshot,
        )
        return snapshot

    async def snapshot(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._sessions.get(request.external_session_id)
        if session is None:
            raise ServiceError(
                "CAPABILITY_MISSING",
                "fresh-process live status is unavailable for terminal managed turns",
            )
        _require_external_execution(request, session.snapshot)
        return session.snapshot

    async def interrupt(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        active = self._active_clients.get(request.external_session_id)
        if active is None:
            raise ServiceError("CAPABILITY_MISSING", "Claude turn is not active")
        client, context, execution_id = active
        try:
            await asyncio.wait_for(client.interrupt(), timeout=self._canary_timeout)
        except BaseException as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Claude interrupt was not confirmed",
                category="adapter",
            ) from exc
        return _terminal_snapshot(
            context,
            session_id=request.external_session_id,
            execution_id=execution_id,
            execution_state="interrupted",
            error=AdapterFailure("INTERRUPTED", "cancelled", False, "Claude turn interrupted"),
        )

    async def close(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        active = self._active_clients.get(request.external_session_id)
        if active is not None:
            raise ServiceError("SESSION_BUSY", "active Claude turn cannot be closed")
        session = self._sessions.get(request.external_session_id)
        if session is None:
            return _placeholder_snapshot(request, conversation_state="closed")
        _require_external_execution(request, session.snapshot)
        current = session.snapshot
        session.closed = True
        session.snapshot = AdapterSnapshot(
            external_session_id=current.external_session_id,
            external_execution_id=current.external_execution_id,
            conversation_state="closed",
            execution_state=current.execution_state,
            effective_model=current.effective_model,
            effective_reasoning=current.effective_reasoning,
            workspace_path=current.workspace_path,
            workspace_key=current.workspace_key,
            context_hash=current.context_hash,
            result_text=current.result_text,
            needs_input=current.needs_input,
            error=current.error,
            evidence={"source": "claude-code-managed-sdk", "native_session_retained": True},
        )
        return session.snapshot

    async def open_session(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._sessions.get(request.external_session_id)
        if session is None:
            return _placeholder_snapshot(request, conversation_state="idle")
        _require_external_execution(request, session.snapshot)
        if session.closed:
            raise ServiceError("SESSION_CLOSED", "Claude session is closed")
        return session.snapshot

    async def _run_terminal_turn(
        self,
        *,
        context: ResolvedContext,
        prompt: str,
        external_execution_id: str,
        expected_session_id: str | None,
    ) -> AdapterSnapshot:
        state, bound, _ = self._bind_no_model()
        if state != "needs_canary" or bound is None:
            raise _service_probe_error(state)
        if self._last_probe_pair != bound.pair_key:
            raise ServiceError("CONTEXT_DRIFT", "Claude runtime identity changed before launch")
        options = _build_lifecycle_options(context, bound, resume=expected_session_id)
        client = self._client_factory(options)
        session_id: str | None = None
        failure: BaseException | None = None
        snapshot: AdapterSnapshot | None = None
        try:
            await asyncio.wait_for(client.connect(None), timeout=self._canary_timeout)
            messages = client.receive_messages().__aiter__()
            await asyncio.wait_for(client.query(prompt), timeout=self._canary_timeout)
            session_id = await self._await_lifecycle_guard(
                client,
                messages,
                context=context,
                expected_session_id=expected_session_id,
            )
            self._active_clients[session_id] = (client, context, external_execution_id)
            snapshot = await self._receive_terminal_result(
                client,
                messages,
                context=context,
                session_id=session_id,
                external_execution_id=external_execution_id,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            failure = ServiceError("RECOVERY_REQUIRED", "Claude terminal turn timed out", category="adapter")
            failure.__cause__ = exc
        except BaseException as exc:
            failure = exc
        finally:
            if session_id is not None:
                self._active_clients.pop(session_id, None)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=self._canary_timeout)
            except BaseException as exc:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Claude terminal turn cleanup was not confirmed",
                    category="adapter",
                ) from exc
        if failure is not None:
            if isinstance(failure, ServiceError):
                raise failure
            raise ServiceError(
                "RECOVERY_REQUIRED",
                f"Claude terminal turn outcome is ambiguous ({type(failure).__name__})",
                category="adapter",
            ) from failure
        assert snapshot is not None
        return snapshot

    async def _await_lifecycle_guard(
        self,
        client: Any,
        messages: Any,
        *,
        context: ResolvedContext,
        expected_session_id: str | None,
    ) -> str:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage

        session_id: str | None = None
        rate_seen = False
        while session_id is None or not rate_seen:
            message = await asyncio.wait_for(messages.__anext__(), timeout=self._canary_timeout)
            if isinstance(message, (AssistantMessage, ResultMessage)):
                failure = (
                    AdapterFailure(
                        "USAGE_CREDITS_FORBIDDEN",
                        "quota",
                        False,
                        "Claude produced model output before safe rate evidence",
                    )
                    if session_id is not None
                    else AdapterFailure(
                        "CONTEXT_DRIFT",
                        "adapter",
                        False,
                        "Claude produced model output before init identity",
                    )
                )
                await _interrupt_or_recovery(
                    client,
                    failure,
                    self._canary_timeout,
                )
            if isinstance(message, SystemMessage) and message.subtype == "init":
                data = message.data
                candidate = data.get("session_id")
                if (
                    data.get("model") != context.effective_model
                    or data.get("mcp_servers") != []
                    or data.get("cwd") != context.workspace_path
                    or not _subscription_oauth_source(data)
                    or not isinstance(candidate, str)
                    or not candidate
                    or (expected_session_id is not None and candidate != expected_session_id)
                ):
                    await _interrupt_or_recovery(
                        client,
                        AdapterFailure(
                            "CONTEXT_DRIFT",
                            "adapter",
                            False,
                            "Claude init identity did not match the resolved context",
                        ),
                        self._canary_timeout,
                    )
                session_id = candidate
            elif isinstance(message, RateLimitEvent):
                unsafe = _unsafe_rate(message.rate_limit_info)
                if unsafe is not None:
                    await _interrupt_or_recovery(
                        client, unsafe, self._canary_timeout
                    )
                rate_seen = True
        return session_id

    async def _receive_terminal_result(
        self,
        client: Any,
        messages: Any,
        *,
        context: ResolvedContext,
        session_id: str,
        external_execution_id: str,
    ) -> AdapterSnapshot:
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            ResultMessage,
            TextBlock,
        )

        assistant_seen = False
        text_parts: list[str] = []
        captured_chars = 0
        while True:
            message = await asyncio.wait_for(messages.__anext__(), timeout=self._canary_timeout)
            if isinstance(message, RateLimitEvent):
                unsafe = _unsafe_rate(message.rate_limit_info)
                if unsafe is not None:
                    await _interrupt_or_recovery(client, unsafe, self._canary_timeout)
                continue
            if isinstance(message, AssistantMessage):
                if message.model != context.effective_model or (
                    message.session_id is not None
                    and message.session_id != session_id
                ):
                    await _interrupt_or_recovery(
                        client,
                        AdapterFailure("CONTEXT_DRIFT", "adapter", False, "Claude model or session changed"),
                        self._canary_timeout,
                    )
                if message.error is not None:
                    raise _service_failure(_assistant_error(message.error))
                assistant_seen = True
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        remaining = CONTROLLER_RESULT_MAX_CHARS + 1 - captured_chars
                        if remaining > 0:
                            part = block.text[:remaining]
                            text_parts.append(part)
                            captured_chars += len(part)
            elif isinstance(message, ResultMessage):
                if message.session_id != session_id:
                    raise ServiceError("CONTEXT_DRIFT", "Claude terminal session changed")
                if (
                    message.is_error is not False
                    or not assistant_seen
                    or message.terminal_reason in {"aborted_streaming", "aborted_tools"}
                ):
                    raise _service_failure(_result_error(message))
                result_text = _bounded_controller_result(text_parts)
                return _terminal_snapshot(
                    context,
                    session_id=session_id,
                    execution_id=f"claude-turn-{external_execution_id}",
                    execution_state="succeeded",
                    result_text=result_text,
                )

    def _bind_no_model(self) -> tuple[str, _BoundRuntime | None, Mapping[str, Any]]:
        if any(name in os.environ for name in CREDENTIAL_OVERRIDE_NAMES):
            return "incompatible", None, {"code": "CREDENTIAL_OVERRIDE"}
        path = self._discover_cli()
        if path is None:
            return "not_installed", None, {"code": "INSTALL_REQUIRED"}
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise OSError("not a file")
        except OSError:
            return "not_installed", None, {"code": "INSTALL_REQUIRED"}
        bundled = (
            tuple(self._bundled_cli_paths)
            if self._bundled_cli_paths is not None
            else _sdk_bundled_cli_paths()
        )
        if any(_same_path(resolved, item) for item in bundled):
            return "incompatible", None, {"code": "BUNDLED_CLI_REJECTED"}
        sdk_version = self._sdk_version or _installed_sdk_version()
        if sdk_version != SDK_VERSION:
            return "incompatible", None, {"code": "SDK_VERSION_MISMATCH"}
        version = self._command_runner((str(resolved), "--version"), 10.0)
        if version.returncode != 0 or not version.stdout.strip():
            return "incompatible", None, {"code": "CLI_VERSION_FAILED"}
        auth = self._command_runner(
            (str(resolved), "auth", "status", "--json"), 10.0
        )
        if auth.returncode != 0:
            return "auth_required", None, {"code": "AUTH_REQUIRED"}
        try:
            auth_value = json.loads(auth.stdout)
        except (TypeError, json.JSONDecodeError):
            return "incompatible", None, {"code": "AUTH_STATUS_INVALID"}
        if not isinstance(auth_value, dict) or auth_value.get("loggedIn") is not True:
            return "auth_required", None, {"code": "AUTH_REQUIRED"}
        if auth_value.get("authMethod") != "claude.ai":
            return "incompatible", None, {"code": "AUTH_SOURCE_INVALID"}
        stat = resolved.stat()
        sha256 = _sha256_file(resolved)
        file_id = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        pair_payload = {
            "adapter_version": "0.1.0a20",
            "sdk_version": sdk_version,
            "cli_path": os.path.normcase(str(resolved)),
            "cli_version": version.stdout.strip()[:256],
            "cli_sha256": sha256,
            "cli_file_id": file_id,
            "transport": "managed-sdk-default",
        }
        pair_key = hashlib.sha256(
            json.dumps(pair_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return (
            "needs_canary",
            _BoundRuntime(
                resolved,
                pair_payload["cli_version"],
                sha256,
                file_id,
                sdk_version,
                pair_key,
            ),
            {},
        )

    def _discover_cli(self) -> Path | None:
        if self._cli_path is not None:
            return Path(self._cli_path)
        found = shutil.which("claude")
        if found:
            return Path(found)
        if os.name == "nt" and os.environ.get("USERPROFILE"):
            candidate = Path(os.environ["USERPROFILE"]) / ".local" / "bin" / "claude.exe"
            if candidate.is_file():
                return candidate
        return None

def _build_canary_options(request: CanaryRequest, bound: _BoundRuntime):
    from claude_agent_sdk import ClaudeAgentOptions, EffortLevel

    validate_model_id(request.model)
    if set(request.reasoning) != {"effort"}:
        raise ValueError("Claude reasoning must contain only effort")
    effort = request.reasoning["effort"]
    if get_args(EffortLevel) != CLAUDE_EFFORTS or effort not in CLAUDE_EFFORTS:
        raise ValueError("Claude effort schema differs from the reviewed SDK pair")
    if request.transport != "managed-sdk":
        raise ValueError("Claude managed canary transport is required")
    context = {
        "model": request.model,
        "effort": effort,
        "strict_mcp_config": True,
        "mcp_servers": {},
        "system_prompt": "claude_code",
        "setting_sources": [],
        "recursion_denies": list(RECURSION_DENIES),
        "base_pair_key": bound.pair_key,
        "pair_key": request.pair_key,
    }
    context_hash = hashlib.sha256(
        json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    options = ClaudeAgentOptions(
        cli_path=bound.cli_path,
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=[],
        strict_mcp_config=True,
        mcp_servers={},
        tools=[],
        disallowed_tools=list(RECURSION_DENIES),
        permission_mode="dontAsk",
        model=request.model,
        effort=effort,
        thinking={"type": "adaptive", "display": "omitted"},
        fallback_model=None,
        max_turns=1,
        include_partial_messages=False,
        forward_subagent_text=False,
        env={**PROVIDER_SAFETY_ENV, "CLAUDE_CODE_EFFORT_LEVEL": str(effort)},
        extra_args={"prompt-suggestions": "false"},
    )
    return options, str(effort), context_hash


def _validate_reasoning(model: str, reasoning: Mapping[str, Any]) -> str:
    from claude_agent_sdk import EffortLevel

    validate_model_id(model)
    if set(reasoning) != {"effort"}:
        raise ServiceError("POLICY_REJECTED", "Claude reasoning must contain only effort")
    effort = reasoning.get("effort")
    if get_args(EffortLevel) != CLAUDE_EFFORTS or effort not in CLAUDE_EFFORTS:
        raise ServiceError("POLICY_REJECTED", "Claude effort schema differs from the pinned SDK")
    return str(effort)


def _lifecycle_context_hash(
    bound: _BoundRuntime,
    *,
    runtime_id: str,
    variant_id: str,
    model: str,
    reasoning: Mapping[str, Any],
    workspace_path: str,
    workspace_key: str,
    transport: str,
    permissions: Sequence[str],
    context_policy_id: str,
    permission_policy_id: str,
) -> str:
    payload = {
        "profile": "claude-managed-terminal-v1",
        "base_pair_key": bound.pair_key,
        "runtime_id": runtime_id,
        "variant_id": variant_id,
        "model": model,
        "reasoning": dict(reasoning),
        "workspace_path": workspace_path,
        "workspace_key": workspace_key,
        "transport": transport,
        "permissions": list(permissions),
        "context_policy_id": context_policy_id,
        "permission_policy_id": permission_policy_id,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ServiceError("POLICY_REJECTED", "Claude context is not canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _build_lifecycle_options(
    context: ResolvedContext,
    bound: _BoundRuntime,
    *,
    resume: str | None,
):
    from claude_agent_sdk import ClaudeAgentOptions

    if context.runtime_id != "claude-code" or context.transport != "managed-sdk":
        raise ServiceError("CONTEXT_DRIFT", "Claude managed context changed")
    effort = _validate_reasoning(context.effective_model, context.effective_reasoning)
    if (
        context.requested_model != context.effective_model
        or dict(context.requested_reasoning) != dict(context.effective_reasoning)
    ):
        raise ServiceError("CONTEXT_DRIFT", "Claude requested/effective model policy changed")
    attestation = context.attestation
    variant_id = attestation.get("variant_id")
    permissions = attestation.get("permissions")
    context_policy_id = attestation.get("context_policy_id")
    permission_policy_id = attestation.get("permission_policy_id")
    if (
        attestation.get("source") != "claude-code-managed-sdk"
        or not isinstance(variant_id, str)
        or not isinstance(permissions, list)
        or not all(isinstance(item, str) for item in permissions)
        or not isinstance(context_policy_id, str)
        or not isinstance(permission_policy_id, str)
    ):
        raise ServiceError("CONTEXT_DRIFT", "Claude resume policy attestation is missing")
    unsupported = set(permissions) - {"repo_read", "workspace_write"}
    if unsupported or "repo_read" not in permissions:
        raise ServiceError("CAPABILITY_MISSING", "Claude preview requires repo_read only or repo_read+workspace_write")
    expected_hash = _lifecycle_context_hash(
        bound,
        runtime_id=context.runtime_id,
        variant_id=variant_id,
        model=context.effective_model,
        reasoning=context.effective_reasoning,
        workspace_path=context.workspace_path,
        workspace_key=context.workspace_key,
        transport=context.transport,
        permissions=tuple(permissions),
        context_policy_id=context_policy_id,
        permission_policy_id=permission_policy_id,
    )
    if expected_hash != context.context_hash:
        raise ServiceError("CONTEXT_DRIFT", "Claude runtime/model/workspace context changed")
    tools = ["Read", "Glob", "Grep"]
    if "workspace_write" in permissions:
        tools.extend(["Edit", "Write"])
    return ClaudeAgentOptions(
        cli_path=bound.cli_path,
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user"],
        skills="all",
        strict_mcp_config=True,
        mcp_servers={},
        tools=tools,
        allowed_tools=list(tools),
        disallowed_tools=list(RECURSION_DENIES),
        permission_mode="dontAsk",
        cwd=Path(context.workspace_path),
        model=context.effective_model,
        effort=effort,
        thinking={"type": "adaptive", "display": "omitted"},
        fallback_model=None,
        max_turns=32,
        include_partial_messages=False,
        forward_subagent_text=False,
        resume=resume,
        fork_session=False,
        env={**PROVIDER_SAFETY_ENV, "CLAUDE_CODE_EFFORT_LEVEL": effort},
        extra_args={"prompt-suggestions": "false"},
    )


def _spawn_prompt(request: AdapterSpawnRequest) -> str:
    task = request.task
    lines = [
        f"Role: {task.role}",
        f"Task: {task.title}",
        task.prompt,
        "Return only the final result. Begin with one concise CAPSULE: line, then put complete non-redundant detail under DETAILS:; omit progress narration and hidden reasoning.",
        "Acceptance criteria:",
        *(f"- {item}" for item in task.acceptance_criteria),
    ]
    if task.authority:
        lines.extend(("Authority:", *(f"- {item}" for item in task.authority)))
    return "\n".join(lines)


def _send_prompt(request: AdapterSendRequest) -> str:
    lines = [
        request.prompt,
        "Return only the final result. Begin with one concise CAPSULE: line, then put complete non-redundant detail under DETAILS:; omit progress narration and hidden reasoning.",
    ]
    if request.reply_to is not None:
        lines.append(f"Reply to: {request.reply_to}")
    if request.answers:
        lines.append(
            "Answers: "
            + json.dumps(
                request.answers,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    return "\n".join(lines)


def _terminal_snapshot(
    context: ResolvedContext,
    *,
    session_id: str,
    execution_id: str,
    execution_state: str,
    result_text: str | None = None,
    error: AdapterFailure | None = None,
) -> AdapterSnapshot:
    return AdapterSnapshot(
        external_session_id=session_id,
        external_execution_id=execution_id,
        conversation_state="idle",
        execution_state=execution_state,
        effective_model=context.effective_model,
        effective_reasoning=context.effective_reasoning,
        workspace_path=context.workspace_path,
        workspace_key=context.workspace_key,
        context_hash=context.context_hash,
        result_text=result_text,
        error=error,
        evidence={
            "source": "claude-code-managed-sdk",
            "terminal_synchronous": True,
            "quota_guard": "subscription-hard-stop-and-live-monitor",
        },
    )


def _placeholder_snapshot(
    request: AdapterSessionRequest,
    *,
    conversation_state: str,
) -> AdapterSnapshot:
    return AdapterSnapshot(
        external_session_id=request.external_session_id,
        external_execution_id=request.external_execution_id or "claude-terminal-session",
        conversation_state=conversation_state,
        execution_state="succeeded",
        effective_model="unavailable-without-resume",
        effective_reasoning={},
        workspace_path="",
        workspace_key="",
        context_hash="",
        evidence={"source": "persisted-session-id", "terminal_synchronous": True},
    )


def _require_external_execution(
    request: AdapterSessionRequest,
    snapshot: AdapterSnapshot,
) -> None:
    if (
        request.external_execution_id is not None
        and request.external_execution_id != snapshot.external_execution_id
    ):
        raise ServiceError("CONTEXT_DRIFT", "Claude external execution identity changed")


def _service_failure(error: AdapterFailure) -> ServiceError:
    return ServiceError(
        error.code,
        error.message,
        category=error.category,
        retryable=error.retryable,
    )


def _service_probe_error(state: str) -> ServiceError:
    return _service_failure(_probe_error(state))


async def _interrupt_or_recovery(
    client: Any,
    failure: AdapterFailure,
    timeout_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(client.interrupt(), timeout=timeout_seconds)
    except BaseException as exc:
        raise ServiceError(
            "RECOVERY_REQUIRED",
            "Claude interrupt was not confirmed after unsafe output",
            category="adapter",
        ) from exc
    raise _service_failure(failure)


def _variant_pair_key(
    base_pair_key: str,
    model: str,
    reasoning: Mapping[str, Any],
    transport: str,
) -> str:
    try:
        encoded = json.dumps(
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
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Claude variant identity is not canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _subscription_oauth_source(data: Mapping[str, Any]) -> bool:
    return data.get("apiKeySource") in {"none", "oauth"}


def _bounded_controller_result(parts: Sequence[str]) -> str:
    result = "".join(parts).strip()
    if not result:
        return "Claude task completed."
    if len(result) <= CONTROLLER_RESULT_MAX_CHARS:
        return result
    content_chars = CONTROLLER_RESULT_MAX_CHARS - len(_CONTROLLER_TRUNCATION_MARKER)
    return result[:content_chars] + _CONTROLLER_TRUNCATION_MARKER


def _unsafe_rate(info: Any) -> AdapterFailure | None:
    raw = getattr(info, "raw", None)
    status = getattr(info, "status", None)
    overage_status = getattr(info, "overage_status", None)
    if (
        status not in {"allowed", "allowed_warning"}
        or not isinstance(raw, Mapping)
        or raw.get("isUsingOverage") is not False
        or overage_status != "rejected"
    ):
        return AdapterFailure(
            "QUOTA_PAUSED",
            "quota",
            False,
            "Claude quota/no-overage prerequisite is unsafe",
        )
    error_code = raw.get("errorCode")
    if isinstance(error_code, str) and any(
        word in error_code.casefold() for word in ("credit", "billing", "rate")
    ):
        return AdapterFailure(
            "QUOTA_PAUSED", "quota", False, "Claude reported a billing/quota error"
        )
    return None


def _assistant_error(category: str) -> AdapterFailure:
    if category in {"authentication_failed"}:
        return AdapterFailure("AUTH_REQUIRED", "authentication", False, "Claude auth failed")
    if category in {"billing_error", "rate_limit"}:
        return AdapterFailure("QUOTA_PAUSED", "quota", False, "Claude quota paused")
    return AdapterFailure("CAPABILITY_MISSING", "adapter", False, "Claude canary failed")


def _result_error(message: Any) -> AdapterFailure:
    status = getattr(message, "api_error_status", None)
    if status == 429:
        return AdapterFailure("QUOTA_PAUSED", "quota", False, "Claude quota paused")
    return AdapterFailure(
        "CAPABILITY_MISSING", "adapter", False, "Claude terminal canary result was unsafe"
    )


def _probe_error(state: str) -> AdapterFailure:
    code = {
        "not_installed": "INSTALL_REQUIRED",
        "auth_required": "AUTH_REQUIRED",
        "incompatible": "TRANSPORT_INCOMPATIBLE",
    }.get(state, "CAPABILITY_MISSING")
    return AdapterFailure(code, "runtime", False, f"Claude runtime is {state}")


def _failure(
    pair_key: str,
    error: AdapterFailure,
    details: Mapping[str, Any],
) -> CanaryResult:
    return CanaryResult(False, pair_key, dict(details), error)


def _lifecycle_gap() -> None:
    raise ServiceError(
        "CAPABILITY_MISSING",
        "Claude ordinary lifecycle awaits declared-native trust/resume proof",
        category="adapter",
    )


def _run_command(argv: tuple[str, ...], timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout[:16_384], completed.stderr[:4096])


def _default_client_factory(options: Any) -> Any:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


def _installed_sdk_version() -> str:
    try:
        return metadata.version("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        return "missing"


def _sdk_bundled_cli_paths() -> tuple[Path, ...]:
    try:
        distribution = metadata.distribution("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        return ()
    result: list[Path] = []
    for item in distribution.files or ():
        if Path(str(item)).name.casefold() not in {"claude", "claude.exe"}:
            continue
        candidate = Path(distribution.locate_file(item))
        try:
            if candidate.resolve(strict=True).is_file():
                result.append(candidate.resolve(strict=True))
        except OSError:
            continue
    return tuple(result)


def _same_path(left: Path, right: Path) -> bool:
    try:
        right_resolved = right.resolve(strict=True)
    except OSError:
        return False
    return os.path.normcase(str(left)) == os.path.normcase(str(right_resolved))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
