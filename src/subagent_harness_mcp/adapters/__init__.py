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
from .conformance import ConformanceReport, run_adapter_conformance

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
    "ConformanceReport",
    "ProbeResult",
    "ResolvedContext",
    "run_adapter_conformance",
]
