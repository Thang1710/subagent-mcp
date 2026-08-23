"""Provider-neutral contracts shared by MCP, CLI, UI, and adapters."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


ADAPTER_API_VERSION = "1.0.0"
CONTRACT_SCHEMA_VERSION = 1
TERMINAL_EXECUTION_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted"}
)
RESULT_CAPSULE_MAX_CHARS = 512
RESULT_SLICE_DEFAULT_CHARS = 4_096
RESULT_SLICE_MAX_CHARS = 8_192
PROMPT_MAX_BYTES = 128 * 1024
_PROMPT_FORMATTING_CONTROLS = frozenset({"\t", "\n", "\r"})
ROUGH_TOKEN_ESTIMATE_BASIS = (
    "content-only rough estimate: ceil(utf8_bytes / 3);"
    " not provider billing or a tokenizer claim"
)
_ICON_TONES = ("blue", "green", "purple", "teal")


class ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceError(RuntimeError):
    """Stable public failure returned by every product surface."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "request",
        retryable: bool = False,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.next_action = next_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "message": str(self),
            "next_action": self.next_action,
        }


class ConversationState(str, Enum):
    OPEN = "open"
    ACTIVE = "active"
    NEEDS_INPUT = "needs_input"
    IDLE = "idle"
    CLOSED = "closed"


class ExecutionState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class EventKind(str, Enum):
    STARTED = "started"
    CHECKPOINT = "checkpoint"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    PERMISSION_DENIED = "permission_denied"
    NEEDS_INPUT = "needs_input"
    QUOTA_PAUSED = "quota_paused"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


_EXECUTION_TRANSITIONS: Mapping[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.QUEUED: frozenset(
        {ExecutionState.STARTING, ExecutionState.FAILED, ExecutionState.CANCELLED}
    ),
    ExecutionState.STARTING: frozenset(
        {
            ExecutionState.RUNNING,
            ExecutionState.NEEDS_INPUT,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.INTERRUPTED,
        }
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.NEEDS_INPUT,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.INTERRUPTED,
        }
    ),
    ExecutionState.NEEDS_INPUT: frozenset(
        {
            ExecutionState.RUNNING,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.INTERRUPTED,
        }
    ),
    ExecutionState.SUCCEEDED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
    ExecutionState.INTERRUPTED: frozenset(),
}


def require_execution_transition(
    current: ExecutionState | str,
    target: ExecutionState | str,
) -> None:
    try:
        current_state = ExecutionState(current)
        target_state = ExecutionState(target)
    except ValueError as exc:
        raise ContractError("STATE_INVALID", "unknown execution state") from exc
    if current_state == target_state:
        return
    if target_state not in _EXECUTION_TRANSITIONS[current_state]:
        raise ContractError(
            "STATE_CONFLICT",
            f"execution cannot transition from {current_state.value} to {target_state.value}",
        )


def validate_model_id(value: object) -> str:
    return validate_bounded_text(value, "model", 256, strip=False)


def validate_identifier(value: object, label: str, max_bytes: int = 128) -> str:
    return validate_bounded_text(value, label, max_bytes, strip=True)


def validate_bounded_text(
    value: object,
    label: str,
    max_bytes: int,
    *,
    strip: bool,
    allowed_controls: frozenset[str] = frozenset(),
) -> str:
    if not isinstance(value, str) or not value or (strip and value != value.strip()):
        raise ContractError("REQUEST_INVALID", f"{label} must be nonempty")
    if not value.strip():
        raise ContractError("REQUEST_INVALID", f"{label} must be nonempty")
    if len(value.encode("utf-8")) > max_bytes:
        raise ContractError("REQUEST_INVALID", f"{label} is too long")
    if any(
        unicodedata.category(character) == "Cc"
        and character not in allowed_controls
        for character in value
    ):
        raise ContractError("REQUEST_INVALID", f"{label} contains a control character")
    return value


