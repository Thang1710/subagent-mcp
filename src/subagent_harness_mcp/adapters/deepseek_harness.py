"""DeepSeek Harness adapter over the harness's native ACP automation server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..contracts import ADAPTER_API_VERSION, AdapterManifest, ServiceError
from .base import (
    AdapterContextRequest,
    AdapterFailure,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSnapshot,
    AdapterSpawnRequest,
    ProbeResult,
    ResolvedContext,
)


RUNTIME_ID = "deepseek-harness"
TRANSPORT = "native-acp"
DEFAULT_TIMEOUT_SECONDS = 300.0
CONTROLLER_RESULT_MAX_CHARS = 4_096
MAX_WIRE_LINE_BYTES = 1024 * 1024
_TRUNCATION_MARKER = "\n[truncated by Subagent MCP]"
_QUOTA_EXHAUSTED = re.compile(
    r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b"
    r"|\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b"
    r"|\bexceed(?:ed|s)?[\s_-]+(?:(?:your|the)[\s_-]+)?(?:current[\s_-]+)?quota\b"
    r"|\b(?:balance|credits?)[\s_-]+(?:exhausted|depleted)\b"
    r"|\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b",
    re.IGNORECASE,
)
_PACKAGES = {
    "settings-file": "@deepseek-ai/dsh-settings-file",
    "credentials-local": "@deepseek-ai/dsh-credentials-local",
    "llm-pi-ai": "@deepseek-ai/dsh-llm-pi-ai",
    "sandbox-local": "@deepseek-ai/dsh-sandbox-local",
    "sandbox-policy": "@deepseek-ai/dsh-sandbox-policy",
    "subprocess-local": "@deepseek-ai/dsh-subprocess-local",
    "pwsh-sandbox": "@deepseek-ai/dsh-pwsh-sandbox",
    "shell-env": "@deepseek-ai/dsh-shell-env",
    "user-approval": "@deepseek-ai/dsh-user-approval",
    "acp-demo": "@deepseek-ai/dsh-acp-demo",
    "token-meter": "@deepseek-ai/dsh-token-meter",
    "compaction-basic": "@deepseek-ai/dsh-compaction-basic",
    "fs-sandbox": "@deepseek-ai/dsh-fs-sandbox",
    "fs-observation-policy": "@deepseek-ai/dsh-fs-observation-policy",
    "tool-fs": "@deepseek-ai/dsh-tool-fs",
    "tool-pwsh": "@deepseek-ai/dsh-tool-pwsh",
}


@dataclass(frozen=True, slots=True)
class DshBinding:
    node_path: Path
    acp_bin_path: Path
    plugins: Mapping[str, Path]
    harness_version: str
    pair_key: str


@dataclass(frozen=True, slots=True)
class DshLaunch:
    binding: DshBinding
    provider: str
    model: str
    workspace_path: str
    permission_mode: str
    persistence_root: Path
    config_path: Path


class _AcpClient(Protocol):
    async def start(self) -> None: ...

    async def new_session(self, cwd: str) -> str: ...

    async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]: ...

    async def cancel(self, session_id: str) -> None: ...

    async def close(self) -> None: ...


BindingLocator = Callable[[], DshBinding | None]
ClientFactory = Callable[[DshLaunch], _AcpClient]


@dataclass(slots=True)
class _Turn:
    execution_id: str
    task: asyncio.Task[None]
    interrupted: bool = False


@dataclass(slots=True)
class _Session:
    context: ResolvedContext
    client: _AcpClient
    snapshot: AdapterSnapshot
    turn: _Turn | None = None
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DeepSeekHarnessAdapter:
    """Own one native ACP process and fresh DSH session per conversation."""

    def __init__(
        self,
        *,
        binding_locator: BindingLocator | None = None,
        client_factory: ClientFactory | None = None,
        data_root: Path | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._binding_locator = binding_locator or locate_dsh_binding
        self._timeout = timeout_seconds
        self._client_factory = client_factory or (
            lambda launch: _StdioAcpClient(launch, timeout_seconds=self._timeout)
        )
        if data_root is None:
            from ..paths import resolve_paths

            data_root = resolve_paths().data_dir
        self._data_root = data_root / RUNTIME_ID
        self._last_pair_key: str | None = None
        self._sessions: dict[str, _Session] = {}
        self._manifest = AdapterManifest(
            adapter_api_version=ADAPTER_API_VERSION,
            runtime_id=RUNTIME_ID,
            provider_id="multi-provider",
            harness_id="deepseek-harness",
            display_name="DeepSeek Harness",
            adapter_version="0.1.0a15",
            supported_platforms=("win32",),
            supported_transports=(TRANSPORT,),
            capabilities=frozenset({"session", "interrupt", "workspace"}),
            semantic_permissions=frozenset(
                {"repo_read", "git_read", "run_tests", "workspace_write"}
            ),
            reasoning_schema={
                "type": "object",
                "additionalProperties": False,
                "maxProperties": 0,
            },
            model_schema={
                "type": "string",
                "minLength": 4,
                "description": (
                    "Exact provider::model pair. Enabling this runtime authorizes "
                    "use of that route's existing subscription, promotion, or prepaid "
                    "balance; Subagent MCP never buys or reloads credits."
                ),
                "placeholder": "provider-name::model-id",
            },
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def probe(self) -> ProbeResult:
        if sys.platform != "win32":
            self._last_pair_key = None
            return ProbeResult("incompatible", {"code": "PLATFORM_UNSUPPORTED"})
        binding = self._binding_locator()
        if binding is None:
            self._last_pair_key = None
            return ProbeResult("not_installed", {"code": "INSTALL_REQUIRED"})
        self._last_pair_key = binding.pair_key
        return ProbeResult(
            "ready",
            {
                "pair_key": binding.pair_key,
                "harness_version": binding.harness_version,
                "transport": TRANSPORT,
            },
        )

    async def resolve_context(self, request: AdapterContextRequest) -> ResolvedContext:
        if request.runtime_id != RUNTIME_ID or request.transport != TRANSPORT:
            raise ServiceError(
                "CAPABILITY_MISSING", "DeepSeek Harness native ACP context is required"
            )
        unsupported = set(request.permissions) - self._manifest.semantic_permissions
        if unsupported:
            raise ServiceError(
                "CAPABILITY_MISSING", "DeepSeek Harness permission is unsupported"
            )
        provider, model = _provider_model(request.model, request.reasoning)
        permission_mode = (
            "workspace-write" if "workspace_write" in request.permissions else "read-only"
        )
        binding = self._binding_locator()
        if binding is None:
            raise ServiceError("INSTALL_REQUIRED", "DeepSeek Harness ACP is not installed")
        if self._last_pair_key != binding.pair_key:
            raise ServiceError(
                "CONTEXT_DRIFT", "DeepSeek Harness identity changed after readiness check"
            )
        attestation = {
            "source": "deepseek-harness-native-acp",
            "variant_id": request.variant_id,
            "provider": provider,
            "model": model,
            "permission_mode": permission_mode,
            "permissions": list(request.permissions),
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
            "pair_key": binding.pair_key,
        }
        payload = {
            "runtime_id": request.runtime_id,
            "variant_id": request.variant_id,
            "provider": provider,
            "model": model,
            "reasoning": {},
            "permission_mode": permission_mode,
            "workspace_path": request.workspace_path,
            "workspace_key": request.workspace_key,
            "transport": request.transport,
            "permissions": list(request.permissions),
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
            "pair_key": binding.pair_key,
        }
        context_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ResolvedContext(
            runtime_id=request.runtime_id,
            requested_model=request.model,
            effective_model=request.model,
            requested_reasoning=dict(request.reasoning),
            effective_reasoning={},
            workspace_path=request.workspace_path,
            workspace_key=request.workspace_key,
            transport=request.transport,
            context_hash=context_hash,
            capability_gaps=(
                "resume_after_restart",
                "provider_quota_evidence",
                "interactive_input",
                "declared_mcp",
            ),
            attestation=attestation,
        )

    async def spawn(self, request: AdapterSpawnRequest) -> AdapterSnapshot:
        binding, provider, model, permission_mode = self._bound_context(request.context)
        root = self._data_root / request.conversation_id
        persistence_root = root / "sessions"
        config_path = root / "cordis.yml"
        persistence_root.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            config_path,
            render_dsh_config(
                binding,
                provider=provider,
                model=model,
                workspace_path=request.context.workspace_path,
                persistence_root=persistence_root,
                permission_mode=permission_mode,
            ).encode("utf-8"),
        )
        launch = DshLaunch(
            binding=binding,
            provider=provider,
            model=model,
            workspace_path=request.context.workspace_path,
            permission_mode=permission_mode,
            persistence_root=persistence_root,
            config_path=config_path,
        )
        client = self._client_factory(launch)
        try:
            await asyncio.wait_for(client.start(), timeout=self._timeout)
            session_id = await asyncio.wait_for(
                client.new_session(request.context.workspace_path), timeout=self._timeout
            )
        except BaseException:
            await _best_effort_close(client, self._timeout)
            raise
        if not session_id:
            await _best_effort_close(client, self._timeout)
            raise ServiceError("ADAPTER_INVALID", "DeepSeek ACP returned no session identity")
        if session_id in self._sessions:
            await _best_effort_close(client, self._timeout)
            raise ServiceError("CONTEXT_DRIFT", "DeepSeek ACP reused a live session identity")
        snapshot = _snapshot(
            request.context,
            session_id=session_id,
            execution_id=request.execution_id,
            execution_state="running",
        )
        session = _Session(request.context, client, snapshot)
        self._sessions[session_id] = session
        self._start_turn(session, request.execution_id, _spawn_prompt(request))
        return snapshot

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        if session.closed:
            raise ServiceError("SESSION_CLOSED", "DeepSeek ACP session is closed")
        if session.context.context_hash != request.context.context_hash:
            raise ServiceError("CONTEXT_DRIFT", "DeepSeek ACP resumed context changed")
        async with session.lock:
            if session.turn is not None and not session.turn.task.done():
                raise ServiceError("SESSION_BUSY", "DeepSeek ACP turn is active")
            session.snapshot = _snapshot(
                session.context,
                session_id=request.external_session_id,
                execution_id=request.execution_id,
                execution_state="running",
            )
            self._start_turn(session, request.execution_id, _send_prompt(request))
            return session.snapshot

    async def snapshot(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        _require_execution(request, session.snapshot)
        return session.snapshot

    async def interrupt(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        _require_execution(request, session.snapshot)
        async with session.lock:
            turn = session.turn
            if turn is None or turn.task.done():
                raise ServiceError("CAPABILITY_MISSING", "DeepSeek ACP turn is not active")
            turn.interrupted = True
            await asyncio.wait_for(
                session.client.cancel(request.external_session_id), timeout=self._timeout
            )
        try:
            await asyncio.wait_for(asyncio.shield(turn.task), timeout=self._timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "DeepSeek ACP cancellation was not confirmed",
                category="adapter",
            ) from exc
        return session.snapshot

    async def close(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        _require_execution(request, session.snapshot)
        if session.closed:
            return session.snapshot
        turn = session.turn
        if turn is not None and not turn.task.done():
            await self.interrupt(request)
        try:
            await asyncio.wait_for(session.client.close(), timeout=self._timeout)
        except BaseException as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "DeepSeek ACP process cleanup was not confirmed",
                category="adapter",
            ) from exc
        session.closed = True
        current = session.snapshot
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
            error=current.error,
            evidence={"source": "deepseek-harness-native-acp", "process_closed": True},
        )
        return session.snapshot

    async def open_session(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._sessions.get(request.external_session_id)
        if session is None:
            raise ServiceError(
                "CAPABILITY_MISSING",
                "DeepSeek ACP sessions cannot resume after the MCP server restarts",
            )
        _require_execution(request, session.snapshot)
        if session.closed:
            raise ServiceError("SESSION_CLOSED", "DeepSeek ACP session is closed")
        return session.snapshot

    def _session(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise ServiceError(
                "CAPABILITY_MISSING",
                "DeepSeek ACP session is not owned by this MCP process",
            ) from exc

    def _bound_context(
        self, context: ResolvedContext
    ) -> tuple[DshBinding, str, str, str]:
        if context.runtime_id != RUNTIME_ID or context.transport != TRANSPORT:
            raise ServiceError("CONTEXT_DRIFT", "DeepSeek ACP context changed")
        provider, model = _provider_model(
            context.effective_model, context.effective_reasoning
        )
        binding = self._binding_locator()
        if binding is None:
            raise ServiceError("INSTALL_REQUIRED", "DeepSeek Harness ACP is not installed")
        permission_mode = context.attestation.get("permission_mode")
        if permission_mode not in {"read-only", "workspace-write"}:
            raise ServiceError("CONTEXT_DRIFT", "DeepSeek permission mode changed")
        if (
            self._last_pair_key != binding.pair_key
            or context.attestation.get("pair_key") != binding.pair_key
            or context.attestation.get("provider") != provider
            or context.attestation.get("model") != model
        ):
            raise ServiceError("CONTEXT_DRIFT", "DeepSeek Harness identity changed")
        return binding, provider, model, str(permission_mode)

    def _start_turn(self, session: _Session, execution_id: str, prompt: str) -> None:
        task = asyncio.create_task(
            self._run_turn(session, execution_id=execution_id, prompt=prompt)
        )
        session.turn = _Turn(execution_id, task)

    async def _run_turn(
        self,
        session: _Session,
        *,
        execution_id: str,
        prompt: str,
    ) -> None:
        result: tuple[str, str] | None = None
        failure: BaseException | None = None
        try:
            result = await asyncio.wait_for(
                session.client.prompt(session.snapshot.external_session_id, prompt),
                timeout=self._timeout,
            )
        except BaseException as exc:
            failure = exc
        async with session.lock:
            turn = session.turn
            if turn is None or turn.execution_id != execution_id:
                return
            if turn.interrupted:
                session.snapshot = _snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=execution_id,
                    execution_state="interrupted",
                    error=AdapterFailure(
                        "INTERRUPTED", "cancelled", False, "DeepSeek ACP turn interrupted"
                    ),
                )
                return
            if failure is not None:
                if isinstance(failure, (TimeoutError, asyncio.TimeoutError)):
                    code = "RECOVERY_REQUIRED"
                    category = "adapter"
                    message = "DeepSeek ACP turn did not complete"
                elif _QUOTA_EXHAUSTED.search(str(failure)):
                    code = "QUOTA_PAUSED"
                    category = "quota"
                    message = "DeepSeek provider usage credit or quota is exhausted"
                else:
                    code = "PROVIDER_ERROR"
                    category = "provider"
                    message = "DeepSeek ACP turn did not complete"
                session.snapshot = _snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=execution_id,
                    execution_state="failed",
                    error=AdapterFailure(
                        code,
                        category,
                        False,
                        message,
                    ),
                )
                return
            assert result is not None
            stop_reason, text = result
            if stop_reason == "cancelled":
                session.snapshot = _snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=execution_id,
                    execution_state="cancelled",
                    error=AdapterFailure(
                        "CANCELLED", "cancelled", False, "DeepSeek ACP turn cancelled"
                    ),
                )
                return
            if stop_reason != "end_turn":
                session.snapshot = _snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=execution_id,
                    execution_state="failed",
                    error=AdapterFailure(
                        "PROVIDER_ERROR",
                        "provider",
                        False,
                        "DeepSeek ACP returned an unsupported stop reason",
                    ),
                )
                return
            session.snapshot = _snapshot(
                session.context,
                session_id=session.snapshot.external_session_id,
                execution_id=execution_id,
                execution_state="succeeded",
                result_text=_bounded_result(text),
            )


class _StdioAcpClient:
    def __init__(self, launch: DshLaunch, *, timeout_seconds: float) -> None:
        self._launch = launch
        self._timeout = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._buffers: dict[str, list[str]] = {}
        self._closed = False

    async def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("ACP client already started")
        env = _dsh_env(self._launch.permission_mode)
        self._process = await asyncio.create_subprocess_exec(
            str(self._launch.binding.node_path),
            str(self._launch.binding.acp_bin_path),
            "--config",
            str(self._launch.config_path),
            cwd=self._launch.workspace_path,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_WIRE_LINE_BYTES + 1,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        result = await asyncio.wait_for(
            self._request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "subagent-mcp", "version": "0.1.0a15"},
                },
            ),
            timeout=self._timeout,
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("ACP initialize response is invalid")

    async def new_session(self, cwd: str) -> str:
        result = await asyncio.wait_for(
            self._request("session/new", {"cwd": cwd, "mcpServers": []}),
            timeout=self._timeout,
        )
        if not isinstance(result, Mapping) or not isinstance(result.get("sessionId"), str):
            raise RuntimeError("ACP session response is invalid")
        return str(result["sessionId"])

    async def prompt(self, session_id: str, prompt: str) -> tuple[str, str]:
        self._buffers[session_id] = []
        result = await asyncio.wait_for(
            self._request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            ),
            timeout=self._timeout,
        )
        if not isinstance(result, Mapping) or not isinstance(result.get("stopReason"), str):
            raise RuntimeError("ACP prompt response is invalid")
        return str(result["stopReason"]), "".join(self._buffers.pop(session_id, ()))

    async def cancel(self, session_id: str) -> None:
        await self._notification("session/cancel", {"sessionId": session_id})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=min(self._timeout, 2.0))
            except (TimeoutError, asyncio.TimeoutError):
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=min(self._timeout, 2.0))
                except (TimeoutError, asyncio.TimeoutError):
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        self._fail_pending(RuntimeError("ACP client closed"))

    async def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _notification(self, method: str, params: Mapping[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeError("ACP process is unavailable")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        ) + b"\n"
        if len(payload) > MAX_WIRE_LINE_BYTES:
            raise RuntimeError("ACP request is too large")
        async with self._write_lock:
            process.stdin.write(payload)
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if len(line) > MAX_WIRE_LINE_BYTES:
                    raise RuntimeError("ACP response is too large")
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, Mapping):
                    raise RuntimeError("ACP response is invalid")
                if "id" in message and ("result" in message or "error" in message):
                    self._handle_response(message)
                elif isinstance(message.get("method"), str):
                    await self._handle_server_message(message)
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass
        finally:
            self._fail_pending(RuntimeError("ACP process ended before responding"))

    def _handle_response(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(RuntimeError(_acp_error_message(message.get("error"))))
        else:
            future.set_result(message.get("result"))

    async def _handle_server_message(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if method == "session/update" and isinstance(params, Mapping):
            session_id = params.get("sessionId")
            update = params.get("update")
            if isinstance(session_id, str) and isinstance(update, Mapping):
                content = update.get("content")
                if (
                    update.get("sessionUpdate") == "agent_message_chunk"
                    and isinstance(content, Mapping)
                    and content.get("type") == "text"
                    and isinstance(content.get("text"), str)
                ):
                    parts = self._buffers.get(session_id)
                    if parts is not None and sum(map(len, parts)) <= CONTROLLER_RESULT_MAX_CHARS:
                        parts.append(str(content["text"]))
            return
        request_id = message.get("id")
        if request_id is None:
            return
        if method == "session/request_permission" and isinstance(params, Mapping):
            result = _reject_permission(params)
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
            return
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while await process.stderr.read(8192):
            pass

    def _fail_pending(self, error: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)


def locate_dsh_binding() -> DshBinding | None:
    """Resolve a reviewed DSH source install without changing user configuration."""

    node_path = _node_path()
    source_root = _source_root()
    if node_path is None or source_root is None:
        return None
    packages = _package_entries(source_root)
    if packages is None:
        return None
    acp_package = packages["acp-demo"].parent.parent
    acp_bin = acp_package / "lib" / "bin.js"
    package_json = acp_package / "package.json"
    try:
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
        version = metadata["version"]
        if not isinstance(version, str) or not version:
            return None
        node_path = node_path.resolve(strict=True)
        acp_bin = acp_bin.resolve(strict=True)
        resolved_plugins = {
            key: value.resolve(strict=True) for key, value in packages.items()
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    pair_payload = {
        "version": version,
        "node": _file_identity(node_path),
        "bin": _file_identity(acp_bin),
        "plugins": {
            key: _file_identity(value) for key, value in sorted(resolved_plugins.items())
        },
    }
    pair_key = hashlib.sha256(
        json.dumps(pair_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DshBinding(node_path, acp_bin, resolved_plugins, version, pair_key)


def render_dsh_config(
    binding: DshBinding,
    *,
    provider: str,
    model: str,
    workspace_path: str,
    persistence_root: Path,
    permission_mode: str,
) -> str:
    """Render a small ACP composition; provider credentials stay DSH-owned."""

    def uri(name: str) -> str:
        return json.dumps(binding.plugins[name].resolve().as_uri())

    if permission_mode not in {"read-only", "workspace-write"}:
        raise ValueError("unsupported DeepSeek permission mode")
    value = json.dumps
    return f"""- id: settings
  name: {uri('settings-file')}
