"""Versioned asynchronous contract implemented by native-harness adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ..contracts import AdapterManifest, TaskPacket


@dataclass(frozen=True, slots=True)
class ProbeResult:
    state: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterContextRequest:
    runtime_id: str
    variant_id: str
    model: str
    reasoning: Mapping[str, Any]
    workspace_path: str
    workspace_key: str
    transport: str
    permissions: tuple[str, ...]
    context_policy_id: str
    permission_policy_id: str


@dataclass(frozen=True, slots=True)
class ResolvedContext:
    runtime_id: str
    requested_model: str
    effective_model: str
    requested_reasoning: Mapping[str, Any]
    effective_reasoning: Mapping[str, Any]
    workspace_path: str
    workspace_key: str
    transport: str
    context_hash: str
    capability_gaps: tuple[str, ...] = ()
    attestation: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterSpawnRequest:
    conversation_id: str
    execution_id: str
    task: TaskPacket
    context: ResolvedContext


@dataclass(frozen=True, slots=True)
class AdapterSendRequest:
    conversation_id: str
    execution_id: str
    external_session_id: str
    prompt: str
    reply_to: str | None
    answers: Mapping[str, Any]
    context: ResolvedContext


@dataclass(frozen=True, slots=True)
class AdapterSessionRequest:
    conversation_id: str
    execution_id: str
    external_session_id: str
    external_execution_id: str | None


@dataclass(frozen=True, slots=True)
class AdapterFailure:
    code: str
    category: str
    retryable: bool
    message: str


@dataclass(frozen=True, slots=True)
class CanaryRequest:
    runtime_id: str
    variant_id: str
    model: str
    reasoning: Mapping[str, Any]
    transport: str
    base_pair_key: str
    pair_key: str


@dataclass(frozen=True, slots=True)
class CanaryResult:
    passed: bool
    pair_key: str
    details: Mapping[str, Any] = field(default_factory=dict)
    error: AdapterFailure | None = None


@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    external_session_id: str
    external_execution_id: str
    conversation_state: str
    execution_state: str
    effective_model: str
    effective_reasoning: Mapping[str, Any]
    workspace_path: str
    workspace_key: str
    context_hash: str
    result_text: str | None = None
    needs_input: tuple[Mapping[str, Any], ...] = ()
    error: AdapterFailure | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    @property
    def manifest(self) -> AdapterManifest:
        ...

    async def probe(self) -> ProbeResult:
        ...

    async def resolve_context(self, request: AdapterContextRequest) -> ResolvedContext:
        ...

    async def spawn(self, request: AdapterSpawnRequest) -> AdapterSnapshot:
        ...

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        ...

    async def snapshot(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        ...

    async def interrupt(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        ...

    async def close(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        ...

    async def open_session(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        ...


@runtime_checkable
class OrphanCleanupAdapter(Protocol):
    """Optional exact cleanup attestation for a lost native connection."""

    async def orphan_cleanup_confirmed(
        self,
        request: AdapterSessionRequest,
        context: ResolvedContext,
    ) -> bool:
        ...


@runtime_checkable
class CanaryAdapter(Adapter, Protocol):
    """Optional bootstrap implemented only by live-capable adapters."""

    async def runtime_canary(self, request: CanaryRequest) -> CanaryResult:
        ...


@runtime_checkable
class QuotaProbeAdapter(Adapter, Protocol):
    """Optional pre-model quota evidence published by a native harness."""

    async def quota_probe(self, request: CanaryRequest) -> CanaryResult:
        ...
