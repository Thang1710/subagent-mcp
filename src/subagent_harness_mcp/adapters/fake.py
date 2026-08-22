"""Deterministic native-harness stand-in used by conformance and E2E tests."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Mapping

from ..contracts import ADAPTER_API_VERSION, AdapterManifest
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


@dataclass(frozen=True, slots=True)
class FakeOutcome:
    kind: str
    result: str | None = None
    question: str | None = None
    error_code: str = "FAKE_FAILURE"


@dataclass(slots=True)
class _FakeSession:
    context: ResolvedContext
    snapshot: AdapterSnapshot
    closed: bool = False


class FakeHarness:
    """State owned by the simulated external harness, not by the service."""

    def __init__(
        self,
        *,
        effective_model: str | None = None,
        effective_reasoning: Mapping[str, Any] | None = None,
        effective_workspace: str | None = None,
    ) -> None:
        self.effective_model = effective_model
        self.effective_reasoning = effective_reasoning
        self.effective_workspace = effective_workspace
        self._outcomes: deque[FakeOutcome] = deque()
        self._sessions: dict[str, _FakeSession] = {}
        self._calls: Counter[str] = Counter()

    def enqueue(
        self,
        kind: str,
        *,
        result: str | None = None,
        question: str | None = None,
        error_code: str = "FAKE_FAILURE",
    ) -> None:
        if kind not in {"done", "needs_input", "failure", "running", "cancelled"}:
            raise ValueError(f"unsupported fake outcome {kind!r}")
        self._outcomes.append(FakeOutcome(kind, result, question, error_code))

    def call_count(self, operation: str) -> int:
        return self._calls[operation]

    def has_session(self, external_session_id: str | None) -> bool:
        return isinstance(external_session_id, str) and external_session_id in self._sessions

    def _next_outcome(self) -> FakeOutcome:
        if self._outcomes:
            return self._outcomes.popleft()
        return FakeOutcome("done", result="fake completed")


class FakeAdapter:
    def __init__(self, harness: FakeHarness | None = None) -> None:
        self._harness = FakeHarness() if harness is None else harness
        self._manifest = AdapterManifest(
            adapter_api_version=ADAPTER_API_VERSION,
            runtime_id="fake",
            provider_id="fake-provider",
            harness_id="fake-native-harness",
            display_name="Fake sub-agent",
            adapter_version="1.0.0",
            supported_platforms=("win32", "darwin", "linux"),
            supported_transports=("managed-sdk",),
            capabilities=frozenset(
                {"session", "resume", "interrupt", "needs_input", "workspace"}
            ),
            semantic_permissions=frozenset(
                {
                    "repo_read",
                    "git_read",
                    "run_tests",
                    "workspace_write",
                    "network",
                    "nested_agents",
                    "browser",
                    "declared_mcp",
                }
            ),
            reasoning_schema={"type": "object", "additionalProperties": True},
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def probe(self) -> ProbeResult:
        self._harness._calls["probe"] += 1
        return ProbeResult("ready", {"mode": "deterministic"})

    async def resolve_context(self, request: AdapterContextRequest) -> ResolvedContext:
        self._harness._calls["resolve_context"] += 1
        model = self._harness.effective_model or request.model
        reasoning = (
            dict(self._harness.effective_reasoning)
            if self._harness.effective_reasoning is not None
            else dict(request.reasoning)
        )
        workspace_path = self._harness.effective_workspace or request.workspace_path
        workspace_key = (
            self._harness.effective_workspace or request.workspace_key
            if self._harness.effective_workspace is not None
            else request.workspace_key
        )
        context_payload = {
            "runtime_id": request.runtime_id,
            "variant_id": request.variant_id,
            "model": model,
            "reasoning": reasoning,
            "workspace_path": workspace_path,
            "workspace_key": workspace_key,
            "transport": request.transport,
            "permissions": list(request.permissions),
            "context_policy_id": request.context_policy_id,
            "permission_policy_id": request.permission_policy_id,
            "write_set": list(request.write_set),
        }
        context_hash = hashlib.sha256(
            json.dumps(
                context_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return ResolvedContext(
            runtime_id=request.runtime_id,
            requested_model=request.model,
            effective_model=model,
            requested_reasoning=dict(request.reasoning),
            effective_reasoning=reasoning,
            workspace_path=workspace_path,
            workspace_key=workspace_key,
            transport=request.transport,
            context_hash=context_hash,
            attestation={
                "source": "deterministic-fake",
                "write_set": list(request.write_set),
            },
        )

    async def spawn(self, request: AdapterSpawnRequest) -> AdapterSnapshot:
        self._harness._calls["spawn"] += 1
        session_id = f"fake-session-{request.conversation_id}"
        if session_id in self._harness._sessions:
            raise RuntimeError("fake session already exists")
        snapshot = _snapshot_for_outcome(
            request.context,
            external_session_id=session_id,
            external_execution_id=f"fake-execution-{request.execution_id}",
            outcome=self._harness._next_outcome(),
        )
        self._harness._sessions[session_id] = _FakeSession(request.context, snapshot)
        return snapshot

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        self._harness._calls["send"] += 1
        session = self._session(request.external_session_id)
        if session.closed:
            raise RuntimeError("fake session is closed")
        if request.context.context_hash != session.context.context_hash:
            raise RuntimeError("fake context drift")
        snapshot = _snapshot_for_outcome(
            session.context,
            external_session_id=request.external_session_id,
            external_execution_id=f"fake-execution-{request.execution_id}",
            outcome=self._harness._next_outcome(),
        )
        session.snapshot = snapshot
        return snapshot

    async def snapshot(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        self._harness._calls["snapshot"] += 1
        return self._session(request.external_session_id).snapshot

    async def interrupt(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        self._harness._calls["interrupt"] += 1
        session = self._session(request.external_session_id)
        current = session.snapshot
        if current.execution_state in {"running", "needs_input"}:
            session.snapshot = AdapterSnapshot(
                external_session_id=current.external_session_id,
                external_execution_id=current.external_execution_id,
                conversation_state="idle",
                execution_state="interrupted",
                effective_model=current.effective_model,
                effective_reasoning=current.effective_reasoning,
                workspace_path=current.workspace_path,
                workspace_key=current.workspace_key,
                context_hash=current.context_hash,
                error=AdapterFailure(
                    code="INTERRUPTED",
                    category="cancelled",
                    retryable=False,
                    message="fake execution interrupted",
                ),
                evidence={"source": "deterministic-fake"},
            )
        return session.snapshot

    async def close(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        self._harness._calls["close"] += 1
        session = self._session(request.external_session_id)
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
            needs_input=current.needs_input,
            error=current.error,
            evidence=current.evidence,
        )
        return session.snapshot

    async def open_session(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        self._harness._calls["open_session"] += 1
        session = self._session(request.external_session_id)
        if (
            request.external_execution_id is not None
            and session.snapshot.external_execution_id != request.external_execution_id
        ):
            raise RuntimeError("fake external execution identity mismatch")
        return session.snapshot

    def _session(self, external_session_id: str) -> _FakeSession:
        try:
            return self._harness._sessions[external_session_id]
        except KeyError as exc:
            raise RuntimeError("fake session not found") from exc


def _snapshot_for_outcome(
    context: ResolvedContext,
    *,
    external_session_id: str,
    external_execution_id: str,
    outcome: FakeOutcome,
) -> AdapterSnapshot:
    common = {
        "external_session_id": external_session_id,
        "external_execution_id": external_execution_id,
        "effective_model": context.effective_model,
        "effective_reasoning": context.effective_reasoning,
        "workspace_path": context.workspace_path,
        "workspace_key": context.workspace_key,
        "context_hash": context.context_hash,
        "evidence": {"source": "deterministic-fake"},
    }
    if outcome.kind == "done":
        return AdapterSnapshot(
            **common,
            conversation_state="idle",
            execution_state="succeeded",
            result_text=outcome.result or "fake completed",
        )
    if outcome.kind == "needs_input":
        return AdapterSnapshot(
            **common,
            conversation_state="needs_input",
            execution_state="needs_input",
            needs_input=(
                {
                    "id": "question-1",
                    "prompt": outcome.question or "Input required",
                },
            ),
        )
    if outcome.kind == "failure":
        return AdapterSnapshot(
            **common,
            conversation_state="idle",
            execution_state="failed",
            error=AdapterFailure(
                code=outcome.error_code,
                category="provider",
                retryable=False,
                message="deterministic fake failure",
            ),
        )
    if outcome.kind == "cancelled":
        return AdapterSnapshot(
            **common,
            conversation_state="idle",
            execution_state="cancelled",
            error=AdapterFailure(
                code="CANCELLED",
                category="cancelled",
                retryable=False,
                message="deterministic fake cancellation",
            ),
        )
    return AdapterSnapshot(
        **common,
        conversation_state="active",
        execution_state="running",
    )
