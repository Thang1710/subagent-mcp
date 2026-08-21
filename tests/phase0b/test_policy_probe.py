from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from spikes.phase0b.contracts import Capability
from spikes.phase0b.policy_probe import (
    CircuitLatch,
    CircuitState,
    NestedPolicy,
    canonical_circuit_cases_digest,
    canonical_json_bytes,
    normalize_circuit,
    validate_circuit_fixture,
)


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "phase0b"
    / "current"
    / "circuit-cases.json"
)


def _run(awaitable):
    return asyncio.run(awaitable)


def test_fifth_concurrent_agent_is_denied_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    async def exercise() -> tuple[list[dict[str, str]], dict[str, object]]:
        policy = NestedPolicy(workspace)
        decisions = await asyncio.gather(
            *(
                policy.before_agent("conversation", "execution", None, workspace)
                for _ in range(5)
            )
        )
        return decisions, await policy.status_snapshot("conversation", "execution")

    decisions, snapshot = _run(exercise())

    assert [item["permissionDecision"] for item in decisions].count("allow") == 4
    assert [item["permissionDecision"] for item in decisions].count("deny") == 1
    assert next(
        item for item in decisions if item["permissionDecision"] == "deny"
    ) == {
        "permissionDecision": "deny",
        "reasonCategory": "limit",
    }
    assert snapshot == {
        "total": 4,
        "active": 4,
        "denials": {"limit": 1},
    }


