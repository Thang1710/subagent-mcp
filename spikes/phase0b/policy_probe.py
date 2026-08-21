from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
import unicodedata
from typing import Any

from spikes.phase0a.fixtures import validate_fixture
from spikes.phase0b.contracts import Capability


_MAX_NESTED_AGENTS = 4
_AUTH_CATEGORIES = frozenset({"authentication_failed", "oauth_org_not_allowed"})
_QUOTA_CATEGORIES = frozenset({"rate_limit", "billing_error"})
_RETRYABLE_CATEGORIES = frozenset({"server_error", "overloaded"})
_CLOSABLE_NON_ERROR_CATEGORIES = frozenset({"success", "rate_limit_event"})
_STICKY_CIRCUITS = frozenset({"auth_open", "quota_open", "model_unavailable"})
_CIRCUIT_CASE_KEYS = {
    "category",
    "result_is_error",
    "result_terminal",
    "is_using_overage",
    "expected_state",
    "expected_capability",
}


def _hook_decision(allowed: bool, category: str) -> dict[str, str]:
    return {
        "permissionDecision": "allow" if allowed else "deny",
        "reasonCategory": category,
    }


def _bounded_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"invalid {label}") from error
    if len(encoded) > 256 or any(
        unicodedata.category(character) == "Cc" for character in value
    ):
        raise ValueError(f"invalid {label}")
    return value


@dataclass
class NestedCount:
    total: int = 0
    active: int = 0
    denials: dict[str, int] = field(default_factory=dict)

    def deny(self, category: str) -> None:
        self.denials[category] = self.denials.get(category, 0) + 1

    def safe_snapshot(self) -> dict[str, object]:
        return {
            "total": self.total,
            "active": self.active,
            "denials": dict(sorted(self.denials.items())),
        }


