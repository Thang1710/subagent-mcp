"""Public adapter API for Subagent MCP."""

from .base import (
    Adapter,
    AdapterContextRequest,
    AdapterFailure,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSnapshot,
    AdapterSpawnRequest,
    AuthenticationAdapter,
    CanaryAdapter,
    CanaryRequest,
    CanaryResult,
    ModelCatalogAdapter,
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
    "AuthenticationAdapter",
    "CanaryAdapter",
    "CanaryRequest",
    "CanaryResult",
    "ConformanceReport",
    "ModelCatalogAdapter",
    "ProbeResult",
    "ResolvedContext",
    "run_adapter_conformance",
]
