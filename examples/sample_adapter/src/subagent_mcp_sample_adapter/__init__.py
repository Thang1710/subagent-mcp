"""Minimal adapter implemented only with Subagent MCP's public API."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from subagent_harness_mcp.adapters import (
    AdapterContextRequest,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSnapshot,
    AdapterSpawnRequest,
    ProbeResult,
    ResolvedContext,
)
from subagent_harness_mcp.contracts import ADAPTER_API_VERSION, AdapterManifest


class SampleEchoAdapter:
    def __init__(self) -> None:
        self._manifest = AdapterManifest(
            adapter_api_version=ADAPTER_API_VERSION,
            runtime_id="sample-echo",
            provider_id="sample-provider",
            harness_id="sample-native-harness",
            display_name="Sample echo sub-agent",
            adapter_version="0.1.0",
            supported_platforms=("win32", "darwin", "linux"),
            supported_transports=("managed-sdk",),
            capabilities=frozenset({"session", "resume", "interrupt", "workspace"}),
            semantic_permissions=frozenset({"repo_read"}),
            reasoning_schema={"type": "object", "additionalProperties": True},
            model_schema={"type": "string", "minLength": 1},
        )
        self._sessions: dict[str, AdapterSnapshot] = {}

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def probe(self) -> ProbeResult:
        return ProbeResult("ready", {"mode": "deterministic-sample"})

    async def resolve_context(self, request: AdapterContextRequest) -> ResolvedContext:
        payload = {
            "runtime_id": request.runtime_id,
            "model": request.model,
            "reasoning": dict(request.reasoning),
            "workspace_path": request.workspace_path,
            "workspace_key": request.workspace_key,
            "transport": request.transport,
        }
        context_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ResolvedContext(
            runtime_id=request.runtime_id,
            requested_model=request.model,
            effective_model=request.model,
            requested_reasoning=dict(request.reasoning),
            effective_reasoning=dict(request.reasoning),
            workspace_path=request.workspace_path,
            workspace_key=request.workspace_key,
            transport=request.transport,
            context_hash=context_hash,
            attestation={"source": "sample-echo"},
        )

    async def spawn(self, request: AdapterSpawnRequest) -> AdapterSnapshot:
        session_id = f"sample-session-{request.conversation_id}"
        snapshot = _completed_snapshot(
            request.context,
            session_id,
            f"sample-execution-{request.execution_id}",
            "sample adapter completed",
        )
        self._sessions[session_id] = snapshot
        return snapshot

    async def send(self, request: AdapterSendRequest) -> AdapterSnapshot:
        self._session(request.external_session_id)
        snapshot = _completed_snapshot(
            request.context,
            request.external_session_id,
            f"sample-execution-{request.execution_id}",
            f"echo: {request.prompt}",
        )
        self._sessions[request.external_session_id] = snapshot
        return snapshot

    async def snapshot(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        return self._session(request.external_session_id)

    async def interrupt(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        return self._session(request.external_session_id)

    async def close(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        snapshot = replace(
            self._session(request.external_session_id),
            conversation_state="closed",
        )
        self._sessions[request.external_session_id] = snapshot
        return snapshot

    async def open_session(self, request: AdapterSessionRequest) -> AdapterSnapshot:
        snapshot = self._session(request.external_session_id)
        if (
            request.external_execution_id is not None
            and request.external_execution_id != snapshot.external_execution_id
        ):
            raise RuntimeError("external execution identity mismatch")
        return snapshot

    def _session(self, external_session_id: str) -> AdapterSnapshot:
        try:
            return self._sessions[external_session_id]
        except KeyError as exc:
            raise RuntimeError("sample session not found") from exc


def _completed_snapshot(
    context: ResolvedContext,
    external_session_id: str,
    external_execution_id: str,
    result_text: str,
) -> AdapterSnapshot:
    return AdapterSnapshot(
        external_session_id=external_session_id,
        external_execution_id=external_execution_id,
        conversation_state="idle",
        execution_state="succeeded",
        effective_model=context.effective_model,
        effective_reasoning=context.effective_reasoning,
        workspace_path=context.workspace_path,
        workspace_key=context.workspace_key,
        context_hash=context.context_hash,
        result_text=result_text,
        evidence={"source": "sample-echo"},
    )


def create_adapter() -> SampleEchoAdapter:
    return SampleEchoAdapter()


__all__ = ["SampleEchoAdapter", "create_adapter"]