def validate_json_object(value: object, label: str, max_bytes: int = 64 * 1024) -> None:
    if not isinstance(value, Mapping):
        raise ContractError("REQUEST_INVALID", f"{label} must be an object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError("REQUEST_INVALID", f"{label} is not JSON-safe") from exc
    if len(encoded) > max_bytes:
        raise ContractError("REQUEST_INVALID", f"{label} is too large")


@dataclass(frozen=True, slots=True)
class AdapterManifest:
    adapter_api_version: str
    runtime_id: str
    provider_id: str
    harness_id: str
    display_name: str
    adapter_version: str
    supported_platforms: tuple[str, ...]
    supported_transports: tuple[str, ...]
    capabilities: frozenset[str]
    semantic_permissions: frozenset[str]
    reasoning_schema: Mapping[str, Any]
    model_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.adapter_api_version != ADAPTER_API_VERSION:
            raise ContractError("ADAPTER_INCOMPATIBLE", "adapter API version is unsupported")
        validate_identifier(self.runtime_id, "runtime_id")
        validate_identifier(self.provider_id, "provider_id")
        validate_identifier(self.harness_id, "harness_id")
        validate_bounded_text(self.display_name, "display_name", 256, strip=True)
        validate_identifier(self.adapter_version, "adapter_version", 64)
        _validate_text_collection(self.supported_platforms, "supported_platforms")
        _validate_text_collection(self.supported_transports, "supported_transports")
        _validate_text_collection(tuple(self.capabilities), "capabilities", allow_empty=True)
        _validate_text_collection(
            tuple(self.semantic_permissions),
            "semantic_permissions",
            allow_empty=True,
        )
        validate_json_object(self.reasoning_schema, "reasoning_schema")
        validate_json_object(self.model_schema, "model_schema")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_api_version": self.adapter_api_version,
            "runtime_id": self.runtime_id,
            "provider_id": self.provider_id,
            "harness_id": self.harness_id,
            "display_name": self.display_name,
            "adapter_version": self.adapter_version,
            "supported_platforms": list(self.supported_platforms),
            "supported_transports": list(self.supported_transports),
            "capabilities": sorted(self.capabilities),
            "semantic_permissions": sorted(self.semantic_permissions),
            "reasoning_schema": dict(self.reasoning_schema),
            "model_schema": dict(self.model_schema),
        }