- id: credentials
  name: {uri('credentials-local')}
- id: llm
  name: {uri('llm-pi-ai')}
- id: sandbox
  name: {uri('sandbox-local')}
- id: sandbox-policy
  name: {uri('sandbox-policy')}
  config:
    mode: {permission_mode}
    workspaceRoot: {value(workspace_path)}
- id: subprocess
  name: {uri('subprocess-local')}
- id: pwsh
  name: {uri('pwsh-sandbox')}
  config:
    timeoutMs: 60000
- id: approval
  name: {uri('user-approval')}
  config:
    policy: ask
- id: shell-env
  name: {uri('shell-env')}
- id: acp-agent
  name: {uri('acp-demo')}
  config:
    provider: {value(provider)}
    model: {value(model)}
    maxParallelToolCalls: 1
    persistenceRoot: {value(str(persistence_root.resolve()))}
    persistenceCompression: zstd
    workspaceContext:
      maxBytes: 65536
    tools:
      mode: native
    toolBash: false
    toolJobs: false
    goals: false
- id: token-meter
  name: {uri('token-meter')}
- id: compaction
  name: {uri('compaction-basic')}
  config:
    thresholdRatio: 0.8
    retainRatio: 0.08
    maxTokens: 8192
    compactionRetries: 1
- id: fs
  name: {uri('fs-sandbox')}
  config:
    cwd: {value(workspace_path)}
