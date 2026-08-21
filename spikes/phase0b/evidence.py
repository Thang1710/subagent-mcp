from __future__ import annotations

from collections.abc import Mapping

from .contracts import (
    BACKGROUND_READY_GATES,
    Capability,
    MANAGED_MVP_GATES,
    PHASE0B_GATES,
)


def adjudicate_phase0b(gates: Mapping[str, str]) -> dict[str, object]:
    if not isinstance(gates, Mapping) or set(gates) != set(PHASE0B_GATES):
        raise ValueError("Phase 0b gate set mismatch")

    normalized: dict[str, Capability] = {}
    for name in PHASE0B_GATES:
        try:
            normalized[name] = Capability(gates[name])
        except (TypeError, ValueError):
            raise ValueError(f"invalid Phase 0b gate status: {name}") from None

    managed_ready = all(
        normalized[name] is Capability.PASS for name in MANAGED_MVP_GATES
    )
    background_ready = managed_ready and all(
        normalized[name] is Capability.PASS for name in BACKGROUND_READY_GATES
    )
    phase_1a_ready = all(
        normalized[name] is Capability.PASS for name in PHASE0B_GATES
    )
    return {
        "managed_mvp_ready": managed_ready,
        "visible_background_ready": background_ready,
        "phase_0b_may_begin_phase_1a": phase_1a_ready,
        "gates": {name: normalized[name].value for name in PHASE0B_GATES},
    }