def test_total_limit_remains_after_agents_stop(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    async def exercise() -> tuple[dict[str, str], dict[str, object]]:
        policy = NestedPolicy(workspace)
        for _ in range(4):
            assert (
                await policy.before_agent(
                    "conversation", "execution", None, workspace,
                )
            )["permissionDecision"] == "allow"
        for _ in range(4):
            await policy.after_agent("conversation", "execution")
        decision = await policy.before_agent(
            "conversation", "execution", None, workspace,
        )
        return decision, await policy.status_snapshot("conversation", "execution")

    decision, snapshot = _run(exercise())

    assert decision["reasonCategory"] == "limit"
    assert snapshot == {
        "total": 4,
        "active": 0,
        "denials": {"limit": 1},
    }


def test_non_null_agent_id_denies_depth_two_without_consuming_a_slot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    async def exercise() -> tuple[dict[str, str], dict[str, object]]:
        policy = NestedPolicy(workspace)
        decision = await policy.before_agent(
            "conversation", "execution", "parent-agent", workspace,
        )
        return decision, await policy.status_snapshot("conversation", "execution")

    decision, snapshot = _run(exercise())

    assert decision == {
        "permissionDecision": "deny",
        "reasonCategory": "depth",
    }
    assert snapshot == {
        "total": 0,
        "active": 0,
        "denials": {"depth": 1},
    }


def test_nested_counts_are_isolated_per_execution(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        policy = NestedPolicy(workspace)
        for _ in range(4):
            await policy.before_agent("conversation", "execution-1", None, workspace)
        await policy.before_agent("conversation", "execution-2", None, workspace)
        return (
            await policy.status_snapshot("conversation", "execution-1"),
            await policy.status_snapshot("conversation", "execution-2"),
        )

    first, second = _run(exercise())

    assert (first["total"], first["active"]) == (4, 4)
    assert (second["total"], second["active"]) == (1, 1)


def test_cancellation_and_final_reconciliation_clear_leaked_counts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        policy = NestedPolicy(workspace)
        started = asyncio.Event()

        async def cancelled_agent() -> None:
            await policy.before_agent("conversation", "execution", None, workspace)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await policy.after_agent("conversation", "execution")

        task = asyncio.create_task(cancelled_agent())
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await policy.before_agent("conversation", "execution", None, workspace)
        reconciled = await policy.reconcile_execution("conversation", "execution")
        reset = await policy.status_snapshot("conversation", "execution")
        return reconciled, reset

    reconciled, reset = _run(exercise())

    assert reconciled == {"total": 2, "active": 0, "denials": {}}
    assert reset == {"total": 0, "active": 0, "denials": {}}


def test_workspace_escape_is_denied_for_agent_cwd_and_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    inside = workspace / "nested"
    outside = tmp_path / "escape"
    inside.mkdir(parents=True)
    outside.mkdir()

    async def exercise() -> tuple[dict[str, str], dict[str, object]]:
        policy = NestedPolicy(workspace)
        assert policy.authorize_write(inside / "new-file.txt") == {
            "permissionDecision": "allow",
            "reasonCategory": "allowed",
        }
        assert policy.authorize_write(Path("relative-new-file.txt"))[
            "permissionDecision"
        ] == "allow"
        assert policy.authorize_write(workspace / ".." / "escape" / "bad.txt") == {
            "permissionDecision": "deny",
            "reasonCategory": "workspace",
        }
        decision = await policy.before_agent(
            "conversation", "execution", None, outside,
        )
        return decision, await policy.status_snapshot("conversation", "execution")

    decision, snapshot = _run(exercise())

    assert decision["reasonCategory"] == "workspace"
    assert snapshot == {
        "total": 0,
        "active": 0,
        "denials": {"workspace": 1},
    }


def test_safe_nested_snapshot_contains_only_counts_and_categories(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    async def exercise() -> dict[str, object]:
        policy = NestedPolicy(workspace)
        await policy.before_agent("private-conversation", "private-execution", None, workspace)
        return await policy.status_snapshot("private-conversation", "private-execution")

    serialized = json.dumps(_run(exercise()), sort_keys=True)

    assert "private-conversation" not in serialized
    assert "private-execution" not in serialized
    assert str(workspace) not in serialized
    assert set(json.loads(serialized)) == {"active", "denials", "total"}


@pytest.mark.parametrize(
    ("category", "state", "capability"),
    [
        ("authentication_failed", CircuitState.AUTH_OPEN, None),
        ("oauth_org_not_allowed", CircuitState.AUTH_OPEN, None),
        ("rate_limit", CircuitState.QUOTA_OPEN, None),
        ("billing_error", CircuitState.QUOTA_OPEN, None),
        ("server_error", CircuitState.RETRYABLE, None),
        ("overloaded", CircuitState.RETRYABLE, None),
        (
            "model_not_found",
            CircuitState.MODEL_UNAVAILABLE,
            Capability.CAPABILITY_MISSING,
        ),
        ("future_provider_error", CircuitState.UNKNOWN_OPEN, None),
    ],
)
def test_terminal_error_categories_normalize_exactly(
    category: str,
    state: CircuitState,
    capability: Capability | None,
) -> None:
    decision = normalize_circuit(
        category,
        result_is_error=True,
        result_terminal=True,
        is_using_overage=False,
    )

    assert decision.state is state
    assert decision.capability is capability


@pytest.mark.parametrize("category", ["success", "rate_limit_event"])
def test_known_terminal_non_overage_success_closes_the_circuit(
    category: str,
) -> None:
    decision = normalize_circuit(
        category,
        result_is_error=False,
        result_terminal=True,
        is_using_overage=False,
    )

    assert decision.state is CircuitState.CLOSED


@pytest.mark.parametrize(
    ("category", "result_is_error", "result_terminal", "is_using_overage"),
    [
        ("future_provider_value", False, True, False),
        ("success", False, False, False),
        ("success", False, True, None),
        ("success", False, True, True),
        ("success", None, True, False),
        ("rate_limit", False, True, False),
        ("server_error", True, False, False),
    ],
)
def test_ambiguous_or_unknown_observations_never_become_success(
    category: str,
    result_is_error: bool | None,
    result_terminal: bool,
    is_using_overage: bool | None,
) -> None:
    decision = normalize_circuit(
        category,
        result_is_error=result_is_error,
        result_terminal=result_terminal,
        is_using_overage=is_using_overage,
    )

    assert decision.state is CircuitState.UNKNOWN_OPEN


def test_circuit_state_survives_a_sanitized_restart_round_trip() -> None:
    latch = CircuitLatch()
    applied = latch.apply(
        "model_not_found",
        result_is_error=True,
        result_terminal=True,
        is_using_overage=False,
    )
    serialized = json.loads(json.dumps(latch.safe_snapshot(), sort_keys=True))
    restored = CircuitLatch.from_snapshot(serialized)

    assert applied.state is CircuitState.MODEL_UNAVAILABLE
    assert serialized == {
        "capability": "capability_missing",
        "state": "model_unavailable",
    }
    assert restored.safe_snapshot() == serialized


def test_quota_latch_ignores_late_success_until_retry_and_approved_canary() -> None:
    latch = CircuitLatch()
    opened = latch.apply(
        "rate_limit",
        result_is_error=True,
        result_terminal=True,
        is_using_overage=False,
    )
    late_success = latch.apply(
        "success",
        result_is_error=False,
        result_terminal=True,
        is_using_overage=False,
    )

    assert opened.state is CircuitState.QUOTA_OPEN
    assert late_success.state is CircuitState.QUOTA_OPEN
    assert latch.recover_after_approved_canary(
        approved=True,
        canary_passed=True,
        retry_after_elapsed=False,
    ).state is CircuitState.QUOTA_OPEN
    assert latch.recover_after_approved_canary(
        approved=True,
        canary_passed=True,
        retry_after_elapsed=True,
    ).state is CircuitState.CLOSED


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("authentication_failed", CircuitState.AUTH_OPEN),
        ("model_not_found", CircuitState.MODEL_UNAVAILABLE),
    ],
)
def test_sticky_circuit_requires_approved_successful_canary(
    category: str,
    expected: CircuitState,
) -> None:
    latch = CircuitLatch()
    latch.apply(
        category,
        result_is_error=True,
        result_terminal=True,
        is_using_overage=False,
    )
    assert latch.apply(
        "success",
        result_is_error=False,
        result_terminal=True,
        is_using_overage=False,
    ).state is expected
    assert latch.recover_after_approved_canary(
        approved=False,
        canary_passed=True,
        retry_after_elapsed=True,
    ).state is expected
    assert latch.recover_after_approved_canary(
        approved=True,
        canary_passed=True,
        retry_after_elapsed=True,
    ).state is CircuitState.CLOSED


@pytest.mark.parametrize(
    "corrupt",
    [
        {"state": "closed"},
        {"state": "closed", "capability": None, "raw_output": "forbidden"},
        {"state": "future", "capability": None},
        {"state": "model_unavailable", "capability": None},
        {"state": "closed", "capability": "capability_missing"},
    ],
)
def test_corrupt_restart_state_fails_closed(corrupt: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="circuit snapshot"):
        CircuitLatch.from_snapshot(corrupt)


def test_deterministic_circuit_fixture_is_canonical_and_replayable() -> None:
    raw = FIXTURE.read_bytes()
    fixture = json.loads(raw.decode("utf-8"))

    assert raw == canonical_json_bytes(fixture)
    assert validate_circuit_fixture(fixture) is None
    assert fixture["source"]["sha256"] == canonical_circuit_cases_digest(
        fixture["payload"]["cases"],
    )
    assert fixture["payload"]["evidence_class"] == "deterministic"
    assert fixture["payload"]["circuit_transitions"] == "pass"


def test_circuit_fixture_rejects_a_false_expected_transition() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    changed = deepcopy(fixture)
    changed["payload"]["cases"][0]["expected_state"] = "closed"
    changed["source"]["sha256"] = canonical_circuit_cases_digest(
        changed["payload"]["cases"],
    )

    with pytest.raises(ValueError, match="transition mismatch"):
        validate_circuit_fixture(changed)


def test_circuit_fixture_contains_no_live_or_sensitive_evidence() -> None:
    serialized = FIXTURE.read_text(encoding="utf-8").casefold()

    for forbidden in (
        "session_id",
        "native_session",
        "prompt",
        "transcript",
        "raw_output",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "live_nested_policy",
    ):
        assert forbidden not in serialized
