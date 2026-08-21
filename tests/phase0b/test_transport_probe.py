from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, Transport
import pytest

from spikes.phase0b.contracts import AdapterPair, Capability
from spikes.phase0b.transport_probe import (
    EXPECTED_TRANSPORT_METHODS,
    JobTransportCapability,
    build_managed_client,
    classify_job_transport,
    static_transport_summary,
)


def _pair(*, cli_version: str = "provider-cli-current") -> AdapterPair:
    return AdapterPair(
        sdk_version="0.2.142",
        cli_version=cli_version,
        cli_sha256="a" * 64,
    )


class RecordingClientFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return kwargs


def test_reviewed_public_transport_abstract_method_set_is_exact() -> None:
    assert set(Transport.__abstractmethods__) == EXPECTED_TRANSPORT_METHODS


def test_static_probe_reports_only_sanitized_non_pass_states() -> None:
    summary = static_transport_summary(public_process_factory=None)

    assert summary == {
        "default_transport": "requires_live_canary",
        "public_transport_contract": "exact",
        "windows_job_transport": "capability_missing",
    }
    assert "pass" not in summary.values()


def test_public_factory_would_still_require_a_live_canary() -> None:
    assert classify_job_transport(object()) == "needs_live_canary"


def test_default_client_explicitly_receives_transport_none() -> None:
    factory = RecordingClientFactory()
    options = ClaudeAgentOptions()

    client = build_managed_client(
        options,
        _pair(),
        client_factory=factory,
    )

    assert client == {"options": options, "transport": None}
    assert factory.calls == [{"options": options, "transport": None}]


@pytest.mark.parametrize(
    "status",
    [Capability.CAPABILITY_MISSING, Capability.BLOCKED],
)
def test_non_pass_job_capability_keeps_the_default_transport(status: Capability) -> None:
    factory = RecordingClientFactory()
    candidate = object()
    capability = JobTransportCapability(
        pair=_pair(),
        status=status,
        evidence_source="static_public_api",
    )

    client = build_managed_client(
        ClaudeAgentOptions(),
        _pair(),
        job_transport=candidate,
        job_capability=capability,
        client_factory=factory,
    )

    assert client["transport"] is None


def test_static_evidence_cannot_claim_job_transport_pass() -> None:
    capability = JobTransportCapability(
        pair=_pair(),
        status=Capability.PASS,
        evidence_source="static_public_api",
    )

    with pytest.raises(ValueError, match="live canary"):
        build_managed_client(
            ClaudeAgentOptions(),
            _pair(),
            job_transport=object(),
            job_capability=capability,
            client_factory=RecordingClientFactory(),
        )


def test_exact_pair_live_pass_selects_the_optional_job_transport() -> None:
    pair = _pair()
    candidate = object()
    capability = JobTransportCapability(
        pair=pair,
        status=Capability.PASS,
        evidence_source="live_canary",
    )

    client = build_managed_client(
        ClaudeAgentOptions(),
        pair,
        job_transport=candidate,
        job_capability=capability,
        client_factory=RecordingClientFactory(),
    )

    assert client["transport"] is candidate


def test_live_pass_for_another_pair_keeps_the_default_transport() -> None:
    requested_pair = _pair(cli_version="provider-cli-current")
    other_pair = _pair(cli_version="provider-cli-other")
    capability = JobTransportCapability(
        pair=other_pair,
        status=Capability.PASS,
        evidence_source="live_canary",
    )

    client = build_managed_client(
        ClaudeAgentOptions(),
        requested_pair,
        job_transport=object(),
        job_capability=capability,
        client_factory=RecordingClientFactory(),
    )

    assert client["transport"] is None