class NestedPolicy:
    def __init__(self, workspace_root: str | Path) -> None:
        try:
            resolved = Path(workspace_root).resolve(strict=True)
        except (OSError, RuntimeError, TypeError) as error:
            raise ValueError("workspace root must be an existing directory") from error
        if not resolved.is_dir():
            raise ValueError("workspace root must be an existing directory")
        self._workspace_root = resolved
        self._lock = asyncio.Lock()
        self._counts: dict[tuple[str, str], NestedCount] = {}

    def _key(
        self,
        conversation_id: object,
        execution_id: object,
    ) -> tuple[str, str]:
        return (
            _bounded_identity(conversation_id, "conversation identity"),
            _bounded_identity(execution_id, "execution identity"),
        )

    def _contains(self, candidate: object) -> bool:
        try:
            path = Path(candidate)
            if not path.is_absolute():
                path = self._workspace_root / path
            resolved = path.resolve(strict=False)
            resolved.relative_to(self._workspace_root)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def authorize_write(self, candidate: str | Path) -> dict[str, str]:
        if not self._contains(candidate):
            return _hook_decision(False, "workspace")
        return _hook_decision(True, "allowed")

    async def before_agent(
        self,
        conversation_id: str,
        execution_id: str,
        agent_id: str | None,
        requested_cwd: str | Path,
    ) -> dict[str, str]:
        key = self._key(conversation_id, execution_id)
        async with self._lock:
            count = self._counts.setdefault(key, NestedCount())
            if agent_id is not None:
                count.deny("depth")
                return _hook_decision(False, "depth")
            if not self._contains(requested_cwd):
                count.deny("workspace")
                return _hook_decision(False, "workspace")
            if count.total >= _MAX_NESTED_AGENTS or count.active >= _MAX_NESTED_AGENTS:
                count.deny("limit")
                return _hook_decision(False, "limit")
            count.total += 1
            count.active += 1
            return _hook_decision(True, "allowed")

    async def after_agent(
        self,
        conversation_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        key = self._key(conversation_id, execution_id)
        async with self._lock:
            count = self._counts.get(key)
            if count is None:
                return NestedCount().safe_snapshot()
            count.active = max(0, count.active - 1)
            return count.safe_snapshot()

    async def reconcile_execution(
        self,
        conversation_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        key = self._key(conversation_id, execution_id)
        async with self._lock:
            count = self._counts.pop(key, None)
            if count is None:
                return NestedCount().safe_snapshot()
            count.active = 0
            return count.safe_snapshot()

    async def status_snapshot(
        self,
        conversation_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        key = self._key(conversation_id, execution_id)
        async with self._lock:
            count = self._counts.get(key)
            if count is None:
                return NestedCount().safe_snapshot()
            return count.safe_snapshot()


class CircuitState(str, Enum):
    AUTH_OPEN = "auth_open"
    QUOTA_OPEN = "quota_open"
    RETRYABLE = "retryable"
    MODEL_UNAVAILABLE = "model_unavailable"
    UNKNOWN_OPEN = "unknown_open"
    CLOSED = "closed"


@dataclass(frozen=True)
class CircuitDecision:
    state: CircuitState
    capability: Capability | None = None

    def safe_snapshot(self) -> dict[str, str | None]:
        return {
            "state": self.state.value,
            "capability": (
                None if self.capability is None else self.capability.value
            ),
        }

    @classmethod
    def from_snapshot(cls, value: object) -> CircuitDecision:
        if not isinstance(value, dict) or set(value) != {"state", "capability"}:
            raise ValueError("invalid circuit snapshot")
        try:
            state = CircuitState(value["state"])
        except (TypeError, ValueError) as error:
            raise ValueError("invalid circuit snapshot") from error
        raw_capability = value["capability"]
        try:
            capability = (
                None if raw_capability is None else Capability(raw_capability)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("invalid circuit snapshot") from error
        if state is CircuitState.MODEL_UNAVAILABLE:
            if capability is not Capability.CAPABILITY_MISSING:
                raise ValueError("invalid circuit snapshot")
        elif capability is not None:
            raise ValueError("invalid circuit snapshot")
        return cls(state=state, capability=capability)


def normalize_circuit(
    category: object,
    *,
    result_is_error: bool | None,
    result_terminal: bool,
    is_using_overage: bool | None,
) -> CircuitDecision:
    if category in _AUTH_CATEGORIES:
        return CircuitDecision(CircuitState.AUTH_OPEN)
    if result_is_error is True and result_terminal is True:
        if category in _QUOTA_CATEGORIES:
            return CircuitDecision(CircuitState.QUOTA_OPEN)
        if category in _RETRYABLE_CATEGORIES:
            return CircuitDecision(CircuitState.RETRYABLE)
        if category == "model_not_found":
            return CircuitDecision(
                CircuitState.MODEL_UNAVAILABLE,
                Capability.CAPABILITY_MISSING,
            )
        return CircuitDecision(CircuitState.UNKNOWN_OPEN)
    if (
        category in _CLOSABLE_NON_ERROR_CATEGORIES
        and result_is_error is False
        and result_terminal is True
        and is_using_overage is False
    ):
        return CircuitDecision(CircuitState.CLOSED)
    return CircuitDecision(CircuitState.UNKNOWN_OPEN)


class CircuitLatch:
    def __init__(self) -> None:
        self._decision = CircuitDecision(CircuitState.UNKNOWN_OPEN)

    def apply(
        self,
        category: object,
        *,
        result_is_error: bool | None,
        result_terminal: bool,
        is_using_overage: bool | None,
    ) -> CircuitDecision:
        observed = normalize_circuit(
            category,
            result_is_error=result_is_error,
            result_terminal=result_terminal,
            is_using_overage=is_using_overage,
        )
        if self._decision.state.value in _STICKY_CIRCUITS:
            return self._decision
        self._decision = observed
        return self._decision

    def recover_after_approved_canary(
        self,
        *,
        approved: bool,
        canary_passed: bool,
        retry_after_elapsed: bool,
    ) -> CircuitDecision:
        if type(approved) is not bool or type(canary_passed) is not bool:
            raise ValueError("invalid circuit recovery evidence")
        if type(retry_after_elapsed) is not bool:
            raise ValueError("invalid circuit recovery evidence")
        if not approved or not canary_passed:
            return self._decision
        if (
            self._decision.state is CircuitState.QUOTA_OPEN
            and not retry_after_elapsed
        ):
            return self._decision
        if self._decision.state in {
            CircuitState.AUTH_OPEN,
            CircuitState.QUOTA_OPEN,
            CircuitState.MODEL_UNAVAILABLE,
        }:
            self._decision = CircuitDecision(CircuitState.CLOSED)
        return self._decision

    def safe_snapshot(self) -> dict[str, str | None]:
        return self._decision.safe_snapshot()

    @classmethod
    def from_snapshot(cls, value: object) -> CircuitLatch:
        restored = cls()
        restored._decision = CircuitDecision.from_snapshot(value)
        return restored


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def canonical_circuit_cases_digest(cases: object) -> str:
    return hashlib.sha256(canonical_json_bytes(cases)).hexdigest()


def validate_circuit_fixture(value: object) -> None:
    validate_fixture(value)
    if not isinstance(value, dict):
        raise ValueError("invalid deterministic circuit fixture")
    if (
        value["kind"] != "circuit_cases"
        or value["observed_cli_version"] != "not_applicable_deterministic"
        or value["source"]["kind"] != "deterministic_transition_matrix"
        or value["coverage"]
        != {
            "observed": [
                "circuit_transitions",
                "terminal_no_overage_semantics",
            ],
            "missing": [],
        }
    ):
        raise ValueError("invalid deterministic circuit fixture")
    payload = value["payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "cases",
        "circuit_transitions",
        "evidence_class",
    }:
        raise ValueError("invalid deterministic circuit fixture")
    if (
        payload["circuit_transitions"] != "pass"
        or payload["evidence_class"] != "deterministic"
        or not isinstance(payload["cases"], list)
        or not payload["cases"]
    ):
        raise ValueError("invalid deterministic circuit fixture")

    seen: set[bytes] = set()
    for case in payload["cases"]:
        if not isinstance(case, dict) or set(case) != _CIRCUIT_CASE_KEYS:
            raise ValueError("invalid deterministic circuit case")
        if (
            not isinstance(case["category"], str)
            or not case["category"]
            or (
                case["result_is_error"] is not None
                and type(case["result_is_error"]) is not bool
            )
            or type(case["result_terminal"]) is not bool
            or (
                case["is_using_overage"] is not None
                and type(case["is_using_overage"]) is not bool
            )
            or not isinstance(case["expected_state"], str)
            or (
                case["expected_capability"] is not None
                and not isinstance(case["expected_capability"], str)
            )
        ):
            raise ValueError("invalid deterministic circuit case")
        inputs = canonical_json_bytes(
            {
                "category": case["category"],
                "result_is_error": case["result_is_error"],
                "result_terminal": case["result_terminal"],
                "is_using_overage": case["is_using_overage"],
            }
        )
        if inputs in seen:
            raise ValueError("duplicate deterministic circuit case")
        seen.add(inputs)
        decision = normalize_circuit(
            case["category"],
            result_is_error=case["result_is_error"],
            result_terminal=case["result_terminal"],
            is_using_overage=case["is_using_overage"],
        )
        if decision.safe_snapshot() != {
            "state": case["expected_state"],
            "capability": case["expected_capability"],
        }:
            raise ValueError("deterministic circuit transition mismatch")

    if value["source"]["sha256"] != canonical_circuit_cases_digest(
        payload["cases"],
    ):
        raise ValueError("deterministic circuit fixture digest mismatch")
