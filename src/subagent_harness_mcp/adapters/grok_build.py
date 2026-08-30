"""Grok Build binding and strict pre-prompt context policy."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
import hashlib
import json
import math
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
from typing import Any, Callable, Iterator, Mapping, Sequence

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
_ADAPTER_VERSION = "1.0.28"
_REVIEW_TOOL_ALLOWLIST = ("read_file",)
_WRITER_TOOL_ALLOWLIST = ("read_file", "search_replace")
_DISALLOWED_META_TOOLS = ("search_tool", "use_tool")
_AGENT_PROFILE_BINDING = "session/new._meta.agentProfile"
_AGENT_PROFILE_PERMISSION_MODE = "bypassPermissions"
_AGENT_TYPE_EVIDENCE_SOURCE = (
    "_x.ai/models/list.availableModels._meta.agentType"
)
_MAX_AGENT_TYPE_BYTES = 128
_ACP_FS_TRANSPORT = ("read_text_file", "write_text_file")
_MAX_REVERSE_IO_COUNT = 2_147_483_647
_REVERSE_IO_SCOPE = "native-session-cumulative"
_REVERSE_IO_COUNTERS = (
    "read_attempts",
    "read_successes",
    "write_attempts",
    "write_successes",
    "terminal_attempts",
    "terminal_denials",
)
_RESUMED_AUTHORITY_FIELDS = (
    "source",
    "variant_id",
    "permissions",
    "context_policy_id",
    "permission_policy_id",
    "write_set",
    "mode",
    "pair_key",
    "workspace_root_identity",
    "project_instructions",
    "project_instruction_count",
    "project_trusted",
    "project_root",
    "git_attestation",
    "discovered_extensions",
    "model_route_isolation",
    "agent_type_evidence_source",
)
_CLEANUP_TIMEOUT_SECONDS = 2.0
_DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 15.0
_DEFAULT_CANCEL_TIMEOUT_SECONDS = 5.0
_MAX_PUBLIC_RESULT_CHARS = 65_536
_MAX_INSTRUCTION_FILES = 32
_MAX_INSTRUCTION_SCAN_ENTRIES = 20_000
_RESULT_TRUNCATION_MARKER = "\n[truncated by Subagent MCP]"
_VERSION = re.compile(
    r"^grok \d{1,16}\.\d{1,16}\.\d{1,16} \([0-9a-f]{7,64}\)"
    r"(?: \[[a-z][a-z0-9._-]{0,31}\])?$"
)
_PROVEN_DISCOVERY_DISPLAY_VERSIONS = frozenset(
    {
        "grok 1.0.5 (5115b46bc9)",
        "grok 1.0.5 (5115b46bc9) [stable]",
    }
)
_ISOLATION_CONFIG_PREFIX = b"""[compat.cursor]
skills = false
rules = false
agents = false
mcps = false
hooks = false
sessions = false

[compat.claude]
skills = false
rules = false
agents = false
mcps = false
hooks = false
sessions = false

[compat.codex]
sessions = false

[claude_compat]
imported = true