- id: fs-observation
  name: {uri('fs-observation-policy')}
- id: tool-fs
  name: {uri('tool-fs')}
- id: tool-pwsh
  name: {uri('tool-pwsh')}
"""


def _provider_model(model: str, reasoning: Mapping[str, Any]) -> tuple[str, str]:
    if reasoning:
        raise ServiceError(
            "POLICY_REJECTED",
            "DeepSeek ACP does not expose a per-session reasoning override",
        )
    provider, separator, native_model = model.partition("::")
    if (
        separator != "::"
        or not provider
        or not native_model
        or provider != provider.strip()
        or native_model != native_model.strip()
        or any(character in model for character in "\r\n\x00")
    ):
        raise ServiceError(
            "POLICY_REJECTED",
            "DeepSeek model must be an exact provider::model pair",
        )
    return provider, native_model


def _dsh_env(permission_mode: str) -> dict[str, str]:
    if permission_mode not in {"read-only", "workspace-write"}:
        raise ValueError("unsupported DeepSeek permission mode")
    env = dict(os.environ)
    env.update(
        {
            "DSH_PERMISSION_MODE": permission_mode,
            "DSH_TELEMETRY_DISABLED": "1",
        }
    )
    return env


def _snapshot(
    context: ResolvedContext,
    *,
    session_id: str,
    execution_id: str,
    execution_state: str,
    result_text: str | None = None,
    error: AdapterFailure | None = None,
) -> AdapterSnapshot:
    conversation_state = "active" if execution_state == "running" else "idle"
    return AdapterSnapshot(
        external_session_id=session_id,
        external_execution_id=execution_id,
        conversation_state=conversation_state,
        execution_state=execution_state,
        effective_model=context.effective_model,
        effective_reasoning=context.effective_reasoning,
        workspace_path=context.workspace_path,
        workspace_key=context.workspace_key,
        context_hash=context.context_hash,
        result_text=result_text,
        error=error,
        evidence={
            "source": "deepseek-harness-native-acp",
            "connection_owned_session": True,
        },
    )


def _spawn_prompt(request: AdapterSpawnRequest) -> str:
    task = request.task
    lines = [
        f"Role: {task.role}",
        f"Task: {task.title}",
        task.prompt,
        "Acceptance criteria:",
        *(f"- {item}" for item in task.acceptance_criteria),
    ]
    if task.authority:
        lines.extend(("Authority:", *(f"- {item}" for item in task.authority)))
    lines.append(
        "Return only the final result in at most 500 words; omit progress narration and hidden reasoning."
    )
    return "\n".join(lines)


def _send_prompt(request: AdapterSendRequest) -> str:
    lines = [request.prompt]
    if request.reply_to is not None:
        lines.append(f"Reply to: {request.reply_to}")
    if request.answers:
        lines.append(
            "Answers: "
            + json.dumps(request.answers, sort_keys=True, separators=(",", ":"))
        )
    lines.append(
        "Return only the final result in at most 500 words; omit progress narration and hidden reasoning."
    )
    return "\n".join(lines)


def _bounded_result(text: str) -> str:
    result = text.strip() or "DeepSeek Harness task completed."
    if len(result) <= CONTROLLER_RESULT_MAX_CHARS:
        return result
    keep = CONTROLLER_RESULT_MAX_CHARS - len(_TRUNCATION_MARKER)
    return result[:keep] + _TRUNCATION_MARKER


def _require_execution(
    request: AdapterSessionRequest, snapshot: AdapterSnapshot
) -> None:
    if (
        request.external_execution_id is not None
        and request.external_execution_id != snapshot.external_execution_id
    ):
        raise ServiceError("CONTEXT_DRIFT", "DeepSeek ACP execution identity changed")


def _reject_permission(params: Mapping[str, Any]) -> Mapping[str, Any]:
    options = params.get("options")
    if isinstance(options, list):
        for option in options:
            if (
                isinstance(option, Mapping)
                and option.get("kind") in {"reject_once", "reject_always"}
                and isinstance(option.get("optionId"), str)
            ):
                return {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": option["optionId"],
                    }
                }
    return {"outcome": {"outcome": "cancelled"}}


def _acp_error_message(error: object) -> str:
    details: list[str] = []
    if isinstance(error, Mapping):
        for key in ("message", "detail"):
            value = error.get(key)
            if isinstance(value, str) and value:
                details.append(value)
        data = error.get("data")
        if isinstance(data, Mapping):
            for key in ("message", "detail", "error"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    details.append(value)
        elif isinstance(data, str) and data:
            details.append(data)
    detail = ": ".join(details)
    return detail[:2_048] if detail else "ACP request was rejected"


def _node_path() -> Path | None:
    override = os.environ.get("SUBAGENT_MCP_DSH_NODE")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    discovered = shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))
    for variable in ("ProgramFiles", "ProgramW6432"):
        program_files = os.environ.get(variable)
        if program_files:
            candidates.append(Path(program_files) / "nodejs" / "node.exe")
    system_drive = os.environ.get("SystemDrive")
    if system_drive:
        drive_root = Path(f"{system_drive}\\" if system_drive.endswith(":") else system_drive)
        candidates.append(drive_root / "Program Files" / "nodejs" / "node.exe")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def _source_root() -> Path | None:
    override = os.environ.get("SUBAGENT_MCP_DSH_SOURCE_ROOT")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    acp_override = os.environ.get("SUBAGENT_MCP_DSH_ACP_BIN")
    if acp_override:
        binary = Path(acp_override)
        try:
            candidates.append(binary.resolve(strict=True).parents[4])
        except (OSError, IndexError):
            pass
    package_link = (
        Path.home()
        / ".dsh"
        / "profiles"
        / "node_modules"
        / "@deepseek-ai"
        / "dsh"
    )
    try:
        target = package_link.resolve(strict=True)
        candidates.extend((target.parents[1], target.parents[2]))
    except (OSError, IndexError):
        pass
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if (resolved / "packages" / "examples" / "acp-demo" / "lib" / "bin.js").is_file():
                return resolved
        except OSError:
            continue
    return None


def _package_entries(source_root: Path) -> dict[str, Path] | None:
    wanted = {package_name: key for key, package_name in _PACKAGES.items()}
    found: dict[str, Path] = {}
    try:
        packages_root = source_root / "packages"
        for directory, child_names, file_names in os.walk(packages_root):
            child_names[:] = [name for name in child_names if name != "node_modules"]
            if "package.json" not in file_names:
                continue
            manifest = Path(directory) / "package.json"
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            key = wanted.get(data.get("name"))
            if key is None:
                continue
            main = data.get("main", "lib/index.js")
            if not isinstance(main, str):
                return None
            entry = manifest.parent / main
            if not entry.is_file():
                return None
            found[key] = entry
            if len(found) == len(wanted):
                return found
    except OSError:
        return None
    return None


def _file_identity(path: Path) -> Mapping[str, Any]:
    stat = path.stat()
    return {
        "path": os.path.normcase(str(path)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


async def _best_effort_close(client: _AcpClient, timeout: float) -> None:
    try:
        await asyncio.wait_for(client.close(), timeout=timeout)
    except BaseException:
        pass
