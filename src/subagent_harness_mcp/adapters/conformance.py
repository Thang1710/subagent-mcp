"""Public, deterministic lifecycle checks for third-party adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import AdapterManifest, TaskPacket
from .base import (
    Adapter,
    AdapterContextRequest,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSnapshot,
    AdapterSpawnRequest,
    ProbeResult,
    ResolvedContext,
)


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    runtime_id: str
    adapter_version: str
    operations: tuple[str, ...]
    final_conversation_state: str


async def run_adapter_conformance(
    factory: Callable[[], Adapter],
    *,
    workspace_path: str,
    model: str,
    reasoning: Mapping[str, Any],
    transport: str,
) -> ConformanceReport:
    """Exercise one deterministic adapter lifecycle through public types only."""

    adapter = factory()
    manifest = adapter.manifest
    if not isinstance(manifest, AdapterManifest):
        raise TypeError("manifest must be an AdapterManifest")
    if transport not in manifest.supported_transports:
        raise ValueError(f"transport {transport!r} is not declared by the adapter")

    operations: list[str] = []
    probe = await adapter.probe()
    operations.append("probe")
    if not isinstance(probe, ProbeResult) or not probe.state:
        raise TypeError("probe must return a ProbeResult with a state")

    context = await adapter.resolve_context(
        AdapterContextRequest(
            runtime_id=manifest.runtime_id,
            variant_id="conformance",
            model=model,
            reasoning=dict(reasoning),
            workspace_path=workspace_path,
            workspace_key=workspace_path,
            transport=transport,
            permissions=(),
            context_policy_id="declared-native",
            permission_policy_id="conformance",
        )
    )
    operations.append("resolve_context")
    _require_context(context, manifest, workspace_path, model, reasoning, transport)

    spawned = await adapter.spawn(
        AdapterSpawnRequest(
            conversation_id="conformance-conversation",
            execution_id="conformance-execution-1",
            task=TaskPacket(
                title="Adapter conformance",
                prompt="Return one normalized deterministic result.",
                acceptance_criteria=("Preserve the declared context.",),
                role="sub-agent",
            ),
            context=context,
        )
    )
    operations.append("spawn")
    _require_snapshot(
        spawned,
        context,
        expected_execution_id="conformance-execution-1",
    )
    session = AdapterSessionRequest(
        conversation_id="conformance-conversation",
        execution_id="conformance-execution-1",
        external_session_id=spawned.external_session_id,
        external_execution_id=spawned.external_execution_id,
    )
    closed = False
    try:
        reopened = await adapter.open_session(session)
        operations.append("open_session")
        _require_snapshot(
            reopened,
            context,
            spawned.external_session_id,
            "conformance-execution-1",
        )

        current = await adapter.snapshot(session)
        operations.append("snapshot")
        _require_snapshot(
            current,
            context,
            spawned.external_session_id,
            "conformance-execution-1",
        )

        sent = await adapter.send(
            AdapterSendRequest(
                conversation_id=session.conversation_id,
                execution_id="conformance-execution-2",
                external_session_id=session.external_session_id,
                prompt="Continue the same native session.",
                reply_to=None,
                answers={},
                context=context,
            )
        )
        operations.append("send")
        _require_snapshot(
            sent,
            context,
            spawned.external_session_id,
            "conformance-execution-2",
        )
        session = AdapterSessionRequest(
            conversation_id=session.conversation_id,
            execution_id="conformance-execution-2",
            external_session_id=session.external_session_id,
            external_execution_id=sent.external_execution_id,
        )

        interrupted = await adapter.interrupt(session)
        operations.append("interrupt")
        _require_snapshot(
            interrupted,
            context,
            spawned.external_session_id,
            "conformance-execution-2",
        )

        final = await adapter.close(session)
        operations.append("close")
        closed = True
        _require_snapshot(
            final,
            context,
            spawned.external_session_id,
            "conformance-execution-2",
        )
        if final.conversation_state != "closed":
            raise ValueError("close must return conversation_state='closed'")
    finally:
        if not closed:
            await adapter.close(session)

    return ConformanceReport(
        runtime_id=manifest.runtime_id,
        adapter_version=manifest.adapter_version,
        operations=tuple(operations),
        final_conversation_state=final.conversation_state,
    )


def _require_context(
    context: object,
    manifest: AdapterManifest,
    workspace_path: str,
    model: str,
    reasoning: Mapping[str, Any],
    transport: str,
) -> None:
    if not isinstance(context, ResolvedContext):
        raise TypeError("resolve_context must return ResolvedContext")
    expected = (
        manifest.runtime_id,
        model,
        dict(reasoning),
        workspace_path,
        workspace_path,
        transport,
    )
    actual = (
        context.runtime_id,
        context.requested_model,
        dict(context.requested_reasoning),
        context.workspace_path,
        context.workspace_key,
        context.transport,
    )
    if actual != expected or not context.context_hash:
        raise ValueError("resolve_context did not preserve the requested context")


def _require_snapshot(
    snapshot: object,
    context: ResolvedContext,
    external_session_id: str | None = None,
    expected_execution_id: str | None = None,
) -> None:
    if not isinstance(snapshot, AdapterSnapshot):
        raise TypeError("adapter operation must return AdapterSnapshot")
    if not snapshot.external_session_id or not snapshot.external_execution_id:
        raise ValueError("adapter snapshot identities must be non-empty")
    if external_session_id is not None and snapshot.external_session_id != external_session_id:
        raise ValueError("adapter changed external_session_id within one conversation")
    if (
        expected_execution_id is not None
        and snapshot.external_execution_id != expected_execution_id
    ):
        raise ValueError("adapter changed the normalized controller execution identity")
    expected = (
        context.effective_model,
        dict(context.effective_reasoning),
        context.workspace_path,
        context.workspace_key,
        context.context_hash,
    )
    actual = (
        snapshot.effective_model,
        dict(snapshot.effective_reasoning),
        snapshot.workspace_path,
        snapshot.workspace_key,
        snapshot.context_hash,
    )
    if actual != expected:
        raise ValueError("adapter snapshot drifted from the resolved context")