[skills]
ignore = ["""
_LEGACY_ISOLATION_CONFIG = _ISOLATION_CONFIG_PREFIX + b'"~/.agents"]\n'
_COMPATIBILITY_CELLS = (
    *(
        ("cursor", surface)
        for surface in ("skills", "rules", "agents", "mcps", "hooks", "sessions")
    ),
    *(
        ("claude", surface)
        for surface in ("skills", "rules", "agents", "mcps", "hooks", "sessions")
    ),
    ("codex", "sessions"),
)
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._:/@+-]{0,127}$")
_GIT_VERSION = re.compile(
    r"^git version [0-9]{1,8}(?:\.[0-9A-Za-z][0-9A-Za-z.+-]{0,31}){1,7}$"
)
_REQUIRED_HELP_TOKENS = (
    "Usage: grok [OPTIONS] [PROMPT] [COMMAND]",
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
        context_guard: Callable[[], None] | None = None,
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
            canonical_workspace, workspace_identity = _attest_workspace_root(workspace)
        except (OSError, RuntimeError, GrokPermissionError) as exc:
            raise GrokPermissionError("Grok workspace is unavailable") from exc
        if not canonical_workspace.is_dir():
            raise GrokPermissionError("Grok workspace is unavailable")
        if permission_mode == "repo-read" and write_roots:
            raise GrokPermissionError("Review mode cannot declare write roots")
        if permission_mode == "workspace-write" and not 1 <= len(write_roots) <= 32:
            raise GrokPermissionError("Writer mode requires one to thirty-two roots")

        self._workspace = canonical_workspace
        self._workspace_identity = workspace_identity
        self._bound_session_id: str | None = None
        self._permission_mode = permission_mode
        self._max_file_bytes = max_file_bytes
        self._context_guard = context_guard
        self._write_lock = asyncio.Lock()
        self._write_worker: asyncio.Task[Mapping[str, object]] | None = None
        self._write_cancel: threading.Event | None = None
        self._turn_condition = asyncio.Condition()
        self._active_execution_id: str | None = None
        self._reverse_execution_id: str | None = None
        self._reverse_callbacks = 0
        self._reverse_io_counts = {name: 0 for name in _REVERSE_IO_COUNTERS}
        self._reverse_io_saturated = False
        roots = tuple(self._build_write_root(root) for root in write_roots)
        for index, root in enumerate(roots):
            for other in roots[index + 1 :]:
                if _parts_overlap(root.folded_parts, other.folded_parts):
                    raise GrokPermissionError("Grok write roots must not overlap")
        self._write_roots = roots

    async def read_text_file(
        self, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        values = _filesystem_read_params(params, self._bound_session_id)
        path = self._relative_acp_path(values["path"])
        return await asyncio.to_thread(
            self._read_text_file,
            path,
            values.get("line"),
            values.get("limit"),
        )

    async def write_text_file(
        self, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        if self._permission_mode != "workspace-write":
            raise GrokPermissionError("Grok filesystem write is not authorized")
        values = _filesystem_write_params(params, self._bound_session_id)
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
                    self._relative_acp_path(values["path"]),
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
        reverse_scope: str | None,
    ) -> Mapping[str, object]:
        if method in {"fs/read_text_file", "fs/write_text_file"}:
            execution_id = await self._admit_reverse_callback(reverse_scope)
            try:
                if method == "fs/read_text_file":
                    self._record_reverse_io("read_attempts")
                    result = await self.read_text_file(params)
                    self._record_reverse_io("read_successes")
                    return result
                self._record_reverse_io("write_attempts")
                try:
                    result = await self.write_text_file(params)
                except GrokFilesystemCleanupError as exc:
                    raise AcpFatalCallbackError(
                        "ACP filesystem cleanup ambiguity"
                    ) from exc
                self._record_reverse_io("write_successes")
                return result
            finally:
                await self._finish_reverse_callback(execution_id)
        if method.startswith("terminal/"):
            self._record_reverse_io("terminal_attempts")
            self._record_reverse_io("terminal_denials")
            raise GrokPermissionError("Grok reverse method is not authorized")
        if method == "session/request_permission" or method.startswith(
            _DENIED_REVERSE_PREFIXES
        ):
            raise GrokPermissionError("Grok reverse method is not authorized")
        raise AcpMethodNotFoundError(method)

    async def activate_turn(self, execution_id: object) -> None:
        normalized = _bounded_public_text(execution_id, 256)
        if normalized is None:
            raise GrokPermissionError("Grok filesystem execution authority is invalid")
        async with self._turn_condition:
            if self._active_execution_id is not None or self._reverse_callbacks:
                raise GrokPermissionError("Grok filesystem execution authority is busy")
            self._active_execution_id = normalized

    async def deactivate_turn(self, execution_id: object) -> None:
        normalized = _bounded_public_text(execution_id, 256)
        if normalized is None:
            raise GrokPermissionError("Grok filesystem execution authority is invalid")
        async with self._turn_condition:
            if self._active_execution_id != normalized:
                raise GrokPermissionError("Grok filesystem execution authority changed")
            self._active_execution_id = None
            while self._reverse_callbacks:
                if self._reverse_execution_id != normalized:
                    raise GrokPermissionError(
                        "Grok filesystem execution authority changed"
                    )
                await self._turn_condition.wait()

    async def _admit_reverse_callback(self, reverse_scope: str | None) -> str:
        async with self._turn_condition:
            execution_id = self._active_execution_id
            if execution_id is None or reverse_scope != execution_id:
                raise GrokPermissionError(
                    "Grok filesystem execution scope is not active"
                )
            if (
                self._reverse_execution_id is not None
                and self._reverse_execution_id != execution_id
            ):
                raise GrokPermissionError("Grok filesystem execution authority changed")
            self._reverse_execution_id = execution_id
            self._reverse_callbacks += 1
            return execution_id

    async def _finish_reverse_callback(self, execution_id: str) -> None:
        async with self._turn_condition:
            if (
                self._reverse_execution_id != execution_id
                or self._reverse_callbacks < 1
            ):
                raise RuntimeError("Grok reverse callback authority accounting failed")
            self._reverse_callbacks -= 1
            if not self._reverse_callbacks:
                self._reverse_execution_id = None
                self._turn_condition.notify_all()

    def _record_reverse_io(self, name: str) -> None:
        current = self._reverse_io_counts[name]
        if current < _MAX_REVERSE_IO_COUNT:
            self._reverse_io_counts[name] = current + 1
        else:
            self._reverse_io_saturated = True

    def _reverse_io_attestation(self) -> Mapping[str, object]:
        return {
            "scope": _REVERSE_IO_SCOPE,
            **self._reverse_io_counts,
            "saturated": self._reverse_io_saturated,
        }

    def bind_session(self, session_id: object) -> None:
        normalized = _bounded_public_text(session_id, 256)
        if normalized is None or self._bound_session_id is not None:
            raise GrokPermissionError("Grok filesystem session binding is invalid")
        self._bound_session_id = normalized

    def _relative_acp_path(self, value: str) -> str:
        try:
            absolute = _lexical_local_dos_path(value, "filesystem")
        except GrokBindingIncompatible as exc:
            raise GrokPermissionError("Grok filesystem path is invalid") from exc
        boundary = PureWindowsPath(str(self._workspace))
        try:
            relative = absolute.relative_to(boundary)
        except ValueError as exc:
            raise GrokPermissionError(
                "Grok filesystem path is outside the workspace"
            ) from exc
        parts = tuple(relative.parts)
        if not parts:
            raise GrokPermissionError("Grok filesystem path is invalid")
        return str(PureWindowsPath(*parts))

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

    def _read_text_file(
        self,
        value: str,
        line: int | None,
        limit: int | None,
    ) -> Mapping[str, object]:
        self._guard_context()
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
        if line is not None or limit is not None:
            lines = content.splitlines(keepends=True)
            start = (line or 1) - 1
            content = "".join(lines[start : None if limit is None else start + limit])
        if len(_canonical_json({"content": content})) > _MAX_FILESYSTEM_RESULT_BYTES:
            raise GrokPermissionError("Grok filesystem result is too large")
        self._guard_context()
        return {"content": content}

    def _write_text_file(
        self,
        value: str,
        data: bytes,
        cancel_event: threading.Event,
    ) -> Mapping[str, object]:
        parts = _windows_relative_parts(value)
        _reject_reserved_writer_path(parts)
        self._guard_context()
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
            self._guard_context()
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
        return {}

    def _guard_context(self) -> None:
        self._validate_workspace_root()
        if self._context_guard is None:
            return
        try:
            self._context_guard()
        except BaseException as exc:
            raise GrokPermissionError("Grok filesystem context changed") from exc

    def _validate_workspace_root(self) -> None:
        try:
            current, identity = _attest_workspace_root(self._workspace)
        except (GrokPermissionError, OSError, RuntimeError) as exc:
            raise GrokPermissionError("Grok workspace identity changed") from exc
        if current != self._workspace or identity != self._workspace_identity:
            raise GrokPermissionError("Grok workspace identity changed")

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
class GrokProjectInstructionAttestation:
    relative_path: str
    sha256: str
    identity: tuple[int, int, int, int]
    size: int


@dataclass(frozen=True, slots=True)
class GrokGitAttestation:
    executable_path: str
    version: str
    sha256: str
    identity: tuple[int, int, int, int]
    root_marker_identity: tuple[int, int, int, int] | None = None
    root_git_dir_path: str | None = None
    root_git_dir_identity: tuple[int, int, int, int] | None = None
    root_common_dir_path: str | None = None
    root_common_dir_identity: tuple[int, int, int, int] | None = None
    root_gitmodules_identity: tuple[int, int, int, int] | None = None
    root_gitmodules_sha256: str | None = None
    repository_context_bound: bool = False
    nested_repository_boundaries: tuple[_GrokGitRepositoryBoundary, ...] = ()


@dataclass(frozen=True, slots=True)
class _GrokGitRepositoryBoundary:
    repository_path: str
    marker_identity: tuple[int, int, int, int]
    marker_is_file: bool
    git_dir_path: str
    git_dir_identity: tuple[int, int, int, int]
    common_dir_path: str
    common_dir_identity: tuple[int, int, int, int]
    gitmodules_identity: tuple[int, int, int, int] | None
    gitmodules_sha256: str | None
    tracked: bool
    is_root: bool


@dataclass(frozen=True, slots=True)
class _GrokNestedRepository:
    parts: tuple[str, ...]
    boundary: _GrokGitRepositoryBoundary


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
    api_key_auth_disabled: bool | None = None
    config_source_layer_count: int | None = None
    config_source_path: str = ""
    compatibility_isolated: bool | None = None
    permission_sources_isolated: bool | None = None
    external_surfaces_empty: bool | None = None
    builtin_agent_count: int | None = None
    project_instructions: tuple[GrokProjectInstructionAttestation, ...] = ()
    project_trusted: bool | None = None
    project_root: str | None = None
    git_attestation: GrokGitAttestation | None = None


@dataclass(frozen=True, slots=True)
class GrokSessionToolAttestation:
    pair_key: str
    external_session_id: str
    workspace_key: str
    mode: str
    workspace_path: str = ""
    effective_model: str = ""
    reasoning_effort: str = ""
    auth_method: str = ""
    provider_no_spend_safe: bool = False
    quota_state: str = "unknown"
    effective_agent_type: str = ""
    agent_type_source: str = ""


@dataclass(frozen=True, slots=True)
class GrokLaunch:
    binding: GrokBinding
    workspace_path: str
    workspace_key: str
    model: str
    reasoning_effort: str
    permission_mode: str
    write_roots: tuple[str, ...]
    agent_profile_json: str
    agent_profile_sha256: str
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
    write_receipt: asyncio.Event
    cancel_sent: bool = False


@dataclass(slots=True)
class _GrokSession:
    conversation_id: str
    context: ResolvedContext
    process: AcpStdioProcess
    bridge: GrokFilesystemBridge
    public_text: _GrokPublicText
    snapshot: AdapterSnapshot
    effective_agent_type: str
    turn: _GrokTurn | None = None
    native_closed: bool = False
    closing: bool = False
    interrupting: bool = False
    interrupt_done: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def _wait_for_prompt_write_or_terminal(turn: _GrokTurn) -> None:
    if turn.write_receipt.is_set() or turn.task.done():
        return
    receipt_wait = asyncio.create_task(turn.write_receipt.wait())
    try:
        await asyncio.wait(
            {receipt_wait, turn.task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not receipt_wait.done():
            receipt_wait.cancel()
        await asyncio.gather(receipt_wait, return_exceptions=True)


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
        data_root: Path | None = None,
        binding_probe_timeout_seconds: float = DEFAULT_BINDING_PROBE_TIMEOUT_SECONDS,
        inspect_timeout_seconds: float = DEFAULT_INSPECT_TIMEOUT_SECONDS,
        acp_process_factory: AcpProcessFactory | None = None,
        handshake_timeout_seconds: float = _DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
        cancel_timeout_seconds: float = _DEFAULT_CANCEL_TIMEOUT_SECONDS,
    ) -> None:
        if min(handshake_timeout_seconds, cancel_timeout_seconds) <= 0:
            raise ValueError("Grok ACP lifecycle timeouts must be positive")
        self._binding_locator = binding_locator or locate_grok_binding
        self._platform = platform or sys.platform
        self._environment = dict(os.environ if environment is None else environment)
        if data_root is None:
            from ..paths import resolve_paths

            data_root = resolve_paths().data_dir
        try:
            self._data_root = data_root.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("Grok product data root is unavailable") from exc
        self._runtime_home = self._data_root / RUNTIME_ID / "home"
        self._billing_guard_root = self._data_root / RUNTIME_ID / "billing-guards"
        self._runtime_config_lock = threading.Lock()
        self._runtime_config_identity: tuple[int, int, int, int] | None = None
        self._billing_guard_identities: dict[
            Path,
            tuple[tuple[int, int], tuple[int, int, int, int]],
        ] = {}
        if not _lexically_within(self._runtime_home, self._data_root):
            raise ValueError("Grok isolated runtime home escapes the product data root")
        if not _lexically_within(self._billing_guard_root, self._data_root):
            raise ValueError("Grok billing guard root escapes the product data root")
        self._runtime_environment = MappingProxyType(
            _isolated_child_env(self._environment, self._runtime_home)
        )
        self._catalog_reader = catalog_reader or self._read_model_catalog
        self._inspect_reader = inspect_reader or self._read_inspect
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

    def _prepare_runtime_home(self) -> None:
        try:
            _reject_reparse_chain(self._data_root, (RUNTIME_ID, "home"))
            self._runtime_home.mkdir(parents=True, exist_ok=True)
            _reject_reparse_chain(self._data_root, (RUNTIME_ID, "home"))
            current = self._runtime_home.resolve(strict=True)
        except (GrokPermissionError, OSError, RuntimeError) as exc:
            raise _capability_error(
                "Grok isolated runtime home is unavailable"
            ) from exc
        if current != self._runtime_home or not current.is_dir():
            raise _capability_error("Grok isolated runtime home is unsafe")
        try:
            with self._runtime_config_lock:
                identity = _ensure_isolation_config(
                    self._data_root,
                    self._runtime_home,
                    (RUNTIME_ID, "home"),
                    expected_identity=self._runtime_config_identity,
                )
                if self._runtime_config_identity is None:
                    self._runtime_config_identity = identity
        except GrokPermissionError as exc:
            raise _capability_error(
                "Grok isolated runtime config is unavailable or differs"
            ) from exc

    def _new_billing_guard_home(self) -> Path:
        home: Path | None = None
        home_identity: tuple[int, int] | None = None
        config_identity: tuple[int, int, int, int] | None = None
        try:
            _reject_reparse_chain(self._data_root, (RUNTIME_ID, "billing-guards"))
            self._billing_guard_root.mkdir(parents=True, exist_ok=True)
            _reject_reparse_chain(self._data_root, (RUNTIME_ID, "billing-guards"))
            guard_root = self._billing_guard_root.resolve(strict=True)
            if guard_root != self._billing_guard_root or not guard_root.is_dir():
                raise OSError("unsafe billing guard root")
            home = Path(
                tempfile.mkdtemp(prefix="billing-", dir=self._billing_guard_root)
            )
            home_details = home.lstat()
            if not stat.S_ISDIR(home_details.st_mode) or _is_reparse_point(home):
                raise OSError("unsafe billing guard home")
            home_identity = _directory_identity(home_details)
            _reject_reparse_chain(
                self._data_root,
                (RUNTIME_ID, "billing-guards", home.name),
            )
            current = home.resolve(strict=True)
            _assert_guard_home_identity(home, home_identity)
            config_identity = _ensure_isolation_config(
                self._data_root,
                home,
                (RUNTIME_ID, "billing-guards", home.name),
            )
            _assert_guard_home_identity(home, home_identity)
        except (GrokPermissionError, OSError, RuntimeError) as exc:
            if home is not None and home_identity is not None:
                try:
                    _remove_uninitialized_guard_home(
                        self._data_root,
                        self._billing_guard_root,
                        home,
                        home_identity,
                        config_identity,
                    )
                except (GrokPermissionError, OSError, RuntimeError) as cleanup_exc:
                    raise ServiceError(
                        "RECOVERY_REQUIRED",
                        "Grok failed billing guard home cleanup was not confirmed",
                        category="adapter",
                    ) from cleanup_exc
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok disposable billing guard home is unavailable",
                category="adapter",
            ) from exc
        if current != home or not current.is_dir():
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok disposable billing guard home is unsafe",
                category="adapter",
            )
        assert home_identity is not None and config_identity is not None
        self._billing_guard_identities[home] = (home_identity, config_identity)
        return home

    def _remove_billing_guard_home(self, home: Path) -> None:
        try:
            if (
                home.parent != self._billing_guard_root
                or not home.name.startswith("billing-")
            ):
                raise OSError("billing guard ownership mismatch")
            expected_authority = self._billing_guard_identities.get(home)
            if expected_authority is None:
                raise OSError("billing guard identity is not registered")
            expected_identity, expected_config_identity = expected_authority
            _reject_reparse_chain(
                self._data_root,
                (RUNTIME_ID, "billing-guards", home.name),
            )
            current = home.resolve(strict=True)
            if current != home or not current.is_dir():
                raise OSError("billing guard identity mismatch")
            _assert_guard_home_identity(home, expected_identity)
            _unlock_exact_isolation_config(
                self._data_root,
                home,
                (RUNTIME_ID, "billing-guards", home.name),
                expected_config_identity,
            )
            _assert_guard_home_identity(home, expected_identity)
            try:
                shutil.rmtree(home)
            except OSError:
                if os.path.lexists(home):
                    _assert_guard_home_identity(home, expected_identity)
                    config_identity, read_only = _verify_isolation_config(
                        self._data_root,
                        home,
                        (RUNTIME_ID, "billing-guards", home.name),
                        required_read_only=None,
                        expected_identity=expected_config_identity,
                    )
                    if not read_only:
                        _set_isolation_config_read_only(
                            self._data_root,
                            home,
                            (RUNTIME_ID, "billing-guards", home.name),
                            config_identity,
                        )
                raise
            if os.path.lexists(home):
                raise OSError("billing guard cleanup incomplete")
            self._billing_guard_identities.pop(home, None)
        except (GrokPermissionError, OSError, RuntimeError) as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok disposable billing guard cleanup was not confirmed",
                category="adapter",
            ) from exc

    def _read_model_catalog(self, binding: GrokBinding) -> object:
        self._prepare_runtime_home()
        return _read_grok_model_catalog(binding, self._runtime_environment)

    def _read_inspect(
        self,
        binding: GrokBinding,
        workspace_path: str,
    ) -> GrokInspectObservation:
        self._prepare_runtime_home()
        return _read_grok_inspect(
            binding,
            workspace_path,
            self._runtime_environment,
        )

    async def _fresh_isolated_inspect(
        self,
        binding: GrokBinding,
        workspace: Path,
        *,
        git_attestation: GrokGitAttestation | None = None,
    ) -> GrokInspectObservation:
        self._prepare_runtime_home()
        for attempt in range(3):
            try:
                _assert_bound_identity(binding)
            except Exception as exc:
                raise _capability_error("Grok inspect evidence is unavailable") from exc
            try:
                inspect = await _run_sync_bounded(
                    self._inspect_reader,
                    binding,
                    str(workspace),
                    timeout=self._inspect_timeout,
                )
            except asyncio.CancelledError:
                raise
            except (TimeoutError, FileNotFoundError) as exc:
                if attempt == 2:
                    raise _capability_error(
                        "Grok inspect evidence is unavailable"
                    ) from exc
                continue
            except Exception as exc:
                raise _capability_error("Grok inspect evidence is unavailable") from exc
            try:
                _assert_bound_identity(binding)
            except Exception as exc:
                raise _capability_error("Grok inspect evidence is unavailable") from exc
            break
        if isinstance(inspect, GrokInspectObservation):
            try:
                if inspect.project_root is None:
                    if git_attestation is not None:
                        raise GrokBindingIncompatible(
                            "Non-Git workspace has unexpected Git evidence"
                        )
                    selected_git = None
                else:
                    selected_git = git_attestation or _attest_git_executable(workspace)
                    selected_git = _bind_git_root_attestation(workspace, selected_git)
                scanned_instructions, selected_git = _scan_grok_instruction_context(
                    workspace,
                    inspect.project_root,
                    selected_git,
                )
                inspect = replace(
                    inspect,
                    project_instructions=_merge_project_instruction_manifest(
                        inspect.project_instructions,
                        scanned_instructions,
                    ),
                    git_attestation=selected_git,
                )
            except GrokBindingIncompatible as exc:
                raise _capability_error(
                    "Grok project instruction evidence is unavailable"
                ) from exc
        observed = _validate_inspect(
            inspect,
            binding,
            workspace,
            self._runtime_home / "config.toml",
        )
        assert isinstance(inspect, GrokInspectObservation)
        if observed:
            raise _capability_error(
                "Grok executable extensions must be removed or disabled before delegation"
            )
        self._prepare_runtime_home()
        return inspect

    async def _assert_context_current(
        self,
        context: ResolvedContext,
    ) -> GrokInspectObservation:
        workspace = _reattest_context_workspace_root(context)
        launch = self._bound_launch(context)
        expected_git = _context_git_attestation(context)
        if expected_git is not None:
            try:
                expected_git = _bind_git_root_attestation(workspace, expected_git)
            except GrokBindingIncompatible as exc:
                raise ServiceError(
                    "CONTEXT_DRIFT",
                    "Grok Git metadata changed",
                    category="context",
                ) from exc
        inspect = await self._fresh_isolated_inspect(
            launch.binding,
            workspace,
            git_attestation=expected_git,
        )
        expected = _context_project_instructions(context)
        if (
            inspect.project_instructions != expected
            or inspect.project_trusted != context.attestation.get("project_trusted")
            or inspect.project_root != context.attestation.get("project_root")
            or inspect.git_attestation != expected_git
        ):
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok project context changed",
                category="context",
            )
        return inspect

    def _context_guard(self, context: ResolvedContext) -> Callable[[], None]:
        def guard() -> None:
            try:
                self._assert_context_current_sync(context)
            except (GrokBindingIncompatible, ServiceError) as exc:
                raise ServiceError(
                    "CONTEXT_DRIFT",
                    "Grok project context could not be reattested",
                    category="context",
                ) from exc

        return guard

    def _assert_context_current_sync(
        self,
        context: ResolvedContext,
    ) -> GrokInspectObservation:
        workspace = _reattest_context_workspace_root(context)
        launch = self._bound_launch(context)
        expected_git = _context_git_attestation(context)
        if expected_git is not None:
            try:
                expected_git = _bind_git_root_attestation(workspace, expected_git)
            except GrokBindingIncompatible as exc:
                raise ServiceError(
                    "CONTEXT_DRIFT",
                    "Grok Git metadata changed",
                    category="context",
                ) from exc
        self._prepare_runtime_home()
        _assert_bound_identity(launch.binding)
        inspect = self._inspect_reader(launch.binding, str(workspace))
        if not isinstance(inspect, GrokInspectObservation):
            raise _capability_error("Grok inspect evidence is unavailable")
        if inspect.project_root is None:
            if expected_git is not None:
                raise _capability_error("Grok Git evidence is mismatched")
            selected_git = None
        else:
            if expected_git is None:
                raise _capability_error("Grok Git evidence is unavailable")
            selected_git = _bind_git_root_attestation(workspace, expected_git)
        scanned_instructions, selected_git = _scan_grok_instruction_context(
            workspace,
            inspect.project_root,
            selected_git,
        )
        inspect = replace(
            inspect,
            project_instructions=_merge_project_instruction_manifest(
                inspect.project_instructions,
                scanned_instructions,
            ),
            git_attestation=selected_git,
        )
        observed_extensions = _validate_inspect(
            inspect,
            launch.binding,
            workspace,
            self._runtime_home / "config.toml",
        )
        if observed_extensions:
            raise _capability_error(
                "Grok executable extensions must be removed or disabled before delegation"
            )
        if (
            inspect.project_instructions != _context_project_instructions(context)
            or inspect.project_trusted != context.attestation.get("project_trusted")
            or inspect.project_root != context.attestation.get("project_root")
            or inspect.git_attestation != expected_git
        ):
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok project context changed",
                category="context",
            )
        return inspect

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
            workspace, workspace_root_identity = _attest_workspace_root(
                request.workspace_path
            )
        except (GrokPermissionError, OSError, RuntimeError) as exc:
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
        if "GROK_AUTH_PATH" not in self._runtime_environment:
            raise ServiceError(
                "CAPABILITY_MISSING",
                "Grok cached-login path could not be isolated",
                category="capability",
                next_action=(
                    "Sign in with the native Grok harness or set GROK_AUTH_PATH "
                    "to its existing auth file; do not enable an API key."
                ),
            )
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
        inspect = await self._fresh_isolated_inspect(binding, workspace)
        observed = _validate_inspect(
            inspect,
            binding,
            workspace,
            self._runtime_home / "config.toml",
        )
        mode = "writer" if "workspace_write" in permissions else "review"
        agent_profile_json, agent_profile_sha256 = _agent_profile_document(mode)
        project_instructions = _serialize_project_instructions(
            inspect.project_instructions
        )
        git_attestation = _serialize_git_attestation(inspect.git_attestation)
        payload = {
            "runtime_id": RUNTIME_ID,
            "variant_id": request.variant_id,
            "model": model,
            "reasoning_effort": effort,
            "workspace_path": str(workspace),
            "workspace_key": request.workspace_key,
            "workspace_root_identity": workspace_root_identity,
            "transport": TRANSPORT,
            "permissions": list(permissions),
            "write_set": list(write_roots),
            "mode": mode,
            "requested_agent_profile_json": agent_profile_json,
            "requested_agent_profile_sha256": agent_profile_sha256,
            "agent_profile_binding": _AGENT_PROFILE_BINDING,
            "agent_type_evidence_source": _AGENT_TYPE_EVIDENCE_SOURCE,
            "acp_fs_transport": list(_ACP_FS_TRANSPORT),
            "acp_terminal_transport": True,
            "terminal_authorized": False,
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
            "pair_key": binding.pair_key,
            "project_instructions": project_instructions,
            "project_instruction_count": len(project_instructions),
            "project_trusted": inspect.project_trusted,
            "project_root": inspect.project_root,
            "git_attestation": git_attestation,
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
            "requested_agent_profile_json": agent_profile_json,
            "requested_agent_profile_sha256": agent_profile_sha256,
            "agent_profile_binding": _AGENT_PROFILE_BINDING,
            "agent_type_evidence_source": _AGENT_TYPE_EVIDENCE_SOURCE,
            "acp_fs_transport": _ACP_FS_TRANSPORT,
            "acp_terminal_transport": True,
            "terminal_authorized": False,
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
            "pair_key": binding.pair_key,
            "workspace_root_identity": workspace_root_identity,
            "project_instructions": project_instructions,
            "project_instruction_count": len(project_instructions),
            "project_trusted": inspect.project_trusted,
            "project_root": inspect.project_root,
            "git_attestation": git_attestation,
            "discovered_extensions": observed,
            "inspect_permission_keys": inspect.permission_keys,
            "inspect_permission_rules": inspect.permission_rules,
            "inspect_permission_modes": inspect.permission_modes,
            "cached_native_login": "not_exposed",
            "no_extra_spend": "not_exposed",
            "builtin_tool_inventory": "not_exposed",
            "provider_readiness": "needs_canary",
            "quota_state": "unknown",
            "model_route_isolation": "verified",
            "model_route_isolation_source": "isolated-home-native-inspect",
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
        mode = context.attestation.get("mode")
        agent_profile_json, agent_profile_sha256 = _agent_profile_document(mode)
        fs_transport = _text_tuple(
            context.attestation.get("acp_fs_transport"),
            "ACP filesystem transport",
        )
        if (
            context.attestation.get("requested_agent_profile_json")
            != agent_profile_json
            or context.attestation.get("requested_agent_profile_sha256")
            != agent_profile_sha256
            or context.attestation.get("agent_profile_binding")
            != _AGENT_PROFILE_BINDING
            or context.attestation.get("agent_type_evidence_source")
            != _AGENT_TYPE_EVIDENCE_SOURCE
            or fs_transport != _ACP_FS_TRANSPORT
            or context.attestation.get("acp_terminal_transport") is not True
            or context.attestation.get("terminal_authorized") is not False
            or (mode == "writer") != bool(write_roots)
        ):
            raise ServiceError("CONTEXT_DRIFT", "Grok launch authority changed")
        argv = (
            str(binding.executable_path),
            "--no-auto-update",
            "--cwd",
            context.workspace_path,
            "--model",
            context.requested_model,
            "--reasoning-effort",
            effort,
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
            permission_mode=_AGENT_PROFILE_PERMISSION_MODE,
            write_roots=write_roots,
            agent_profile_json=agent_profile_json,
            agent_profile_sha256=agent_profile_sha256,
            argv=argv,
            env=self._runtime_environment,
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
        _validate_agent_type(attestation.effective_agent_type)
        if _bounded_public_text(attestation.agent_type_source, 256) is None:
            raise _capability_error("Grok agent type evidence is invalid")
        exact = (
            attestation.pair_key == binding.pair_key
            and _bounded_public_text(attestation.external_session_id, 256) is not None
            and attestation.workspace_key == context.workspace_key
            and _fold_path(attestation.workspace_path) == _fold_path(context.workspace_path)
            and attestation.mode == mode
            and effective_model == context.requested_model
            and attestation.reasoning_effort == effort
            and attestation.agent_type_source == _AGENT_TYPE_EVIDENCE_SOURCE
        )
        if not exact:
            raise _capability_error("Grok ACP session identity is incomplete or mismatched")
        if attestation.auth_method != "cached_token":
            raise _capability_error("Grok cached-native authentication is unproven")
        if attestation.provider_no_spend_safe is not True:
            raise _capability_error("Grok native no-spend attestation is unproven")
        if attestation.quota_state != "unknown":
            raise _capability_error("Grok quota evidence is malformed")
        if _extension_set(context.attestation.get("discovered_extensions")):
            raise _capability_error("Grok executable extensions are present")
        launch = self.launch_for(context)
        if (
            context.attestation.get("model_route_isolation") != "verified"
            or launch.env != self._runtime_environment
        ):
            raise _capability_error("Grok model route isolation is unproven")

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

    async def _verify_provider_no_spend(
        self,
        launch: GrokLaunch,
    ) -> bool:
        guard_home = self._new_billing_guard_home()
        guard_authority = self._billing_guard_identities.get(guard_home)
        if guard_authority is None:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok disposable billing guard authority is unavailable",
                category="adapter",
            )
        _guard_home_identity, guard_config_identity = guard_authority
        guard_environment = dict(launch.env)
        guard_environment["GROK_HOME"] = str(guard_home)
        guard_launch = replace(
            launch,
            env=MappingProxyType(dict(sorted(guard_environment.items()))),
        )
        process: AcpStdioProcess | None = None
        guard_child_cleanup_confirmed = True
        stage = "handshake"
        try:
            lock_paths = (
                guard_home / "config.toml",
                launch.binding.executable_path,
            )
            with _locked_grok_startup(lock_paths):
                try:
                    _assert_bound_identity(launch.binding)
                    _ensure_isolation_config(
                        self._data_root,
                        guard_home,
                        (RUNTIME_ID, "billing-guards", guard_home.name),
                        expected_identity=guard_config_identity,
                    )
                    process = self._acp_process_factory(
                        guard_launch,
                        _deny_billing_guard_reverse_request,
                        _ignore_billing_guard_notification,
                    )
                    guard_child_cleanup_confirmed = False
                    await process.start()
                    initialize = await process.request(
                        "initialize",
                        _initialize_params(),
                        timeout_seconds=self._handshake_timeout,
                    )
                    _validate_initialize_response(initialize)
                    await asyncio.wait_for(
                        process.notify("initialized", {}),
                        timeout=self._handshake_timeout,
                    )
                    auth = await process.request(
                        "authenticate",
                        {"methodId": "cached_token", "_meta": {"headless": True}},
                        timeout_seconds=self._handshake_timeout,
                    )
                    _validate_authenticate_response(auth)
                    stage = "billing"
                    billing = await process.request(
                        "_x.ai/billing",
                        {},
                        timeout_seconds=self._handshake_timeout,
                    )
                    auto_topup = await process.request(
                        "_x.ai/auto-topup-rule",
                        {},
                        timeout_seconds=self._handshake_timeout,
                    )
                finally:
                    if process is not None:
                        cleanup_error = await self._close_failed_start(process)
                        process = None
                        if cleanup_error is not None:
                            raise ServiceError(
                                "RECOVERY_REQUIRED",
                                "Grok disposable billing guard cleanup was not confirmed",
                                category="adapter",
                            ) from cleanup_error
                        guard_child_cleanup_confirmed = True
        except asyncio.CancelledError:
            raise
        except AcpRpcError as exc:
            failure, _provider_code, _provider_detail = _failure_from_rpc(exc)
            if stage == "billing" and exc.code == -32601:
                raise ServiceError(
                    "CAPABILITY_MISSING",
                    "Grok native no-spend attestation is unavailable",
                    category="capability",
                    next_action=(
                        "Update Grok Build to a version that exposes native billing "
                        "attestation, then start a fresh task; do not enable credits."
                    ),
                ) from None
            if stage == "billing" and failure.code == "CAPABILITY_MISSING":
                failure = _provider_failure("unknown", failure.retryable)
            raise ServiceError(
                failure.code,
                failure.message,
                category=failure.category,
                retryable=failure.retryable,
                next_action=failure.next_action,
            ) from None
        except (AcpProcessError, AcpProtocolError, TimeoutError):
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok disposable billing guard ended ambiguously",
                category="adapter",
                next_action=(
                    "Refresh native Grok billing availability, then start a fresh "
                    "task; do not enable credits or auto-topup."
                ),
            ) from None
        except ServiceError:
            raise
        except BaseException:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok disposable billing guard failed",
                category="adapter",
            ) from None
        else:
            return _validate_provider_no_spend(billing, auto_topup)
        finally:
            if guard_child_cleanup_confirmed:
                try:
                    self._remove_billing_guard_home(guard_home)
                except BaseException as exc:
                    raise ServiceError(
                        "RECOVERY_REQUIRED",
                        "Grok disposable billing guard cleanup was not confirmed",
                        category="adapter",
                    ) from exc

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
        attested_agent_type: str | None = None
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
            attested_agent_type = attestation.effective_agent_type
            _assert_bound_identity(binding)
            result = CanaryResult(
                True,
                request.pair_key,
                {
                    "model": request.model,
                    "effort": _reasoning_effort(request.reasoning),
                    "cleanup_confirmed": False,
                    "provider_no_spend_safe": attestation.provider_no_spend_safe,
                    "quota_state": attestation.quota_state,
                    "effective_agent_type": attestation.effective_agent_type,
                    "agent_type_evidence_source": attestation.agent_type_source,
                    "route_isolation": "verified",
                    "route_isolation_source": "isolated-home-native-inspect",
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
        if attested_agent_type is None:
            return _canary_failure(
                request.pair_key,
                "CAPABILITY_MISSING",
                "Grok selected model agent type is unavailable",
            )
        return CanaryResult(
            True,
            request.pair_key,
            {
                "model": request.model,
                "effort": _reasoning_effort(request.reasoning),
                "cleanup_confirmed": True,
                "provider_no_spend_safe": True,
                "quota_state": "unknown",
                "effective_agent_type": attested_agent_type,
                "agent_type_evidence_source": _AGENT_TYPE_EVIDENCE_SOURCE,
                "route_isolation": "verified",
                "route_isolation_source": "isolated-home-native-inspect",
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
            provider_no_spend_safe=attestation.provider_no_spend_safe,
            effective_agent_type=attestation.effective_agent_type,
            reverse_io=bridge._reverse_io_attestation(),
        )
        session = _GrokSession(
            request.conversation_id,
            request.context,
            process,
            bridge,
            public_text,
            snapshot,
            attestation.effective_agent_type,
        )
        self._sessions[session_id] = session
        self._conversation_sessions[request.conversation_id] = session_id
        await self._start_turn(session, request.execution_id, prompt)
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
        self._prepare_runtime_home()
        workspace = _reattest_context_workspace_root(context)
        expected_instructions = _context_project_instructions(context)
        instruction_paths = tuple(
            workspace.joinpath(*PureWindowsPath(row.relative_path).parts)
            for row in expected_instructions
        )
        lock_paths = (
            self._runtime_home / "config.toml",
            launch.binding.executable_path,
            *instruction_paths,
        )
        process: AcpStdioProcess | None = None
        try:
            with _locked_grok_startup(lock_paths):
                _assert_bound_identity(launch.binding)
                self._prepare_runtime_home()
                _reject_workspace_native_extension_directories(workspace)
                _reattest_context_project_instructions(context, workspace)
                await self._assert_context_current(context)
                provider_no_spend_safe = await self._verify_provider_no_spend(launch)
                try:
                    bridge = GrokFilesystemBridge(
                        workspace=context.workspace_path,
                        permission_mode=(
                            "workspace-write" if mode == "writer" else "repo-read"
                        ),
                        write_roots=launch.write_roots,
                        context_guard=self._context_guard(context),
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
                await process.start()
                initialize = await process.request(
                    "initialize",
                    _initialize_params(),
                    timeout_seconds=self._handshake_timeout,
                )
                auth_method = _validate_initialize_response(initialize)
                await asyncio.wait_for(
                    process.notify("initialized", {}),
                    timeout=self._handshake_timeout,
                )
                auth = await process.request(
                    "authenticate",
                    {"methodId": "cached_token", "_meta": {"headless": True}},
                    timeout_seconds=self._handshake_timeout,
                )
                _validate_authenticate_response(auth)
                model_state = await process.request(
                    "_x.ai/models/list",
                    {},
                    timeout_seconds=self._handshake_timeout,
                )
                expected_agent_type = _parse_models_list_response(
                    model_state,
                    context.requested_model,
                )
                session_result = await process.request(
                    "session/new",
                    {
                        "cwd": context.workspace_path,
                        "mcpServers": [],
                        "_meta": {
                            "agentProfile": json.loads(launch.agent_profile_json)
                        },
                    },
                    timeout_seconds=self._handshake_timeout,
                )
                session_id, attestation = _parse_session_handshake(
                    context,
                    launch.binding,
                    auth_method,
                    provider_no_spend_safe,
                    session_result,
                    expected_agent_type,
                )
                bridge.bind_session(session_id)
                self.validate_session_attestation(context, attestation)
                await self._assert_context_current(context)
        except asyncio.CancelledError:
            cleanup_error = (
                await self._close_failed_start(process)
                if process is not None
                else None
            )
            if cleanup_error is not None:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Grok ACP cancelled startup cleanup was not confirmed",
                    category="adapter",
                ) from cleanup_error
            raise
        except BaseException as exc:
            cleanup_error = (
                await self._close_failed_start(process)
                if process is not None
                else None
            )
            if cleanup_error is not None:
                raise ServiceError(
                    "RECOVERY_REQUIRED",
                    "Grok ACP startup cleanup was not confirmed",
                    category="adapter",
                ) from cleanup_error
            raise _startup_service_error(exc) from exc

        assert process is not None
        return process, bridge, public_text, session_id, attestation

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        session = self._session(request.external_session_id)
        _require_resumed_context_authority(request.context, session.context)
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
                    await self._assert_context_current(session.context)
                    await self._verify_provider_no_spend(
                        self._bound_launch(session.context)
                    )
                    await self._assert_context_current(session.context)
                    session.snapshot = _grok_snapshot(
                        session.context,
                        session_id=request.external_session_id,
                        execution_id=request.execution_id,
                        execution_state="running",
                        quota_state=str(
                            session.snapshot.evidence.get("quota_state", "unknown")
                        ),
                        provider_no_spend_safe=True,
                        effective_agent_type=session.effective_agent_type,
                        reverse_io=session.bridge._reverse_io_attestation(),
                    )
                    await self._start_turn(session, request.execution_id, prompt)
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
        submission_ready = False
        try:
            await asyncio.wait_for(
                _wait_for_prompt_write_or_terminal(turn),
                timeout=self._cancel_timeout,
            )
            submission_ready = True
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok ACP prompt submission was not confirmed before cancellation",
                category="adapter",
            ) from exc
        finally:
            if not submission_ready:
                async with session.lock:
                    if session.turn is turn:
                        session.interrupting = False
                        session.interrupt_done.set()
        async with session.lock:
            _require_grok_action(request, session)
            if session.turn is not turn:
                session.interrupting = False
                session.interrupt_done.set()
                raise ServiceError(
                    "CONTEXT_DRIFT", "Grok ACP turn identity changed"
                )
            if turn.task.done():
                captured = session.snapshot
                session.interrupting = False
                session.interrupt_done.set()
                return captured
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
                    provider_no_spend_safe=True,
                    effective_agent_type=session.effective_agent_type,
                    reverse_io=session.bridge._reverse_io_attestation(),
                )
            session.closed = True
            session.closing = False
            session.native_closed = True
            session.snapshot = replace(
                session.snapshot,
                evidence={
                    **dict(session.snapshot.evidence),
                    "reverse_io": _reverse_io_evidence(
                        session.bridge._reverse_io_attestation()
                    ),
                },
            )
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

    async def _start_turn(
        self,
        session: _GrokSession,
        execution_id: str,
        prompt: str,
    ) -> None:
        await session.bridge.activate_turn(execution_id)
        session.public_text.reset()
        write_receipt = asyncio.Event()
        task = asyncio.create_task(
            self._run_turn(
                session,
                execution_id=execution_id,
                prompt=prompt,
                write_receipt=write_receipt,
            )
        )
        session.turn = _GrokTurn(execution_id, task, write_receipt)

    async def _run_turn(
        self,
        session: _GrokSession,
        *,
        execution_id: str,
        prompt: str,
        write_receipt: asyncio.Event,
    ) -> None:
        result: Mapping[str, object] | None = None
        failure: AdapterFailure | None = None
        provider_code: str | None = None
        provider_detail: str | None = None
        rpc_code: int | str | None = None
        stop_reason: str | None = None
        try:
            try:
                result = await session.process.request(
                    "session/prompt",
                    {
                        "sessionId": session.snapshot.external_session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    },
                    write_receipt=write_receipt,
                    reverse_scope=execution_id,
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
        finally:
            cleanup = asyncio.create_task(session.bridge.deactivate_turn(execution_id))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                await cleanup
                raise
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
            if failure is None:
                try:
                    await self._assert_context_current(session.context)
                except ServiceError:
                    failure = AdapterFailure(
                        "CONTEXT_DRIFT",
                        "context",
                        False,
                        "Grok project context changed before terminal acceptance",
                    )
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
                    provider_no_spend_safe=True,
                    effective_agent_type=session.effective_agent_type,
                    reverse_io=session.bridge._reverse_io_attestation(),
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
                provider_no_spend_safe=True,
                effective_agent_type=session.effective_agent_type,
                reverse_io=session.bridge._reverse_io_attestation(),
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


async def _deny_billing_guard_reverse_request(
    method: str,
    params: Mapping[str, object],
    reverse_scope: str | None,
) -> Mapping[str, object]:
    del method, params, reverse_scope
    raise AcpMethodNotFoundError("Billing guard exposes no reverse methods")


async def _ignore_billing_guard_notification(
    method: str,
    params: Mapping[str, object],
) -> None:
    del method, params


def _initialize_params() -> dict[str, object]:
    return {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        },
        "clientInfo": {"name": "subagent-mcp", "version": __version__},
    }


def _validate_initialize_response(value: Mapping[str, object]) -> str:
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
    metadata = value.get("_meta")
    if not isinstance(metadata, Mapping):
        raise _capability_error("Grok ACP authentication metadata is incomplete")
    default_method = metadata.get("defaultAuthMethodId")
    if default_method != "cached_token" or default_method not in identifiers:
        raise _capability_error("Grok cached-native authentication is not selected")
    return "cached_token"


def _validate_authenticate_response(value: Mapping[str, object]) -> None:
    if not value:
        return
    if set(value) != {"_meta"} or not isinstance(value.get("_meta"), Mapping):
        raise _capability_error("Grok ACP authentication response is malformed")


def _validate_provider_no_spend(
    billing: Mapping[str, object],
    auto_topup: Mapping[str, object],
) -> bool:
    if len(billing) > 64 or len(auto_topup) > 64:
        raise _billing_capability_error()
    config = billing.get("config")
    prepaid = config.get("prepaidBalance") if isinstance(config, Mapping) else None
    on_demand_cap = config.get("onDemandCap") if isinstance(config, Mapping) else None
    if (
        not isinstance(config, Mapping)
        or len(config) > 64
        or not isinstance(prepaid, Mapping)
        or len(prepaid) > 64
        or not isinstance(on_demand_cap, Mapping)
        or len(on_demand_cap) > 64
        or config.get("isUnifiedBillingUser") is not True
    ):
        if isinstance(config, Mapping) and config.get("isUnifiedBillingUser") is False:
            raise _billing_policy_error()
        raise _billing_capability_error()

    prepaid_value = _billing_number(prepaid.get("val", 0))
    on_demand_value = _billing_number(on_demand_cap.get("val", 0))
    if prepaid_value is None or on_demand_value is None:
        raise _billing_capability_error()
    if prepaid_value < 0 or on_demand_value < 0:
        raise _billing_capability_error()
    if prepaid_value != 0 or on_demand_value != 0:
        raise _billing_policy_error()

    if "onDemandEnabled" in billing:
        on_demand_enabled = billing.get("onDemandEnabled")
        if not isinstance(on_demand_enabled, bool):
            raise _billing_capability_error()
        if on_demand_enabled:
            raise _billing_policy_error()

    if "creditUsagePercent" in config:
        usage = _billing_number(config.get("creditUsagePercent"))
        if usage is None or usage < 0 or usage > 100:
            raise _billing_capability_error()
        if usage == 100:
            raise ServiceError(
                "QUOTA_PAUSED",
                "Grok included allowance is exhausted",
                category="quota",
                next_action=(
                    "Wait for provider allowance or refresh current availability; "
                    "do not enable credits or auto-topup."
                ),
            )

    if "rule" in auto_topup:
        rule = auto_topup.get("rule")
        if rule is None:
            return True
        if not isinstance(rule, Mapping) or len(rule) > 64:
            raise _billing_capability_error()
        enabled = rule.get("enabled", False)
        if not isinstance(enabled, bool):
            raise _billing_capability_error()
        if enabled:
            raise _billing_policy_error()
    return True


def _billing_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _billing_capability_error() -> ServiceError:
    return ServiceError(
        "CAPABILITY_MISSING",
        "Grok native no-spend attestation is incomplete or malformed",
        category="capability",
        next_action=(
            "Refresh native Grok billing availability, then start a fresh task; "
            "do not enable credits or auto-topup."
        ),
    )


def _billing_policy_error() -> ServiceError:
    return ServiceError(
        "POLICY_REJECTED",
        "Grok paid spending is not proven disabled",
        category="policy",
        next_action=(
            "Disable paid balance, on-demand spending, and auto-topup in the native "
            "provider UI before a fresh task; Subagent MCP will not change billing."
        ),
    )


def _parse_models_list_response(
    value: Mapping[str, object],
    selected_model: str,
) -> str:
    if len(value) > 64 or value.get("error") is not None:
        raise _capability_error("Grok ACP model-agent metadata is unavailable")
    state = value.get("result")
    if not isinstance(state, Mapping):
        raise _capability_error("Grok ACP model-agent metadata is unavailable")
    return _selected_model_agent_type(state, selected_model)


def _validate_agent_type(value: object) -> str:
    agent_type = _bounded_public_text(value, _MAX_AGENT_TYPE_BYTES)
    if (
        agent_type is None
        or agent_type != agent_type.strip()
        or len(agent_type.encode("utf-8")) > _MAX_AGENT_TYPE_BYTES
    ):
        raise _capability_error("Grok selected model agent type is unavailable")
    return agent_type


def _selected_model_agent_type(
    state: Mapping[str, object],
    selected_model: str,
) -> str:
    if len(state) > 64:
        raise _capability_error("Grok ACP model state is malformed")
    try:
        current_model = validate_model_id(state.get("currentModelId"))
    except ContractError as exc:
        raise _capability_error("Grok ACP model state is malformed") from exc
    available = state.get("availableModels")
    if not isinstance(available, list) or not 1 <= len(available) <= _MAX_CATALOG_ITEMS:
        raise _capability_error("Grok ACP model state is malformed")
    selected_metadata: Mapping[str, object] | None = None
    seen: set[str] = set()
    for item in available:
        if not isinstance(item, Mapping) or len(item) > 64:
            raise _capability_error("Grok ACP model state is malformed")
        try:
            model_id = validate_model_id(item.get("modelId"))
        except ContractError as exc:
            raise _capability_error("Grok ACP model state is malformed") from exc
        if model_id in seen:
            raise _capability_error("Grok ACP model state is ambiguous")
        seen.add(model_id)
        if model_id == selected_model:
            metadata = item.get("_meta")
            if not isinstance(metadata, Mapping) or len(metadata) > 64:
                raise _capability_error(
                    "Grok selected model agent type is unavailable"
                )
            selected_metadata = metadata
    if current_model not in seen or selected_metadata is None:
        raise _capability_error("Grok selected model is absent from ACP model state")
    if current_model != selected_model:
        raise _capability_error("Grok selected model is not current in ACP model state")
    return _validate_agent_type(selected_metadata.get("agentType"))


def _parse_session_handshake(
    context: ResolvedContext,
    binding: GrokBinding,
    auth_method: str,
    provider_no_spend_safe: bool,
    session_result: Mapping[str, object],
    expected_agent_type: str,
) -> tuple[str, GrokSessionToolAttestation]:
    session_id = _bounded_public_text(session_result.get("sessionId"), 256)
    models = session_result.get("models")
    metadata = session_result.get("_meta")
    if (
        session_id is None
        or not isinstance(models, Mapping)
        or not isinstance(metadata, Mapping)
    ):
        raise _capability_error("Grok ACP session metadata is incomplete")
    current_model = models.get("currentModelId")
    try:
        effective_model = validate_model_id(current_model)
    except ContractError as exc:
        raise _capability_error("Grok ACP effective model is invalid") from exc
    effective_agent_type = _selected_model_agent_type(models, effective_model)
    if effective_agent_type != expected_agent_type:
        raise _capability_error(
            "Grok selected model agent type changed during session creation"
        )
    workspace_path = _bounded_public_text(
        metadata.get("currentWorkingDirectory"), 4096
    )
    session_config = metadata.get("x.ai/sessionConfig")
    if workspace_path is None or not isinstance(session_config, Mapping):
        raise _capability_error("Grok ACP session metadata is incomplete")
    selected_model, reasoning_effort = _selected_session_options(session_config)
    if selected_model != effective_model:
        raise _capability_error("Grok ACP model selection is inconsistent")
    return session_id, GrokSessionToolAttestation(
        pair_key=binding.pair_key,
        external_session_id=session_id,
        workspace_key=context.workspace_key,
        workspace_path=workspace_path,
        mode=str(context.attestation.get("mode", "")),
        effective_model=effective_model,
        reasoning_effort=reasoning_effort,
        auth_method=auth_method,
        provider_no_spend_safe=provider_no_spend_safe,
        effective_agent_type=effective_agent_type,
        agent_type_source=_AGENT_TYPE_EVIDENCE_SOURCE,
    )


def _selected_session_options(value: Mapping[str, object]) -> tuple[str, str]:
    options = value.get("options")
    if not isinstance(options, list) or not 1 <= len(options) <= 256:
        raise _capability_error("Grok ACP session options are incomplete")
    selected: dict[str, str] = {}
    for item in options:
        if not isinstance(item, Mapping) or not isinstance(item.get("selected"), bool):
            raise _capability_error("Grok ACP session options are malformed")
        category = _bounded_public_text(item.get("category"), 64)
        identifier = _bounded_public_text(item.get("id"), 256)
        if category is None or identifier is None:
            raise _capability_error("Grok ACP session options are malformed")
        if item["selected"] is not True or category not in {"model", "mode"}:
            continue
        if category in selected:
            raise _capability_error("Grok ACP session selection is ambiguous")
        selected[category] = identifier
    if set(selected) != {"model", "mode"}:
        raise _capability_error("Grok ACP session selection is incomplete")
    try:
        model = validate_model_id(selected["model"])
    except ContractError as exc:
        raise _capability_error("Grok ACP selected model is invalid") from exc
    effort = _reasoning_effort({"effort": selected["mode"]})
    return model, effort


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
    if reason == "max_tokens":
        return (
            reason,
            AdapterFailure(
                "MAX_TOKENS_REACHED",
                "provider",
                False,
                "Grok ACP reached the output token limit",
                (
                    "Inspect the partial result, then send a bounded follow-up in "
                    "the same conversation; do not retry, switch models, or enable "
                    "credits automatically."
                ),
            ),
            None,
            None,
        )
    if reason == "max_turn_requests":
        return (
            reason,
            AdapterFailure(
                "MAX_TURN_REQUESTS_REACHED",
                "provider",
                False,
                "Grok ACP reached the native turn-request limit",
                (
                    "Inspect the partial result, then start a new bounded task if "
                    "more work is required; do not retry, switch models, or enable "
                    "credits automatically."
                ),
            ),
            None,
            None,
        )
    if reason == "refusal":
        return (
            reason,
            AdapterFailure(
                "REQUEST_REFUSED",
                "policy",
                False,
                "Grok declined the bounded request",
                (
                    "Review the task and authority, then submit a materially revised "
                    "request only if appropriate; do not switch models or enable "
                    "credits automatically."
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


def _reverse_io_evidence(value: Mapping[str, object]) -> dict[str, object]:
    if value.get("scope") != _REVERSE_IO_SCOPE or not isinstance(
        value.get("saturated"), bool
    ):
        raise ServiceError(
            "RECOVERY_REQUIRED",
            "Grok reverse-I/O attestation is malformed",
            category="adapter",
        )
    evidence: dict[str, object] = {"scope": _REVERSE_IO_SCOPE}
    for name in _REVERSE_IO_COUNTERS:
        count = value.get(name)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= _MAX_REVERSE_IO_COUNT
        ):
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "Grok reverse-I/O attestation is malformed",
                category="adapter",
            )
        evidence[name] = count
    evidence["saturated"] = value["saturated"]
    return evidence


def _grok_snapshot(
    context: ResolvedContext,
    *,
    session_id: str,
    execution_id: str,
    execution_state: str,
    quota_state: str,
    provider_no_spend_safe: bool = False,
    effective_agent_type: str,
    result_text: str | None = None,
    error: AdapterFailure | None = None,
    stop_reason: str | None = None,
    provider_code: str | None = None,
    rpc_code: int | str | None = None,
    public_text_truncated: bool = False,
    provider_detail: str | None = None,
    reverse_io: Mapping[str, object],
) -> AdapterSnapshot:
    mode = str(context.attestation.get("mode", "unknown"))
    write_set = context.attestation.get("write_set", ())
    profile_digest = context.attestation.get("requested_agent_profile_sha256")
    if (
        not isinstance(profile_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", profile_digest) is None
        or context.attestation.get("agent_profile_binding")
        != _AGENT_PROFILE_BINDING
        or context.attestation.get("agent_type_evidence_source")
        != _AGENT_TYPE_EVIDENCE_SOURCE
    ):
        raise ServiceError(
            "RECOVERY_REQUIRED",
            "Grok agent-profile attestation is malformed",
            category="adapter",
        )
    effective_agent_type = _validate_agent_type(effective_agent_type)
    evidence: dict[str, object] = {
        "source": "grok-build-native-acp",
        "pair_key": str(context.attestation.get("pair_key", "")),
        "protocol_version": 1,
        "workspace_hash": context.context_hash,
        "permission_mode": mode,
        "write_set_digest": hashlib.sha256(_canonical_json(write_set)).hexdigest(),
        "auth_method": "cached_token",
        "auth_evidence_source": "initialize._meta.defaultAuthMethodId",
        "route_isolation": "verified",
        "route_isolation_source": "isolated-home-native-inspect",
        "client_terminal_enabled": True,
        "reverse_terminal_authorized": False,
        "agent_profile_request_sha256": profile_digest,
        "agent_profile_request_source": _AGENT_PROFILE_BINDING,
        "effective_agent_type": effective_agent_type,
        "agent_type_evidence_source": _AGENT_TYPE_EVIDENCE_SOURCE,
        "web_search_enabled": False,
        "nested_agents_enabled": False,
        "mcp_servers": [],
        "quota_state": quota_state,
        "connection_owned_session": True,
        "public_text_truncated": public_text_truncated,
        "reverse_io": _reverse_io_evidence(reverse_io),
    }
    if provider_no_spend_safe:
        evidence["provider_no_spend_safe"] = True
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


def _read_grok_model_catalog(
    binding: GrokBinding,
    environment: Mapping[str, str],
) -> object:
    return _parse_catalog(
        _run_grok(binding.executable_path, ("models",), environment=environment)
    )


def _read_grok_inspect(
    binding: GrokBinding,
    workspace_path: str,
    environment: Mapping[str, str] | None = None,
) -> GrokInspectObservation:
    raw = _run_grok(
        binding.executable_path,
        ("--cwd", workspace_path, "inspect", "--json"),
        environment=environment,
    )
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise GrokBindingIncompatible("Grok inspect JSON is invalid") from exc
    if not isinstance(value, Mapping):
        raise GrokBindingIncompatible("Grok inspect root is invalid")
    login_policy = value.get("loginPolicy")
    config_sources = value.get("configSources")
    if not isinstance(login_policy, Mapping) or not isinstance(config_sources, Mapping):
        raise GrokBindingIncompatible("Grok inspect isolation policy is unavailable")
    api_key_auth_disabled = login_policy.get("apiKeyAuthDisabled")
    if api_key_auth_disabled is not True:
        raise GrokBindingIncompatible("Grok inspect isolation policy is invalid")
    project_trusted = value.get("projectTrusted")
    if type(project_trusted) is not bool:
        raise GrokBindingIncompatible("Grok inspect project trust is invalid")
    if "projectRoot" not in value:
        raise GrokBindingIncompatible("Grok inspect project root is unavailable")
    project_root = _validate_inspect_project_root(
        value.get("projectRoot"),
        Path(workspace_path),
    )
    expected_config = _inspect_runtime_config_path(environment)
    config_source_path = _validate_inspect_config_source(
        config_sources,
        expected_config,
    )
    _validate_inspect_external_compat(value.get("externalCompat"))
    _validate_inspect_permissions(value.get("permissions"))
    _validate_inspect_effective_surfaces(value, binding.version)
    inspect_instructions = _attest_project_instructions(
        value.get("projectInstructions"),
        Path(workspace_path),
    )
    builtin_agent_count = _validate_inspect_builtin_agents(value.get("agents"))
    _validate_inspect_warnings(value)
    return GrokInspectObservation(
        pair_key=binding.pair_key,
        workspace_path=workspace_path,
        mcp_servers=(),
        hooks=(),
        plugins=(),
        compatibility_mcp_servers=(),
        builtin_tool_inventory="not_exposed",
        permission_keys=(),
        permission_rules=(),
        permission_modes=(),
        api_key_auth_disabled=api_key_auth_disabled,
        config_source_layer_count=1,
        config_source_path=config_source_path,
        compatibility_isolated=True,
        permission_sources_isolated=True,
        external_surfaces_empty=True,
        builtin_agent_count=builtin_agent_count,
        project_instructions=inspect_instructions,
        project_trusted=project_trusted,
        project_root=project_root,
        git_attestation=None,
    )


def _inspect_runtime_config_path(environment: Mapping[str, str] | None) -> Path:
    if not isinstance(environment, Mapping):
        raise GrokBindingIncompatible("Grok inspect runtime home is unavailable")
    raw_home = environment.get("GROK_HOME")
    if _bounded_public_text(raw_home, 32_768) is None:
        raise GrokBindingIncompatible("Grok inspect runtime home is invalid")
    home = Path(raw_home)
    if not home.is_absolute():
        raise GrokBindingIncompatible("Grok inspect runtime home is invalid")
    return home / "config.toml"


def _validate_inspect_project_root(
    value: object,
    workspace: Path,
) -> str | None:
    if value is None:
        return None
    candidate = _lexical_local_dos_path(value, "project root")
    expected = _lexical_local_dos_path(str(workspace), "workspace")
    if _fold_lexical_dos_path(candidate) != _fold_lexical_dos_path(expected):
        raise GrokBindingIncompatible("Grok inspect project root is outside workspace")
    assert isinstance(value, str)
    return value


def _validate_inspect_config_source(
    value: Mapping[str, object],
    expected_config: Path,
) -> str:
    if set(value) != {"layers"}:
        raise GrokBindingIncompatible("Grok inspect config sources are invalid")
    layers = value.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise GrokBindingIncompatible("Grok inspect config sources are invalid")
    layer = layers[0]
    if not isinstance(layer, Mapping) or not {"role", "path"} <= set(layer) <= {
        "role",
        "path",
        "note",
    }:
        raise GrokBindingIncompatible("Grok inspect config source is invalid")
    path = layer.get("path")
    if (
        layer.get("role") != "user"
        or ("note" in layer and layer.get("note") is not None)
    ):
        raise GrokBindingIncompatible("Grok inspect config source is not isolated")
    candidate = _lexical_local_dos_path(path, "config source")
    expected = _lexical_local_dos_path(str(expected_config), "runtime config")
    if _fold_lexical_dos_path(candidate) != _fold_lexical_dos_path(expected):
        raise GrokBindingIncompatible("Grok inspect config source is not isolated")
    assert isinstance(path, str)
    return path


def _validate_inspect_external_compat(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "remoteSettingsLoaded",
        "cells",
    }:
        raise GrokBindingIncompatible("Grok inspect compatibility policy is invalid")
    cells = value.get("cells")
    if value.get("remoteSettingsLoaded") is not False or not isinstance(cells, list):
        raise GrokBindingIncompatible("Grok inspect compatibility policy is unsafe")
    observed: list[tuple[str, str]] = []
    for cell in cells:
        if not isinstance(cell, Mapping) or set(cell) != {
            "vendor",
            "surface",
            "enabled",
            "source",
        }:
            raise GrokBindingIncompatible("Grok inspect compatibility cell is invalid")
        vendor = cell.get("vendor")
        surface = cell.get("surface")
        if (
            not _safe_name(vendor)
            or not _safe_name(surface)
            or cell.get("enabled") is not False
            or cell.get("source") != "config"
        ):
            raise GrokBindingIncompatible("Grok inspect compatibility cell is unsafe")
        assert isinstance(vendor, str) and isinstance(surface, str)
        observed.append((vendor, surface))
    if len(observed) != len(_COMPATIBILITY_CELLS) or set(observed) != set(
        _COMPATIBILITY_CELLS
    ):
        raise GrokBindingIncompatible("Grok inspect compatibility cells are incomplete")


def _validate_inspect_permissions(value: object) -> None:
    required = {
        "sources",
        "loaded",
        "skipped",
        "mcpServerAllowlist",
        "marketplaceAllowlist",
        "managedSettingsExists",
        "managedSettingsActive",
    }
    optional = {"managedSettingsPath", "enforced"}
    if not isinstance(value, Mapping) or not required <= set(value) <= required | optional:
        raise GrokBindingIncompatible("Grok inspect permissions are invalid")
    if (
        value.get("sources") != []
        or type(value.get("loaded")) is not int
        or value.get("loaded") != 0
        or value.get("skipped") != []
        or value.get("mcpServerAllowlist") != []
        or value.get("marketplaceAllowlist") != []
        or value.get("managedSettingsExists") is not False
        or value.get("managedSettingsActive") is not False
        or ("managedSettingsPath" in value and value.get("managedSettingsPath") is not None)
        or ("enforced" in value and value.get("enforced") != [])
    ):
        raise GrokBindingIncompatible("Grok inspect permissions are not isolated")


def _validate_inspect_effective_surfaces(
    value: Mapping[str, object],
    binding_version: str,
) -> None:
    # Grok 1.0.5 inspect lists discovered plugins, not the live enabled registry.
    # The exact locked config owns enablement; these rows are display-only evidence.
    if (
        any(value.get(key) for key in ("plugins", "hooks", "skills"))
        and binding_version not in _PROVEN_DISCOVERY_DISPLAY_VERSIONS
    ):
        raise GrokBindingIncompatible(
            "Grok inspect discovery-display semantics are unproven for this build"
        )
    plugins = _validate_inspect_inert_plugins(value.get("plugins"))
    _validate_inspect_inert_plugin_hooks(value.get("hooks"), plugins)
    _validate_inspect_disabled_skills(value.get("skills"), plugins)
    for key in ("mcpServers", "lspServers", "marketplaces"):
        if value.get(key) != []:
            raise GrokBindingIncompatible(f"Grok inspect {key} must be empty")
    for key in ("compatibilityMcpServers", "permissionRules", "permissionModes"):
        if key in value and value.get(key) != []:
            raise GrokBindingIncompatible(f"Grok inspect {key} must be empty")


def _validate_inspect_inert_plugins(
    value: object,
) -> dict[tuple[str, str], PureWindowsPath]:
    if not isinstance(value, list) or len(value) > 128:
        raise GrokBindingIncompatible("Grok inspect plugins are invalid")
    observed: dict[tuple[str, str], PureWindowsPath] = {}
    names: set[str] = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {
            "name",
            "scope",
            "path",
            "enabled",
            "provides",
        }:
            raise GrokBindingIncompatible("Grok inspect plugin discovery is invalid")
        name = row.get("name")
        path = _lexical_local_dos_path(row.get("path"), "plugin")
        provides = row.get("provides")
        if (
            not _safe_name(name)
            or row.get("scope") not in {"user", "project"}
            or type(row.get("enabled")) is not bool
            or not isinstance(provides, Mapping)
            or set(provides) != {"skills", "agents", "hooks", "mcpServers"}
            or type(provides.get("skills")) is not int
            or not 0 <= provides.get("skills", -1) <= 128
            or type(provides.get("agents")) is not int
            or not 0 <= provides.get("agents", -1) <= 128
            or type(provides.get("hooks")) is not bool
            or type(provides.get("mcpServers")) is not int
            or not 0 <= provides.get("mcpServers", -1) <= 128
        ):
            raise GrokBindingIncompatible("Grok inspect plugin discovery is unsafe")
        assert isinstance(name, str)
        key = (name.casefold(), _fold_lexical_dos_path(path))
        if name.casefold() in names or key in observed:
            raise GrokBindingIncompatible("Grok inspect plugin discovery is duplicated")
        names.add(name.casefold())
        observed[key] = path
    return observed


def _validate_inspect_inert_plugin_hooks(
    value: object,
    plugins: Mapping[tuple[str, str], PureWindowsPath],
) -> None:
    if not isinstance(value, list) or len(value) > 128:
        raise GrokBindingIncompatible("Grok inspect hooks are invalid")
    seen: set[tuple[str, str, str]] = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {
            "event",
            "hookType",
            "target",
            "source",
            "matcher",
        }:
            raise GrokBindingIncompatible("Grok inspect plugin hook is invalid")
        source = row.get("source")
        if not isinstance(source, Mapping) or set(source) != {
            "type",
            "plugin_name",
            "path",
        }:
            raise GrokBindingIncompatible("Grok inspect plugin hook source is invalid")
        name = source.get("plugin_name")
        source_path = _lexical_local_dos_path(source.get("path"), "plugin hook source")
        key = (
            name.casefold() if isinstance(name, str) else "",
            _fold_lexical_dos_path(source_path),
        )
        hook_type = row.get("hookType")
        target = row.get("target")
        if (
            source.get("type") != "plugin"
            or not _safe_name(name)
            or key not in plugins
            or row.get("event") != "(plugin)"
            or hook_type not in {"file", "inline"}
            or row.get("matcher") is not None
            or not isinstance(target, str)
        ):
            raise GrokBindingIncompatible("Grok inspect plugin hook is unsafe")
        if hook_type == "file":
            target_path = _lexical_inert_plugin_hook_file_target(target)
            try:
                relative = target_path.relative_to(plugins[key])
            except ValueError as exc:
                raise GrokBindingIncompatible(
                    "Grok inspect plugin hook escapes its discovery root"
                ) from exc
            if not relative.parts:
                raise GrokBindingIncompatible("Grok inspect plugin hook is unsafe")
            normalized_target = _fold_lexical_dos_path(target_path)
        elif target != "":
            raise GrokBindingIncompatible("Grok inspect inline plugin hook is unsafe")
        else:
            normalized_target = ""
        identity = (key[0], key[1], normalized_target)
        if identity in seen:
            raise GrokBindingIncompatible("Grok inspect plugin hook is duplicated")
        seen.add(identity)


def _validate_inspect_disabled_skills(
    value: object,
    plugins: Mapping[tuple[str, str], PureWindowsPath],
) -> None:
    if not isinstance(value, list) or len(value) > 128:
        raise GrokBindingIncompatible("Grok inspect skills are invalid")
    required = {"name", "description", "source", "userInvocable", "disabled"}
    optional = {"vendor", "compatibilityStatus", "collidesWith", "invocableAs"}
    seen: set[tuple[str, str, str]] = set()
    for row in value:
        if not isinstance(row, Mapping) or not required <= set(row) <= required | optional:
            raise GrokBindingIncompatible("Grok inspect disabled skill is invalid")
        source = row.get("source")
        if not isinstance(source, Mapping):
            raise GrokBindingIncompatible("Grok inspect disabled skill source is invalid")
        source_type = source.get("type")
        if source_type == "plugin":
            if set(source) != {"type", "plugin_name", "path"}:
                raise GrokBindingIncompatible("Grok inspect disabled skill source is invalid")
            plugin_name = source.get("plugin_name")
            source_path = _lexical_local_dos_path(source.get("path"), "skill source")
            plugin_roots = [
                root
                for (name, _path), root in plugins.items()
                if isinstance(plugin_name, str) and name == plugin_name.casefold()
            ]
            if not _safe_name(plugin_name) or len(plugin_roots) != 1:
                raise GrokBindingIncompatible("Grok inspect disabled plugin skill is unsafe")
            try:
                relative = source_path.relative_to(plugin_roots[0])
            except ValueError as exc:
                raise GrokBindingIncompatible(
                    "Grok inspect disabled plugin skill escapes its discovery root"
                ) from exc
            if not relative.parts:
                raise GrokBindingIncompatible(
                    "Grok inspect disabled plugin skill is not a discovery child"
                )
        else:
            if (
                source_type not in {
                    "user",
                    "project",
                    "bundled",
                    "server",
                    "configToml",
                }
                or set(source) != {"type", "path"}
            ):
                raise GrokBindingIncompatible("Grok inspect disabled skill source is unsafe")
            source_path = _lexical_local_dos_path(source.get("path"), "skill source")
        name = _bounded_public_text(row.get("name"), 256)
        if (
            name is None
            or _bounded_public_text(row.get("description"), 4096) is None
            or type(row.get("userInvocable")) is not bool
            or row.get("disabled") is not True
            or (
                "compatibilityStatus" in row
                and row.get("compatibilityStatus") != "disabled"
            )
            or (
                "vendor" in row
                and not _safe_name(row.get("vendor"))
            )
            or any(
                key in row and _bounded_public_text(row.get(key), 256) is None
                for key in ("collidesWith", "invocableAs")
            )
        ):
            raise GrokBindingIncompatible("Grok inspect active skill is unsafe")
        identity = (
            name.casefold(),
            str(source_type),
            _fold_lexical_dos_path(source_path),
        )
        if identity in seen:
            raise GrokBindingIncompatible("Grok inspect disabled skill is duplicated")
        seen.add(identity)


def _validate_inspect_warnings(value: Mapping[str, object]) -> None:
    if "configWarnings" in value and value.get("configWarnings") != [
        {
            "target": "configKey",
            "path": "claude_compat",
            "kind": "unknown-field",
            "reason": "unrecognized config key",
        }
    ]:
        raise GrokBindingIncompatible("Grok inspect configuration warnings are unsafe")
    if "mcpConfigProblems" in value:
        raise GrokBindingIncompatible("Grok inspect MCP configuration problems are unsafe")


def _lexical_local_dos_path(value: object, label: str) -> PureWindowsPath:
    text = _bounded_public_text(value, 32_768)
    if text is None:
        raise GrokBindingIncompatible(f"Grok inspect {label} path is invalid")
    normalized = text.replace("/", "\\")
    if (
        normalized.startswith("\\\\")
        or not re.match(r"^[A-Za-z]:\\", normalized)
        or any(unicodedata.category(character) == "Cc" for character in text)
    ):
        raise GrokBindingIncompatible(f"Grok inspect {label} path is invalid")
    parts = normalized[3:].split("\\")
    if not parts:
        raise GrokBindingIncompatible(f"Grok inspect {label} path is invalid")
    for part in parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if (
            not part
            or part in {".", ".."}
            or part.endswith((" ", "."))
            or ":" in part
            or stem in _WINDOWS_RESERVED_NAMES
        ):
            raise GrokBindingIncompatible(f"Grok inspect {label} path is invalid")
    path = PureWindowsPath(normalized)
    if not path.is_absolute() or len(path.drive) != 2:
        raise GrokBindingIncompatible(f"Grok inspect {label} path is invalid")
    return path


def _lexical_inert_plugin_hook_file_target(value: object) -> PureWindowsPath:
    text = _bounded_public_text(value, 32_768)
    if text is None:
        raise GrokBindingIncompatible(
            "Grok inspect plugin hook target path is invalid"
        )
    normalized = text.replace("/", "\\")
    if normalized.startswith("\\\\") or not re.match(r"^[A-Za-z]:\\", normalized):
        raise GrokBindingIncompatible(
            "Grok inspect plugin hook target path is invalid"
        )
    parts = normalized.split("\\")
    normalized = "\\".join(
        part
        for index, part in enumerate(parts)
        if part != "." or index in {0, len(parts) - 1}
    )
    return _lexical_local_dos_path(normalized, "plugin hook target")


def _fold_lexical_dos_path(path: PureWindowsPath) -> str:
    return ntpath.normcase(str(path)).casefold()


def _attest_project_instructions(
    value: object,
    workspace: Path,
) -> tuple[GrokProjectInstructionAttestation, ...]:
    if not isinstance(value, list) or len(value) > _MAX_INSTRUCTION_FILES:
        raise GrokBindingIncompatible("Grok inspect project instructions are invalid")
    workspace_lexical = _lexical_local_dos_path(str(workspace), "workspace")
    observed: list[GrokProjectInstructionAttestation] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise GrokBindingIncompatible(
                "Grok inspect project instruction is invalid"
            )
        candidate = _lexical_local_dos_path(item.get("path"), "project instruction")
        candidate_key = _fold_lexical_dos_path(candidate)
        if candidate_key in seen:
            raise GrokBindingIncompatible(
                "Grok inspect project instruction is duplicated"
            )
        seen.add(candidate_key)
        if {"vendor", "disabled", "compatibilityStatus"} & set(item):
            _validate_disabled_compat_instruction(
                item,
                candidate,
                workspace_lexical,
            )
            continue
        if set(item) != {
            "path",
            "scope",
            "fileType",
            "sizeBytes",
            "approxTokens",
        }:
            raise GrokBindingIncompatible(
                "Grok inspect project instruction is invalid"
            )
        size = item.get("sizeBytes")
        tokens = item.get("approxTokens")
        if (
            item.get("scope") != "project"
            or item.get("fileType") != "agents_md"
            or type(size) is not int
            or not 0 <= size <= _MAX_COMMAND_BYTES
            or type(tokens) is not int
            or not 0 <= tokens <= _MAX_COMMAND_BYTES
        ):
            raise GrokBindingIncompatible(
                "Grok inspect project instruction is invalid"
            )
        try:
            relative = candidate.relative_to(workspace_lexical)
        except ValueError as exc:
            raise GrokBindingIncompatible(
                "Grok inspect project instruction escapes the workspace"
            ) from exc
        relative_parts = tuple(relative.parts)
        if (
            not relative_parts
            or not _is_grok_instruction_basename(relative_parts[-1])
        ):
            raise GrokBindingIncompatible(
                "Grok inspect project instruction is unsupported"
            )
        normalized_relative = "/".join(relative_parts)
        observed.append(
            _attest_project_instruction_file(
                workspace,
                relative_parts,
                normalized_relative,
                size,
            )
        )
    return tuple(sorted(observed, key=lambda row: row.relative_path.casefold()))


def _validate_disabled_compat_instruction(
    item: Mapping[str, object],
    candidate: PureWindowsPath,
    workspace: PureWindowsPath,
) -> None:
    if set(item) != {
        "path",
        "scope",
        "fileType",
        "sizeBytes",
        "approxTokens",
        "vendor",
        "disabled",
        "compatibilityStatus",
    }:
        raise GrokBindingIncompatible(
            "Grok inspect disabled compatibility instruction is invalid"
        )
    size = item.get("sizeBytes")
    tokens = item.get("approxTokens")
    file_type = item.get("fileType")
    if (
        item.get("scope") not in {"global", "project"}
        or file_type not in {"agents_md", "rules"}
        or item.get("vendor") not in {"claude", "cursor"}
        or item.get("disabled") is not True
        or item.get("compatibilityStatus") != "disabled"
        or type(size) is not int
        or not 0 <= size <= _MAX_COMMAND_BYTES
        or type(tokens) is not int
        or not 0 <= tokens <= _MAX_COMMAND_BYTES
        or (
            file_type == "agents_md"
            and not _is_grok_instruction_basename(candidate.name)
        )
    ):
        raise GrokBindingIncompatible(
            "Grok inspect disabled compatibility instruction is unsafe"
        )
    if item.get("scope") == "project":
        try:
            relative = candidate.relative_to(workspace)
        except ValueError as exc:
            raise GrokBindingIncompatible(
                "Grok inspect disabled project instruction escapes the workspace"
            ) from exc
        if not relative.parts:
            raise GrokBindingIncompatible(
                "Grok inspect disabled project instruction is invalid"
            )


def _scan_grok_instruction_manifest(
    workspace: Path,
    project_root: str | None,
    git_attestation: GrokGitAttestation | None = None,
) -> tuple[GrokProjectInstructionAttestation, ...]:
    rows, _selected_git = _scan_grok_instruction_context(
        workspace,
        project_root,
        git_attestation,
    )
    return rows


def _scan_grok_instruction_context(
    workspace: Path,
    project_root: str | None,
    git_attestation: GrokGitAttestation | None = None,
) -> tuple[
    tuple[GrokProjectInstructionAttestation, ...],
    GrokGitAttestation | None,
]:
    try:
        root = workspace.resolve(strict=True)
        if root != workspace or not root.is_dir() or _is_reparse_point(root):
            raise OSError("unsafe workspace root")
        if project_root is None:
            if git_attestation is not None:
                raise OSError("non-Git workspace has unexpected Git evidence")
            with os.scandir(root) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
            if len(entries) > _MAX_INSTRUCTION_SCAN_ENTRIES:
                raise OSError("instruction root scan limit exceeded")
            relative_paths = tuple(
                (entry.name,)
                for entry in entries
                if not entry.is_dir(follow_symlinks=False)
                and _is_grok_instruction_basename(entry.name)
            )
            selected_git = None
        else:
            _validate_inspect_project_root(project_root, root)
            selected_git = git_attestation or _attest_git_executable(root)
            selected_git = _bind_git_root_attestation(root, selected_git)
            relative_paths, nested_boundaries = _git_instruction_paths(
                root,
                selected_git,
            )
            observed_git = replace(
                selected_git,
                repository_context_bound=True,
                nested_repository_boundaries=nested_boundaries,
            )
            if selected_git.repository_context_bound and observed_git != selected_git:
                raise GrokBindingIncompatible("Git repository context changed")
            selected_git = observed_git
        rows: list[GrokProjectInstructionAttestation] = []
        for parts in relative_paths:
            if len(rows) >= _MAX_INSTRUCTION_FILES:
                raise OSError("instruction file limit exceeded")
            target = root.joinpath(*parts)
            rows.append(
                _attest_project_instruction_file(
                    root,
                    parts,
                    "/".join(parts),
                    target.lstat().st_size,
                )
            )
    except (GrokBindingIncompatible, GrokPermissionError, OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible(
            "Grok project instruction manifest is unavailable or unsafe"
        ) from exc
    return (
        tuple(sorted(rows, key=lambda row: row.relative_path.casefold())),
        selected_git,
    )


def _resolve_git_executable(workspace: Path) -> Path:
    located = shutil.which("git.exe") or shutil.which("git")
    if not located:
        raise GrokBindingIncompatible("Git is required to attest repository instructions")
    try:
        executable = Path(located).resolve(strict=True)
        details = executable.lstat()
    except (OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible("Git executable is unavailable") from exc
    if (
        _is_reparse_point(executable)
        or not stat.S_ISREG(details.st_mode)
        or _is_within(executable, workspace)
    ):
        raise GrokBindingIncompatible("Git executable identity is unsafe")
    return executable


def _git_environment() -> dict[str, str]:
    names = (
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    )
    environment = {
        name: os.environ[name]
        for name in names
        if name in os.environ and isinstance(os.environ[name], str)
    }
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git_version(executable: Path) -> str:
    completed = _run_owned_command(
        (str(executable), "--version"),
        env=_git_environment(),
        timeout_seconds=DEFAULT_INSPECT_TIMEOUT_SECONDS,
        cleanup_timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
    )
    if (
        completed.returncode != 0
        or completed.timed_out
        or completed.overflow
        or completed.cancelled
        or completed.read_failed
        or completed.stderr
        or len(completed.stdout) > 256
    ):
        raise GrokBindingIncompatible("Git version evidence is unavailable")
    raw = completed.stdout
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\r" in raw or b"\n" in raw:
        raise GrokBindingIncompatible("Git version evidence is malformed")
    try:
        version = raw.decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise GrokBindingIncompatible("Git version evidence is malformed") from exc
    if not _GIT_VERSION.fullmatch(version):
        raise GrokBindingIncompatible("Git version evidence is malformed")
    return version


def _read_git_attestation_unlocked(
    workspace: Path,
    executable: Path,
) -> GrokGitAttestation:
    try:
        canonical = executable.resolve(strict=True)
        details = executable.lstat()
        if (
            canonical != executable
            or _is_reparse_point(executable)
            or not stat.S_ISREG(details.st_mode)
            or _is_within(executable, workspace)
        ):
            raise OSError("unsafe Git executable")
        before_identity = _isolation_file_identity(details)
        file_identity = _file_identity(executable)
        version = _git_version(executable)
        after_details = executable.lstat()
        if (
            executable.resolve(strict=True) != executable
            or _is_reparse_point(executable)
            or not stat.S_ISREG(after_details.st_mode)
            or before_identity != _isolation_file_identity(after_details)
        ):
            raise OSError("Git executable changed while attesting")
    except (GrokBindingIncompatible, OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible("Git executable evidence is unavailable") from exc
    return GrokGitAttestation(
        executable_path=str(executable),
        version=version,
        sha256=str(file_identity["sha256"]),
        identity=before_identity,
    )


def _attest_git_executable(
    workspace: Path,
    executable: Path | None = None,
) -> GrokGitAttestation:
    selected = _resolve_git_executable(workspace) if executable is None else executable
    if not selected.is_absolute():
        raise GrokBindingIncompatible("Git executable path is invalid")
    try:
        with _locked_grok_startup((selected,)):
            return _read_git_attestation_unlocked(workspace, selected)
    except (GrokBindingIncompatible, ServiceError, OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible("Git executable evidence is unavailable") from exc


def _read_git_control_path(path: Path, label: str) -> tuple[str, tuple[int, int, int, int]]:
    try:
        before = path.lstat()
        if (
            _is_reparse_point(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= 4096
        ):
            raise OSError("unsafe Git control file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened_before = os.fstat(stream.fileno())
            raw = stream.read(4097)
            opened_after = os.fstat(stream.fileno())
        after = path.lstat()
        identity = _isolation_file_identity(opened_after)
        if (
            len(raw) > 4096
            or not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or _isolation_file_identity(before) != identity
            or _isolation_file_identity(opened_before) != identity
            or _isolation_file_identity(after) != identity
            or _is_reparse_point(path)
        ):
            raise OSError("Git control file changed")
        text = raw.decode("utf-8", errors="strict")
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise GrokBindingIncompatible(f"{label} is unsafe") from exc
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if (
        not text
        or "\r" in text
        or "\n" in text
        or any(unicodedata.category(character) == "Cc" for character in text)
    ):
        raise GrokBindingIncompatible(f"{label} is malformed")
    return text, identity


def _canonical_git_metadata_directory(raw: str, base: Path, label: str) -> Path:
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = base / candidate
        lexical = Path(os.path.abspath(candidate))
        canonical = lexical.resolve(strict=True)
        details = lexical.lstat()
        if (
            canonical != lexical
            or _is_reparse_point(lexical)
            or not stat.S_ISDIR(details.st_mode)
        ):
            raise OSError("unsafe Git metadata directory")
    except (OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible(f"{label} is unsafe") from exc
    return canonical


def _attest_optional_git_control_file(
    path: Path,
    label: str,
    *,
    maximum_size: int,
) -> tuple[tuple[int, int, int, int], str] | None:
    if not os.path.lexists(path):
        return None
    try:
        before = path.lstat()
        if (
            _is_reparse_point(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 <= before.st_size <= maximum_size
        ):
            raise OSError("unsafe Git control file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened_before = os.fstat(stream.fileno())
            raw = stream.read(maximum_size + 1)
            opened_after = os.fstat(stream.fileno())
        after = path.lstat()
        identity = _isolation_file_identity(opened_after)
        if (
            len(raw) > maximum_size
            or not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or _isolation_file_identity(before) != identity
            or _isolation_file_identity(opened_before) != identity
            or _isolation_file_identity(after) != identity
            or _is_reparse_point(path)
        ):
            raise OSError("Git control file changed")
    except (OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible(f"{label} is unsafe") from exc
    return identity, hashlib.sha256(raw).hexdigest()


def _metadata_within_root_authority(
    path: Path,
    root_authority: _GrokGitRepositoryBoundary,
) -> bool:
    return _is_within(path, Path(root_authority.git_dir_path)) or _is_within(
        path,
        Path(root_authority.common_dir_path),
    )


def _attest_git_repository_boundary(
    workspace: Path,
    repository: Path,
    *,
    root_authority: _GrokGitRepositoryBoundary | None,
    tracked: bool,
    is_root: bool,
) -> _GrokGitRepositoryBoundary:
    try:
        canonical_repository, _identity = _attest_workspace_root(repository)
    except GrokPermissionError as exc:
        raise GrokBindingIncompatible("Git repository boundary is unsafe") from exc
    if canonical_repository != repository or not _is_within(repository, workspace):
        raise GrokBindingIncompatible("Git repository boundary is unsafe")
    marker = repository / ".git"
    try:
        marker_details = marker.lstat()
    except OSError as exc:
        raise GrokBindingIncompatible("Git repository marker is unavailable") from exc
    if _is_reparse_point(marker):
        raise GrokBindingIncompatible("Git repository marker is unsafe")
    if stat.S_ISDIR(marker_details.st_mode):
        marker_is_file = False
        marker_identity = _isolation_file_identity(marker_details)
        git_dir = _canonical_git_metadata_directory(str(marker), marker.parent, "Git dir")
    elif stat.S_ISREG(marker_details.st_mode):
        marker_is_file = True
        marker_text, marker_identity = _read_git_control_path(
            marker,
            "Git repository marker",
        )
        if not marker_text.startswith("gitdir: ") or len(marker_text) <= len("gitdir: "):
            raise GrokBindingIncompatible("Git repository marker is malformed")
        git_dir = _canonical_git_metadata_directory(
            marker_text[len("gitdir: ") :],
            marker.parent,
            "Git dir",
        )
    else:
        raise GrokBindingIncompatible("Git repository marker is unsafe")

    if not is_root and not _is_within(git_dir, workspace):
        if (
            not tracked
            or root_authority is None
            or not _metadata_within_root_authority(git_dir, root_authority)
        ):
            raise GrokBindingIncompatible("Git nested metadata escapes authority")

    commondir_path = git_dir / "commondir"
    if os.path.lexists(commondir_path):
        commondir_text, _commondir_identity = _read_git_control_path(
            commondir_path,
            "Git common-dir marker",
        )
        common_dir = _canonical_git_metadata_directory(
            commondir_text,
            git_dir,
            "Git common dir",
        )
    else:
        common_dir = git_dir
    if not is_root and not _is_within(common_dir, workspace):
        if (
            not tracked
            or root_authority is None
            or not _metadata_within_root_authority(common_dir, root_authority)
        ):
            raise GrokBindingIncompatible("Git nested common dir escapes authority")
    gitmodules = _attest_optional_git_control_file(
        repository / ".gitmodules",
        "Git submodule manifest",
        maximum_size=64 * 1024,
    )
    return _GrokGitRepositoryBoundary(
        repository_path=str(repository),
        marker_identity=marker_identity,
        marker_is_file=marker_is_file,
        git_dir_path=str(git_dir),
        git_dir_identity=_isolation_file_identity(git_dir.lstat()),
        common_dir_path=str(common_dir),
        common_dir_identity=_isolation_file_identity(common_dir.lstat()),
        gitmodules_identity=(gitmodules[0] if gitmodules is not None else None),
        gitmodules_sha256=(gitmodules[1] if gitmodules is not None else None),
        tracked=tracked,
        is_root=is_root,
    )


def _bind_git_root_attestation(
    workspace: Path,
    git_attestation: GrokGitAttestation,
) -> GrokGitAttestation:
    current = _attest_git_repository_boundary(
        workspace,
        workspace,
        root_authority=None,
        tracked=True,
        is_root=True,
    )
    expected = (
        git_attestation.root_marker_identity,
        git_attestation.root_git_dir_path,
        git_attestation.root_git_dir_identity,
        git_attestation.root_common_dir_path,
        git_attestation.root_common_dir_identity,
    )
    observed = (
        current.marker_identity,
        current.git_dir_path,
        current.git_dir_identity,
        current.common_dir_path,
        current.common_dir_identity,
    )
    if all(item is None for item in expected):
        return replace(
            git_attestation,
            root_marker_identity=current.marker_identity,
            root_git_dir_path=current.git_dir_path,
            root_git_dir_identity=current.git_dir_identity,
            root_common_dir_path=current.common_dir_path,
            root_common_dir_identity=current.common_dir_identity,
            root_gitmodules_identity=current.gitmodules_identity,
            root_gitmodules_sha256=current.gitmodules_sha256,
        )
    if expected != observed or (
        git_attestation.root_gitmodules_identity,
        git_attestation.root_gitmodules_sha256,
    ) != (
        current.gitmodules_identity,
        current.gitmodules_sha256,
    ):
        raise GrokBindingIncompatible("Git root metadata identity changed")
    if git_attestation.repository_context_bound:
        root_authority = current
        for boundary in git_attestation.nested_repository_boundaries:
            repository = Path(boundary.repository_path)
            reattested = _attest_git_repository_boundary(
                workspace,
                repository,
                root_authority=root_authority,
                tracked=boundary.tracked,
                is_root=False,
            )
            if reattested != boundary:
                raise GrokBindingIncompatible("Git nested metadata identity changed")
    return git_attestation


def _same_git_executable_attestation(
    first: GrokGitAttestation,
    second: GrokGitAttestation,
) -> bool:
    return (
        first.executable_path,
        first.version,
        first.sha256,
        first.identity,
    ) == (
        second.executable_path,
        second.version,
        second.sha256,
        second.identity,
    )


def _root_git_repository_boundary(
    workspace: Path,
    git_attestation: GrokGitAttestation,
) -> _GrokGitRepositoryBoundary:
    bound = _bind_git_root_attestation(workspace, git_attestation)
    current = _attest_git_repository_boundary(
        workspace,
        workspace,
        root_authority=None,
        tracked=True,
        is_root=True,
    )
    if (
        bound.root_marker_identity != current.marker_identity
        or bound.root_git_dir_path != current.git_dir_path
        or bound.root_git_dir_identity != current.git_dir_identity
        or bound.root_common_dir_path != current.common_dir_path
        or bound.root_common_dir_identity != current.common_dir_identity
    ):
        raise GrokBindingIncompatible("Git root metadata identity changed")
    return current


def _git_instruction_paths(
    workspace: Path,
    git_attestation: GrokGitAttestation,
) -> tuple[
    tuple[tuple[str, ...], ...],
    tuple[_GrokGitRepositoryBoundary, ...],
]:
    bound_git = _bind_git_root_attestation(workspace, git_attestation)
    root_boundary = _root_git_repository_boundary(workspace, bound_git)
    repositories: list[
        tuple[Path, tuple[str, ...], _GrokGitRepositoryBoundary]
    ] = [(workspace, (), root_boundary)]
    seen_repositories: set[tuple[str, ...]] = {()}
    nested_boundaries: list[_GrokGitRepositoryBoundary] = []
    paths: list[tuple[str, ...]] = []
    seen_paths: set[tuple[str, ...]] = set()
    while repositories:
        repository, prefix, repository_boundary = repositories.pop(0)
        for parts in _git_repository_instruction_paths(
            workspace,
            repository,
            bound_git,
            repository_boundary,
        ):
            combined = (*prefix, *parts)
            folded = _fold_parts(combined)
            if folded in seen_paths:
                raise GrokBindingIncompatible("Git instruction path is invalid")
            seen_paths.add(folded)
            paths.append(combined)
            if len(paths) > _MAX_INSTRUCTION_FILES:
                raise GrokBindingIncompatible("Git instruction listing is unbounded")
        for nested_repository in _git_nested_repository_paths(
            workspace,
            repository,
            bound_git,
            repository_boundary,
            root_boundary,
        ):
            parts = nested_repository.parts
            combined = (*prefix, *parts)
            folded = _fold_parts(combined)
            if folded in seen_repositories:
                raise GrokBindingIncompatible("Git nested repository is duplicated")
            seen_repositories.add(folded)
            if len(seen_repositories) > _MAX_INSTRUCTION_FILES:
                raise GrokBindingIncompatible("Git nested repository listing is unbounded")
            nested = workspace.joinpath(*combined)
            try:
                canonical, _identity = _attest_workspace_root(nested)
            except GrokPermissionError as exc:
                raise GrokBindingIncompatible(
                    "Git nested repository boundary is unsafe"
                ) from exc
            if canonical != nested or not _windows_contains(canonical, workspace):
                raise GrokBindingIncompatible("Git nested repository escapes workspace")
            nested_boundaries.append(nested_repository.boundary)
            repositories.append((canonical, combined, nested_repository.boundary))
    return (
        tuple(sorted(paths, key=lambda parts: "/".join(parts).casefold())),
        tuple(
            sorted(
                nested_boundaries,
                key=lambda boundary: boundary.repository_path.casefold(),
            )
        ),
    )


def _reject_git_discovery_config(config_names: bytes) -> None:
    if config_names and not config_names.endswith(b"\0"):
        raise GrokBindingIncompatible("Git local config listing is malformed")
    for raw_name in config_names[:-1].split(b"\0") if config_names else ():
        try:
            name = raw_name.decode("utf-8", errors="strict").casefold()
        except UnicodeError as exc:
            raise GrokBindingIncompatible("Git local config listing is malformed") from exc
        if (
            name == "include.path"
            or name.startswith("includeif.")
            and name.endswith(".path")
        ):
            raise GrokBindingIncompatible("Git local discovery config is forbidden")


def _run_bound_git_listing(
    workspace: Path,
    repository: Path,
    git_attestation: GrokGitAttestation,
    repository_boundary: _GrokGitRepositoryBoundary,
    root_boundary: _GrokGitRepositoryBoundary,
    arguments: Sequence[str],
    accepted_returncodes: frozenset[int] = frozenset({0}),
    check_local_config: bool = True,
) -> bytes:
    if check_local_config:
        config_names = _run_bound_git_listing(
            workspace,
            repository,
            git_attestation,
            repository_boundary,
            root_boundary,
            ("config", "--local", "--no-includes", "--null", "--name-only", "--list"),
            check_local_config=False,
        )
        _reject_git_discovery_config(config_names)
        worktree_config_path = Path(repository_boundary.git_dir_path) / "config.worktree"
        if _attest_optional_git_control_file(
            worktree_config_path,
            "Git worktree config",
            maximum_size=_MAX_COMMAND_BYTES,
        ) is not None:
            worktree_config_names = _run_bound_git_listing(
                workspace,
                repository,
                git_attestation,
                repository_boundary,
                root_boundary,
                (
                    "config",
                    "--file",
                    str(worktree_config_path),
                    "--no-includes",
                    "--null",
                    "--name-only",
                    "--list",
                ),
                check_local_config=False,
            )
            _reject_git_discovery_config(worktree_config_names)
        top_level = _run_bound_git_listing(
            workspace,
            repository,
            git_attestation,
            repository_boundary,
            root_boundary,
            ("rev-parse", "--show-toplevel"),
            check_local_config=False,
        )
        try:
            top_level_text = top_level.decode("utf-8", errors="strict")
            if (
                not top_level_text.endswith("\n")
                or "\0" in top_level_text
                or "\r" in top_level_text
                or "\n" in top_level_text[:-1]
            ):
                raise ValueError("malformed Git top-level")
            lexical_top_level = Path(os.path.abspath(top_level_text[:-1]))
            canonical_top_level = lexical_top_level.resolve(strict=True)
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            raise GrokBindingIncompatible("Git top-level evidence is malformed") from exc
        if lexical_top_level != repository or canonical_top_level != repository:
            raise GrokBindingIncompatible("Git top-level mismatches repository boundary")
    executable = Path(git_attestation.executable_path)
    safe_directory = f"safe.directory={repository.as_posix()}"
    argv = (
        str(executable),
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        f"core.excludesFile={os.devnull}",
        "-c",
        "extensions.worktreeConfig=false",
        "-c",
        f"core.worktree={repository}",
        "-c",
        safe_directory,
        "--no-optional-locks",
        f"--git-dir={repository_boundary.git_dir_path}",
        f"--work-tree={repository_boundary.repository_path}",
        "-C",
        str(repository),
        *arguments,
    )
    try:
        with _locked_grok_startup((executable,)):
            if _root_git_repository_boundary(workspace, git_attestation) != root_boundary:
                raise GrokBindingIncompatible("Git root metadata changed")
            current_repository = _attest_git_repository_boundary(
                workspace,
                repository,
                root_authority=(None if repository_boundary.is_root else root_boundary),
                tracked=repository_boundary.tracked,
                is_root=repository_boundary.is_root,
            )
            if current_repository != repository_boundary:
                raise GrokBindingIncompatible("Git repository metadata changed")
            before = _read_git_attestation_unlocked(workspace, executable)
            if not _same_git_executable_attestation(before, git_attestation):
                raise GrokBindingIncompatible("Git executable identity changed")
            completed = _run_owned_command(
                argv,
                env=_git_environment(),
                timeout_seconds=DEFAULT_INSPECT_TIMEOUT_SECONDS,
                cleanup_timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
            )
            after = _read_git_attestation_unlocked(workspace, executable)
            if not _same_git_executable_attestation(after, git_attestation):
                raise GrokBindingIncompatible("Git executable identity changed")
            current_repository = _attest_git_repository_boundary(
                workspace,
                repository,
                root_authority=(None if repository_boundary.is_root else root_boundary),
                tracked=repository_boundary.tracked,
                is_root=repository_boundary.is_root,
            )
            if current_repository != repository_boundary:
                raise GrokBindingIncompatible("Git repository metadata changed")
            if _root_git_repository_boundary(workspace, git_attestation) != root_boundary:
                raise GrokBindingIncompatible("Git root metadata changed")
    except (GrokBindingIncompatible, ServiceError, OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible("Git instruction listing failed") from exc
    if (
        completed.returncode not in accepted_returncodes
        or completed.timed_out
        or completed.overflow
        or completed.cancelled
        or completed.read_failed
        or completed.stderr
        or len(completed.stdout) > _MAX_COMMAND_BYTES
    ):
        raise GrokBindingIncompatible("Git instruction listing failed")
    return completed.stdout


def _decode_git_nul_paths(
    output: bytes,
    *,
    allow_directories: bool = False,
) -> tuple[tuple[str, ...], ...]:
    if not output:
        return ()
    if not output.endswith(b"\0"):
        raise GrokBindingIncompatible("Git instruction listing is malformed")
    raw_paths = output[:-1].split(b"\0")
    if not raw_paths or len(raw_paths) > _MAX_INSTRUCTION_SCAN_ENTRIES:
        raise GrokBindingIncompatible("Git instruction listing is unbounded")
    paths: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in raw_paths:
        try:
            text = raw.decode("utf-8", errors="strict")
            if allow_directories:
                text = text.rstrip("/\\")
            parts = _windows_relative_parts(text)
        except (UnicodeError, GrokPermissionError) as exc:
            raise GrokBindingIncompatible("Git instruction path is invalid") from exc
        folded = _fold_parts(parts)
        if folded in seen:
            raise GrokBindingIncompatible("Git instruction path is invalid")
        seen.add(folded)
        paths.append(parts)
    return tuple(paths)


def _git_repository_instruction_paths(
    workspace: Path,
    repository: Path,
    git_attestation: GrokGitAttestation,
    repository_boundary: _GrokGitRepositoryBoundary,
) -> tuple[tuple[str, ...], ...]:
    output = _run_bound_git_listing(
        workspace,
        repository,
        git_attestation,
        repository_boundary,
        _root_git_repository_boundary(workspace, git_attestation),
        (
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        ":(icase,glob)AGENT.md",
        ":(icase,glob)AGENTS.md",
        ":(icase,glob)CLAUDE.md",
        ":(icase,glob)**/AGENT.md",
        ":(icase,glob)**/AGENTS.md",
        ":(icase,glob)**/CLAUDE.md",
        ),
    )
    paths = _decode_git_nul_paths(output)
    if len(paths) > _MAX_INSTRUCTION_FILES:
        raise GrokBindingIncompatible("Git instruction listing is unbounded")
    for parts in paths:
        if not _is_grok_instruction_basename(parts[-1]):
            raise GrokBindingIncompatible("Git instruction path is invalid")
    return tuple(sorted(paths, key=lambda parts: "/".join(parts).casefold()))


def _git_nested_repository_paths(
    workspace: Path,
    repository: Path,
    git_attestation: GrokGitAttestation,
    repository_boundary: _GrokGitRepositoryBoundary,
    root_boundary: _GrokGitRepositoryBoundary,
) -> tuple[_GrokNestedRepository, ...]:
    status = _run_bound_git_listing(
        workspace,
        repository,
        git_attestation,
        repository_boundary,
        root_boundary,
        ("submodule--helper", "status", "--cached"),
    )
    try:
        status_text = status.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise GrokBindingIncompatible("Git submodule status is malformed") from exc
    if "\0" in status_text or "\r" in status_text:
        raise GrokBindingIncompatible("Git submodule status is malformed")
    status_rows = tuple(row for row in status_text.split("\n") if row)
    if len(status_rows) > _MAX_INSTRUCTION_FILES:
        raise GrokBindingIncompatible("Git submodule status is unbounded")
    gitlinks: list[tuple[str, ...]] = []
    gitmodules = repository / ".gitmodules"
    if os.path.lexists(gitmodules):
        if _is_reparse_point(gitmodules):
            raise GrokBindingIncompatible("Git submodule manifest is unsafe")
        try:
            details = gitmodules.stat()
        except OSError as exc:
            raise GrokBindingIncompatible("Git submodule manifest is unavailable") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_size > 64 * 1024
        ):
            raise GrokBindingIncompatible("Git submodule manifest is unsafe")
        declared = _run_bound_git_listing(
            workspace,
            repository,
            git_attestation,
            repository_boundary,
            root_boundary,
            (
                "config",
                "-z",
                "--file",
                str(gitmodules),
                "--get-regexp",
                r"^submodule\..*\.path$",
            ),
        )
        if declared:
            if not declared.endswith(b"\0"):
                raise GrokBindingIncompatible("Git submodule manifest is malformed")
            rows = declared[:-1].split(b"\0")
            if not rows or len(rows) > _MAX_INSTRUCTION_FILES:
                raise GrokBindingIncompatible("Git submodule manifest is unbounded")
            for row in rows:
                try:
                    _key, raw_path = row.split(b"\n", 1)
                    parts = _decode_git_nul_paths(raw_path + b"\0")[0]
                except (IndexError, ValueError) as exc:
                    raise GrokBindingIncompatible(
                        "Git submodule manifest is malformed"
                    ) from exc
                staged = _run_bound_git_listing(
                    workspace,
                    repository,
                    git_attestation,
                    repository_boundary,
                    root_boundary,
                    ("ls-files", "-z", "--stage", "--", "/".join(parts)),
                )
                if not staged.endswith(b"\0") or staged.count(b"\0") != 1:
                    raise GrokBindingIncompatible("Git submodule index is malformed")
                try:
                    metadata, indexed_path = staged[:-1].split(b"\t", 1)
                    mode, _object_id, stage = metadata.split(b" ", 2)
                except ValueError as exc:
                    raise GrokBindingIncompatible("Git submodule index is malformed") from exc
                if mode != b"160000" or stage != b"0":
                    raise GrokBindingIncompatible("Git submodule index is malformed")
                if _decode_git_nul_paths(indexed_path + b"\0") != (parts,):
                    raise GrokBindingIncompatible("Git submodule index is malformed")
                gitlinks.append(parts)
    if len(gitlinks) != len(status_rows):
        raise GrokBindingIncompatible("Git submodule manifest mismatches the index")

    untracked = _run_bound_git_listing(
        workspace,
        repository,
        git_attestation,
        repository_boundary,
        root_boundary,
        ("ls-files", "-z", "--others", "--exclude-standard"),
    )
    embedded: list[tuple[str, ...]] = []
    for parts in _decode_git_nul_paths(untracked, allow_directories=True):
        candidate = repository.joinpath(*parts)
        marker = candidate / ".git"
        if candidate.is_dir() and os.path.lexists(marker):
            if _is_reparse_point(candidate) or _is_reparse_point(marker):
                raise GrokBindingIncompatible("Git nested repository boundary is unsafe")
            embedded.append(parts)

    combined: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}
    for parts, tracked in (
        *((parts, True) for parts in gitlinks),
        *((parts, False) for parts in embedded),
    ):
        folded = _fold_parts(parts)
        if folded in combined:
            if tracked:
                combined[folded] = (parts, True)
            continue
        candidate = repository.joinpath(*parts)
        marker = candidate / ".git"
        if not candidate.is_dir() or not os.path.lexists(marker):
            raise GrokBindingIncompatible("Git nested repository is unavailable")
        combined[folded] = (parts, tracked)
    nested_repositories = tuple(
        _GrokNestedRepository(
            parts,
            _attest_git_repository_boundary(
                workspace,
                repository.joinpath(*parts),
                root_authority=root_boundary,
                tracked=tracked,
                is_root=False,
            ),
        )
        for parts, tracked in combined.values()
    )
    return tuple(
        sorted(
            nested_repositories,
            key=lambda item: "/".join(item.parts).casefold(),
        )
    )


def _merge_project_instruction_manifest(
    inspect_rows: tuple[GrokProjectInstructionAttestation, ...],
    scanned_rows: tuple[GrokProjectInstructionAttestation, ...],
) -> tuple[GrokProjectInstructionAttestation, ...]:
    scanned = {row.relative_path.casefold(): row for row in scanned_rows}
    if any(scanned.get(row.relative_path.casefold()) != row for row in inspect_rows):
        raise GrokBindingIncompatible(
            "Grok inspect project instruction evidence mismatches the workspace"
        )
    return scanned_rows


def _attest_project_instruction_file(
    workspace: Path,
    relative_parts: tuple[str, ...],
    normalized_relative: str,
    expected_size: int,
) -> GrokProjectInstructionAttestation:
    candidate = workspace.joinpath(*relative_parts)
    try:
        _reject_reparse_chain(workspace, relative_parts)
        canonical = candidate.resolve(strict=True)
        if canonical != candidate or not _is_within(canonical, workspace):
            raise OSError("project instruction escapes the workspace")
        current = candidate.lstat()
        if (
            _is_reparse_point(candidate)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            raise OSError("unsafe project instruction")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OSError("unsafe project instruction")
            hasher = hashlib.sha256()
            total = 0
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                total += len(chunk)
                if total > _MAX_COMMAND_BYTES:
                    raise OSError("project instruction is too large")
                hasher.update(chunk)
            after = os.fstat(stream.fileno())
        final = candidate.lstat()
        identity = _isolation_file_identity(after)
        if (
            _is_reparse_point(candidate)
            or not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or _isolation_file_identity(before) != identity
            or _isolation_file_identity(final) != identity
            or total != expected_size
        ):
            raise OSError("project instruction changed while hashing")
        _reject_reparse_chain(workspace, relative_parts)
    except (GrokPermissionError, OSError, RuntimeError) as exc:
        raise GrokBindingIncompatible(
            "Grok inspect project instruction is unavailable or unsafe"
        ) from exc
    return GrokProjectInstructionAttestation(
        relative_path=normalized_relative,
        sha256=hasher.hexdigest(),
        identity=identity,
        size=total,
    )


def _validate_inspect_builtin_agents(value: object) -> int:
    if not isinstance(value, list) or len(value) > 128:
        raise GrokBindingIncompatible("Grok inspect built-in agents are invalid")
    names: list[str] = []
    for agent in value:
        if not isinstance(agent, Mapping) or set(agent) != {
            "name",
            "description",
            "source",
        }:
            raise GrokBindingIncompatible("Grok inspect built-in agent is invalid")
        name = agent.get("name")
        source = agent.get("source")
        if (
            not _safe_name(name)
            or _bounded_public_text(agent.get("description"), 4096) is None
            or not isinstance(source, Mapping)
            or dict(source) != {"type": "builtin"}
            or name in names
        ):
            raise GrokBindingIncompatible("Grok inspect external agent is unsafe")
        assert isinstance(name, str)
        names.append(name)
    return len(names)


def _run_grok(
    executable: Path,
    suffix: tuple[str, ...],
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    argv = (str(executable), "--no-auto-update", *suffix)
    completed = _run_owned_command(
        argv,
        env=_child_env(os.environ) if environment is None else environment,
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
    expected_config_path: Path,
) -> tuple[tuple[str, str], ...]:
    try:
        config_matches = _fold_lexical_dos_path(
            _lexical_local_dos_path(value.config_source_path, "config source")
        ) == _fold_lexical_dos_path(
            _lexical_local_dos_path(str(expected_config_path), "runtime config")
        ) if isinstance(value, GrokInspectObservation) else False
    except GrokBindingIncompatible:
        config_matches = False
    try:
        project_root_matches = (
            _validate_inspect_project_root(value.project_root, workspace)
            == value.project_root
            if isinstance(value, GrokInspectObservation)
            else False
        )
    except GrokBindingIncompatible:
        project_root_matches = False
    try:
        if not isinstance(value, GrokInspectObservation):
            git_attestation_matches = False
        elif value.project_root is None:
            git_attestation_matches = value.git_attestation is None
        else:
            git_attestation_matches = (
                _validate_git_attestation(value.git_attestation)
                is value.git_attestation
            )
    except GrokBindingIncompatible:
        git_attestation_matches = False
    if (
        not isinstance(value, GrokInspectObservation)
        or value.pair_key != binding.pair_key
        or _fold_path(value.workspace_path) != _fold_path(str(workspace))
        or value.builtin_tool_inventory != "not_exposed"
        or value.api_key_auth_disabled is not True
        or value.config_source_layer_count != 1
        or not config_matches
        or value.compatibility_isolated is not True
        or value.permission_sources_isolated is not True
        or value.external_surfaces_empty is not True
        or type(value.builtin_agent_count) is not int
        or not 0 <= value.builtin_agent_count <= 128
        or type(value.project_trusted) is not bool
        or not project_root_matches
        or not git_attestation_matches
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
    _validate_project_instruction_attestations(value.project_instructions)
    return tuple(sorted(extensions))


def _validate_project_instruction_attestations(
    value: object,
) -> tuple[GrokProjectInstructionAttestation, ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_INSTRUCTION_FILES:
        raise _capability_error("Grok project instruction evidence is malformed")
    seen: set[str] = set()
    rows: list[GrokProjectInstructionAttestation] = []
    for row in value:
        if not isinstance(row, GrokProjectInstructionAttestation):
            raise _capability_error("Grok project instruction evidence is malformed")
        try:
            parts = _windows_relative_parts(row.relative_path)
        except GrokPermissionError as exc:
            raise _capability_error(
                "Grok project instruction evidence is malformed"
            ) from exc
        normalized = "/".join(parts)
        if (
            normalized != row.relative_path
            or not _is_grok_instruction_basename(parts[-1])
            or not re.fullmatch(r"[0-9a-f]{64}", row.sha256)
            or not isinstance(row.identity, tuple)
            or len(row.identity) != 4
            or any(type(item) is not int or item < 0 for item in row.identity)
            or type(row.size) is not int
            or not 0 <= row.size <= _MAX_COMMAND_BYTES
            or row.identity[2] != row.size
            or normalized.casefold() in seen
        ):
            raise _capability_error("Grok project instruction evidence is malformed")
        seen.add(normalized.casefold())
        rows.append(row)
    if rows != sorted(rows, key=lambda item: item.relative_path.casefold()):
        raise _capability_error("Grok project instruction evidence is malformed")
    return tuple(rows)


def _serialize_project_instructions(
    value: tuple[GrokProjectInstructionAttestation, ...],
) -> tuple[Mapping[str, object], ...]:
    rows = _validate_project_instruction_attestations(value)
    return tuple(
        {
            "path": row.relative_path,
            "sha256": row.sha256,
            "identity": row.identity,
            "size": row.size,
        }
        for row in rows
    )


def _validate_git_attestation(value: object) -> GrokGitAttestation:
    if not isinstance(value, GrokGitAttestation):
        raise GrokBindingIncompatible("Grok Git evidence is malformed")
    _lexical_local_dos_path(value.executable_path, "Git executable")
    if (
        not _GIT_VERSION.fullmatch(value.version)
        or re.fullmatch(r"[0-9a-f]{64}", value.sha256) is None
        or not isinstance(value.identity, tuple)
        or len(value.identity) != 4
        or any(type(item) is not int or item < 0 for item in value.identity)
    ):
        raise GrokBindingIncompatible("Grok Git evidence is malformed")
    metadata = (
        value.root_marker_identity,
        value.root_git_dir_path,
        value.root_git_dir_identity,
        value.root_common_dir_path,
        value.root_common_dir_identity,
    )
    if any(item is not None for item in metadata):
        if any(item is None for item in metadata):
            raise GrokBindingIncompatible("Grok Git evidence is malformed")
        _lexical_local_dos_path(value.root_git_dir_path, "Git dir")
        _lexical_local_dos_path(value.root_common_dir_path, "Git common dir")
        for identity in (
            value.root_marker_identity,
            value.root_git_dir_identity,
            value.root_common_dir_identity,
        ):
            if (
                not isinstance(identity, tuple)
                or len(identity) != 4
                or any(type(item) is not int or item < 0 for item in identity)
            ):
                raise GrokBindingIncompatible("Grok Git evidence is malformed")
    if (value.root_gitmodules_identity is None) != (
        value.root_gitmodules_sha256 is None
    ):
        raise GrokBindingIncompatible("Grok Git evidence is malformed")
    if value.root_gitmodules_identity is not None and (
        len(value.root_gitmodules_identity) != 4
        or any(
            type(item) is not int or item < 0
            for item in value.root_gitmodules_identity
        )
        or re.fullmatch(r"[0-9a-f]{64}", str(value.root_gitmodules_sha256)) is None
    ):
        raise GrokBindingIncompatible("Grok Git evidence is malformed")
    if type(value.repository_context_bound) is not bool or not isinstance(
        value.nested_repository_boundaries,
        tuple,
    ):
        raise GrokBindingIncompatible("Grok Git evidence is malformed")
    if not value.repository_context_bound and value.nested_repository_boundaries:
        raise GrokBindingIncompatible("Grok Git evidence is malformed")
    seen_repositories: set[str] = set()
    for boundary in value.nested_repository_boundaries:
        if not isinstance(boundary, _GrokGitRepositoryBoundary):
            raise GrokBindingIncompatible("Grok Git evidence is malformed")
        _lexical_local_dos_path(boundary.repository_path, "Git repository")
        _lexical_local_dos_path(boundary.git_dir_path, "Git dir")
        _lexical_local_dos_path(boundary.common_dir_path, "Git common dir")
        folded_repository = boundary.repository_path.casefold()
        if (
            folded_repository in seen_repositories
            or type(boundary.marker_is_file) is not bool
            or type(boundary.tracked) is not bool
            or boundary.is_root is not False
            or any(
                len(identity) != 4
                or any(type(item) is not int or item < 0 for item in identity)
                for identity in (
                    boundary.marker_identity,
                    boundary.git_dir_identity,
                    boundary.common_dir_identity,
                )
            )
            or (boundary.gitmodules_identity is None)
            != (boundary.gitmodules_sha256 is None)
        ):
            raise GrokBindingIncompatible("Grok Git evidence is malformed")
        if boundary.gitmodules_identity is not None and (
            len(boundary.gitmodules_identity) != 4
            or any(
                type(item) is not int or item < 0
                for item in boundary.gitmodules_identity
            )
            or re.fullmatch(r"[0-9a-f]{64}", str(boundary.gitmodules_sha256))
            is None
        ):
            raise GrokBindingIncompatible("Grok Git evidence is malformed")
        seen_repositories.add(folded_repository)
    if value.nested_repository_boundaries != tuple(
        sorted(
            value.nested_repository_boundaries,
            key=lambda boundary: boundary.repository_path.casefold(),
        )
    ):
        raise GrokBindingIncompatible("Grok Git evidence is malformed")
    return value


def _serialize_git_attestation(
    value: GrokGitAttestation | None,
) -> Mapping[str, object] | None:
    if value is None:
        return None
    row = _validate_git_attestation(value)
    return {
        "executable_path": row.executable_path,
        "version": row.version,
        "sha256": row.sha256,
        "identity": row.identity,
        "root_marker_identity": row.root_marker_identity,
        "root_git_dir_path": row.root_git_dir_path,
        "root_git_dir_identity": row.root_git_dir_identity,
        "root_common_dir_path": row.root_common_dir_path,
        "root_common_dir_identity": row.root_common_dir_identity,
        "root_gitmodules_identity": row.root_gitmodules_identity,
        "root_gitmodules_sha256": row.root_gitmodules_sha256,
        "repository_context_bound": row.repository_context_bound,
        "nested_repository_boundaries": tuple(
            {
                "repository_path": boundary.repository_path,
                "marker_identity": boundary.marker_identity,
                "marker_is_file": boundary.marker_is_file,
                "git_dir_path": boundary.git_dir_path,
                "git_dir_identity": boundary.git_dir_identity,
                "common_dir_path": boundary.common_dir_path,
                "common_dir_identity": boundary.common_dir_identity,
                "gitmodules_identity": boundary.gitmodules_identity,
                "gitmodules_sha256": boundary.gitmodules_sha256,
                "tracked": boundary.tracked,
                "is_root": boundary.is_root,
            }
            for boundary in row.nested_repository_boundaries
        ),
    }


def _context_git_attestation(context: ResolvedContext) -> GrokGitAttestation | None:
    raw = context.attestation.get("git_attestation")
    project_root = context.attestation.get("project_root")
    if raw is None:
        if project_root is not None:
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok Git evidence changed",
                category="context",
            )
        return None
    if not isinstance(raw, Mapping) or set(raw) != {
        "executable_path",
        "version",
        "sha256",
        "identity",
        "root_marker_identity",
        "root_git_dir_path",
        "root_git_dir_identity",
        "root_common_dir_path",
        "root_common_dir_identity",
        "root_gitmodules_identity",
        "root_gitmodules_sha256",
        "repository_context_bound",
        "nested_repository_boundaries",
    } or not isinstance(project_root, str):
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok Git evidence changed",
            category="context",
        )
    identity = raw.get("identity")
    if not isinstance(identity, tuple):
        identity = tuple(identity) if isinstance(identity, list) else ()
    root_marker_identity = raw.get("root_marker_identity")
    if not isinstance(root_marker_identity, tuple):
        root_marker_identity = (
            tuple(root_marker_identity)
            if isinstance(root_marker_identity, list)
            else None
        )
    root_git_dir_identity = raw.get("root_git_dir_identity")
    if not isinstance(root_git_dir_identity, tuple):
        root_git_dir_identity = (
            tuple(root_git_dir_identity)
            if isinstance(root_git_dir_identity, list)
            else None
        )
    root_common_dir_identity = raw.get("root_common_dir_identity")
    if not isinstance(root_common_dir_identity, tuple):
        root_common_dir_identity = (
            tuple(root_common_dir_identity)
            if isinstance(root_common_dir_identity, list)
            else None
        )
    root_gitmodules_identity = raw.get("root_gitmodules_identity")
    if not isinstance(root_gitmodules_identity, tuple):
        root_gitmodules_identity = (
            tuple(root_gitmodules_identity)
            if isinstance(root_gitmodules_identity, list)
            else None
        )
    raw_boundaries = raw.get("nested_repository_boundaries")
    if not isinstance(raw_boundaries, (tuple, list)):
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok Git evidence changed",
            category="context",
        )
    nested_boundaries: list[_GrokGitRepositoryBoundary] = []
    for raw_boundary in raw_boundaries:
        if not isinstance(raw_boundary, Mapping) or set(raw_boundary) != {
            "repository_path",
            "marker_identity",
            "marker_is_file",
            "git_dir_path",
            "git_dir_identity",
            "common_dir_path",
            "common_dir_identity",
            "gitmodules_identity",
            "gitmodules_sha256",
            "tracked",
            "is_root",
        }:
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok Git evidence changed",
                category="context",
            )

        def boundary_identity(key: str) -> tuple[int, ...]:
            item = raw_boundary.get(key)
            return item if isinstance(item, tuple) else tuple(item) if isinstance(item, list) else ()

        raw_gitmodules_identity = raw_boundary.get("gitmodules_identity")
        nested_boundaries.append(
            _GrokGitRepositoryBoundary(
                repository_path=str(raw_boundary.get("repository_path", "")),
                marker_identity=boundary_identity("marker_identity"),  # type: ignore[arg-type]
                marker_is_file=raw_boundary.get("marker_is_file"),  # type: ignore[arg-type]
                git_dir_path=str(raw_boundary.get("git_dir_path", "")),
                git_dir_identity=boundary_identity("git_dir_identity"),  # type: ignore[arg-type]
                common_dir_path=str(raw_boundary.get("common_dir_path", "")),
                common_dir_identity=boundary_identity("common_dir_identity"),  # type: ignore[arg-type]
                gitmodules_identity=(
                    raw_gitmodules_identity
                    if isinstance(raw_gitmodules_identity, tuple)
                    else tuple(raw_gitmodules_identity)
                    if isinstance(raw_gitmodules_identity, list)
                    else None
                ),  # type: ignore[arg-type]
                gitmodules_sha256=(
                    str(raw_boundary["gitmodules_sha256"])
                    if isinstance(raw_boundary.get("gitmodules_sha256"), str)
                    else None
                ),
                tracked=raw_boundary.get("tracked"),  # type: ignore[arg-type]
                is_root=raw_boundary.get("is_root"),  # type: ignore[arg-type]
            )
        )
    value = GrokGitAttestation(
        executable_path=str(raw.get("executable_path", "")),
        version=str(raw.get("version", "")),
        sha256=str(raw.get("sha256", "")),
        identity=identity,  # type: ignore[arg-type]
        root_marker_identity=root_marker_identity,  # type: ignore[arg-type]
        root_git_dir_path=(
            str(raw["root_git_dir_path"])
            if isinstance(raw.get("root_git_dir_path"), str)
            else None
        ),
        root_git_dir_identity=root_git_dir_identity,  # type: ignore[arg-type]
        root_common_dir_path=(
            str(raw["root_common_dir_path"])
            if isinstance(raw.get("root_common_dir_path"), str)
            else None
        ),
        root_common_dir_identity=root_common_dir_identity,  # type: ignore[arg-type]
        root_gitmodules_identity=root_gitmodules_identity,  # type: ignore[arg-type]
        root_gitmodules_sha256=(
            str(raw["root_gitmodules_sha256"])
            if isinstance(raw.get("root_gitmodules_sha256"), str)
            else None
        ),
        repository_context_bound=raw.get("repository_context_bound"),  # type: ignore[arg-type]
        nested_repository_boundaries=tuple(nested_boundaries),
    )
    try:
        return _validate_git_attestation(value)
    except GrokBindingIncompatible as exc:
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok Git evidence changed",
            category="context",
        ) from exc


def _context_project_instructions(
    context: ResolvedContext,
) -> tuple[GrokProjectInstructionAttestation, ...]:
    raw = context.attestation.get("project_instructions")
    count = context.attestation.get("project_instruction_count")
    if (
        not isinstance(raw, (tuple, list))
        or type(count) is not int
        or len(raw) != count
    ):
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok project instruction evidence changed",
            category="context",
        )
    rows: list[GrokProjectInstructionAttestation] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "sha256",
            "identity",
            "size",
        }:
            raise ServiceError(
                "CONTEXT_DRIFT",
                "Grok project instruction evidence changed",
                category="context",
            )
        identity = item.get("identity")
        if not isinstance(identity, tuple):
            identity = tuple(identity) if isinstance(identity, list) else ()
        rows.append(
            GrokProjectInstructionAttestation(
                relative_path=str(item.get("path", "")),
                sha256=str(item.get("sha256", "")),
                identity=identity,  # type: ignore[arg-type]
                size=item.get("size", -1),  # type: ignore[arg-type]
            )
        )
    try:
        return _validate_project_instruction_attestations(tuple(rows))
    except ServiceError as exc:
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok project instruction evidence changed",
            category="context",
        ) from exc


def _reattest_context_project_instructions(
    context: ResolvedContext,
    workspace: Path,
) -> tuple[GrokProjectInstructionAttestation, ...]:
    expected = _context_project_instructions(context)
    try:
        observed = tuple(
            _attest_project_instruction_file(
                workspace,
                tuple(PureWindowsPath(row.relative_path).parts),
                row.relative_path,
                row.size,
            )
            for row in expected
        )
    except GrokBindingIncompatible as exc:
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok project instructions changed before launch",
            category="context",
        ) from exc
    if observed != expected:
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok project instructions changed before launch",
            category="context",
        )
    return observed


def _reattest_context_workspace_root(context: ResolvedContext) -> Path:
    expected = context.attestation.get("workspace_root_identity")
    if (
        not isinstance(expected, (tuple, list))
        or len(expected) != 2
        or any(type(item) is not int or item < 0 for item in expected)
    ):
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok workspace root evidence changed",
            category="context",
        )
    try:
        workspace, identity = _attest_workspace_root(context.workspace_path)
    except (GrokPermissionError, OSError, RuntimeError) as exc:
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok workspace root changed",
            category="context",
        ) from exc
    if str(workspace) != context.workspace_path or identity != tuple(expected):
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok workspace root changed",
            category="context",
        )
    return workspace


def _filesystem_meta(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise GrokPermissionError("Grok filesystem request is invalid")
    try:
        encoded = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise GrokPermissionError("Grok filesystem request is invalid") from exc
    if len(encoded) > 16_384:
        raise GrokPermissionError("Grok filesystem request is invalid")


def _filesystem_session(value: object, bound_session_id: str | None) -> str:
    session_id = _bounded_public_text(value, 256)
    if bound_session_id is None or session_id != bound_session_id:
        raise GrokPermissionError("Grok filesystem session is not authorized")
    return session_id


def _filesystem_read_params(
    value: Mapping[str, object],
    bound_session_id: str | None,
) -> dict[str, Any]:
    required = {"sessionId", "path"}
    optional = {"line", "limit", "_meta"}
    if not isinstance(value, Mapping) or not required <= set(value) <= required | optional:
        raise GrokPermissionError("Grok filesystem request is invalid")
    path = value.get("path")
    if not isinstance(path, str):
        raise GrokPermissionError("Grok filesystem request is invalid")
    result: dict[str, Any] = {
        "sessionId": _filesystem_session(value.get("sessionId"), bound_session_id),
        "path": path,
    }
    for key in ("line", "limit"):
        if key in value:
            item = value.get(key)
            if item is None:
                continue
            minimum = 1 if key == "line" else 0
            if type(item) is not int or not minimum <= item <= 4_294_967_295:
                raise GrokPermissionError("Grok filesystem request is invalid")
            result[key] = item
    if "_meta" in value:
        _filesystem_meta(value.get("_meta"))
    return result


def _filesystem_write_params(
    value: Mapping[str, object],
    bound_session_id: str | None,
) -> dict[str, str]:
    required = {"sessionId", "path", "content"}
    optional = {"_meta"}
    if not isinstance(value, Mapping) or not required <= set(value) <= required | optional:
        raise GrokPermissionError("Grok filesystem request is invalid")
    result: dict[str, str] = {
        "sessionId": _filesystem_session(value.get("sessionId"), bound_session_id)
    }
    for key in ("path", "content"):
        item = value.get(key)
        if not isinstance(item, str):
            raise GrokPermissionError("Grok filesystem request is invalid")
        result[key] = item
    if "_meta" in value:
        _filesystem_meta(value.get("_meta"))
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


def _is_grok_instruction_basename(value: str) -> bool:
    return value.casefold() in {"agent.md", "agents.md", "claude.md"}


def _reject_reserved_writer_path(parts: tuple[str, ...]) -> None:
    folded = tuple(part.casefold() for part in parts)
    if (
        any(
            part in {".git", ".grok", ".agents", ".cursor", ".claude"}
            for part in folded
        )
        or folded[-1] in {".gitmodules", ".mcp.json", ".envrc"}
        or _is_grok_instruction_basename(parts[-1])
    ):
        raise GrokPermissionError("Grok reserved context path is not writable")


def _fold_parts(parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(ntpath.normcase(part) for part in parts)


def _parts_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    common = min(len(first), len(second))
    return first[:common] == second[:common]


def _windows_contains(path: Path, root: Path) -> bool:
    candidate = PureWindowsPath(str(path))
    boundary = PureWindowsPath(str(root))
    return candidate == boundary or boundary in candidate.parents


def _lexically_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _filesystem_identity(path: Path) -> tuple[int, int]:
    try:
        details = path.stat()
    except OSError as exc:
        raise GrokPermissionError("Grok filesystem identity is unavailable") from exc
    return int(details.st_dev), int(details.st_ino)


def _attest_workspace_root(
    value: str | os.PathLike[str] | object,
) -> tuple[Path, tuple[int, int]]:
    try:
        lexical_windows = _lexical_local_dos_path(os.fspath(value), "workspace")
    except (GrokBindingIncompatible, TypeError, ValueError) as exc:
        raise GrokPermissionError("Grok workspace path is invalid") from exc
    lexical = Path(str(lexical_windows))
    chain = tuple(reversed(lexical.parents)) + (lexical,)
    try:
        for component in chain:
            if component.exists() and _is_reparse_point(component):
                raise GrokPermissionError("Grok workspace reparse path is denied")
        before = lexical.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise GrokPermissionError("Grok workspace is unavailable")
        identity = (int(before.st_dev), int(before.st_ino))
        canonical = lexical.resolve(strict=True)
        after = lexical.lstat()
    except GrokPermissionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise GrokPermissionError("Grok workspace is unavailable") from exc
    if (
        PureWindowsPath(str(canonical)) != lexical_windows
        or _is_reparse_point(lexical)
        or not stat.S_ISDIR(after.st_mode)
        or (int(after.st_dev), int(after.st_ino)) != identity
    ):
        raise GrokPermissionError("Grok workspace identity changed")
    return canonical, identity


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
    if _is_reparse_point(workspace):
        raise GrokPermissionError("Grok filesystem reparse path is denied")
    current = workspace
    for part in parts:
        current = current / part
        if _is_reparse_point(current):
            raise GrokPermissionError("Grok filesystem reparse path is denied")
        if not current.exists():
            return


def _render_isolation_config(home: Path) -> bytes:
    return _render_isolation_config_for_path(home, home / "bundled")


def _render_legacy_skills_isolation_config(home: Path) -> bytes:
    return _render_isolation_config_for_path(
        home,
        home / "bundled" / "skills",
    )


def _render_isolation_config_for_path(home: Path, ignored_path: Path) -> bytes:
    if not home.is_absolute() or not _lexically_within(ignored_path, home):
        raise GrokPermissionError("Grok isolation config home is invalid")
    try:
        ignored_literal = json.dumps(
            str(ignored_path),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError) as exc:
        raise GrokPermissionError("Grok isolation config path is invalid") from exc
    return (
        _ISOLATION_CONFIG_PREFIX
        + b'"~/.agents", '
        + ignored_literal
        + b"]\n"
    )


def _ensure_isolation_config(
    data_root: Path,
    home: Path,
    home_parts: tuple[str, ...],
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    config = home / "config.toml"
    expected_content = _render_isolation_config(home)
    if not _lexically_within(config, data_root):
        raise GrokPermissionError("Grok isolation config escapes product data")
    if expected_identity is not None:
        identity, _read_only = _verify_isolation_config(
            data_root,
            home,
            home_parts,
            required_read_only=True,
            expected_identity=expected_identity,
        )
        return identity
    if os.path.lexists(config):
        try:
            identity, read_only = _verify_isolation_config(
                data_root,
                home,
                home_parts,
                required_read_only=None,
            )
        except GrokPermissionError:
            return _migrate_legacy_isolation_config(
                data_root,
                home,
                home_parts,
            )
        if not read_only:
            _set_isolation_config_read_only(
                data_root,
                home,
                home_parts,
                identity,
            )
        _verify_isolation_config(
            data_root,
            home,
            home_parts,
            required_read_only=True,
            expected_identity=identity,
        )
        return identity
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(config, flags, 0o600)
    except FileExistsError:
        return _ensure_isolation_config(data_root, home, home_parts)
    except OSError as exc:
        raise GrokPermissionError("Grok isolation config could not be created") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            written = stream.write(expected_content)
            stream.flush()
            os.fsync(stream.fileno())
        if written != len(expected_content):
            raise OSError("short isolation config write")
    except OSError as exc:
        raise GrokPermissionError("Grok isolation config write was incomplete") from exc
    identity, _read_only = _verify_isolation_config(
        data_root,
        home,
        home_parts,
        required_read_only=None,
    )
    _set_isolation_config_read_only(
        data_root,
        home,
        home_parts,
        identity,
    )
    final_identity, _final_read_only = _verify_isolation_config(
        data_root,
        home,
        home_parts,
        required_read_only=True,
        expected_identity=identity,
    )
    return final_identity


def _remove_owned_isolation_stage(
    home: Path,
    staged: Path,
    expected_anchor: tuple[int, int],
) -> None:
    if (
        staged.parent != home
        or not staged.name.startswith(".config.toml.subagent-mcp-")
        or not staged.name.endswith(".tmp")
    ):
        raise GrokPermissionError("Grok isolation stage ownership is invalid")
    try:
        current = staged.lstat()
        anchor = (int(current.st_dev), int(current.st_ino))
        if (
            _is_reparse_point(staged)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or anchor != expected_anchor
        ):
            raise OSError("isolation stage identity changed")
        os.chmod(staged, stat.S_IMODE(current.st_mode) | stat.S_IWUSR)
        writable = staged.lstat()
        if (
            _is_reparse_point(staged)
            or (int(writable.st_dev), int(writable.st_ino)) != expected_anchor
        ):
            raise OSError("isolation stage changed before cleanup")
        staged.unlink()
    except OSError as exc:
        raise GrokPermissionError("Grok isolation stage cleanup failed") from exc
    if os.path.lexists(staged):
        raise GrokPermissionError("Grok isolation stage cleanup is incomplete")


def _stage_isolation_config(
    home: Path,
    content: bytes,
) -> tuple[Path, tuple[int, int, int, int]]:
    descriptor = -1
    staged: Path | None = None
    anchor: tuple[int, int] | None = None
    operation_error: BaseException | None = None
    try:
        descriptor, raw_staged = tempfile.mkstemp(
            prefix=".config.toml.subagent-mcp-",
            suffix=".tmp",
            dir=home,
        )
        staged = Path(raw_staged)
        opened = os.fstat(descriptor)
        anchor = (int(opened.st_dev), int(opened.st_ino))
        if (
            staged.parent != home
            or _is_reparse_point(staged)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise OSError("isolation stage is unsafe")
        written = os.write(descriptor, content)
        if written != len(content):
            raise OSError("short isolation stage write")
        os.fsync(descriptor)
    except (GrokPermissionError, OSError, RuntimeError) as exc:
        operation_error = exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                if operation_error is None:
                    operation_error = exc
    if operation_error is not None:
        cleanup_error: BaseException | None = None
        if staged is not None and anchor is not None:
            try:
                _remove_owned_isolation_stage(home, staged, anchor)
            except GrokPermissionError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise GrokPermissionError(
                "Grok failed isolation stage cleanup was not confirmed"
            ) from cleanup_error
        raise GrokPermissionError("Grok isolation config staging failed") from operation_error
    assert staged is not None and anchor is not None
    try:
        identity, read_only = _verify_exact_isolation_file(
            staged,
            content,
            required_read_only=False,
        )
        if (identity[0], identity[1]) != anchor or read_only:
            raise GrokPermissionError("Grok isolation stage identity is invalid")
        current = staged.lstat()
        if _isolation_file_identity(current) != identity:
            raise OSError("isolation stage changed before locking")
        os.chmod(
            staged,
            stat.S_IMODE(current.st_mode)
            & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
        )
        final_identity, _final_read_only = _verify_exact_isolation_file(
            staged,
            content,
            required_read_only=True,
            expected_identity=identity,
        )
    except (GrokPermissionError, OSError, RuntimeError) as exc:
        try:
            _remove_owned_isolation_stage(home, staged, anchor)
        except GrokPermissionError as cleanup_exc:
            raise GrokPermissionError(
                "Grok failed isolation stage cleanup was not confirmed"
            ) from cleanup_exc
        raise GrokPermissionError("Grok isolation stage could not be locked") from exc
    return staged, final_identity


def _migrate_legacy_isolation_config(
    data_root: Path,
    home: Path,
    home_parts: tuple[str, ...],
) -> tuple[int, int, int, int]:
    config = home / "config.toml"
    legacy_identity: tuple[int, int, int, int] | None = None
    legacy_content: bytes | None = None
    legacy_read_only: bool | None = None
    for candidate in (
        _LEGACY_ISOLATION_CONFIG,
        _render_legacy_skills_isolation_config(home),
    ):
        try:
            legacy_identity, legacy_read_only = _verify_isolation_config(
                data_root,
                home,
                home_parts,
                required_read_only=None,
                expected_content=candidate,
            )
        except GrokPermissionError:
            continue
        legacy_content = candidate
        break
    if (
        legacy_identity is None
        or legacy_content is None
        or legacy_read_only is None
    ):
        raise GrokPermissionError("Grok isolation config is not an exact legacy file")
    replacement = _render_isolation_config(home)
    staged, staged_identity = _stage_isolation_config(home, replacement)
    staged_anchor = (staged_identity[0], staged_identity[1])
    try:
        _verify_isolation_config(
            data_root,
            home,
            home_parts,
            required_read_only=legacy_read_only,
            expected_identity=legacy_identity,
            expected_content=legacy_content,
        )
        if legacy_read_only:
            _unlock_exact_isolation_config(
                data_root,
                home,
                home_parts,
                legacy_identity,
                expected_content=legacy_content,
            )
        os.replace(staged, config)
    except (GrokPermissionError, OSError, RuntimeError) as exc:
        recovery_error: BaseException | None = None
        try:
            current_identity, current_read_only = _verify_isolation_config(
                data_root,
                home,
                home_parts,
                required_read_only=None,
                expected_identity=legacy_identity,
                expected_content=legacy_content,
            )
            if not current_read_only:
                _set_isolation_config_read_only(
                    data_root,
                    home,
                    home_parts,
                    current_identity,
                    expected_content=legacy_content,
                )
        except (GrokPermissionError, OSError, RuntimeError) as recovery_exc:
            recovery_error = recovery_exc
        try:
            if not os.path.lexists(staged):
                raise GrokPermissionError("Grok isolation stage disappeared")
            _remove_owned_isolation_stage(home, staged, staged_anchor)
        except (GrokPermissionError, OSError, RuntimeError) as cleanup_exc:
            if recovery_error is None:
                recovery_error = cleanup_exc
        if recovery_error is not None:
            raise GrokPermissionError(
                "Grok legacy migration recovery was not confirmed"
            ) from recovery_error
        raise GrokPermissionError("Grok legacy isolation config migration failed") from exc
    final_identity, _final_read_only = _verify_isolation_config(
        data_root,
        home,
        home_parts,
        required_read_only=True,
        expected_identity=staged_identity,
    )
    return final_identity


def _verify_isolation_config(
    data_root: Path,
    home: Path,
    home_parts: tuple[str, ...],
    *,
    required_read_only: bool | None,
    expected_identity: tuple[int, int, int, int] | None = None,
    expected_content: bytes | None = None,
) -> tuple[tuple[int, int, int, int], bool]:
    config = home / "config.toml"
    exact_content = (
        _render_isolation_config(home)
        if expected_content is None
        else expected_content
    )
    if not _lexically_within(config, data_root):
        raise GrokPermissionError("Grok isolation config escapes product data")
    _reject_reparse_chain(data_root, (*home_parts, "config.toml"))
    identity, read_only = _verify_exact_isolation_file(
        config,
        exact_content,
        required_read_only=required_read_only,
        expected_identity=expected_identity,
    )
    _reject_reparse_chain(data_root, (*home_parts, "config.toml"))
    return identity, read_only


def _verify_exact_isolation_file(
    path: Path,
    exact_content: bytes,
    *,
    required_read_only: bool | None,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> tuple[tuple[int, int, int, int], bool]:
    if _is_reparse_point(path):
        raise GrokPermissionError("Grok isolation file reparse path is denied")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OSError("isolation config is not one regular file")
            content = stream.read(len(exact_content) + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError as exc:
        raise GrokPermissionError("Grok isolation config is unavailable") from exc
    identity = _isolation_file_identity(current)
    read_only = _is_read_only_file(current)
    if (
        _is_reparse_point(path)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or _isolation_file_identity(before) != _isolation_file_identity(after)
        or _isolation_file_identity(after) != identity
        or (expected_identity is not None and identity != expected_identity)
        or content != exact_content
        or (required_read_only is not None and read_only is not required_read_only)
    ):
        raise GrokPermissionError("Grok isolation config is not exact")
    return identity, read_only


def _set_isolation_config_read_only(
    data_root: Path,
    home: Path,
    home_parts: tuple[str, ...],
    expected_identity: tuple[int, int, int, int],
    *,
    expected_content: bytes | None = None,
) -> None:
    config = home / "config.toml"
    _verify_isolation_config(
        data_root,
        home,
        home_parts,
        required_read_only=None,
        expected_identity=expected_identity,
        expected_content=expected_content,
    )
    try:
        current = config.lstat()
        if _is_reparse_point(config) or _isolation_file_identity(current) != expected_identity:
            raise OSError("isolation config changed before read-only lock")
        mode = stat.S_IMODE(current.st_mode)
        os.chmod(config, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except (GrokPermissionError, OSError) as exc:
        raise GrokPermissionError("Grok isolation config could not be locked") from exc
    _verify_isolation_config(
        data_root,
        home,
        home_parts,
        required_read_only=True,
        expected_identity=expected_identity,
        expected_content=expected_content,
    )


def _unlock_exact_isolation_config(
    data_root: Path,
    home: Path,
    home_parts: tuple[str, ...],
    expected_identity: tuple[int, int, int, int],
    *,
    expected_content: bytes | None = None,
) -> None:
    config = home / "config.toml"
    identity, _read_only = _verify_isolation_config(
        data_root,
        home,
        home_parts,
        required_read_only=True,
        expected_identity=expected_identity,
        expected_content=expected_content,
    )
    try:
        current = config.lstat()
        if _is_reparse_point(config) or _isolation_file_identity(current) != identity:
            raise OSError("isolation config changed before cleanup")
        os.chmod(config, stat.S_IMODE(current.st_mode) | stat.S_IWUSR)
    except (GrokPermissionError, OSError) as exc:
        raise GrokPermissionError("Grok isolation config cleanup is unavailable") from exc
    _verify_isolation_config(
        data_root,
        home,
        home_parts,
        required_read_only=False,
        expected_identity=identity,
        expected_content=expected_content,
    )


def _is_read_only_file(details: os.stat_result) -> bool:
    if sys.platform == "win32":
        attributes = int(getattr(details, "st_file_attributes", 0))
        return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_READONLY", 1)))
    return not bool(
        stat.S_IMODE(details.st_mode)
        & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )


def _isolation_file_identity(details: os.stat_result) -> tuple[int, int, int, int]:
    modified_ns = getattr(details, "st_mtime_ns", None)
    if not isinstance(modified_ns, int):
        modified_ns = int(float(details.st_mtime) * 1_000_000_000)
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        modified_ns,
    )


def _directory_identity(details: os.stat_result) -> tuple[int, int]:
    return int(details.st_dev), int(details.st_ino)


def _assert_guard_home_identity(
    home: Path,
    expected_identity: tuple[int, int],
) -> None:
    try:
        details = home.lstat()
        current = home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GrokPermissionError("Grok billing guard identity is unavailable") from exc
    if (
        current != home
        or _is_reparse_point(home)
        or not stat.S_ISDIR(details.st_mode)
        or _directory_identity(details) != expected_identity
    ):
        raise GrokPermissionError("Grok billing guard identity changed")


@contextmanager
def _locked_grok_startup(paths: tuple[Path, ...]) -> Iterator[None]:
    canonical: list[tuple[Path, tuple[int, int, int, int], bool]] = []
    seen: set[str] = set()
    try:
        for raw in paths:
            path = raw.resolve(strict=True)
            folded = os.path.normcase(str(path)).casefold()
            if folded in seen:
                continue
            seen.add(folded)
            if _is_reparse_point(path):
                raise OSError("Grok startup path is a reparse point")
            details = path.lstat()
            if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
                raise OSError("Grok startup path is not regular")
            canonical.append(
                (path, _isolation_file_identity(details), stat.S_ISDIR(details.st_mode))
            )
    except (GrokPermissionError, OSError, RuntimeError) as exc:
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok startup authority could not be pinned",
            category="context",
        ) from exc

    if sys.platform != "win32":
        yield
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    handles: list[int] = []
    try:
        for path, identity, is_directory in canonical:
            flags = 0x02000000 if is_directory else 0x00000080
            handle = create_file(
                str(path),
                0x80000000,  # GENERIC_READ
                0x00000001,  # FILE_SHARE_READ only
                None,
                3,  # OPEN_EXISTING
                flags,
                None,
            )
            if handle == invalid_handle:
                raise OSError(ctypes.get_last_error(), "CreateFileW failed")
            handles.append(handle)
            current = path.lstat()
            if (
                _is_reparse_point(path)
                or _isolation_file_identity(current) != identity
            ):
                raise OSError("Grok startup path changed while locking")
    except (GrokPermissionError, OSError, RuntimeError) as exc:
        for handle in reversed(handles):
            close_handle(handle)
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok startup authority could not be pinned",
            category="context",
        ) from exc
    try:
        yield
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _reject_workspace_native_extension_directories(workspace: Path) -> None:
    for name in (".grok", ".agents"):
        if os.path.lexists(workspace / name):
            raise _capability_error(
                f"Workspace {name} executable surface must be removed before delegation"
            )


def _remove_uninitialized_guard_home(
    data_root: Path,
    guard_root: Path,
    home: Path,
    expected_identity: tuple[int, int],
    expected_config_identity: tuple[int, int, int, int] | None,
) -> None:
    if home.parent != guard_root or not home.name.startswith("billing-"):
        raise GrokPermissionError("Grok billing guard ownership is invalid")
    _reject_reparse_chain(data_root, (RUNTIME_ID, "billing-guards", home.name))
    _assert_guard_home_identity(home, expected_identity)
    try:
        children = tuple(home.iterdir())
    except (OSError, RuntimeError) as exc:
        raise GrokPermissionError("Grok billing guard cleanup is unavailable") from exc
    for child in children:
        if (
            child.name != "config.toml"
            or _is_reparse_point(child)
            or not child.is_file()
        ):
            raise GrokPermissionError("Grok billing guard cleanup content is unsafe")
    try:
        if children:
            config = children[0]
            identity, read_only = _verify_isolation_config(
                data_root,
                home,
                (RUNTIME_ID, "billing-guards", home.name),
                required_read_only=None,
                expected_identity=expected_config_identity,
            )
            if read_only:
                _unlock_exact_isolation_config(
                    data_root,
                    home,
                    (RUNTIME_ID, "billing-guards", home.name),
                    identity,
                )
            else:
                _verify_isolation_config(
                    data_root,
                    home,
                    (RUNTIME_ID, "billing-guards", home.name),
                    required_read_only=False,
                    expected_identity=identity,
                )
            _assert_guard_home_identity(home, expected_identity)
            config.unlink()
            if os.path.lexists(config):
                raise OSError("Grok billing guard config cleanup is incomplete")
        _assert_guard_home_identity(home, expected_identity)
        home.rmdir()
    except OSError as exc:
        raise GrokPermissionError("Grok billing guard cleanup failed") from exc
    if os.path.lexists(home):
        raise GrokPermissionError("Grok billing guard cleanup is incomplete")


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


def _agent_profile_document(mode: object) -> tuple[str, str]:
    if mode == "review":
        label = "review"
        tools = _REVIEW_TOOL_ALLOWLIST
    elif mode == "writer":
        label = "writer"
        tools = _WRITER_TOOL_ALLOWLIST
    else:
        raise ServiceError("CONTEXT_DRIFT", "Grok launch mode is invalid")
    encoded = _canonical_json(
        {
            "name": f"subagent-mcp-{label}",
            "description": f"Bounded Subagent MCP {label} profile.",
            "permissionMode": _AGENT_PROFILE_PERMISSION_MODE,
            "discoverSkills": False,
            "inheritSkills": False,
            "agentsMd": False,
            "injectDefaultTools": False,
            "tools": list(tools),
            "disallowedTools": list(_DISALLOWED_META_TOOLS),
            "skills": [],
            "mcpServers": [],
            "promptMode": "extend",
            "promptBody": "Follow the caller's requested final-output format exactly.",
        }
    )
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


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


def _extension_set(value: object) -> set[tuple[str, str]]:
    if not isinstance(value, (tuple, list)) or len(value) > 128:
        raise _capability_error("Grok extension isolation evidence is malformed")
    result: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
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
    if (
        not isinstance(value, (tuple, list))
        or len(value) > 32
        or not all(isinstance(item, str) for item in value)
    ):
        raise ServiceError("CONTEXT_DRIFT", f"Grok {label} attestation is invalid")
    return tuple(value)


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


def _child_env(source: Mapping[str, str]) -> dict[str, str]:
    result = {
        name: value
        for name, value in source.items()
        if name.upper() in _CHILD_ENV_NAMES and isinstance(value, str)
    }
    result["GROK_DISABLE_AUTOUPDATER"] = "1"
    return dict(sorted(result.items()))


def _isolated_child_env(
    source: Mapping[str, str],
    runtime_home: Path,
) -> dict[str, str]:
    result = _child_env(source)
    auth_path = _original_auth_path(source)
    if auth_path is not None:
        result["GROK_AUTH_PATH"] = str(auth_path)
    result.update(
        {
            "GROK_CAMPAIGNS": "0",
            "GROK_DISABLE_API_KEY_AUTH": "1",
            "GROK_FOLDER_TRUST": "1",
            "GROK_HOME": str(runtime_home),
            "GROK_MANAGED_CONFIG": "0",
        }
    )
    return dict(sorted(result.items()))


def _original_auth_path(source: Mapping[str, str]) -> Path | None:
    if "GROK_AUTH_PATH" in source:
        return _absolute_environment_path(source.get("GROK_AUTH_PATH"))
    if "GROK_HOME" in source:
        original_home = _absolute_environment_path(source.get("GROK_HOME"))
        return None if original_home is None else original_home / "auth.json"
    user_profile = _absolute_environment_path(source.get("USERPROFILE"))
    if user_profile is not None:
        return user_profile / ".grok" / "auth.json"
    return None


def _absolute_environment_path(value: object) -> Path | None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _bounded_text(value, 4096) is None
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        return None
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        return None
    return path


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
                "file_identity": {
                    "size": identity["size"],
                    "sha256": identity["sha256"],
                },
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


def _require_resumed_context_authority(
    requested: ResolvedContext,
    owned: ResolvedContext,
) -> None:
    def normalized(context: ResolvedContext) -> dict[str, object]:
        return {
            "runtime_id": context.runtime_id,
            "requested_model": context.requested_model,
            "effective_model": context.effective_model,
            "requested_reasoning": context.requested_reasoning,
            "effective_reasoning": context.effective_reasoning,
            "workspace_path": context.workspace_path,
            "workspace_key": context.workspace_key,
            "transport": context.transport,
            "context_hash": context.context_hash,
            "capability_gaps": context.capability_gaps,
            "attestation": {
                key: context.attestation[key]
                for key in _RESUMED_AUTHORITY_FIELDS
            },
        }

    try:
        matches = _canonical_json(normalized(requested)) == _canonical_json(
            normalized(owned)
        )
    except (KeyError, TypeError, ValueError):
        matches = False
    if not matches:
        raise ServiceError(
            "CONTEXT_DRIFT",
            "Grok ACP resumed authority changed",
            category="context",
        )


def _capability_error(message: str) -> ServiceError:
    return ServiceError(
        "CAPABILITY_MISSING",
        message,
        category="capability",
        retryable=False,
    )
