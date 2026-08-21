from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Transport

from .contracts import AdapterPair, Capability


EXPECTED_TRANSPORT_METHODS = {
    "connect",
    "write",
    "read_messages",
    "close",
    "is_ready",
    "end_input",
}

_EVIDENCE_SOURCES = frozenset({"static_public_api", "live_canary"})


@dataclass(frozen=True)
class JobTransportCapability:
    pair: AdapterPair
    status: Capability
    evidence_source: str

    def validate(self) -> None:
        if not isinstance(self.pair, AdapterPair):
            raise ValueError("invalid adapter pair")
        self.pair.validate()
        if not isinstance(self.status, Capability):
            raise ValueError("invalid Job Object transport status")
        if self.evidence_source not in _EVIDENCE_SOURCES:
            raise ValueError("invalid sanitized transport evidence source")
        if (
            self.status is Capability.PASS
            and self.evidence_source != "live_canary"
        ):
            raise ValueError("Job Object transport PASS requires a live canary")


def _public_transport_contract_is_exact() -> bool:
    return set(getattr(Transport, "__abstractmethods__", ())) == (
        EXPECTED_TRANSPORT_METHODS
    )


def classify_job_transport(public_process_factory: object | None) -> str:
    if not _public_transport_contract_is_exact() or public_process_factory is None:
        return Capability.CAPABILITY_MISSING.value
    return "needs_live_canary"


def static_transport_summary(
    public_process_factory: object | None,
) -> dict[str, str]:
    return {
        "default_transport": "requires_live_canary",
        "public_transport_contract": (
            "exact" if _public_transport_contract_is_exact() else "mismatch"
        ),
        "windows_job_transport": classify_job_transport(public_process_factory),
    }


def _select_transport(
    pair: AdapterPair,
    *,
    job_transport: Transport | None,
    job_capability: JobTransportCapability | None,
) -> Transport | None:
    pair.validate()
    if job_capability is None:
        return None
    job_capability.validate()
    if (
        job_transport is not None
        and job_capability.status is Capability.PASS
        and job_capability.pair == pair
    ):
        return job_transport
    return None


def build_managed_client(
    options: ClaudeAgentOptions,
    pair: AdapterPair,
    *,
    job_transport: Transport | None = None,
    job_capability: JobTransportCapability | None = None,
    client_factory: Callable[..., Any] = ClaudeSDKClient,
) -> Any:
    transport = _select_transport(
        pair,
        job_transport=job_transport,
        job_capability=job_capability,
    )
    return client_factory(options=options, transport=transport)
