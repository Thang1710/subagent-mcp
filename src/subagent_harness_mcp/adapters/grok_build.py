"""Grok Build binding and strict pre-prompt context policy."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .. import __version__
from ..contracts import (
    ADAPTER_API_VERSION,
    PROMPT_MAX_BYTES,
    TASK_INPUT_MAX_BYTES,
    TASK_INPUT_MAX_FILES,
    AdapterManifest,
    ContractError,
    ServiceError,
    validate_model_id,
)
from .acp_stdio import (
    AcpFatalCallbackError,
    AcpMethodNotFoundError,
    AcpProcessError,
    AcpProtocolError,
    AcpRpcError,
    AcpStdioProcess,
    NotificationHandler,
    ReverseRequestHandler,
)
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


RUNTIME_ID = "grok-build"
TRANSPORT = "native-acp"
DEFAULT_BINDING_PROBE_TIMEOUT_SECONDS = 15.0
DEFAULT_INSPECT_TIMEOUT_SECONDS = 15.0
_MAX_COMMAND_BYTES = 1024 * 1024
_MAX_CATALOG_ITEMS = 128
_ADAPTER_VERSION = "1.0.0"
_CLEANUP_TIMEOUT_SECONDS = 2.0
_DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 15.0
_DEFAULT_CANCEL_TIMEOUT_SECONDS = 5.0
_MAX_PUBLIC_RESULT_CHARS = 65_536
_RESULT_TRUNCATION_MARKER = "\n[truncated by Subagent MCP]"
_VERSION = re.compile(r"^grok \d{1,16}\.\d{1,16}\.\d{1,16} \([0-9a-f]{7,64}\)$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._:/@+-]{0,127}$")
_REQUIRED_HELP_TOKENS = (
    "Usage: grok [OPTIONS] [PROMPT] [COMMAND]",
    "--no-auto-update",
    "--cwd",
    "--model",
    "--reasoning-effort",
    "--permission-mode",
    "--disable-web-search",
    "--no-subagents",
    "Usage: grok agent [OPTIONS] [COMMAND]",
    "--no-leader",
    "Usage: grok agent stdio [OPTIONS]",
)
_CHILD_ENV_NAMES = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PUBLIC",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "COLORTERM",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "TERM",
        "TZ",
    }
)
_CAPABILITY_GAPS = (
    "terminal",
    "run_tests",
    "git_read",
    "network",
    "browser",
    "web_search",
    "declared_mcp",
    "plugins",
    "hooks",
    "nested_agents",
    "worktree_create",
    "resume_after_restart",
    "provider_quota_evidence",
    "interactive_input",
    "windows_os_sandbox",
)
_EXTENSION_KINDS = frozenset({"mcp", "hook", "plugin", "compatibility_mcp"})
_MAX_FILESYSTEM_PATH_BYTES = 4096
_MAX_FILESYSTEM_FILE_BYTES = 1024 * 1024
_MAX_FILESYSTEM_RESULT_BYTES = 1_000_000
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "AUX",
        "CLOCK$",
        "CON",
        "CONIN$",
        "CONOUT$",
        "NUL",
        "PRN",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
        *(f"COM{index}" for index in "¹²³"),
        *(f"LPT{index}" for index in "¹²³"),
    }
)
_DENIED_REVERSE_PREFIXES = (
    "browser/",
    "fs/",
    "mcp/",
    "network/",
    "process/",
    "terminal/",
)
_EXPLICIT_QUOTA_CODES = frozenset(
    {
        "billing_limit_reached",
        "credit_exhausted",
        "credits_exhausted",
        "quota_exhausted",
        "usage_limit_reached",
    }
)
_EXPLICIT_AUTH_CODES = frozenset(
    {
        "auth_required",
        "authentication_required",
        "invalid_token",
        "unauthorized",
    }
)
_EXPLICIT_MODEL_CODES = frozenset(
    {"model_not_found", "model_removed", "model_unavailable", "route_not_found"}
)
_EXPLICIT_PERMISSION_CODES = frozenset(
    {"permission_denied", "policy_rejected"}
)


class GrokBindingIncompatible(RuntimeError):
    """The executable exists but its public contract is not recognized."""


class GrokBindingTimeout(TimeoutError):
    """A bounded public CLI operation did not complete."""


class GrokPermissionError(PermissionError):
    """A reverse filesystem request exceeded the declared semantic policy."""


class GrokFilesystemCleanupError(GrokPermissionError):
    """An owned filesystem resource could not be cleaned up unambiguously."""

    def __init__(
        self,
        *,
        original_error: BaseException | None,
        cleanup_error: BaseException,
    ) -> None:
        super().__init__("Grok filesystem cleanup is ambiguous")
        self.original_error = original_error
        self.cleanup_error = cleanup_error


@dataclass(frozen=True, slots=True)
class _GrokWriteRoot:
    parts: tuple[str, ...]
    folded_parts: tuple[str, ...]
    is_directory: bool
    anchor_path: Path
    anchor_identity: tuple[int, int]


class GrokFilesystemBridge:
    """Enforce bounded ACP text-file access inside one canonical workspace."""

    def __init__(
        self,
        *,
        workspace: str | os.PathLike[str],
        permission_mode: str,
        write_roots: Sequence[str],
        max_file_bytes: int = _MAX_FILESYSTEM_FILE_BYTES,
    ) -> None:
        if permission_mode not in {"repo-read", "workspace-write"}:
            raise GrokPermissionError("Grok filesystem permission mode is invalid")
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or not 1 <= max_file_bytes <= _MAX_FILESYSTEM_FILE_BYTES
        ):
            raise GrokPermissionError("Grok filesystem size limit is invalid")
        try:
            canonical_workspace = Path(workspace).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GrokPermissionError("Grok workspace is unavailable") from exc
        if not canonical_workspace.is_dir():
            raise GrokPermissionError("Grok workspace is unavailable")
        if permission_mode == "repo-read" and write_roots:
            raise GrokPermissionError("Review mode cannot declare write roots")
        if permission_mode == "workspace-write" and not 1 <= len(write_roots) <= 32:
            raise GrokPermissionError("Writer mode requires one to thirty-two roots")

        self._workspace = canonical_workspace
        self._permission_mode = permission_mode
        self._max_file_bytes = max_file_bytes
        self._write_lock = asyncio.Lock()
        self._write_worker: asyncio.Task[Mapping[str, object]] | None = None
        self._write_cancel: threading.Event | None = None
        roots = tuple(self._build_write_root(root) for root in write_roots)
        for index, root in enumerate(roots):
            for other in roots[index + 1 :]:
                if _parts_overlap(root.folded_parts, other.folded_parts):
                    raise GrokPermissionError("Grok write roots must not overlap")
        self._write_roots = roots

    async def read_text_file(
        self, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        path = _filesystem_params(params, {"path"})["path"]
        return await asyncio.to_thread(self._read_text_file, path)

    async def write_text_file(
        self, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        if self._permission_mode != "workspace-write":
            raise GrokPermissionError("Grok filesystem write is not authorized")
        values = _filesystem_params(params, {"path", "content"})
        content = values["content"]
        if len(content) > self._max_file_bytes:
            raise GrokPermissionError("Grok filesystem content is too large")
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise GrokPermissionError("Grok filesystem content is not UTF-8") from exc
        if len(encoded) > self._max_file_bytes:
            raise GrokPermissionError("Grok filesystem content is too large")
        async with self._write_lock:
            cancel_event = threading.Event()
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._write_text_file,
                    values["path"],
                    encoded,
                    cancel_event,
                )
            )
            self._write_worker = worker
            self._write_cancel = cancel_event
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel_event.set()
                try:
                    await asyncio.shield(worker)
                except GrokFilesystemCleanupError:
                    raise
                except (GrokPermissionError, OSError, RuntimeError):
                    pass
                raise
            finally:
                if worker.done():
                    self._write_worker = None
                    self._write_cancel = None

    async def handle_reverse_request(
        self,
        method: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        if method == "fs/read_text_file":
            return await self.read_text_file(params)
        if method == "fs/write_text_file":
            try:
                return await self.write_text_file(params)
            except GrokFilesystemCleanupError as exc:
                raise AcpFatalCallbackError(
                    "ACP filesystem cleanup ambiguity"
                ) from exc
        if method == "session/request_permission" or method.startswith(
            _DENIED_REVERSE_PREFIXES
        ):
            raise GrokPermissionError("Grok reverse method is not authorized")
        raise AcpMethodNotFoundError(method)

    def _build_write_root(self, value: object) -> _GrokWriteRoot:
        if value == ".":
            return _GrokWriteRoot(
                (),
                (),
                True,
                self._workspace,
                _filesystem_identity(self._workspace),
            )
        parts = _windows_relative_parts(value)
        candidate = self._workspace.joinpath(*parts)
        parent_parts = parts[:-1]
        try:
            _reject_reparse_chain(self._workspace, parts)
            if candidate.exists():
                canonical = candidate.resolve(strict=True)
                if not _windows_contains(canonical, self._workspace):
                    raise GrokPermissionError("Grok write root escapes the workspace")
                if candidate.is_dir():
                    return _GrokWriteRoot(
                        parts,
                        _fold_parts(parts),
                        True,
                        canonical,
                        _filesystem_identity(canonical),
                    )
                if not candidate.is_file():
                    raise GrokPermissionError("Grok write root is not a file or directory")
            parent = _resolve_existing_directory(self._workspace, parent_parts)
        except (OSError, RuntimeError) as exc:
            raise GrokPermissionError("Grok write root is unavailable") from exc
        return _GrokWriteRoot(
            parts,
            _fold_parts(parts),
            False,
            parent,
            _filesystem_identity(parent),
        )

    def _read_text_file(self, value: str) -> Mapping[str, object]:
        parts = _windows_relative_parts(value)
        target = _resolve_existing_file(self._workspace, parts)
        expected = _filesystem_identity(target)
        try:
            with target.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    (opened.st_dev, opened.st_ino) != expected
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                ):
                    raise GrokPermissionError("Grok filesystem target changed")
                if opened.st_size > self._max_file_bytes:
                    raise GrokPermissionError("Grok filesystem file is too large")
                data = stream.read(self._max_file_bytes + 1)
        except GrokPermissionError:
            raise
        except OSError as exc:
            raise GrokPermissionError("Grok filesystem read failed") from exc
        if len(data) > self._max_file_bytes:
            raise GrokPermissionError("Grok filesystem file is too large")
        try:
            content = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GrokPermissionError("Grok filesystem file is not UTF-8") from exc
        if len(_canonical_json({"content": content})) > _MAX_FILESYSTEM_RESULT_BYTES:
            raise GrokPermissionError("Grok filesystem result is too large")
        return {"content": content}

    def _write_text_file(
        self,
        value: str,
        data: bytes,
        cancel_event: threading.Event,
    ) -> Mapping[str, object]:
        parts = _windows_relative_parts(value)
        root = self._authorized_root(parts)
        parent = _resolve_existing_directory(self._workspace, parts[:-1])
        self._validate_root_anchor(root)
        parent_identity = _filesystem_identity(parent)
        target = parent / parts[-1]
        before_identity: tuple[int, int] | None
        if target.exists():
            existing = _resolve_existing_file(self._workspace, parts)
            before_identity = _filesystem_identity(existing)
            target = existing
        else:
            _reject_reparse_chain(self._workspace, parts)
            before_identity = None

        descriptor = -1
        temp_path: Path | None = None
        operation_error: BaseException | None = None
        try:
            descriptor, raw_temp = tempfile.mkstemp(
                prefix=f".{target.name}.subagent-mcp-",
                suffix=".tmp",
                dir=parent,
            )
            temp_path = Path(raw_temp)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if cancel_event.is_set():
                raise GrokPermissionError("Grok filesystem write was cancelled")
            self._revalidate_before_replace(
                root,
                parts,
                parent,
                parent_identity,
                before_identity,
            )
            if cancel_event.is_set():
                raise GrokPermissionError("Grok filesystem write was cancelled")
            os.replace(temp_path, target)
            temp_path = None
        except (GrokPermissionError, OSError, RuntimeError) as exc:
            operation_error = exc

        cleanup_error: BaseException | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = exc
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            raise GrokFilesystemCleanupError(
                original_error=operation_error,
                cleanup_error=cleanup_error,
            ) from operation_error or cleanup_error
        if operation_error is not None:
            if isinstance(operation_error, GrokPermissionError):
                raise operation_error
            raise GrokPermissionError("Grok filesystem write failed") from operation_error
        return {"written": True}

    def _authorized_root(self, parts: tuple[str, ...]) -> _GrokWriteRoot:
        folded = _fold_parts(parts)
        for root in self._write_roots:
            if folded == root.folded_parts or (
                root.is_directory
                and len(folded) > len(root.folded_parts)
                and folded[: len(root.folded_parts)] == root.folded_parts
            ):
                return root
        raise GrokPermissionError("Grok filesystem write is outside the write set")

    def _validate_root_anchor(self, root: _GrokWriteRoot) -> None:
        try:
            current = root.anchor_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GrokPermissionError("Grok write root changed") from exc
        if _filesystem_identity(current) != root.anchor_identity:
            raise GrokPermissionError("Grok write root changed")
        if root.is_directory and not current.is_dir():
            raise GrokPermissionError("Grok write root changed")

    def _revalidate_before_replace(
        self,
        root: _GrokWriteRoot,
        parts: tuple[str, ...],
        parent: Path,
        parent_identity: tuple[int, int],
        before_identity: tuple[int, int] | None,
    ) -> None:
        self._validate_root_anchor(root)
        current_parent = _resolve_existing_directory(self._workspace, parts[:-1])
        if (
            current_parent != parent
            or _filesystem_identity(current_parent) != parent_identity
        ):
            raise GrokPermissionError("Grok filesystem parent changed")
        target = current_parent / parts[-1]
        if before_identity is None:
            if target.exists() or target.is_symlink():
                raise GrokPermissionError("Grok filesystem target changed")
            _reject_reparse_chain(self._workspace, parts)
            return
        current_target = _resolve_existing_file(self._workspace, parts)
        if _filesystem_identity(current_target) != before_identity:
            raise GrokPermissionError("Grok filesystem target changed")


@dataclass(frozen=True, slots=True)
class GrokCliContract:
    version: str
    help_text: str


@dataclass(frozen=True, slots=True)
class GrokBinding:
    executable_path: Path
    version: str
    executable_sha256: str
    capability_hash: str
    pair_key: str
    file_identity: tuple[int, int, int] = ()


@dataclass(frozen=True, slots=True)
class GrokInspectObservation:
    pair_key: str
    workspace_path: str
    mcp_servers: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    compatibility_mcp_servers: tuple[str, ...] = ()
    builtin_tool_inventory: str = "not_exposed"
    permission_keys: tuple[str, ...] = ()
    permission_rules: tuple[str, ...] = ()
    permission_modes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GrokSessionToolAttestation:
    pair_key: str
    external_session_id: str
    workspace_key: str
    mode: str
    builtin_tool_names: tuple[str, ...]
    permission_routes: tuple[tuple[str, str], ...]
    workspace_path: str = ""
    effective_model: str = ""
    reasoning_effort: str = ""
    auth_method: str = ""
    api_key_override: bool | None = None
    custom_paid_route: bool | None = None
    no_extra_spend: bool | None = None
    loaded_executable_extensions: tuple[tuple[str, str], ...] | None = None
    disabled_executable_extensions: tuple[tuple[str, str], ...] | None = None
    web_search_enabled: bool | None = None
    nested_agents_enabled: bool | None = None
    terminal_enabled: bool | None = None
    quota_state: str = "unknown"


@dataclass(frozen=True, slots=True)
class GrokLaunch:
    binding: GrokBinding
    workspace_path: str
    workspace_key: str
    model: str
    reasoning_effort: str
    permission_mode: str
    write_roots: tuple[str, ...]
    argv: tuple[str, ...]
    env: Mapping[str, str]


AcpProcessFactory = Callable[
    [GrokLaunch, ReverseRequestHandler, NotificationHandler],
    AcpStdioProcess,
]


@dataclass(slots=True)
class _GrokPublicText:
    session_id: str | None = None
    parts: list[str] = field(default_factory=list)
    chars: int = 0
    truncated: bool = False

    def reset(self) -> None:
        self.parts.clear()
        self.chars = 0
        self.truncated = False

    async def handle(
        self,
        method: str,
        params: Mapping[str, object],
    ) -> None:
        if (
            method != "session/update"
            or params.get("sessionId") != self.session_id
        ):
            return
        update = params.get("update")
        if (
            not isinstance(update, Mapping)
            or update.get("sessionUpdate") != "agent_message_chunk"
        ):
            return
        content = update.get("content")
        if (
            not isinstance(content, Mapping)
            or content.get("type") != "text"
            or not isinstance(content.get("text"), str)
        ):
            return
        text = str(content["text"])
        if self.truncated or not text:
            return
        remaining = (
            _MAX_PUBLIC_RESULT_CHARS
            - len(_RESULT_TRUNCATION_MARKER)
            - self.chars
        )
        if remaining <= 0:
            self.parts.append(_RESULT_TRUNCATION_MARKER)
            self.truncated = True
            return
        self.parts.append(text[:remaining])
        self.chars += min(len(text), remaining)
        if len(text) > remaining:
            self.parts.append(_RESULT_TRUNCATION_MARKER)
            self.truncated = True

    def result(self) -> str:
        return "".join(self.parts).strip() or "Grok Build task completed."


@dataclass(slots=True)
class _GrokTurn:
    execution_id: str
    task: asyncio.Task[None]
    cancel_sent: bool = False


@dataclass(slots=True)
class _GrokSession:
    conversation_id: str
    context: ResolvedContext
    process: AcpStdioProcess
    bridge: GrokFilesystemBridge
    public_text: _GrokPublicText
    snapshot: AdapterSnapshot
    turn: _GrokTurn | None = None
    native_closed: bool = False
    closing: bool = False
    interrupting: bool = False
    interrupt_done: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


BindingLocator = Callable[[], GrokBinding | None]
CatalogReader = Callable[[GrokBinding], object]
InspectReader = Callable[[GrokBinding, str], GrokInspectObservation | None]
ExecutableResolver = Callable[[str], str | None]
ContractReader = Callable[[Path], GrokCliContract]


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflow: bool = False
    cancelled: bool = False
    read_failed: bool = False


_COMMAND_CANCEL_EVENT: contextvars.ContextVar[threading.Event | None] = (
    contextvars.ContextVar("grok_command_cancel_event", default=None)
)


class GrokBuildAdapter:
    """Resolve a Grok child policy without starting an ACP or provider turn."""

    def __init__(
        self,
        *,
        binding_locator: BindingLocator | None = None,
        catalog_reader: CatalogReader | None = None,
        inspect_reader: InspectReader | None = None,
        platform: str | None = None,
        environment: Mapping[str, str] | None = None,
        binding_probe_timeout_seconds: float = DEFAULT_BINDING_PROBE_TIMEOUT_SECONDS,
        inspect_timeout_seconds: float = DEFAULT_INSPECT_TIMEOUT_SECONDS,
        acp_process_factory: AcpProcessFactory | None = None,
        handshake_timeout_seconds: float = _DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
        cancel_timeout_seconds: float = _DEFAULT_CANCEL_TIMEOUT_SECONDS,
    ) -> None:
        if min(handshake_timeout_seconds, cancel_timeout_seconds) <= 0:
            raise ValueError("Grok ACP lifecycle timeouts must be positive")
        self._binding_locator = binding_locator or locate_grok_binding
        self._catalog_reader = catalog_reader or _read_grok_model_catalog
        self._inspect_reader = inspect_reader or _read_grok_inspect
        self._platform = platform or sys.platform
        self._environment = dict(os.environ if environment is None else environment)
        self._binding_probe_timeout = binding_probe_timeout_seconds
        self._inspect_timeout = inspect_timeout_seconds
        self._handshake_timeout = handshake_timeout_seconds
        self._cancel_timeout = cancel_timeout_seconds
        self._acp_process_factory = acp_process_factory or self._new_acp_process
        self._last_binding: GrokBinding | None = None
        self._catalog_pair: str | None = None
        self._catalog_cache: tuple[Mapping[str, str], ...] = ()
        self._catalog_authoritative = False
        self._sessions: dict[str, _GrokSession] = {}
        self._conversation_sessions: dict[str, str] = {}
        self._pending_conversations: set[str] = set()
        self._manifest = AdapterManifest(
            adapter_api_version=ADAPTER_API_VERSION,
            runtime_id=RUNTIME_ID,
            provider_id="xai",
            harness_id="grok-build",
            display_name="Grok Build",
            adapter_version=_ADAPTER_VERSION,
            supported_platforms=("win32",),
            supported_transports=(TRANSPORT,),
            capabilities=frozenset({"session", "interrupt", "workspace"}),
            semantic_permissions=frozenset({"repo_read", "workspace_write"}),
            reasoning_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["effort"],
                "properties": {
                    "effort": {"type": "string", "minLength": 1, "maxLength": 64}
                },
            },
            model_schema={
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "Exact bounded Grok Build model ID; catalog-backed when available."
                ),
            },
            max_write_roots_per_session=32,
            write_root_mode="path-prefix",
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def probe(self) -> ProbeResult:
        if self._platform != "win32":
            self._last_binding = None
            return ProbeResult("incompatible", {"code": "PLATFORM_UNSUPPORTED"})
        try:
            binding = await _run_sync_bounded(
                self._binding_locator, timeout=self._binding_probe_timeout
            )
        except (TimeoutError, asyncio.TimeoutError, GrokBindingTimeout):
            self._last_binding = None
            return ProbeResult("recovery_required", {"code": "BINDING_PROBE_TIMEOUT"})
        except (GrokBindingIncompatible, OSError, ValueError):
            self._last_binding = None
            return ProbeResult("incompatible", {"code": "CAPABILITY_MISSING"})
        if binding is None:
            self._last_binding = None
            return ProbeResult("not_installed", {"code": "INSTALL_REQUIRED"})
        try:
            _validate_binding(binding)
        except ValueError:
            self._last_binding = None
            return ProbeResult("incompatible", {"code": "CAPABILITY_MISSING"})
        self._last_binding = binding
        return ProbeResult(
            "needs_canary",
            {
                "pair_key": binding.pair_key,
                "harness_version": binding.version,
                "transport": TRANSPORT,
                "cached_native_login": "not_exposed",
                "no_extra_spend": "not_exposed",
                "builtin_tool_inventory": "not_exposed",
                "provider_readiness": "needs_canary",
                "quota_state": "unknown",
            },
        )

    async def model_catalog(
        self, *, refresh: bool = False
    ) -> tuple[Mapping[str, str], ...]:
        if self._platform != "win32":
            return ()
        try:
            binding = await _run_sync_bounded(
                self._binding_locator, timeout=self._binding_probe_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return ()
        if binding is None:
            return ()
        try:
            _validate_binding(binding)
        except ValueError:
            return ()
        return await self._catalog_for_binding(binding, refresh=refresh)

    async def _catalog_for_binding(
        self, binding: GrokBinding, *, refresh: bool = False
    ) -> tuple[Mapping[str, str], ...]:
        try:
            _assert_bound_identity(binding)
        except (OSError, ValueError):
            self._clear_catalog()
            return ()
        if (
            not refresh
            and self._catalog_pair == binding.pair_key
            and self._catalog_authoritative
        ):
            return self._catalog_cache
        try:
            raw = await _run_sync_bounded(
                self._catalog_reader,
                binding,
                timeout=self._binding_probe_timeout,
            )
            _assert_bound_identity(binding)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._clear_catalog()
            normalized = None
        else:
            normalized = _normalize_catalog(raw)
        if normalized:
            self._catalog_pair = binding.pair_key
            self._catalog_cache = normalized
            self._catalog_authoritative = True
            return normalized
        if self._catalog_pair == binding.pair_key and self._catalog_authoritative:
            return self._catalog_cache
        self._catalog_pair = binding.pair_key
        self._catalog_cache = ()
        self._catalog_authoritative = False
        return ()

    def _clear_catalog(self) -> None:
        self._catalog_pair = None
        self._catalog_cache = ()
        self._catalog_authoritative = False

    async def resolve_context(self, request: AdapterContextRequest) -> ResolvedContext:
        if request.runtime_id != RUNTIME_ID or request.transport != TRANSPORT:
            raise _capability_error("Grok Build native ACP context is required")
        if request.context_policy_id != "declared-native":
            raise _capability_error(
                "Grok Build implements only context_policy_id='declared-native'"
            )
        permissions = _permissions(request.permissions, request.write_set)
        try:
            model = validate_model_id(request.model)
        except ContractError as exc:
            raise ServiceError(
                "POLICY_REJECTED",
                "Grok model ID is invalid",
                category="policy",
            ) from exc
        effort = _reasoning_effort(request.reasoning)
        try:
            workspace = Path(request.workspace_path).resolve(strict=True)
        except OSError as exc:
            raise _capability_error("Grok workspace is unavailable") from exc
        if not workspace.is_dir():
            raise _capability_error("Grok workspace must be an existing directory")
        write_roots = _write_roots(workspace, request.write_set, permissions)
        binding = self._last_binding
        if binding is None:
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok binding was not attested by the current readiness check",
                category="context",
            )
        _validate_binding(binding)
        try:
            _assert_bound_identity(binding)
        except (OSError, ValueError) as exc:
            raise ServiceError(
                "CONTEXT_DRIFT", "Grok executable identity drifted", category="context"
            ) from exc
        if _is_within(binding.executable_path, workspace):
            raise _capability_error("Workspace-local Grok executable is rejected")
        catalog = await self._catalog_for_binding(binding)
        if (
            self._catalog_pair == binding.pair_key
            and self._catalog_authoritative
            and model not in {row["value"] for row in catalog}
        ):
            raise ServiceError(
                "POLICY_REJECTED",
                "Configured Grok model is absent from the current native catalog",
                category="policy",
            )
        try:
            _assert_bound_identity(binding)
            inspect = await _run_sync_bounded(
                self._inspect_reader,
                binding,
                str(workspace),
                timeout=self._inspect_timeout,
            )
            _assert_bound_identity(binding)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _capability_error("Grok inspect evidence is unavailable") from exc
        observed = _validate_inspect(inspect, binding, workspace)
        mode = "writer" if "workspace_write" in permissions else "review"
        payload = {
            "runtime_id": RUNTIME_ID,
            "variant_id": request.variant_id,
            "model": model,
            "reasoning_effort": effort,
            "workspace_path": str(workspace),
            "workspace_key": request.workspace_key,
            "transport": TRANSPORT,
            "permissions": list(permissions),
            "write_set": list(write_roots),
            "mode": mode,
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
            "pair_key": binding.pair_key,
        }
        context_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
        attestation = {
            "source": "grok-build-native-acp-pending-handshake",
            "variant_id": request.variant_id,
            "model": model,
            "reasoning_effort": effort,
            "permissions": permissions,
            "write_set": write_roots,
            "mode": mode,
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
            "pair_key": binding.pair_key,
            "discovered_extensions": observed,
            "inspect_permission_keys": inspect.permission_keys,
            "inspect_permission_rules": inspect.permission_rules,
            "inspect_permission_modes": inspect.permission_modes,
            "cached_native_login": "not_exposed",
            "no_extra_spend": "not_exposed",
            "builtin_tool_inventory": "not_exposed",
            "provider_readiness": "needs_canary",
            "quota_state": "unknown",
        }
        return ResolvedContext(
            runtime_id=RUNTIME_ID,
            requested_model=model,
            effective_model=model,
            requested_reasoning={"effort": effort},
            effective_reasoning={"effort": effort},
            workspace_path=str(workspace),
            workspace_key=request.workspace_key,
            transport=TRANSPORT,
            context_hash=context_hash,
            capability_gaps=_CAPABILITY_GAPS,
            attestation=attestation,
        )

    def launch_for(self, context: ResolvedContext) -> GrokLaunch:
        binding = self._last_binding
        if binding is None or context.runtime_id != RUNTIME_ID or context.transport != TRANSPORT:
            raise ServiceError("CONTEXT_DRIFT", "Grok launch context is not bound")
        if context.attestation.get("pair_key") != binding.pair_key:
            raise ServiceError("CONTEXT_DRIFT", "Grok executable identity drifted")
        effort = _reasoning_effort(context.requested_reasoning)
        write_roots = _text_tuple(context.attestation.get("write_set"), "write set")
        argv = (
            str(binding.executable_path),
            "--no-auto-update",
            "--cwd",
            context.workspace_path,
            "--model",
            context.requested_model,
            "--reasoning-effort",
            effort,
            "--permission-mode",
            "dontAsk",
            "--disable-web-search",
            "--no-subagents",
            "agent",
            "--no-leader",
            "stdio",
        )
        return GrokLaunch(
            binding=binding,
            workspace_path=context.workspace_path,
            workspace_key=context.workspace_key,
            model=context.requested_model,
            reasoning_effort=effort,
            permission_mode="dontAsk",
            write_roots=write_roots,
            argv=argv,
            env=MappingProxyType(_child_env(self._environment)),
        )

    def validate_session_attestation(
        self,
        context: ResolvedContext,
        attestation: GrokSessionToolAttestation,
    ) -> None:
        binding = self._last_binding
        if binding is None or context.attestation.get("pair_key") != binding.pair_key:
            raise _capability_error("Grok session binding is not current")
        mode = context.attestation.get("mode")
        effort = _reasoning_effort(context.requested_reasoning)
        if mode not in {"review", "writer"}:
            raise _capability_error("Grok session mode is invalid")
        if _bounded_public_text(attestation.pair_key, 64) is None:
            raise _capability_error("Grok session pair is invalid")
        if _bounded_public_text(attestation.workspace_key, 4096) is None:
            raise _capability_error("Grok workspace key is invalid")
        if _bounded_public_text(attestation.workspace_path, 4096) is None:
            raise _capability_error("Grok workspace path is invalid")
        if _bounded_public_text(attestation.mode, 16) is None:
            raise _capability_error("Grok session mode is invalid")
        try:
            effective_model = validate_model_id(attestation.effective_model)
        except ContractError as exc:
            raise _capability_error("Grok effective model is invalid") from exc
        if _bounded_public_text(attestation.reasoning_effort, 64) is None:
            raise _capability_error("Grok reasoning evidence is invalid")
        if not isinstance(attestation.quota_state, str):
            raise _capability_error("Grok quota evidence is malformed")
        exact = (
            attestation.pair_key == binding.pair_key
            and _bounded_public_text(attestation.external_session_id, 256) is not None
            and attestation.workspace_key == context.workspace_key
            and _fold_path(attestation.workspace_path) == _fold_path(context.workspace_path)
            and attestation.mode == mode
            and effective_model == context.requested_model
            and attestation.reasoning_effort == effort
        )
        if not exact:
            raise _capability_error("Grok ACP session identity is incomplete or mismatched")
        if not (
            attestation.auth_method == "cached-native"
            and attestation.api_key_override is False
            and attestation.custom_paid_route is False
            and attestation.no_extra_spend is True
        ):
            raise _capability_error("Cached-native auth and no-extra-spend are unproven")
        if attestation.quota_state not in {"unknown", "available"}:
            raise _capability_error("Grok session is not eligible for a provider turn")
        if not (
            attestation.web_search_enabled is False
            and attestation.nested_agents_enabled is False
            and attestation.terminal_enabled is False
        ):
            raise _capability_error("Grok web, nested-agent, or terminal isolation is unproven")
        loaded = _extension_set(attestation.loaded_executable_extensions)
        disabled = _extension_set(attestation.disabled_executable_extensions)
        discovered = _extension_set(context.attestation.get("discovered_extensions"))
        if loaded or not discovered.issubset(disabled):
            raise _capability_error("Grok executable extensions are not proven disabled")
        tools = _bounded_unique_names(attestation.builtin_tool_names, "built-in tools")
        routes = _permission_routes(attestation.permission_routes)
        if set(tools) != set(routes):
            raise _capability_error("Grok built-in tool inventory and routes do not match")
        allowed_routes = (
            {"repo_read"}
            if mode == "review"
            else {"repo_read", "workspace_write_bridge"}
        )
        if any(route not in allowed_routes for route in routes.values()):
            raise _capability_error("Grok exposes an undeclared permission route")
        if "repo_read" not in routes.values():
            raise _capability_error("Grok repository-read route is missing")
        if mode == "writer" and "workspace_write_bridge" not in routes.values():
            raise _capability_error("Grok writer bridge route is missing")

    def _new_acp_process(
        self,
        launch: GrokLaunch,
        request_handler: ReverseRequestHandler,
        notification_handler: NotificationHandler,
    ) -> AcpStdioProcess:
        return AcpStdioProcess(
            argv=launch.argv,
            cwd=launch.workspace_path,
            env=launch.env,
            request_handler=request_handler,
            notification_handler=notification_handler,
            startup_timeout_seconds=self._handshake_timeout,
            request_timeout_seconds=float("inf"),
            close_timeout_seconds=self._cancel_timeout,
            max_line_bytes=_MAX_COMMAND_BYTES,
        )

    async def runtime_canary(self, request: CanaryRequest) -> CanaryResult:
        if request.runtime_id != RUNTIME_ID or request.transport != TRANSPORT:
            return _canary_failure(
                request.pair_key,
                "CAPABILITY_MISSING",
                "Grok canary identity is unsupported",
            )
        try:
            binding = await _run_sync_bounded(
                self._binding_locator, timeout=self._binding_probe_timeout
            )
            if binding is None:
                raise GrokBindingIncompatible("Grok is not installed")
            _validate_binding(binding)
            _assert_bound_identity(binding)
        except asyncio.CancelledError:
            raise
        except BaseException:
            return _canary_failure(
                request.pair_key,
                "RECOVERY_REQUIRED",
                "Grok binding could not be revalidated for canary",
            )
        try:
            variant_pair = _variant_pair_key(
                request.base_pair_key,
                request.model,
                request.reasoning,
                request.transport,
            )
        except ServiceError:
            variant_pair = ""
        if (
            binding.pair_key != request.base_pair_key
            or variant_pair != request.pair_key
        ):
            return _canary_failure(
                request.pair_key,
                "CONTEXT_DRIFT",
                "Grok canary pair identity changed",
            )

        temporary: tempfile.TemporaryDirectory[str] | None = None
        process: AcpStdioProcess | None = None
        result: CanaryResult
        self._last_binding = binding
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix="subagent-mcp-grok-canary-"
            )
            workspace = str(Path(temporary.name).resolve(strict=True))
            workspace_key = hashlib.sha256(workspace.encode("utf-8")).hexdigest()
            context = await self.resolve_context(
                AdapterContextRequest(
                    runtime_id=RUNTIME_ID,
                    variant_id=request.variant_id,
                    model=request.model,
                    reasoning=request.reasoning,
                    workspace_path=workspace,
                    workspace_key=workspace_key,
                    transport=TRANSPORT,
                    permissions=("repo_read",),
                    context_policy_id="declared-native",
                    permission_policy_id="fail-closed",
                )
            )
            (
                process,
                _bridge,
                _public_text,
                _session_id,
                attestation,
            ) = await self._open_native_session(context)
            _assert_bound_identity(binding)
            result = CanaryResult(
                True,
                request.pair_key,
                {
                    "model": request.model,
                    "effort": _reasoning_effort(request.reasoning),
                    "is_using_overage": False,
                    "overage_blocked": True,
                    "cleanup_confirmed": False,
                    "quota_state": attestation.quota_state,
                },
            )
        except asyncio.CancelledError:
            if temporary is not None:
                temporary.cleanup()
            raise
        except ServiceError as exc:
            result = CanaryResult(
                False,
                request.pair_key,
                {},
                AdapterFailure(
                    exc.code,
                    exc.category,
                    exc.retryable,
                    str(exc),
                    exc.next_action,
                ),
            )
        except BaseException:
            result = _canary_failure(
                request.pair_key,
                "RECOVERY_REQUIRED",
                "Grok canary handshake did not complete unambiguously",
            )

        cleanup_error: BaseException | None = None
        if process is not None:
            cleanup_error = await self._close_failed_start(process)
        if temporary is not None:
            try:
                temporary.cleanup()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            return _canary_failure(
                request.pair_key,
                "RECOVERY_REQUIRED",
                "Grok canary cleanup was not confirmed",
            )
        if not result.passed:
            return result
        return CanaryResult(
            True,
            request.pair_key,
            {
                "model": request.model,
                "effort": _reasoning_effort(request.reasoning),
                "is_using_overage": False,
                "overage_blocked": True,
                "cleanup_confirmed": True,
            },
        )

    async def spawn(self, request: AdapterSpawnRequest) -> AdapterSnapshot:
        prompt = _spawn_prompt(request)
        conversation_id = request.conversation_id
        if (
            conversation_id in self._conversation_sessions
            or conversation_id in self._pending_conversations
        ):
            raise ServiceError(
                "SESSION_BUSY",
                "Grok Build conversation already owns a native session",
            )
        self._pending_conversations.add(conversation_id)
        try:
            return await self._spawn_once(request, prompt)
        finally:
            self._pending_conversations.discard(conversation_id)

    async def _spawn_once(
        self, request: AdapterSpawnRequest, prompt: str
    ) -> AdapterSnapshot:
        (
            process,
            bridge,
            public_text,
            session_id,
            attestation,
        ) = await self._open_native_session(request.context)

        if session_id in self._sessions:
            cleanup_error = await self._close_failed_start(process)
            if cleanup_error is not None:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Grok ACP duplicate-session cleanup was not confirmed",
                    category="adapter",
                ) from cleanup_error
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok ACP reused a live native session identity",
                category="context",
            )
        public_text.session_id = session_id
        snapshot = _grok_snapshot(
            request.context,
            session_id=session_id,
            execution_id=request.execution_id,
            execution_state="running",
            quota_state=attestation.quota_state,
        )
        session = _GrokSession(
            request.conversation_id,
            request.context,
            process,
            bridge,
            public_text,
            snapshot,
        )
        self._sessions[session_id] = session
        self._conversation_sessions[request.conversation_id] = session_id
        self._start_turn(session, request.execution_id, prompt)
        return snapshot

    async def _open_native_session(
        self, context: ResolvedContext
    ) -> tuple[
        AcpStdioProcess,
        GrokFilesystemBridge,
        _GrokPublicText,
        str,
        GrokSessionToolAttestation,
    ]:
        launch = self._bound_launch(context)
        mode = context.attestation.get("mode")
        if mode not in {"review", "writer"}:
            raise ServiceError("CONTEXT_DRIFT", "Grok session mode changed")
        try:
            bridge = GrokFilesystemBridge(
                workspace=context.workspace_path,
                permission_mode=(
                    "workspace-write" if mode == "writer" else "repo-read"
                ),
                write_roots=launch.write_roots,
            )
        except GrokPermissionError as exc:
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok filesystem authority changed before launch",
                category="context",
            ) from exc
        public_text = _GrokPublicText()
        try:
            process = self._acp_process_factory(
                launch,
                bridge.handle_reverse_request,
                public_text.handle,
            )
        except Exception as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok ACP child could not be constructed",
                category="adapter",
            ) from exc
        try:
            await process.start()
            initialize = await process.request(
                "initialize",
                _initialize_params(writer=mode == "writer"),
                timeout_seconds=self._handshake_timeout,
            )
            _validate_initialize_response(initialize)
            await asyncio.wait_for(
                process.notify("initialized", {}),
                timeout=self._handshake_timeout,
            )
            auth = await process.request(
                "authenticate",
                {"methodId": "cached_token"},
                timeout_seconds=self._handshake_timeout,
            )
            session_result = await process.request(
                "session/new",
                {"cwd": context.workspace_path, "mcpServers": []},
                timeout_seconds=self._handshake_timeout,
            )
            session_id, attestation = _parse_session_handshake(
                context,
                launch.binding,
                auth,
                session_result,
            )
            self.validate_session_attestation(context, attestation)
        except asyncio.CancelledError:
            cleanup_error = await self._close_failed_start(process)
            if cleanup_error is not None:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Grok ACP cancelled startup cleanup was not confirmed",
                    category="adapter",
                ) from cleanup_error
            raise
        except BaseException as exc:
            cleanup_error = await self._close_failed_start(process)
            if cleanup_error is not None:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Grok ACP startup cleanup was not confirmed",
                    category="adapter",
                ) from cleanup_error
            raise _startup_service_error(exc) from exc

        return process, bridge, public_text, session_id, attestation

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        if request.context.context_hash != session.context.context_hash:
            raise ServiceError("CONTEXT_DRIFT", "Grok ACP resumed context changed")
        prompt = _send_prompt(request)
        while True:
            async with session.lock:
                _require_grok_action(request, session)
                if session.closed or session.native_closed or session.closing:
                    raise ServiceError("SESSION_CLOSED", "Grok ACP session is closed")
                if session.interrupting:
                    interrupt_done = session.interrupt_done
                else:
                    if session.turn is not None and not session.turn.task.done():
                        raise ServiceError("SESSION_BUSY", "Grok ACP turn is active")
                    session.snapshot = _grok_snapshot(
                        session.context,
                        session_id=request.external_session_id,
                        execution_id=request.execution_id,
                        execution_state="running",
                        quota_state=str(
                            session.snapshot.evidence.get("quota_state", "unknown")
                        ),
                    )
                    self._start_turn(session, request.execution_id, prompt)
                    return session.snapshot
            await interrupt_done.wait()

    async def snapshot(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        async with session.lock:
            _require_grok_action(request, session)
            if "post_handshake_attestation" in session.snapshot.evidence:
                session.snapshot = replace(
                    session.snapshot,
                    evidence={
                        key: value
                        for key, value in session.snapshot.evidence.items()
                        if key != "post_handshake_attestation"
                    },
                )
            return session.snapshot

    async def interrupt(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        async with session.lock:
            _require_grok_action(request, session)
            turn = session.turn
            if turn is None or turn.task.done():
                return session.snapshot
            session.interrupting = True
            session.interrupt_done.clear()
            if not turn.cancel_sent:
                turn.cancel_sent = True
                try:
                    await asyncio.wait_for(
                        session.process.notify(
                            "session/cancel",
                            {"sessionId": request.external_session_id},
                        ),
                        timeout=self._cancel_timeout,
                    )
                except BaseException as exc:
                    session.interrupting = False
                    session.interrupt_done.set()
                    raise ServiceError(
                        "RECOVERY_REQUIRED",
                        "Grok ACP cancellation request was not confirmed",
                        category="adapter",
                    ) from exc
        try:
            await asyncio.wait_for(
                asyncio.shield(turn.task),
                timeout=self._cancel_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok ACP cancellation did not reach a terminal result",
                category="adapter",
            ) from exc
        finally:
            async with session.lock:
                _require_grok_action(request, session)
                if session.turn is not turn:
                    raise ServiceError(
                        "CONTEXT_DRIFT", "Grok ACP turn identity changed"
                    )
                captured = session.snapshot
                session.interrupting = False
                session.interrupt_done.set()
        return captured

    async def close(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        async with session.lock:
            _require_grok_action(request, session)
            if session.closed:
                return session.snapshot
            if session.closing:
                raise ServiceError("SESSION_BUSY", "Grok ACP cleanup is active")
            session.closing = True
        cleanup_error: BaseException | None = None
        turn = session.turn
        if turn is not None and not turn.task.done():
            try:
                await self.interrupt(request)
            except BaseException as exc:
                cleanup_error = exc
        try:
            await session.process.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        # A cancellation may be ambiguous while owned-process cleanup is still exact.
        # Never route a follow-up into a child that the ACP helper has closed.
        session.native_closed = session.process.closed
        if turn is not None and not turn.task.done():
            turn.task.cancel()
            try:
                await asyncio.wait_for(turn.task, timeout=self._cancel_timeout)
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            async with session.lock:
                session.closing = False
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok ACP process cleanup was not confirmed",
                category="adapter",
            ) from cleanup_error
        async with session.lock:
            if session.snapshot.execution_state == "running":
                session.snapshot = _grok_snapshot(
                    session.context,
                    session_id=request.external_session_id,
                    execution_id=session.snapshot.external_execution_id,
                    execution_state="interrupted",
                    error=AdapterFailure(
                        "INTERRUPTED",
                        "cancelled",
                        False,
                        "Grok ACP turn interrupted during close",
                    ),
                    quota_state=str(
                        session.snapshot.evidence.get("quota_state", "unknown")
                    ),
                )
            session.closed = True
            session.closing = False
            session.native_closed = True
            session.snapshot = _closed_snapshot(session.snapshot)
            return session.snapshot

    async def open_session(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        session = self._sessions.get(request.external_session_id)
        if session is None:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok ACP sessions cannot resume after the MCP server restarts",
                category="adapter",
            )
        _require_grok_execution(request, session.snapshot)
        if session.closed:
            raise ServiceError("SESSION_CLOSED", "Grok ACP session is closed")
        return session.snapshot

    def _bound_launch(self, context: ResolvedContext) -> GrokLaunch:
        launch = self.launch_for(context)
        try:
            _assert_bound_identity(launch.binding)
        except (OSError, ValueError) as exc:
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok executable identity drifted before ACP launch",
                category="context",
            ) from exc
        return launch

    def _session(self, session_id: str) -> _GrokSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok ACP session is not owned by this MCP process",
                category="adapter",
            ) from exc

    def _start_turn(
        self,
        session: _GrokSession,
        execution_id: str,
        prompt: str,
    ) -> None:
        session.public_text.reset()
        task = asyncio.create_task(
            self._run_turn(session, execution_id=execution_id, prompt=prompt)
        )
        session.turn = _GrokTurn(execution_id, task)

    async def _run_turn(
        self,
        session: _GrokSession,
        *,
        execution_id: str,
        prompt: str,
    ) -> None:
        result: Mapping[str, object] | None = None
        failure: AdapterFailure | None = None
        provider_code: str | None = None
        provider_detail: str | None = None
        rpc_code: int | str | None = None
        stop_reason: str | None = None
        try:
            result = await session.process.request(
                "session/prompt",
                {
                    "sessionId": session.snapshot.external_session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            )
        except asyncio.CancelledError:
            raise
        except AcpRpcError as exc:
            rpc_code = exc.code
            failure, provider_code, provider_detail = _failure_from_rpc(exc)
        except (AcpProcessError, AcpProtocolError, TimeoutError):
            failure = AdapterFailure(
                "RECOVERY_REQUIRED",
                "adapter",
                False,
                "Grok ACP turn ended without an unambiguous terminal result",
            )
        except Exception:
            failure = AdapterFailure(
                "RECOVERY_REQUIRED",
                "adapter",
                False,
                "Grok ACP turn handling failed",
            )

        # The wire reader enqueues notifications before resolving the response.
        # The callback is intentionally non-blocking, so one scheduler yield drains
        # the FIFO queue without imposing a model-turn deadline.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        async with session.lock:
            turn = session.turn
            if turn is None or turn.execution_id != execution_id:
                return
            if failure is None:
                assert result is not None
                (
                    stop_reason,
                    failure,
                    provider_code,
                    provider_detail,
                ) = _terminal_result(result, turn)
            if failure is not None:
                state = (
                    "interrupted"
                    if failure.code == "INTERRUPTED"
                    else "cancelled"
                    if failure.code == "CANCELLED"
                    else "failed"
                )
                session.snapshot = _grok_snapshot(
                    session.context,
                    session_id=session.snapshot.external_session_id,
                    execution_id=execution_id,
                    execution_state=state,
                    error=failure,
                    quota_state=str(session.snapshot.evidence.get("quota_state", "unknown")),
                    stop_reason=stop_reason,
                    provider_code=provider_code,
                    rpc_code=rpc_code,
                    provider_detail=provider_detail,
                )
                return
            session.snapshot = _grok_snapshot(
                session.context,
                session_id=session.snapshot.external_session_id,
                execution_id=execution_id,
                execution_state="succeeded",
                result_text=session.public_text.result(),
                quota_state=str(session.snapshot.evidence.get("quota_state", "unknown")),
                stop_reason=stop_reason,
                public_text_truncated=session.public_text.truncated,
            )

    async def _close_failed_start(
        self,
        process: AcpStdioProcess,
    ) -> BaseException | None:
        try:
            await process.close()
        except BaseException as exc:
            return exc
        return None


def _initialize_params(*, writer: bool) -> dict[str, object]:
    return {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": writer}
        },
        "clientInfo": {"name": "subagent-mcp", "version": __version__},
    }


def _validate_initialize_response(value: Mapping[str, object]) -> None:
    if value.get("protocolVersion") != 1:
        raise _capability_error("Grok ACP protocol version is unavailable")
    methods = value.get("authMethods")
    if not isinstance(methods, list) or not 1 <= len(methods) <= 16:
        raise _capability_error("Grok cached-native authentication is unavailable")
    identifiers: list[str] = []
    for item in methods:
        if not isinstance(item, Mapping) or not _safe_name(item.get("id")):
            raise _capability_error("Grok ACP authentication metadata is malformed")
        identifiers.append(str(item["id"]))
    if len(identifiers) != len(set(identifiers)) or "cached_token" not in identifiers:
        raise _capability_error("Grok cached-native authentication is unavailable")


def _parse_session_handshake(
    context: ResolvedContext,
    binding: GrokBinding,
    auth: Mapping[str, object],
    session_result: Mapping[str, object],
) -> tuple[str, GrokSessionToolAttestation]:
    session_id = _bounded_public_text(session_result.get("sessionId"), 256)
    models = session_result.get("models")
    metadata = session_result.get("_meta")
    if (
        session_id is None
        or not isinstance(models, Mapping)
        or not isinstance(metadata, Mapping)
        or not isinstance(metadata.get("subagentMcp"), Mapping)
    ):
        raise _capability_error("Grok ACP session metadata is incomplete")
    session = metadata["subagentMcp"]
    assert isinstance(session, Mapping)
    current_model = models.get("currentModelId")
    try:
        effective_model = validate_model_id(current_model)
    except ContractError as exc:
        raise _capability_error("Grok ACP effective model is invalid") from exc
    authenticated = auth.get("authenticated")
    method_id = auth.get("methodId")
    auth_method = auth.get("authMethod")
    if authenticated is not True or method_id != "cached_token":
        raise _capability_error("Grok cached-native authentication is unproven")
    return session_id, GrokSessionToolAttestation(
        pair_key=_required_public_text(session, "pairKey", 64),
        external_session_id=session_id,
        workspace_key=_required_public_text(session, "workspaceKey", 4096),
        workspace_path=_required_public_text(session, "workspacePath", 4096),
        mode=_required_public_text(session, "mode", 16),
        effective_model=effective_model,
        reasoning_effort=_required_public_text(session, "reasoningEffort", 64),
        builtin_tool_names=_json_name_tuple(session.get("builtinToolNames")),
        permission_routes=_json_pair_tuple(session.get("permissionRoutes")),
        auth_method=str(auth_method) if isinstance(auth_method, str) else "",
        api_key_override=_strict_bool(auth, "apiKeyOverride"),
        custom_paid_route=_strict_bool(auth, "customPaidRoute"),
        no_extra_spend=_strict_bool(auth, "noExtraSpend"),
        loaded_executable_extensions=_json_pair_tuple(
            session.get("loadedExecutableExtensions"),
            allow_empty=True,
        ),
        disabled_executable_extensions=_json_pair_tuple(
            session.get("disabledExecutableExtensions"),
            allow_empty=True,
        ),
        web_search_enabled=_strict_bool(session, "webSearchEnabled"),
        nested_agents_enabled=_strict_bool(session, "nestedAgentsEnabled"),
        terminal_enabled=_strict_bool(session, "terminalEnabled"),
        quota_state=_required_public_text(session, "quotaState", 64),
    )


def _required_public_text(
    value: Mapping[str, object],
    key: str,
    maximum: int,
) -> str:
    result = _bounded_public_text(value.get(key), maximum)
    if result is None:
        raise _capability_error(f"Grok ACP {key} evidence is missing")
    return result


def _strict_bool(value: Mapping[str, object], key: str) -> bool | None:
    item = value.get(key)
    return item if isinstance(item, bool) else None


def _json_name_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise _capability_error("Grok ACP tool inventory is invalid")
    result: list[str] = []
    for item in value:
        if not _safe_name(item):
            raise _capability_error("Grok ACP tool inventory is invalid")
        assert isinstance(item, str)
        result.append(item)
    if len(result) != len(set(result)):
        raise _capability_error("Grok ACP tool inventory is invalid")
    return tuple(result)


def _json_pair_tuple(
    value: object,
    *,
    allow_empty: bool = False,
) -> tuple[tuple[str, str], ...]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or len(value) > 128
    ):
        raise _capability_error("Grok ACP pair evidence is invalid")
    result: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not _safe_name(item[0])
            or not _safe_name(item[1])
        ):
            raise _capability_error("Grok ACP pair evidence is invalid")
        pair = (str(item[0]), str(item[1]))
        if pair in result:
            raise _capability_error("Grok ACP pair evidence is invalid")
        result.append(pair)
    return tuple(result)


def _startup_service_error(error: BaseException) -> ServiceError:
    if isinstance(error, ServiceError):
        return error
    if isinstance(error, AcpRpcError):
        failure, _provider_code, _provider_detail = _failure_from_rpc(error)
        return ServiceError(
            failure.code,
            failure.message,
            category=failure.category,
            retryable=failure.retryable,
            next_action=failure.next_action,
        )
    if isinstance(error, (AcpProcessError, AcpProtocolError, TimeoutError)):
        return ServiceError(
            "RECOVERY_REQUIRED",
            "Grok ACP startup handshake did not complete unambiguously",
            category="adapter",
        )
    return ServiceError(
        "RECOVERY_REQUIRED",
        "Grok ACP startup failed",
        category="adapter",
    )


def _failure_from_rpc(
    error: AcpRpcError,
) -> tuple[AdapterFailure, str | None, str | None]:
    data = error.data
    provider_code: str | None = None
    retryable = False
    detail: str | None = None
    if isinstance(data, Mapping):
        candidate = data.get("providerCode", data.get("code"))
        if _safe_name(candidate):
            provider_code = str(candidate)
        retryable = data.get("retryable") is True
        candidate_detail = _bounded_error_scalar(data.get("detail"), 2048)
        if isinstance(candidate_detail, str):
            detail = candidate_detail
    return (
        _provider_failure(provider_code or "unknown", retryable),
        provider_code,
        detail,
    )


def _terminal_result(
    result: Mapping[str, object],
    turn: _GrokTurn,
) -> tuple[str | None, AdapterFailure | None, str | None, str | None]:
    stop_reason = result.get("stopReason")
    if not _safe_name(stop_reason):
        return (
            None,
            AdapterFailure(
                "RECOVERY_REQUIRED",
                "adapter",
                False,
                "Grok ACP terminal response is malformed",
            ),
            None,
            None,
        )
    reason = str(stop_reason)
    if reason == "end_turn":
        return reason, None, None, None
    if reason == "cancelled":
        return (
            reason,
            AdapterFailure(
                "INTERRUPTED" if turn.cancel_sent else "CANCELLED",
                "cancelled",
                False,
                (
                    "Grok ACP turn interrupted"
                    if turn.cancel_sent
                    else "Grok ACP turn cancelled"
                ),
            ),
            None,
            None,
        )
    error = result.get("error")
    if reason == "error" and isinstance(error, Mapping):
        code = error.get("code")
        provider_code = str(code) if _safe_name(code) else "unknown"
        detail_value = _bounded_error_scalar(error.get("detail"), 2048)
        return (
            reason,
            _provider_failure(provider_code, error.get("retryable") is True),
            provider_code if provider_code != "unknown" else None,
            detail_value if isinstance(detail_value, str) else None,
        )
    return (
        reason,
        AdapterFailure(
            "PROVIDER_ERROR",
            "provider",
            False,
            "Grok ACP returned an unsupported terminal result",
        ),
        None,
        None,
    )


def _provider_failure(provider_code: str, retryable: bool) -> AdapterFailure:
    if provider_code in _EXPLICIT_QUOTA_CODES:
        return AdapterFailure(
            "QUOTA_PAUSED",
            "quota",
            False,
            "Grok provider explicitly reported quota or credit exhaustion",
            "Refresh current provider availability before a new task; do not buy, reload, or enable credits automatically.",
        )
    if provider_code in _EXPLICIT_AUTH_CODES:
        return AdapterFailure(
            "AUTH_REQUIRED",
            "authentication",
            False,
            "Grok cached-native authentication is unavailable",
            "Sign in through the native Grok harness, then start a fresh task; do not enable an API key automatically.",
        )
    if provider_code in _EXPLICIT_MODEL_CODES:
        return AdapterFailure(
            "CAPABILITY_MISSING",
            "capability",
            False,
            "The exact configured Grok model route is unavailable",
            "Refresh the native catalog or explicitly configure another model; no fallback is automatic.",
        )
    if provider_code in _EXPLICIT_PERMISSION_CODES:
        return AdapterFailure(
            "POLICY_REJECTED",
            "policy",
            False,
            "Grok denied an operation under the declared permission policy",
        )
    return AdapterFailure(
        "PROVIDER_ERROR",
        "provider",
        retryable,
        "Grok provider turn did not complete",
        (
            "Start a new bounded task only through the controller recovery policy; do not switch models or credits automatically."
            if retryable
            else None
        ),
    )


def _grok_snapshot(
    context: ResolvedContext,
    *,
    session_id: str,
    execution_id: str,
    execution_state: str,
    quota_state: str,
    result_text: str | None = None,
    error: AdapterFailure | None = None,
    stop_reason: str | None = None,
    provider_code: str | None = None,
    rpc_code: int | str | None = None,
    public_text_truncated: bool = False,
    provider_detail: str | None = None,
) -> AdapterSnapshot:
    mode = str(context.attestation.get("mode", "unknown"))
    write_set = context.attestation.get("write_set", ())
    evidence: dict[str, object] = {
        "source": "grok-build-native-acp",
        "pair_key": str(context.attestation.get("pair_key", "")),
        "protocol_version": 1,
        "workspace_hash": context.context_hash,
        "permission_mode": mode,
        "write_set_digest": hashlib.sha256(_canonical_json(write_set)).hexdigest(),
        "auth_method": "cached-native",
        "no_extra_spend": True,
        "quota_state": quota_state,
        "connection_owned_session": True,
        "public_text_truncated": public_text_truncated,
    }
    if execution_state == "running":
        evidence["post_handshake_attestation"] = {
            "reasoning_source": "grok-build-native-acp-session",
            "reasoning_binding": [
                str(context.attestation.get("pair_key", "")),
                session_id,
                context.effective_model,
                dict(context.effective_reasoning),
                context.context_hash,
            ],
            "reasoning_provider_reported": True,
        }
    if stop_reason is not None:
        evidence["stop_reason"] = stop_reason
    provider_error: dict[str, object] = {"source": "native-acp"}
    safe_rpc = _bounded_error_scalar(rpc_code, 128)
    safe_code = _bounded_error_scalar(provider_code, 128)
    safe_detail = _bounded_error_scalar(provider_detail, 2048)
    if safe_rpc is not None:
        provider_error["rpc_code"] = safe_rpc
    if safe_code is not None:
        provider_error["provider_code"] = safe_code
    if safe_detail is not None:
        provider_error["detail"] = safe_detail
    if len(provider_error) > 1:
        evidence["provider_error"] = provider_error
    if error is not None and error.code == "RECOVERY_REQUIRED":
        evidence["cleanup_confirmed"] = False
    return AdapterSnapshot(
        external_session_id=session_id,
        external_execution_id=execution_id,
        conversation_state="active" if execution_state == "running" else "idle",
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


def _closed_snapshot(snapshot: AdapterSnapshot) -> AdapterSnapshot:
    return AdapterSnapshot(
        external_session_id=snapshot.external_session_id,
        external_execution_id=snapshot.external_execution_id,
        conversation_state="closed",
        execution_state=snapshot.execution_state,
        effective_model=snapshot.effective_model,
        effective_reasoning=snapshot.effective_reasoning,
        workspace_path=snapshot.workspace_path,
        workspace_key=snapshot.workspace_key,
        context_hash=snapshot.context_hash,
        result_text=snapshot.result_text,
        needs_input=snapshot.needs_input,
        error=snapshot.error,
        evidence={**dict(snapshot.evidence), "cleanup_confirmed": True},
    )


def _require_grok_execution(
    request: AdapterSessionRequest,
    snapshot: AdapterSnapshot,
) -> None:
    if (
        request.external_execution_id is not None
        and request.external_execution_id != snapshot.external_execution_id
    ):
        raise ServiceError("CONTEXT_DRIFT", "Grok ACP execution identity changed")


def _require_grok_action(
    request: AdapterSessionRequest | AdapterSendRequest,
    session: _GrokSession,
) -> None:
    if request.conversation_id != session.conversation_id:
        raise ServiceError("CONTEXT_DRIFT", "Grok conversation identity changed")
    if request.external_session_id != session.snapshot.external_session_id:
        raise ServiceError("CONTEXT_DRIFT", "Grok ACP session identity changed")
    if isinstance(request, AdapterSessionRequest):
        _require_grok_execution(request, session.snapshot)


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
    lines.extend(_authority_prompt_lines(request.context))
    return _bounded_prompt(lines)


def _send_prompt(request: AdapterSendRequest) -> str:
    lines = [request.prompt]
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
    lines.extend(_authority_prompt_lines(request.context))
    return _bounded_prompt(lines)


def _authority_prompt_lines(context: ResolvedContext) -> list[str]:
    raw_inputs = context.attestation.get("input_attestations", ())
    if (
        not isinstance(raw_inputs, (list, tuple))
        or len(raw_inputs) > TASK_INPUT_MAX_FILES
    ):
        raise ServiceError(
            "REQUEST_INVALID",
            "Grok input attestations are invalid",
            category="request",
        )
    lines: list[str] = []
    if raw_inputs:
        lines.append("Trusted input attestations:")
    for item in raw_inputs:
        if not isinstance(item, Mapping):
            raise ServiceError(
                "REQUEST_INVALID",
                "Grok input attestation is invalid",
                category="request",
            )
        path = _bounded_prompt_text(item.get("path"), 2048)
        digest = item.get("sha256")
        byte_count = item.get("byte_count")
        if (
            path is None
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or not 0 <= byte_count <= TASK_INPUT_MAX_BYTES
        ):
            raise ServiceError(
                "REQUEST_INVALID",
                "Grok input attestation is invalid",
                category="request",
            )
        lines.append(f"- path={path}; sha256={digest}; byte_count={byte_count}")
    if context.attestation.get("mode") == "writer":
        roots = context.attestation.get("write_set", ())
        if not isinstance(roots, (list, tuple)) or len(roots) > 32:
            raise ServiceError(
                "REQUEST_INVALID",
                "Grok write authority is invalid",
                category="request",
            )
        lines.append("Verified write set:")
        for root in roots:
            safe = _bounded_prompt_text(root, 4096)
            if safe is None:
                raise ServiceError(
                    "REQUEST_INVALID",
                    "Grok write authority is invalid",
                    category="request",
                )
            lines.append(f"- {safe}")
    return lines


def _bounded_prompt(lines: Sequence[str]) -> str:
    prompt = "\n".join(lines)
    try:
        encoded = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ServiceError(
            "REQUEST_INVALID",
            "Grok prompt is not valid UTF-8",
            category="request",
        ) from exc
    if len(encoded) > PROMPT_MAX_BYTES:
        raise ServiceError(
            "REQUEST_INVALID",
            "Grok prompt exceeds the bounded native limit",
            category="request",
        )
    return prompt


def _bounded_prompt_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _bounded_error_scalar(value: object, limit: int) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return _bounded_prompt_text(value, limit)


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
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok canary variant identity is not canonical",
            category="context",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _canary_failure(
    pair_key: str,
    code: str,
    message: str,
) -> CanaryResult:
    category = "context" if code == "CONTEXT_DRIFT" else "adapter"
    return CanaryResult(
        False,
        pair_key,
        {},
        AdapterFailure(code, category, False, message),
    )


def locate_grok_binding(
    *,
    executable_resolver: ExecutableResolver = shutil.which,
    contract_reader: ContractReader | None = None,
) -> GrokBinding | None:
    """Resolve and content-bind a standalone CLI without changing host state."""

    located = executable_resolver("grok")
    if not located:
        return None
    unresolved = Path(located)
    if not unresolved.is_absolute() or unresolved.suffix.casefold() != ".exe":
        raise GrokBindingIncompatible("Grok must resolve to an absolute native .exe")
    try:
        executable = unresolved.resolve(strict=True)
    except OSError:
        return None
    if not executable.is_file():
        return None
    try:
        cwd = Path.cwd().resolve(strict=True)
        repository = _repository_root(cwd)
        if _is_within(executable, repository):
            raise GrokBindingIncompatible("repository-local Grok executable is rejected")
    except OSError as exc:
        raise GrokBindingIncompatible("Grok executable identity is unavailable") from exc
    identity = _file_identity(executable)
    reader = contract_reader or _read_grok_cli_contract
    contract = reader(executable)
    _validate_cli_contract(contract)
    if _file_identity(executable) != identity:
        raise GrokBindingIncompatible("Grok executable changed during binding")
    capability_hash = hashlib.sha256(contract.help_text.encode("utf-8")).hexdigest()
    pair_key = _grok_pair_key(
        executable,
        identity,
        contract.version,
        capability_hash,
        adapter_version=_ADAPTER_VERSION,
        adapter_api_version=ADAPTER_API_VERSION,
    )
    return GrokBinding(
        executable_path=executable,
        version=contract.version,
        executable_sha256=str(identity["sha256"]),
        capability_hash=capability_hash,
        pair_key=pair_key,
        file_identity=(
            int(identity["device"]),
            int(identity["inode"]),
            int(identity["size"]),
        ),
    )


def _validate_binding(binding: GrokBinding) -> None:
    if not isinstance(binding, GrokBinding):
        raise ValueError("invalid Grok binding")
    if not binding.executable_path.is_absolute():
        raise ValueError("Grok binding path must be absolute")
    if binding.executable_path.suffix.casefold() != ".exe":
        raise ValueError("Grok binding must be a native .exe")
    if not _VERSION.fullmatch(binding.version):
        raise ValueError("Grok binding version is invalid")
    for value in (
        binding.executable_sha256,
        binding.capability_hash,
        binding.pair_key,
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Grok binding digest is invalid")
    if (
        not isinstance(binding.file_identity, tuple)
        or len(binding.file_identity) != 3
        or any(not isinstance(value, int) or value < 0 for value in binding.file_identity)
    ):
        raise ValueError("Grok binding file identity is invalid")
    expected = _grok_pair_key(
        binding.executable_path,
        {
            "path": os.path.normcase(str(binding.executable_path)),
            "device": binding.file_identity[0],
            "inode": binding.file_identity[1],
            "size": binding.file_identity[2],
            "sha256": binding.executable_sha256,
        },
        binding.version,
        binding.capability_hash,
        adapter_version=_ADAPTER_VERSION,
        adapter_api_version=ADAPTER_API_VERSION,
    )
    if expected != binding.pair_key:
        raise ValueError("Grok binding pair is invalid")


def _validate_cli_contract(contract: GrokCliContract) -> None:
    if not isinstance(contract, GrokCliContract) or not _VERSION.fullmatch(contract.version):
        raise GrokBindingIncompatible("Grok version output is not recognized")
    if _bounded_text(contract.help_text, _MAX_COMMAND_BYTES) is None or any(
        token not in contract.help_text for token in _REQUIRED_HELP_TOKENS
    ):
        raise GrokBindingIncompatible("Grok ACP/help contract is incomplete")


def _read_grok_cli_contract(executable: Path) -> GrokCliContract:
    version = _run_grok(executable, ("--version",))
    help_text = "\n".join(
        (
            _run_grok(executable, ("--help",)),
            _run_grok(executable, ("agent", "--help")),
            _run_grok(executable, ("agent", "stdio", "--help")),
        )
    )
    return GrokCliContract(version=version.rstrip("\r\n"), help_text=help_text)


def _read_grok_model_catalog(binding: GrokBinding) -> object:
    return _parse_catalog(_run_grok(binding.executable_path, ("models",)))


def _read_grok_inspect(binding: GrokBinding, workspace_path: str) -> GrokInspectObservation:
    raw = _run_grok(
        binding.executable_path,
        ("--cwd", workspace_path, "inspect", "--json"),
    )
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise GrokBindingIncompatible("Grok inspect JSON is invalid") from exc
    if not isinstance(value, Mapping):
        raise GrokBindingIncompatible("Grok inspect root is invalid")
    return GrokInspectObservation(
        pair_key=binding.pair_key,
        workspace_path=workspace_path,
        mcp_servers=_inspect_names(value, "mcpServers"),
        hooks=_inspect_names(value, "hooks"),
        plugins=_inspect_names(value, "plugins"),
        compatibility_mcp_servers=_inspect_names(
            value, "compatibilityMcpServers", optional=True
        ),
        builtin_tool_inventory="not_exposed",
        permission_keys=_mapping_keys(value.get("permissions")),
        permission_rules=_inspect_names(value, "permissionRules", optional=True),
        permission_modes=_inspect_names(value, "permissionModes", optional=True),
    )


def _run_grok(executable: Path, suffix: tuple[str, ...]) -> str:
    argv = (str(executable), "--no-auto-update", *suffix)
    completed = _run_owned_command(
        argv,
        env=_child_env(os.environ),
        timeout_seconds=DEFAULT_BINDING_PROBE_TIMEOUT_SECONDS,
        cleanup_timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
        cancel_event=_COMMAND_CANCEL_EVENT.get(),
    )
    if completed.timed_out or completed.cancelled:
        raise GrokBindingTimeout("Grok public CLI probe timed out")
    if (
        completed.returncode != 0
        or completed.overflow
        or completed.read_failed
    ):
        raise GrokBindingIncompatible("Grok public CLI probe failed")
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GrokBindingIncompatible("Grok public CLI output is not UTF-8") from exc


class _OutputBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0
        self.overflow = threading.Event()
        self.lock = threading.Lock()

    def accept(self, chunk: bytes) -> bytes:
        with self.lock:
            remaining = self.maximum - self.used
            accepted = chunk[: max(remaining, 0)]
            self.used += len(accepted)
            if len(accepted) != len(chunk):
                self.overflow.set()
            return accepted


def _read_pipe(
    pipe: Any,
    output: bytearray,
    budget: _OutputBudget,
    read_failed: threading.Event,
) -> None:
    try:
        while not budget.overflow.is_set():
            chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            output.extend(budget.accept(chunk))
    except (OSError, ValueError, TypeError):
        read_failed.set()


def _bounded_wait(process: Any, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except BaseException:
        return False


def _stop_owned_child(process: Any, timeout: float) -> None:
    try:
        exited = process.poll() is not None
    except BaseException:
        exited = False
    if exited and _bounded_wait(process, timeout):
        return
    if not exited:
        try:
            process.terminate()
        except BaseException:
            pass
        if _bounded_wait(process, timeout):
            return
        try:
            process.kill()
        except BaseException:
            pass
        if _bounded_wait(process, timeout):
            return
    raise GrokBindingIncompatible("Grok command process cleanup failed")


def _run_owned_command(
    argv: tuple[str, ...],
    *,
    env: Mapping[str, str],
    timeout_seconds: float,
    cleanup_timeout_seconds: float,
    cancel_event: threading.Event | None = None,
    process_factory: Callable[..., Any] = subprocess.Popen,
) -> _CommandResult:
    if not argv or any(not isinstance(part, str) for part in argv):
        raise GrokBindingIncompatible("Grok command arguments are invalid")
    process = process_factory(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        shell=False,
    )
    if process.stdout is None or process.stderr is None:
        _stop_owned_child(process, cleanup_timeout_seconds)
        raise GrokBindingIncompatible("Grok command pipes are unavailable")
    stdout = bytearray()
    stderr = bytearray()
    budget = _OutputBudget(_MAX_COMMAND_BYTES)
    read_failed = threading.Event()
    readers = (
        threading.Thread(
            target=_read_pipe,
            args=(process.stdout, stdout, budget, read_failed),
            daemon=True,
        ),
        threading.Thread(
            target=_read_pipe,
            args=(process.stderr, stderr, budget, read_failed),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    cancelled = False
    try:
        while process.poll() is None:
            cancelled = cancel_event is not None and cancel_event.is_set()
            timed_out = time.monotonic() >= deadline
            if cancelled or timed_out or budget.overflow.is_set() or read_failed.is_set():
                break
            time.sleep(0.005)
    finally:
        cleanup_failed = False
        try:
            _stop_owned_child(process, cleanup_timeout_seconds)
        except BaseException:
            cleanup_failed = True
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except BaseException:
                cleanup_failed = True
        for reader in readers:
            try:
                reader.join(timeout=cleanup_timeout_seconds)
                cleanup_failed = cleanup_failed or reader.is_alive()
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            raise GrokBindingIncompatible("Grok command cleanup failed")
    return _CommandResult(
        returncode=int(process.returncode if process.returncode is not None else -1),
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        timed_out=timed_out,
        overflow=budget.overflow.is_set(),
        cancelled=cancelled,
        read_failed=read_failed.is_set() or any(reader.is_alive() for reader in readers),
    )


async def _run_sync_bounded(
    function: Callable[..., Any],
    *args: object,
    timeout: float,
) -> Any:
    cancel = threading.Event()

    def invoke() -> Any:
        token = _COMMAND_CANCEL_EVENT.set(cancel)
        try:
            return function(*args)
        finally:
            _COMMAND_CANCEL_EVENT.reset(token)

    task = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
        cancel.set()
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


def _normalize_catalog(raw: object) -> tuple[Mapping[str, str], ...] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return None
    if not raw or len(raw) > _MAX_CATALOG_ITEMS:
        return None
    rows: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"value", "label"}:
            return None
        value = item.get("value")
        label = item.get("label")
        if (
            not isinstance(value, str)
            or not _MODEL.fullmatch(value)
            or value in seen
            or _bounded_text(label, 256) is None
            or not str(label).strip()
        ):
            return None
        seen.add(value)
        rows.append(MappingProxyType({"value": value, "label": str(label)}))
    return tuple(rows)


def _parse_catalog(text: str) -> tuple[Mapping[str, str], ...]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line or line.count("\t") > 1:
            return ()
        value, separator, label = line.partition("\t")
        rows.append({"value": value, "label": label if separator else value})
    normalized = _normalize_catalog(rows)
    return normalized or ()


def _validate_inspect(
    value: GrokInspectObservation | None,
    binding: GrokBinding,
    workspace: Path,
) -> tuple[tuple[str, str], ...]:
    if (
        not isinstance(value, GrokInspectObservation)
        or value.pair_key != binding.pair_key
        or _fold_path(value.workspace_path) != _fold_path(str(workspace))
        or value.builtin_tool_inventory != "not_exposed"
    ):
        raise _capability_error("Grok inspect evidence is incomplete or mismatched")
    groups = (
        ("mcp", value.mcp_servers),
        ("hook", value.hooks),
        ("plugin", value.plugins),
        ("compatibility_mcp", value.compatibility_mcp_servers),
    )
    extensions: list[tuple[str, str]] = []
    for kind, names in groups:
        for name in _bounded_unique_names(names, f"{kind} extensions", allow_empty=True):
            extensions.append((kind, name))
    _bounded_unique_names(value.permission_keys, "inspect permission keys", allow_empty=True)
    _bounded_unique_names(value.permission_rules, "inspect permission rules", allow_empty=True)
    _bounded_unique_names(value.permission_modes, "inspect permission modes", allow_empty=True)
    return tuple(sorted(extensions))


def _filesystem_params(
    value: Mapping[str, object], expected: set[str]
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise GrokPermissionError("Grok filesystem request is invalid")
    result: dict[str, str] = {}
    for key in expected:
        item = value.get(key)
        if not isinstance(item, str):
            raise GrokPermissionError("Grok filesystem request is invalid")
        result[key] = item
    return result


def _windows_relative_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise GrokPermissionError("Grok filesystem path is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise GrokPermissionError("Grok filesystem path is invalid") from exc
    if (
        not encoded
        or len(encoded) > _MAX_FILESYSTEM_PATH_BYTES
        or value.startswith(("\\\\", "//"))
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise GrokPermissionError("Grok filesystem path is invalid")
    path = PureWindowsPath(value.replace("/", "\\"))
    if path.is_absolute() or path.drive or path.root:
        raise GrokPermissionError("Grok filesystem path is invalid")
    parts = tuple(path.parts)
    if not parts:
        raise GrokPermissionError("Grok filesystem path is invalid")
    for part in parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if (
            part in {"", ".", ".."}
            or part.endswith((" ", "."))
            or ":" in part
            or stem in _WINDOWS_RESERVED_NAMES
        ):
            raise GrokPermissionError("Grok filesystem path is invalid")
    return parts


def _fold_parts(parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(ntpath.normcase(part) for part in parts)


def _parts_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    common = min(len(first), len(second))
    return first[:common] == second[:common]


def _windows_contains(path: Path, root: Path) -> bool:
    candidate = PureWindowsPath(path.resolve(strict=False))
    boundary = PureWindowsPath(root.resolve(strict=False))
    return candidate == boundary or boundary in candidate.parents


def _filesystem_identity(path: Path) -> tuple[int, int]:
    try:
        details = path.stat()
    except OSError as exc:
        raise GrokPermissionError("Grok filesystem identity is unavailable") from exc
    return int(details.st_dev), int(details.st_ino)


def _is_reparse_point(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GrokPermissionError("Grok filesystem path is unavailable") from exc
    attributes = int(getattr(details, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _reject_reparse_chain(workspace: Path, parts: Sequence[str]) -> None:
    current = workspace
    for part in parts:
        current = current / part
        if _is_reparse_point(current):
            raise GrokPermissionError("Grok filesystem reparse path is denied")
        if not current.exists():
            return


def _resolve_existing_directory(workspace: Path, parts: Sequence[str]) -> Path:
    _reject_reparse_chain(workspace, parts)
    candidate = workspace.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GrokPermissionError("Grok filesystem parent is unavailable") from exc
    if not resolved.is_dir() or not _windows_contains(resolved, workspace):
        raise GrokPermissionError("Grok filesystem parent is outside the workspace")
    return resolved


def _resolve_existing_file(workspace: Path, parts: Sequence[str]) -> Path:
    _reject_reparse_chain(workspace, parts)
    candidate = workspace.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GrokPermissionError("Grok filesystem target is unavailable") from exc
    try:
        details = resolved.stat()
    except OSError as exc:
        raise GrokPermissionError("Grok filesystem target is unavailable") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or not _windows_contains(resolved, workspace)
    ):
        raise GrokPermissionError("Grok filesystem target is outside the workspace")
    return resolved


def _permissions(
    permissions: tuple[str, ...], write_set: tuple[str, ...]
) -> tuple[str, ...]:
    if (
        not permissions
        or len(permissions) != len(set(permissions))
        or set(permissions) - {"repo_read", "workspace_write"}
        or "repo_read" not in permissions
    ):
        raise _capability_error("Grok permission policy is unsupported")
    writer = "workspace_write" in permissions
    if writer != bool(write_set):
        raise _capability_error("Grok writer mode requires an explicit nonempty write set")
    return permissions


def _write_roots(
    workspace: Path,
    roots: tuple[str, ...],
    permissions: tuple[str, ...],
) -> tuple[str, ...]:
    if "workspace_write" not in permissions:
        return ()
    if not 1 <= len(roots) <= 32:
        raise _capability_error("Grok accepts one to thirty-two write roots")
    normalized: list[str] = []
    folded: set[str] = set()
    for root in roots:
        if not isinstance(root, str) or not root or _bounded_text(root, 4096) is None:
            raise _capability_error("Grok write root is invalid")
        candidate = Path(root)
        if candidate.is_absolute():
            raise _capability_error("Grok write roots must be workspace-relative")
        resolved = (workspace / candidate).resolve(strict=False)
        if not _is_within(resolved, workspace):
            raise _capability_error("Grok write root escapes the workspace")
        relative = resolved.relative_to(workspace).as_posix() or "."
        key = relative.casefold()
        if key in folded:
            raise _capability_error("Grok write roots must be unique")
        folded.add(key)
        normalized.append(relative)
    return tuple(normalized)


def _reasoning_effort(reasoning: Mapping[str, Any]) -> str:
    if not isinstance(reasoning, Mapping) or set(reasoning) != {"effort"}:
        raise ServiceError("POLICY_REJECTED", "Grok reasoning requires only effort")
    effort = reasoning.get("effort")
    encoded = _utf8_length(effort)
    if (
        not isinstance(effort, str)
        or not effort
        or encoded is None
        or encoded > 64
        or any(unicodedata.category(character) == "Cc" for character in effort)
    ):
        raise ServiceError("POLICY_REJECTED", "Grok reasoning effort is invalid")
    return effort


def _permission_routes(value: object) -> dict[str, str]:
    if not isinstance(value, tuple) or not value:
        raise _capability_error("Grok permission-route evidence is missing")
    routes: dict[str, str] = {}
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise _capability_error("Grok permission-route evidence is malformed")
        name, route = item
        if not _safe_name(name) or not _safe_name(route) or name in routes:
            raise _capability_error("Grok permission-route evidence is malformed")
        routes[name] = route
    return routes


def _extension_set(value: object) -> set[tuple[str, str]]:
    if not isinstance(value, tuple):
        raise _capability_error("Grok extension isolation evidence is malformed")
    result: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise _capability_error("Grok extension isolation evidence is malformed")
        kind, name = item
        if (
            not isinstance(kind, str)
            or kind not in _EXTENSION_KINDS
            or not _safe_name(name)
            or (kind, name) in result
        ):
            raise _capability_error("Grok extension isolation evidence is malformed")
        result.add((kind, name))
    return result


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise ServiceError("CONTEXT_DRIFT", f"Grok {label} attestation is invalid")
    return value


def _bounded_unique_names(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (not value and not allow_empty) or len(value) > 128:
        raise _capability_error(f"Grok {label} evidence is invalid")
    if any(not _safe_name(item) for item in value) or len(value) != len(set(value)):
        raise _capability_error(f"Grok {label} evidence is invalid")
    return value


def _safe_name(value: object) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _inspect_names(
    value: Mapping[str, Any], key: str, *, optional: bool = False
) -> tuple[str, ...]:
    raw = value.get(key)
    if raw is None and optional:
        return ()
    if not isinstance(raw, list) or len(raw) > 128:
        raise GrokBindingIncompatible(f"Grok inspect {key} is invalid")
    result: list[str] = []
    for item in raw:
        if isinstance(item, str):
            name = item
        elif isinstance(item, Mapping):
            name = item.get("name") or item.get("id")
        else:
            name = None
        if not _safe_name(name) or name in result:
            raise GrokBindingIncompatible(f"Grok inspect {key} is invalid")
        result.append(name)
    return tuple(result)


def _mapping_keys(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or len(value) > 128:
        raise GrokBindingIncompatible("Grok inspect permissions are invalid")
    result = tuple(str(key) for key in value)
    if any(not _safe_name(key) for key in result):
        raise GrokBindingIncompatible("Grok inspect permissions are invalid")
    return result


def _child_env(source: Mapping[str, str]) -> dict[str, str]:
    result = {
        name: value
        for name, value in source.items()
        if name.upper() in _CHILD_ENV_NAMES and isinstance(value, str)
    }
    result["GROK_DISABLE_AUTOUPDATER"] = "1"
    return dict(sorted(result.items()))


def _file_identity(path: Path) -> Mapping[str, Any]:
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        hasher = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
        after = os.fstat(stream.fileno())
    current = path.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(
        getattr(before, field) != getattr(after, field)
        or getattr(after, field) != getattr(current, field)
        for field in stable_fields
    ):
        raise OSError("Grok executable changed while hashing")
    return {
        "path": os.path.normcase(str(path)),
        "device": current.st_dev,
        "inode": current.st_ino,
        "size": current.st_size,
        "sha256": hasher.hexdigest(),
    }


def _grok_pair_key(
    executable: Path,
    identity: Mapping[str, Any],
    version: str,
    capability_hash: str,
    *,
    adapter_version: str = _ADAPTER_VERSION,
    adapter_api_version: int = ADAPTER_API_VERSION,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "adapter_api_version": adapter_api_version,
                "adapter_version": adapter_version,
                "canonical_path": os.path.normcase(str(executable)),
                "file_identity": identity,
                "version": version,
                "capability_hash": capability_hash,
                "transport": TRANSPORT,
            }
        )
    ).hexdigest()


def _assert_bound_identity(binding: GrokBinding) -> None:
    _validate_binding(binding)
    current = _file_identity(binding.executable_path)
    actual = (
        int(current["device"]),
        int(current["inode"]),
        int(current["size"]),
    )
    if actual != binding.file_identity or current["sha256"] != binding.executable_sha256:
        raise ValueError("Grok executable identity drifted")


def _repository_root(cwd: Path) -> Path:
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fold_path(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).casefold()


def _bounded_public_text(value: object, maximum: int) -> str | None:
    text = _bounded_text(value, maximum)
    if text is None or not text or any(
        unicodedata.category(character) == "Cc" for character in text
    ):
        return None
    return text


def _bounded_text(value: object, maximum: int) -> str | None:
    encoded = _utf8_length(value)
    if not isinstance(value, str) or encoded is None or encoded > maximum:
        return None
    if any(
        unicodedata.category(character) == "Cc" and character not in "\t\r\n"
        for character in value
    ):
        return None
    return value


def _utf8_length(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        return None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _capability_error(message: str) -> ServiceError:
    return ServiceError(
        "CAPABILITY_MISSING",
        message,
        category="capability",
        retryable=False,
    )
