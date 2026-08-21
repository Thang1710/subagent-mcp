from __future__ import annotations

import json

import pytest

from spikes.phase0b.contracts import MANAGED_MVP_GATES, PHASE0B_GATES
from spikes.phase0b.evidence import adjudicate_phase0b


def _passing_gates() -> dict[str, str]:
    return {gate: "pass" for gate in PHASE0B_GATES}


def test_all_exact_gates_make_every_readiness_level_true() -> None:
    decision = adjudicate_phase0b(_passing_gates())

    assert decision == {
        "managed_mvp_ready": True,
        "visible_background_ready": True,
        "phase_0b_may_begin_phase_1a": True,
        "gates": _passing_gates(),
    }


@pytest.mark.parametrize("status", ["blocked", "capability_missing"])
def test_promotion_gap_does_not_falsely_block_managed_mvp(status: str) -> None:
    gates = _passing_gates()
    gates["visible_managed_promotion"] = status

    decision = adjudicate_phase0b(gates)

    assert decision["managed_mvp_ready"] is True
    assert decision["visible_background_ready"] is False
    assert decision["phase_0b_may_begin_phase_1a"] is False


@pytest.mark.parametrize("gate", MANAGED_MVP_GATES)
@pytest.mark.parametrize("status", ["blocked", "capability_missing"])
def test_any_managed_gate_blocks_managed_and_background_readiness(
    gate: str, status: str,
) -> None:
    gates = _passing_gates()
    gates[gate] = status

    decision = adjudicate_phase0b(gates)

    assert decision["managed_mvp_ready"] is False
    assert decision["visible_background_ready"] is False
    assert decision["phase_0b_may_begin_phase_1a"] is False


@pytest.mark.parametrize("gate", PHASE0B_GATES)
@pytest.mark.parametrize("status", ["blocked", "capability_missing"])
def test_full_phase1a_decision_remains_strict_for_every_gate(
    gate: str, status: str,
) -> None:
    gates = _passing_gates()
    gates[gate] = status

    assert adjudicate_phase0b(gates)["phase_0b_may_begin_phase_1a"] is False


@pytest.mark.parametrize("unknown", ["PASS", "unknown", "ready", "", None, True])
def test_unknown_gate_status_fails_closed(unknown: object) -> None:
    gates = _passing_gates()
    gates["managed_sdk_context"] = unknown

    with pytest.raises(ValueError, match="status"):
        adjudicate_phase0b(gates)


def test_missing_and_extra_gate_names_fail_closed() -> None:
    missing = _passing_gates()
    missing.pop("managed_sdk_context")
    extra = _passing_gates()
    extra["deterministic_build_permission"] = "pass"

    with pytest.raises(ValueError, match="gate set mismatch"):
        adjudicate_phase0b(missing)
    with pytest.raises(ValueError, match="gate set mismatch"):
        adjudicate_phase0b(extra)


def test_decision_contains_only_sanitized_aggregates_not_live_evidence() -> None:
    serialized = json.dumps(adjudicate_phase0b(_passing_gates()), sort_keys=True)

    for forbidden in (
        "session_id",
        "native_session",
        "raw_output",
        "prompt",
        "transcript",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "deterministic_build_permission",
    ):
        assert forbidden not in serialized
