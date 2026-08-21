from __future__ import annotations

from dataclasses import fields, replace

import pytest

from spikes.phase0b.contracts import (
    AdapterPair,
    BACKGROUND_READY_GATES,
    Capability,
    MANAGED_MVP_GATES,
    ManagedObservation,
    PHASE0B_GATES,
)


def _pair() -> AdapterPair:
    return AdapterPair(
        sdk_version="0.2.142",
        cli_version="2.1.224 (Claude Code)",
        cli_sha256="a" * 64,
    )


def _observation() -> ManagedObservation:
    return ManagedObservation(
        pair=_pair(),
        status=Capability.PASS,
        session_present=True,
        context_equal=True,
        model_equal=True,
        cleanup_confirmed=True,
        result_terminal=True,
    )


def test_gate_sets_are_exact_and_capability_values_are_bounded() -> None:
    assert PHASE0B_GATES == (
        "managed_sdk_context",
        "managed_sdk_cleanup",
        "managed_needs_input",
        "managed_resume",
        "managed_interrupt",
        "visible_managed_promotion",
        "nested_agent_policy",
        "circuit_transitions",
        "restart_rollback",
        "default_transport",
    )
    assert MANAGED_MVP_GATES == tuple(
        gate for gate in PHASE0B_GATES if gate != "visible_managed_promotion"
    )
    assert BACKGROUND_READY_GATES == MANAGED_MVP_GATES + (
        "visible_managed_promotion",
    )
    assert Capability("pass") is Capability.PASS
    assert Capability("blocked") is Capability.BLOCKED
    assert Capability("capability_missing") is Capability.CAPABILITY_MISSING


def test_adapter_pair_accepts_only_the_reviewed_sdk_and_bound_cli() -> None:
    assert _pair().validate() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sdk_version", "0.2.143"),
        ("sdk_version", ""),
        ("cli_version", ""),
        ("cli_version", "   "),
        ("cli_version", None),
        ("cli_sha256", "a" * 63),
        ("cli_sha256", "A" * 64),
        ("cli_sha256", "g" * 64),
        ("cli_sha256", None),
    ],
)
def test_adapter_pair_rejects_unbound_or_malformed_values(
    field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(_pair(), **{field: value}).validate()


def test_managed_observation_is_a_minimal_sanitized_aggregate() -> None:
    observation = _observation()

    assert observation.validate() is None
    assert {field.name for field in fields(ManagedObservation)} == {
        "pair",
        "status",
        "session_present",
        "context_equal",
        "model_equal",
        "cleanup_confirmed",
        "result_terminal",
        "error_category",
    }
    field_names = {field.name for field in fields(ManagedObservation)}
    assert field_names.isdisjoint(
        {
            "session_id",
            "raw_output",
            "prompt",
            "result_text",
            "cost_usd",
            "input_tokens",
            "output_tokens",
        }
    )


def test_managed_observation_accepts_a_bounded_future_error_category() -> None:
    observation = replace(
        _observation(),
        status=Capability.CAPABILITY_MISSING,
        error_category="future_provider_error",
    )

    assert observation.validate() is None


@pytest.mark.parametrize(
    "field",
    [
        "session_present",
        "context_equal",
        "model_equal",
        "cleanup_confirmed",
        "result_terminal",
    ],
)
def test_pass_observation_requires_every_confirmation(field: str) -> None:
    with pytest.raises(ValueError, match="PASS"):
        replace(_observation(), **{field: False}).validate()


def test_pass_observation_rejects_an_error_category() -> None:
    with pytest.raises(ValueError, match="PASS"):
        replace(_observation(), error_category="provider_error").validate()


@pytest.mark.parametrize(
    "status", [Capability.BLOCKED, Capability.CAPABILITY_MISSING],
)
def test_nonpass_observation_retains_truthful_failures(
    status: Capability,
) -> None:
    observation = replace(
        _observation(),
        status=status,
        session_present=False,
        context_equal=False,
        model_equal=False,
        cleanup_confirmed=False,
        result_terminal=False,
        error_category="provider_error",
    )

    assert observation.validate() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pair", object()),
        ("status", "pass"),
        ("session_present", 1),
        ("context_equal", None),
        ("model_equal", "true"),
        ("cleanup_confirmed", 0),
        ("result_terminal", object()),
        ("error_category", ""),
        ("error_category", "provider returned raw text"),
        ("error_category", "x" * 65),
    ],
)
def test_managed_observation_rejects_unvalidated_or_raw_values(
    field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(_observation(), **{field: value}).validate()


def test_managed_observation_validates_the_nested_adapter_pair() -> None:
    observation = replace(
        _observation(), pair=replace(_pair(), sdk_version="0.2.143"),
    )

    with pytest.raises(ValueError, match="SDK"):
        observation.validate()
