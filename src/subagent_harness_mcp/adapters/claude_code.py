"""Capability-gated managed Claude Code adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import AsyncIterator, Any, Callable, Iterable, Mapping, Protocol, Sequence, get_args

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
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 180.0
DEFAULT_TURN_TIMEOUT_SECONDS: float | None = None
CLAUDE_MAX_WIRE_BYTES = 8 * 1024 * 1024
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
class _ProcessObservation:
    name: str
    executable_path: str | None
    command_line: str | None
    cwd: str | None


ProcessInventory = Callable[[], Iterable[_ProcessObservation]]


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
            "adapter_version": "1.0.3",
            "sdk_version": self.sdk_version,
            "cli_path": str(self.cli_path),
            "cli_version": self.cli_version,
            "cli_sha256": self.cli_sha256,
            "cli_file_id": self.cli_file_id,
            "transport": "managed-sdk-default",
            "auth_method": "claude.ai",
        }


@dataclass(slots=True)
class _ManagedTurn:
    execution_id: str
    client: Any
    rate_seen: bool
    rate_safe: bool | None = None
    task: asyncio.Task[None] | None = None
    interrupted: bool = False
    interrupt_ambiguous: bool = False
    finishing: bool = False
    finalized: bool = False


@dataclass(slots=True)
class _ManagedSession:
    context: ResolvedContext
    snapshot: AdapterSnapshot
    turn: _ManagedTurn | None = None
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _turn_done(turn: _ManagedTurn) -> bool:
    return turn.finalized or (turn.task is not None and turn.task.done())


async def _replay_messages(
    prefix: Sequence[Any], messages: Any
) -> AsyncIterator[Any]:
    for message in prefix:
        yield message
    async for message in messages:
        yield message


class ClaudeCodeAdapter:
    def __init__(
        self,
        *,
        cli_path: Path | None = None,
        command_runner: CommandRunner | None = None,
        client_factory: Callable[[Any], Any] | None = None,
        sdk_version: str | None = None,
        bundled_cli_paths: Sequence[Path] | None = None,
        process_inventory: ProcessInventory | None = None,
        canary_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        turn_timeout_seconds: float | None = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> None:
        self._cli_path = cli_path
        self._command_runner = command_runner or _run_command
        self._client_factory = client_factory or _default_client_factory
        self._sdk_version = sdk_version
        self._bundled_cli_paths = bundled_cli_paths
        self._process_inventory = process_inventory or _windows_process_inventory
        self._canary_timeout = canary_timeout_seconds
        self._turn_timeout = turn_timeout_seconds
        self._last_probe_pair: str | None = None
        self._sessions: dict[str, _ManagedSession] = {}
        self._manifest = AdapterManifest(
            adapter_api_version=ADAPTER_API_VERSION,
            runtime_id="claude-code",
            provider_id="anthropic",
            harness_id="claude-code",
            display_name="Claude sub-agent",
            adapter_version="1.0.3",
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
            max_write_roots_per_session=32,
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
        except Exception:
            return _failure(
                request.pair_key,
                AdapterFailure(
                    "CAPABILITY_MISSING",
                    "adapter",
                    False,
                    (
                        "Claude quota refresh cleanup was not confirmed; "
                        "no model task was sent"
                    ),
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
        del effort
        await asyncio.wait_for(client.connect(None), timeout=self._canary_timeout)
        return _failure(
            request.pair_key,
            AdapterFailure(
                "CAPABILITY_MISSING",
                "provider",
                False,
                "Claude SDK exposes exact rate status only on a provider response",
            ),
            {},
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
        while not init_seen:
            message = await asyncio.wait_for(
                messages.__anext__(), timeout=self._canary_timeout
            )
            if isinstance(message, AssistantMessage) or isinstance(message, ResultMessage):
                return await self._interrupt_for_guard_failure(
                    client,
                    request.pair_key,
                    AdapterFailure(
                        "CONTEXT_DRIFT",
                        "adapter",
                        False,
                        "Claude emitted output before startup status completed",
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
                if message.error is not None:
                    return _failure(
                        request.pair_key,
                        _assistant_error(message.error),
                        {},
                    )
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
                assistant_seen = True
                # Content is intentionally never inspected; ThinkingBlock and raw
                # provider output cannot enter the service or durable state.
            elif isinstance(message, ResultMessage):
                if (
                    message.is_error is not False
                    or message.session_id != init_session
                    or not assistant_seen
                    or not rate_seen
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
        write_set = request.write_set or (
            (".",) if "workspace_write" in request.permissions else ()
        )
        attestation = {
            "source": "claude-code-managed-sdk",
            "reasoning_source": "claude-code-managed-sdk",
            "reasoning_binding": [
                "ClaudeAgentOptions.effort",
                "CLAUDE_CODE_EFFORT_LEVEL",
            ],
            "reasoning_provider_reported": False,
            "variant_id": request.variant_id,
            "permissions": list(request.permissions),
            "write_set": list(write_set),
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
            write_set=write_set,
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
                "live_status_after_restart",
                "needs_input",
                "declared_mcp",
                "project_local_context_and_hooks",
            ),
            attestation=attestation,
        )

    async def spawn(self, request: AdapterSpawnRequest) -> AdapterSnapshot:
        client, messages, session_id, rate_seen = await self._open_turn(
            context=request.context,
            prompt=_spawn_prompt(request),
            expected_session_id=None,
        )
        if session_id in self._sessions:
            await _disconnect_or_recovery(
                client,
                self._canary_timeout,
                "Claude reused a live session identity",
            )
            raise ServiceError("CONTEXT_DRIFT", "Claude reused a live session identity")
        snapshot = _running_snapshot(
            request.context,
            session_id=session_id,
            execution_id=request.execution_id,
            rate_seen=rate_seen,
        )
        session = _ManagedSession(request.context, snapshot)
        self._sessions[session_id] = session
        self._start_background_turn(
            session,
            client=client,
            messages=messages,
            execution_id=request.execution_id,
            rate_seen=rate_seen,
        )
        return snapshot

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        existing = self._sessions.get(request.external_session_id)
        if existing is not None:
            if existing.closed:
                raise ServiceError("SESSION_CLOSED", "Claude session is closed")
            if existing.context.context_hash != request.context.context_hash:
                raise ServiceError("CONTEXT_DRIFT", "Claude resumed context changed")
            async with existing.lock:
                if existing.turn is not None and not _turn_done(existing.turn):
                    raise ServiceError("SESSION_BUSY", "Claude turn is active")
                client, messages, session_id, rate_seen = await self._open_turn(
                    context=request.context,
                    prompt=_send_prompt(request),
                    expected_session_id=request.external_session_id,
                )
                snapshot = _running_snapshot(
                    request.context,
                    session_id=session_id,
                    execution_id=request.execution_id,
                    rate_seen=rate_seen,
                )
                existing.snapshot = snapshot
                self._start_background_turn(
                    existing,
                    client=client,
                    messages=messages,
                    execution_id=request.execution_id,
                    rate_seen=rate_seen,
                )
                return snapshot

        client, messages, session_id, rate_seen = await self._open_turn(
            context=request.context,
            prompt=_send_prompt(request),
            expected_session_id=request.external_session_id,
        )
        snapshot = _running_snapshot(
            request.context,
            session_id=session_id,
            execution_id=request.execution_id,
            rate_seen=rate_seen,
        )
        session = _ManagedSession(request.context, snapshot)
        self._sessions[session_id] = session
        self._start_background_turn(
            session,
            client=client,
            messages=messages,
            execution_id=request.execution_id,
            rate_seen=rate_seen,
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
        session = self._sessions.get(request.external_session_id)
        if session is None:
            raise ServiceError("CAPABILITY_MISSING", "Claude turn is not active")
        _require_external_execution(request, session.snapshot)
        async with session.lock:
            turn = session.turn
            if turn is None or _turn_done(turn):
                return session.snapshot
            if not turn.finishing:
                turn.interrupted = True
                try:
                    await asyncio.wait_for(
                        turn.client.interrupt(), timeout=self._canary_timeout
                    )
                except BaseException as exc:
                    turn.interrupted = False
                    turn.interrupt_ambiguous = True
                    raise ServiceError(
                        "RECOVERY_REQUIRED",
                        "Claude interrupt was not confirmed",
                        category="adapter",
                    ) from exc
            task = turn.task
        assert task is not None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._canary_timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Claude interrupt terminal state was not confirmed",
                category="adapter",
            ) from exc
        return session.snapshot

    async def close(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._sessions.get(request.external_session_id)
        if session is None:
            return _placeholder_snapshot(request, conversation_state="closed")
        _require_external_execution(request, session.snapshot)
        if session.turn is not None and not _turn_done(session.turn):
            raise ServiceError("SESSION_BUSY", "active Claude turn cannot be closed")
        current = session.snapshot
        if current.evidence.get("cleanup_confirmed") is False:
            turn = session.turn
            if turn is None:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Claude session cleanup is unverified",
                    category="adapter",
                )
            try:
                await asyncio.wait_for(
                    turn.client.disconnect(), timeout=self._canary_timeout
                )
            except BaseException as exc:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Claude session cleanup is still unverified",
                    category="adapter",
                ) from exc
            current = AdapterSnapshot(
                external_session_id=current.external_session_id,
                external_execution_id=current.external_execution_id,
                conversation_state=current.conversation_state,
                execution_state=current.execution_state,
                effective_model=current.effective_model,
                effective_reasoning=current.effective_reasoning,
                workspace_path=current.workspace_path,
                workspace_key=current.workspace_key,
                context_hash=current.context_hash,
                result_text=current.result_text,
                needs_input=current.needs_input,
                error=current.error,
                evidence={**dict(current.evidence), "cleanup_confirmed": True},
            )
            session.snapshot = current
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
            evidence={
                **dict(current.evidence),
                "source": "claude-code-managed-sdk",
                "native_session_retained": True,
            },
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

    async def orphan_cleanup_confirmed(
        self,
        request: AdapterSessionRequest,
        context: ResolvedContext,
    ) -> bool:
        del request
        state, bound, _ = self._bind_no_model()
        if (
            state != "needs_canary"
            or bound is None
            or not _bound_matches_context(bound, context)
        ):
            return False
        try:
            processes = await asyncio.to_thread(
                lambda: tuple(self._process_inventory())
            )
        except Exception:
            return False
        workspace = _fold_path(context.workspace_path)
        cli_path = _fold_path(bound.cli_path)
        cli_name = bound.cli_path.name.casefold()
        candidate_names = {cli_name, "claude", "claude.exe", "node", "node.exe"}
        for process in processes:
            name = str(process.name or "").casefold()
            executable = (
                None
                if not process.executable_path
                else _fold_path(process.executable_path)
            )
            command = (process.command_line or "").casefold()
            markers_match = bool(command) and all(
                marker in command
                for marker in (
                    "--output-format",
                    "stream-json",
                    "--input-format",
                    "--strict-mcp-config",
                )
            )
            cli_matches = bool(
                executable == cli_path
                or cli_path in command
                or (executable is None and name == cli_name)
            )
            if not process.cwd:
                if cli_matches or (
                    name in candidate_names and (not command or markers_match)
                ):
                    return False
                continue
            if _fold_path(process.cwd) != workspace:
                continue
            if cli_matches or (
                name in candidate_names and (not command or markers_match)
            ):
                return False
        return True

    async def _open_turn(
        self,
        *,
        context: ResolvedContext,
        prompt: str,
        expected_session_id: str | None,
    ) -> tuple[Any, Any, str, bool]:
        state, bound, _ = self._bind_no_model()
        if state != "needs_canary" or bound is None:
            raise _service_probe_error(state)
        if self._last_probe_pair != bound.pair_key:
            raise ServiceError("CONTEXT_DRIFT", "Claude runtime identity changed before launch")
        options = _build_lifecycle_options(context, bound, resume=expected_session_id)
        client = self._client_factory(options)
        try:
            await asyncio.wait_for(client.connect(None), timeout=self._canary_timeout)
            messages = client.receive_messages().__aiter__()
            await asyncio.wait_for(client.query(prompt), timeout=self._canary_timeout)
            session_id, rate_seen, buffered = await self._await_lifecycle_guard(
                client,
                messages,
                context=context,
                expected_session_id=expected_session_id,
            )
            messages = _replay_messages(buffered, messages)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            await _disconnect_or_recovery(
                client,
                self._canary_timeout,
                "Claude startup cleanup was not confirmed",
            )
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Claude startup handshake timed out",
                category="adapter",
            ) from exc
        except ServiceError:
            await _disconnect_or_recovery(
                client,
                self._canary_timeout,
                "Claude startup cleanup was not confirmed",
            )
            raise
        except BaseException as exc:
            await _disconnect_or_recovery(
                client,
                self._canary_timeout,
                "Claude startup cleanup was not confirmed",
            )
            raise ServiceError(
                "RECOVERY_REQUIRED",
                f"Claude startup outcome is ambiguous ({_ambiguous_failure_label(exc)})",
                category="adapter",
            ) from exc
        return client, messages, session_id, rate_seen

    def _start_background_turn(
        self,
        session: _ManagedSession,
        *,
        client: Any,
        messages: Any,
        execution_id: str,
        rate_seen: bool,
    ) -> None:
        turn = _ManagedTurn(
            execution_id,
            client,
            rate_seen,
            rate_safe=True if rate_seen else None,
        )
        turn.task = asyncio.create_task(
            self._finish_background_turn(
                session,
                turn=turn,
                messages=messages,
            )
        )
        session.turn = turn

    async def _finish_background_turn(
        self,
        session: _ManagedSession,
        *,
        turn: _ManagedTurn,
        messages: Any,
    ) -> None:
        snapshot: AdapterSnapshot | None = None
        failure: BaseException | None = None
        cleanup_failure: BaseException | None = None
        try:
            snapshot = await self._receive_terminal_result(
                turn.client,
                messages,
                context=session.context,
                session_id=session.snapshot.external_session_id,
                external_execution_id=turn.execution_id,
                turn=turn,
            )
        except BaseException as exc:
            failure = exc
        turn.finishing = True
        try:
            await asyncio.wait_for(turn.client.disconnect(), timeout=self._canary_timeout)
        except BaseException as exc:
            cleanup_failure = exc

        async with session.lock:
            if session.turn is not turn:
                return
            if cleanup_failure is not None:
                reported_failure = failure
                if reported_failure is None:
                    reported_failure = ServiceError(
                        "RECOVERY_REQUIRED",
                        "Claude terminal turn cleanup was not confirmed",
                        category="adapter",
                    )
                session.snapshot = _failed_snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=turn.execution_id,
                    failure=reported_failure,
                    cleanup_confirmed=False,
                    rate_seen=turn.rate_seen,
                    rate_safe=turn.rate_safe,
                    result_text=None if snapshot is None else snapshot.result_text,
                )
            elif turn.interrupt_ambiguous:
                session.snapshot = _failed_snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=turn.execution_id,
                    failure=ServiceError(
                        "RECOVERY_REQUIRED",
                        "Claude interrupt outcome is ambiguous",
                        category="adapter",
                    ),
                    cleanup_confirmed=True,
                    rate_seen=turn.rate_seen,
                    rate_safe=turn.rate_safe,
                    result_text=None if snapshot is None else snapshot.result_text,
                )
            elif turn.interrupted and snapshot is None:
                session.snapshot = _terminal_snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=turn.execution_id,
                    execution_state="interrupted",
                    error=AdapterFailure(
                        "INTERRUPTED", "cancelled", False, "Claude turn interrupted"
                    ),
                    rate_seen=turn.rate_seen,
                    rate_safe=turn.rate_safe,
                )
            elif failure is not None:
                session.snapshot = _failed_snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=turn.execution_id,
                    failure=failure,
                    cleanup_confirmed=True,
                    rate_seen=turn.rate_seen,
                    rate_safe=turn.rate_safe,
                )
            else:
                assert snapshot is not None
                session.snapshot = snapshot
            turn.finalized = True

    async def _await_lifecycle_guard(
        self,
        client: Any,
        messages: Any,
        *,
        context: ResolvedContext,
        expected_session_id: str | None,
    ) -> tuple[str, bool, tuple[Any, ...]]:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage

        session_id: str | None = None
        rate_session_id: str | None = None
        rate_seen = False
        buffered: list[Any] = []
        while session_id is None:
            message = await asyncio.wait_for(messages.__anext__(), timeout=self._canary_timeout)
            if isinstance(message, (AssistantMessage, ResultMessage)):
                buffered.append(message)
                continue
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
                candidate = message.session_id
                if not isinstance(candidate, str) or not candidate:
                    await _interrupt_or_recovery(
                        client,
                        AdapterFailure(
                            "CONTEXT_DRIFT",
                            "adapter",
                            False,
                            "Claude rate status did not identify its session",
                        ),
                        self._canary_timeout,
                    )
                rate_session_id = candidate
                rate_seen = True
        if rate_seen and rate_session_id != session_id:
            await _interrupt_or_recovery(
                client,
                AdapterFailure(
                    "CONTEXT_DRIFT",
                    "adapter",
                    False,
                    "Claude rate status did not match the native session",
                ),
                self._canary_timeout,
            )
        return session_id, rate_seen, tuple(buffered)

    async def _receive_terminal_result(
        self,
        client: Any,
        messages: Any,
        *,
        context: ResolvedContext,
        session_id: str,
        external_execution_id: str,
        turn: _ManagedTurn,
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
            pending = messages.__anext__()
            message = (
                await pending
                if self._turn_timeout is None
                else await asyncio.wait_for(pending, timeout=self._turn_timeout)
            )
            if isinstance(message, RateLimitEvent):
                if message.session_id != session_id:
                    await _interrupt_or_recovery(
                        client,
                        AdapterFailure(
                            "CONTEXT_DRIFT",
                            "adapter",
                            False,
                            "Claude rate status did not match the native session",
                        ),
                        self._canary_timeout,
                    )
                turn.rate_seen = True
                unsafe = _unsafe_rate(message.rate_limit_info)
                if unsafe is not None:
                    turn.rate_safe = False
                    await _interrupt_or_recovery(client, unsafe, self._canary_timeout)
                turn.rate_safe = True
                continue
            if isinstance(message, AssistantMessage):
                if message.error is not None:
                    raise _service_failure(_assistant_error(message.error))
                if message.model != context.effective_model or (
                    message.session_id is not None
                    and message.session_id != session_id
                ):
                    await _interrupt_or_recovery(
                        client,
                        AdapterFailure("CONTEXT_DRIFT", "adapter", False, "Claude model or session changed"),
                        self._canary_timeout,
                    )
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
                if not turn.rate_seen:
                    raise ServiceError(
                        "CAPABILITY_MISSING",
                        "Claude task response did not publish exact rate status",
                        category="provider",
                    )
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
                    execution_id=external_execution_id,
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
            "adapter_version": "1.0.3",
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
        max_buffer_size=CLAUDE_MAX_WIRE_BYTES,
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
    write_set: Sequence[str],
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
        "write_set": list(write_set),
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


def _bound_matches_context(bound: _BoundRuntime, context: ResolvedContext) -> bool:
    attestation = context.attestation
    variant_id = attestation.get("variant_id")
    permissions = attestation.get("permissions")
    write_set = attestation.get("write_set")
    context_policy_id = attestation.get("context_policy_id")
    permission_policy_id = attestation.get("permission_policy_id")
    if (
        context.runtime_id != "claude-code"
        or context.transport != "managed-sdk"
        or attestation.get("source") != "claude-code-managed-sdk"
        or not isinstance(variant_id, str)
        or not isinstance(permissions, list)
        or not all(isinstance(item, str) for item in permissions)
        or not isinstance(write_set, list)
        or not all(isinstance(item, str) for item in write_set)
        or not isinstance(context_policy_id, str)
        or not isinstance(permission_policy_id, str)
    ):
        return False
    try:
        expected = _lifecycle_context_hash(
            bound,
            runtime_id=context.runtime_id,
            variant_id=variant_id,
            model=context.effective_model,
            reasoning=context.effective_reasoning,
            workspace_path=context.workspace_path,
            workspace_key=context.workspace_key,
            transport=context.transport,
            permissions=permissions,
            write_set=write_set,
            context_policy_id=context_policy_id,
            permission_policy_id=permission_policy_id,
        )
    except ServiceError:
        return False
    return expected == context.context_hash


def _fold_path(value: str | Path) -> str:
    try:
        resolved = Path(value).resolve(strict=False)
    except OSError:
        resolved = Path(value).absolute()
    folded = os.path.normcase(str(resolved))
    return folded.casefold() if os.name == "nt" else folded


def _windows_process_inventory() -> tuple[_ProcessObservation, ...]:
    import psutil

    observations: list[_ProcessObservation] = []
    for process in psutil.process_iter(
        ("name", "exe", "cmdline", "cwd"),
        ad_value=None,
    ):
        try:
            info = process.info
            command = info.get("cmdline")
            if isinstance(command, (list, tuple)):
                command_line = "\x00".join(str(part) for part in command) or None
            else:
                command_line = None if command is None else str(command)
            observations.append(
                _ProcessObservation(
                    str(info.get("name") or ""),
                    None if not info.get("exe") else str(info["exe"]),
                    command_line,
                    None if not info.get("cwd") else str(info["cwd"]),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return tuple(observations)


def _build_lifecycle_options(
    context: ResolvedContext,
    bound: _BoundRuntime,
    *,
    resume: str | None,
):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

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
    write_set = attestation.get("write_set")
    context_policy_id = attestation.get("context_policy_id")
    permission_policy_id = attestation.get("permission_policy_id")
    if (
        attestation.get("source") != "claude-code-managed-sdk"
        or not isinstance(variant_id, str)
        or not isinstance(permissions, list)
        or not all(isinstance(item, str) for item in permissions)
        or not isinstance(write_set, list)
        or not all(isinstance(item, str) for item in write_set)
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
        write_set=tuple(write_set),
        context_policy_id=context_policy_id,
        permission_policy_id=permission_policy_id,
    )
    if expected_hash != context.context_hash:
        raise ServiceError("CONTEXT_DRIFT", "Claude runtime/model/workspace context changed")
    tools = ["Read", "Glob", "Grep"]
    hooks = None
    if "workspace_write" in permissions:
        tools.extend(["Edit", "Write"])
        hooks = {
            "PreToolUse": [
                HookMatcher(
                    matcher="Edit|Write",
                    hooks=[_claude_write_scope_hook(context.workspace_path, tuple(write_set))],
                    timeout=5.0,
                )
            ]
        }
    return ClaudeAgentOptions(
        cli_path=bound.cli_path,
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=(
            []
            if "workspace_write" in permissions and tuple(write_set) != (".",)
            else ["user"]
        ),
        skills="all",
        strict_mcp_config=True,
        mcp_servers={},
        tools=tools,
        allowed_tools=list(tools),
        disallowed_tools=list(RECURSION_DENIES),
        hooks=hooks,
        permission_mode="dontAsk",
        cwd=Path(context.workspace_path),
        model=context.effective_model,
        effort=effort,
        thinking={"type": "adaptive", "display": "omitted"},
        fallback_model=None,
        max_turns=None,
        max_buffer_size=CLAUDE_MAX_WIRE_BYTES,
        include_partial_messages=False,
        forward_subagent_text=False,
        resume=resume,
        fork_session=False,
        env={**PROVIDER_SAFETY_ENV, "CLAUDE_CODE_EFFORT_LEVEL": effort},
        extra_args={"prompt-suggestions": "false"},
    )


def _claude_write_scope_hook(workspace_path: str, write_set: tuple[str, ...]):
    workspace = Path(workspace_path).resolve(strict=True)
    roots = tuple(
        workspace if scope == "." else (workspace / Path(*scope.split("/"))).resolve(strict=False)
        for scope in write_set
    )

    async def guard(tool_input: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
        payload = tool_input.get("tool_input") if isinstance(tool_input, Mapping) else None
        raw_path = payload.get("file_path") if isinstance(payload, Mapping) else None
        allowed = False
        if isinstance(raw_path, str) and raw_path:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            candidate = candidate.resolve(strict=False)
            allowed = any(_path_is_within(candidate, root) for root in roots)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": (
                    "path is inside the attested write set"
                    if allowed
                    else "path is outside the attested write set"
                ),
            }
        }

    return guard


def _path_is_within(candidate: Path, root: Path) -> bool:
    candidate_key = os.path.normcase(str(candidate))
    root_key = os.path.normcase(str(root))
    if os.name == "nt":
        candidate_key = candidate_key.casefold()
        root_key = root_key.casefold()
    try:
        return os.path.commonpath((candidate_key, root_key)) == root_key
    except ValueError:
        return False


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
    context = getattr(request, "context", None)
    attestation = getattr(context, "attestation", {})
    write_set = attestation.get("write_set", ()) if isinstance(attestation, Mapping) else ()
    input_attestations = (
        attestation.get("input_attestations", ())
        if isinstance(attestation, Mapping)
        else ()
    )
    if isinstance(input_attestations, (list, tuple)) and input_attestations:
        lines.extend(
            (
                "Trusted input attestations computed read-only by Subagent MCP immediately before launch:",
                *(
                    f"- {item['path']} sha256={item['sha256']} ({item['byte_count']} bytes)"
                    for item in input_attestations
                    if isinstance(item, Mapping)
                    and isinstance(item.get("path"), str)
                    and isinstance(item.get("sha256"), str)
                    and isinstance(item.get("byte_count"), int)
                ),
                "Bind the final decision to these controller-verified hashes.",
            )
        )
    if write_set:
        lines.extend(
            (
                f"Verified repository root: {context.workspace_path}",
                "Write only within these repository-relative paths:",
                *(f"- {item}" for item in write_set),
            )
        )
    if task.authority:
        lines.extend(("Authority:", *(f"- {item}" for item in task.authority)))
    return "\n".join(lines)


def _send_prompt(request: AdapterSendRequest) -> str:
    lines = [
        request.prompt,
        "Return only the final result. Begin with one concise CAPSULE: line, then put complete non-redundant detail under DETAILS:; omit progress narration and hidden reasoning.",
    ]
    input_attestations = request.context.attestation.get("input_attestations", ())
    if isinstance(input_attestations, (list, tuple)) and input_attestations:
        lines.extend(
            (
                "Trusted input attestations computed read-only by Subagent MCP immediately before launch:",
                *(
                    f"- {item['path']} sha256={item['sha256']} ({item['byte_count']} bytes)"
                    for item in input_attestations
                    if isinstance(item, Mapping)
                    and isinstance(item.get("path"), str)
                    and isinstance(item.get("sha256"), str)
                    and isinstance(item.get("byte_count"), int)
                ),
                "Bind the final decision to these controller-verified hashes.",
            )
        )
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


def _running_snapshot(
    context: ResolvedContext,
    *,
    session_id: str,
    execution_id: str,
    rate_seen: bool,
) -> AdapterSnapshot:
    evidence: dict[str, Any] = {
        "source": "claude-code-managed-sdk",
        "background_lifecycle": True,
        "quota_guard": "exact-task-response-and-live-monitor",
        "rate_evidence_seen": rate_seen,
        "cleanup_confirmed": False,
    }
    if rate_seen:
        evidence.update(
            {
                "is_using_overage": False,
                "overage_blocked": True,
            }
        )
    return AdapterSnapshot(
        external_session_id=session_id,
        external_execution_id=execution_id,
        conversation_state="active",
        execution_state="running",
        effective_model=context.effective_model,
        effective_reasoning=context.effective_reasoning,
        workspace_path=context.workspace_path,
        workspace_key=context.workspace_key,
        context_hash=context.context_hash,
        evidence=evidence,
    )


def _failed_snapshot(
    context: ResolvedContext,
    *,
    session_id: str,
    execution_id: str,
    failure: BaseException,
    cleanup_confirmed: bool,
    rate_seen: bool,
    rate_safe: bool | None,
    result_text: str | None = None,
) -> AdapterSnapshot:
    if isinstance(failure, ServiceError):
        error = AdapterFailure(
            failure.code,
            failure.category,
            failure.retryable,
            str(failure),
        )
    else:
        error = AdapterFailure(
            "RECOVERY_REQUIRED",
            "adapter",
            False,
            (
                "Claude terminal turn outcome is ambiguous "
                f"({_ambiguous_failure_label(failure)})"
            ),
        )
    evidence: dict[str, Any] = {
        "source": "claude-code-managed-sdk",
        "background_lifecycle": True,
        "quota_guard": "exact-task-response-and-live-monitor",
        "rate_evidence_seen": rate_seen,
        "cleanup_confirmed": cleanup_confirmed,
    }
    if rate_safe is True:
        evidence.update(
            {
                "is_using_overage": False,
                "overage_blocked": True,
            }
        )
    return AdapterSnapshot(
        external_session_id=session_id,
        external_execution_id=execution_id,
        conversation_state="idle",
        execution_state="failed",
        effective_model=context.effective_model,
        effective_reasoning=context.effective_reasoning,
        workspace_path=context.workspace_path,
        workspace_key=context.workspace_key,
        context_hash=context.context_hash,
        result_text=result_text,
        error=error,
        evidence=evidence,
    )


def _ambiguous_failure_label(failure: BaseException) -> str:
    from claude_agent_sdk import CLIJSONDecodeError

    if isinstance(failure, CLIJSONDecodeError):
        if isinstance(failure.original_error, json.JSONDecodeError):
            return "stdout frame was not valid JSON"
        return "stdout frame exceeded managed buffer limit"
    return type(failure).__name__


def _terminal_snapshot(
    context: ResolvedContext,
    *,
    session_id: str,
    execution_id: str,
    execution_state: str,
    result_text: str | None = None,
    error: AdapterFailure | None = None,
    rate_seen: bool = True,
    rate_safe: bool | None = True,
) -> AdapterSnapshot:
    evidence: dict[str, Any] = {
        "source": "claude-code-managed-sdk",
        "background_lifecycle": True,
        "quota_guard": "exact-task-response-and-live-monitor",
        "rate_evidence_seen": rate_seen,
        "cleanup_confirmed": True,
    }
    if rate_safe is True:
        evidence.update(
            {
                "is_using_overage": False,
                "overage_blocked": True,
            }
        )
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
        evidence=evidence,
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


async def _disconnect_or_recovery(
    client: Any,
    timeout_seconds: float,
    message: str,
) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), timeout=timeout_seconds)
    except BaseException as exc:
        raise ServiceError(
            "RECOVERY_REQUIRED",
            message,
            category="adapter",
        ) from exc


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
    if not isinstance(raw, Mapping):
        return AdapterFailure(
            "CAPABILITY_MISSING",
            "provider",
            False,
            "Claude no-overage evidence is unavailable",
        )
    is_using_overage = raw.get("isUsingOverage")
    if is_using_overage is True:
        return AdapterFailure(
            "USAGE_CREDITS_FORBIDDEN",
            "quota",
            False,
            "Claude usage credits/overage are active",
        )
    if status not in {"allowed", "allowed_warning", "rejected"}:
        return AdapterFailure(
            "CAPABILITY_MISSING",
            "provider",
            False,
            "Claude quota status is unavailable",
        )
    if is_using_overage is not False:
        return AdapterFailure(
            "CAPABILITY_MISSING",
            "provider",
            False,
            "Claude no-overage evidence is unavailable",
        )
    if overage_status in {"allowed", "allowed_warning"}:
        return AdapterFailure(
            "USAGE_CREDITS_FORBIDDEN",
            "quota",
            False,
            "Claude usage credits/overage are available",
        )
    if overage_status not in {None, "rejected"}:
        return AdapterFailure(
            "CAPABILITY_MISSING",
            "provider",
            False,
            "Claude no-overage evidence is unavailable",
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
    if (
        getattr(message, "subtype", None) == "error_max_turns"
        or getattr(message, "terminal_reason", None) == "max_turns"
    ):
        turns = getattr(message, "num_turns", None)
        suffix = (
            f" after {turns} turns"
            if isinstance(turns, int) and not isinstance(turns, bool) and turns >= 0
            else ""
        )
        return AdapterFailure(
            "CAPABILITY_MISSING",
            "adapter",
            False,
            f"Claude task reached its turn limit{suffix}",
        )
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
