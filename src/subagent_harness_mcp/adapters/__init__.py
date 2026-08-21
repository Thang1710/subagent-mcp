"""Public adapter API for Subagent MCP."""

from .base import (
    Adapter,
    AdapterContextRequest,
    AdapterFailure,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSnapshot,
    AdapterSpawnRequest,
    CanaryAdapter,
    CanaryRequest,
    CanaryResult,
    ProbeResult,
    ResolvedContext,
)

__all__ = [
    "Adapter",
    "AdapterContextRequest",
    "AdapterFailure",
    "AdapterSendRequest",
    "AdapterSessionRequest",
    "AdapterSnapshot",
    "AdapterSpawnRequest",
    "CanaryAdapter",
    "CanaryRequest",
    "CanaryResult",
    "ProbeResult",
    "ResolvedContext",
]