def _descriptor_icon(runtime_id: str, display_name: str) -> dict[str, str]:
    """Provider-neutral badge: a derived monogram plus a stable package tone."""

    monogram = next(
        (character.upper() for character in display_name if character.isalnum()),
        "S",
    )
    tone_index = hashlib.sha256(runtime_id.encode("utf-8")).digest()[0] % len(_ICON_TONES)
    return {
        "kind": "monogram",
        "text": monogram,
        "tone": _ICON_TONES[tone_index],
    }


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    schema_version: int
    runtime_id: str
    provider_id: str
    harness_id: str
    display_name: str
    model_display_name: str
    transport: str
    capability_gaps: tuple[str, ...] = ()
    icon: Mapping[str, Any] = field(
        default_factory=lambda: {"kind": "monogram", "text": "S"}
    )
    ui_surfaces: Mapping[str, str] = field(
        default_factory=lambda: {
            "localhost_activity": "supported",
            "mcp_app": "unsupported",
            "native_host_panel": "unsupported",
        }
    )

    @classmethod
    def from_manifest(
        cls,
        manifest: AdapterManifest,
        *,
        model: str,
        transport: str,
        capability_gaps: Sequence[str] = (),
    ) -> "AgentDescriptor":
        validate_model_id(model)
        validate_identifier(transport, "transport", 64)
        _validate_text_collection(tuple(capability_gaps), "capability_gaps", allow_empty=True)
        return cls(
            schema_version=CONTRACT_SCHEMA_VERSION,
            runtime_id=manifest.runtime_id,
            provider_id=manifest.provider_id,
            harness_id=manifest.harness_id,
            display_name=manifest.display_name,
            model_display_name=model,
            transport=transport,
            capability_gaps=tuple(capability_gaps),
            icon=_descriptor_icon(manifest.runtime_id, manifest.display_name),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_id": self.runtime_id,
            "provider_id": self.provider_id,
            "harness_id": self.harness_id,
            "display_name": self.display_name,
            "model_display_name": self.model_display_name,
            "transport": self.transport,
            "capability_gaps": list(self.capability_gaps),
            "icon": dict(self.icon),
            "ui_surfaces": dict(self.ui_surfaces),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentDescriptor":
        return cls(
            schema_version=int(value["schema_version"]),
            runtime_id=str(value["runtime_id"]),
            provider_id=str(value["provider_id"]),
            harness_id=str(value["harness_id"]),
            display_name=str(value["display_name"]),
            model_display_name=str(value["model_display_name"]),
            transport=str(value["transport"]),
            capability_gaps=tuple(value.get("capability_gaps", ())),
            icon=dict(value.get("icon", {"kind": "monogram", "text": "S"})),
            ui_surfaces=dict(value.get("ui_surfaces", {})),
        )


@dataclass(frozen=True, slots=True)
class TaskPacket:
    title: str
    prompt: str
    acceptance_criteria: tuple[str, ...]
    role: str
    authority: tuple[str, ...] = ()
    repository_base: str | None = None
    repository_head: str | None = None

    def __post_init__(self) -> None:
        validate_bounded_text(self.title, "task.title", 512, strip=False)
        validate_bounded_text(
            self.prompt,
            "task.prompt",
            PROMPT_MAX_BYTES,
            strip=False,
            allowed_controls=_PROMPT_FORMATTING_CONTROLS,
        )
        validate_bounded_text(self.role, "task.role", 128, strip=False)
        _validate_text_collection(
            self.acceptance_criteria,
            "task.acceptance_criteria",
            max_items=64,
        )
        _validate_text_collection(self.authority, "task.authority", max_items=64, allow_empty=True)


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    request_id: str
    runtime_id: str
    variant_id: str
    task: TaskPacket
    cwd: str
    mode: str
    transport: str = "auto"
    permissions: tuple[str, ...] = ()
    context_policy_id: str = "declared-native"
    permission_policy_id: str = "default"
    write_set: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_identifier(self.request_id, "request_id", 256)
        validate_identifier(self.runtime_id, "runtime_id")
        validate_identifier(self.variant_id, "variant_id")
        validate_bounded_text(self.cwd, "cwd", 4096, strip=True)
        if self.mode not in {"review", "plan", "test", "implement"}:
            raise ContractError("REQUEST_INVALID", "mode is unsupported")
        if self.transport not in {
            "auto",
            "visible-background",
            "managed-sdk",
            "native-acp",
        }:
            raise ContractError("REQUEST_INVALID", "transport is unsupported")
        validate_identifier(self.context_policy_id, "context_policy_id")
        validate_identifier(self.permission_policy_id, "permission_policy_id")
        _validate_text_collection(self.permissions, "permissions", max_items=32, allow_empty=True)
        _validate_text_collection(self.write_set, "write_set", max_items=32, allow_empty=True)
        if self.write_set and "workspace_write" not in self.permissions:
            raise ContractError(
                "REQUEST_INVALID", "write_set requires the workspace_write capability"
            )
        for value in self.write_set:
            validate_bounded_text(value, "write_set", 2048, strip=True)
            normalized = value.replace("\\", "/")
            parts = normalized.split("/")
            if (
                normalized.startswith("/")
                or (len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":")
                or ".." in parts
                or any(_unsafe_write_set_component(part) for part in parts)
            ):
                raise ContractError(
                    "REQUEST_INVALID", "write_set entries must be repository-relative"
                )


def _unsafe_write_set_component(component: str) -> bool:
    if component == ".":
        return False
    if not component or ":" in component or component.endswith((".", " ")):
        return True
    stem = component.split(".", 1)[0].casefold()
    return stem in {"con", "prn", "aux", "nul", "clock$"} or (
        len(stem) == 4
        and stem[:3] in {"com", "lpt"}
        and stem[3] in "123456789"
    )


def rough_token_estimate(utf8_bytes: int) -> int:
    """Content-only comparison signal: ``ceil(utf8_bytes / 3)``.

    This is not provider billing and not a tokenizer claim. Exact token use
    still depends on the controller model and tool-schema overhead.
    """

    if isinstance(utf8_bytes, bool) or not isinstance(utf8_bytes, int):
        raise ValueError("utf8_bytes must be an integer")
    if utf8_bytes < 0:
        raise ValueError("utf8_bytes must be nonnegative")
    return (utf8_bytes + 2) // 3


def content_transfer_metrics(
    full_text: str,
    compact_text: str | None,
) -> dict[str, Any]:
    """Exact UTF-8 byte counts plus labelled rough token comparison values."""

    full_bytes = len(full_text.encode("utf-8"))
    compact_bytes = 0 if not compact_text else len(compact_text.encode("utf-8"))
    full_tokens = rough_token_estimate(full_bytes)
    compact_tokens = rough_token_estimate(compact_bytes)
    return {
        "basis": ROUGH_TOKEN_ESTIMATE_BASIS,
        "full_utf8_bytes": full_bytes,
        "compact_utf8_bytes": compact_bytes,
        "rough_tokens_full": full_tokens,
        "rough_tokens_compact": compact_tokens,
        "rough_tokens_saved": max(full_tokens - compact_tokens, 0),
    }


def slice_transfer_metrics(text: str) -> dict[str, Any]:
    """Exact slice character/byte counts plus the same content-only estimate."""

    raw_bytes = len(text.encode("utf-8"))
    return {
        "basis": ROUGH_TOKEN_ESTIMATE_BASIS,
        "chars": len(text),
        "utf8_bytes": raw_bytes,
        "rough_tokens": rough_token_estimate(raw_bytes),
    }


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Hash-bound pointer to one persisted redacted result artifact.

    The reference alone is durable state; artifact text is expanded only in
    memory by the service for the target adapter request.
    """

    conversation_id: str
    execution_id: str
    expected_sha256: str

    def __post_init__(self) -> None:
        validate_identifier(self.conversation_id, "artifact.conversation_id")
        validate_identifier(self.execution_id, "artifact.execution_id")
        if not isinstance(self.expected_sha256, str) or len(self.expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.expected_sha256
        ):
            raise ContractError(
                "REQUEST_INVALID",
                "artifact.expected_sha256 must be lowercase SHA-256",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "execution_id": self.execution_id,
            "expected_sha256": self.expected_sha256,
        }


@dataclass(frozen=True, slots=True)
class SendRequest:
    request_id: str
    conversation_id: str
    prompt: str
    reply_to: str | None = None
    answers: Mapping[str, Any] = field(default_factory=dict)
    artifact: ArtifactReference | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.request_id, "request_id", 256)
        validate_identifier(self.conversation_id, "conversation_id")
        validate_bounded_text(
            self.prompt,
            "prompt",
            PROMPT_MAX_BYTES,
            strip=False,
            allowed_controls=_PROMPT_FORMATTING_CONTROLS,
        )
        if self.reply_to is not None:
            validate_identifier(self.reply_to, "reply_to", 256)
        validate_json_object(self.answers, "answers", 64 * 1024)
        if self.artifact is not None and not isinstance(self.artifact, ArtifactReference):
            raise ContractError(
                "REQUEST_INVALID", "artifact must be an ArtifactReference"
            )


@dataclass(frozen=True, slots=True)
class StatusRequest:
    conversation_id: str
    after_cursor: int = 0
    refresh: bool = True

    def __post_init__(self) -> None:
        validate_identifier(self.conversation_id, "conversation_id")
        if isinstance(self.after_cursor, bool) or self.after_cursor < 0:
            raise ContractError("REQUEST_INVALID", "after_cursor must be nonnegative")


@dataclass(frozen=True, slots=True)
class ResultReadRequest:
    conversation_id: str
    execution_id: str
    expected_sha256: str
    offset: int = 0
    limit: int = RESULT_SLICE_DEFAULT_CHARS

    def __post_init__(self) -> None:
        validate_identifier(self.conversation_id, "conversation_id")
        validate_identifier(self.execution_id, "execution_id")
        if not isinstance(self.expected_sha256, str) or len(self.expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.expected_sha256
        ):
            raise ContractError("REQUEST_INVALID", "expected_sha256 must be lowercase SHA-256")
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or self.offset < 0
        ):
            raise ContractError("REQUEST_INVALID", "offset must be nonnegative")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= RESULT_SLICE_MAX_CHARS
        ):
            raise ContractError(
                "REQUEST_INVALID",
                f"limit must be between 1 and {RESULT_SLICE_MAX_CHARS}",
            )


@dataclass(frozen=True, slots=True)
class WaitTarget:
    conversation_id: str
    after_revision: int = 0
    after_cursor: int = 0

    def __post_init__(self) -> None:
        validate_identifier(self.conversation_id, "conversation_id")
        if isinstance(self.after_revision, bool) or self.after_revision < 0:
            raise ContractError("REQUEST_INVALID", "after_revision must be nonnegative")
        if isinstance(self.after_cursor, bool) or self.after_cursor < 0:
            raise ContractError("REQUEST_INVALID", "after_cursor must be nonnegative")


@dataclass(frozen=True, slots=True)
class WaitRequest:
    targets: tuple[WaitTarget, ...]
    timeout_seconds: float = 240.0

    def __post_init__(self) -> None:
        if not 1 <= len(self.targets) <= 8:
            raise ContractError("REQUEST_INVALID", "agent_wait requires one to eight targets")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 <= self.timeout_seconds <= 240
        ):
            raise ContractError("REQUEST_INVALID", "timeout_seconds must be between 0 and 240")


@dataclass(frozen=True, slots=True)
class ActionRequest:
    request_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        validate_identifier(self.request_id, "request_id", 256)
        validate_identifier(self.conversation_id, "conversation_id")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    cursor: int
    kind: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"cursor": self.cursor, "kind": self.kind, "payload": dict(self.payload)}


@dataclass(frozen=True, slots=True)
class AgentStatus:
    conversation_id: str
    execution_id: str
    external_session_id: str | None
    workspace_path: str
    conversation_state: str
    execution_state: str
    state_revision: int
    descriptor: AgentDescriptor
    result: Mapping[str, Any] | None
    needs_input: tuple[Mapping[str, Any], ...]
    events: tuple[AgentEvent, ...]
    next_event_cursor: int
    recovery_required: bool = False

    def to_compact_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "conversation_id": self.conversation_id,
            "conversation_state": self.conversation_state,
            "execution_state": self.execution_state,
            "status": self.execution_state,
            "state_revision": self.state_revision,
            "next_event_cursor": self.next_event_cursor,
        }
        if self.result is not None and self.execution_state in TERMINAL_EXECUTION_STATES:
            if "error" in self.result:
                payload["result"] = dict(self.result)
            else:
                artifact = result_artifact_metadata(self.execution_id, self.result)
                payload["result"] = (
                    dict(self.result) if artifact is None else {"artifact": artifact}
                )
        if self.needs_input:
            payload["needs_input"] = [dict(item) for item in self.needs_input]
        if self.recovery_required:
            payload["recovery_required"] = True
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "conversation_id": self.conversation_id,
            "execution_id": self.execution_id,
            "external_session_id": self.external_session_id,
            "workspace_path": self.workspace_path,
            "conversation_state": self.conversation_state,
            "execution_state": self.execution_state,
            "status": self.execution_state,
            "state_revision": self.state_revision,
            "descriptor": self.descriptor.to_dict(),
            "result": None if self.result is None else dict(self.result),
            "needs_input": [dict(item) for item in self.needs_input],
            "events": [event.to_dict() for event in self.events],
            "next_event_cursor": self.next_event_cursor,
            "recovery_required": self.recovery_required,
        }


def result_artifact_metadata(
    execution_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any] | None:
    text = result.get("text")
    if not isinstance(text, str):
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    metadata: dict[str, Any] = {
        "artifact_id": f"result:{execution_id}:{digest}",
        "execution_id": execution_id,
        "sha256": digest,
        "char_count": len(text),
    }
    capsule_at = text.casefold().find("capsule:")
    compact_text: str | None = None
    if capsule_at >= 0:
        capsule = text[capsule_at + len("capsule:") :].partition("\n")[0].strip()
        if capsule:
            compact_text = capsule[:RESULT_CAPSULE_MAX_CHARS]
            metadata["capsule"] = compact_text
            metadata["transfer_metrics"] = content_transfer_metrics(text, compact_text)
            return metadata
    preview = " ".join(text.split())[:RESULT_CAPSULE_MAX_CHARS]
    if preview:
        metadata["preview"] = preview
        compact_text = preview
    metadata["transfer_metrics"] = content_transfer_metrics(text, compact_text)
    return metadata


def _validate_text_collection(
    values: Sequence[str],
    label: str,
    *,
    max_items: int = 128,
    allow_empty: bool = False,
) -> None:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ContractError("REQUEST_INVALID", f"{label} must be an array")
    if not allow_empty and not values:
        raise ContractError("REQUEST_INVALID", f"{label} must not be empty")
    if len(values) > max_items:
        raise ContractError("REQUEST_INVALID", f"{label} has too many entries")
    seen: set[str] = set()
    for value in values:
        checked = validate_bounded_text(value, label, 4096, strip=False)
        if checked in seen:
            raise ContractError("REQUEST_INVALID", f"{label} contains duplicates")
        seen.add(checked)
