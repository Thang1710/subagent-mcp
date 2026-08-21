from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


_REVIEWED_SDK_VERSION = "0.2.142"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Capability(str, Enum):
    PASS = "pass"
    BLOCKED = "blocked"
    CAPABILITY_MISSING = "capability_missing"


PHASE0B_GATES = (
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

MANAGED_MVP_GATES = tuple(
    gate for gate in PHASE0B_GATES if gate != "visible_managed_promotion"
)

BACKGROUND_READY_GATES = MANAGED_MVP_GATES + ("visible_managed_promotion",)


@dataclass(frozen=True)
class AdapterPair:
    sdk_version: str
    cli_version: str
    cli_sha256: str

    def validate(self) -> None:
        if self.sdk_version != _REVIEWED_SDK_VERSION:
            raise ValueError("unreviewed SDK version")
        if not isinstance(self.cli_version, str) or not self.cli_version.strip():
            raise ValueError("invalid CLI version")
        if (
            not isinstance(self.cli_sha256, str)
            or _SHA256.fullmatch(self.cli_sha256) is None
        ):
            raise ValueError("invalid CLI SHA-256")


@dataclass(frozen=True)
class ManagedObservation:
    pair: AdapterPair
    status: Capability
    session_present: bool
    context_equal: bool
    model_equal: bool
    cleanup_confirmed: bool
    result_terminal: bool
    error_category: str | None = None

    def validate(self) -> None:
        if not isinstance(self.pair, AdapterPair):
            raise ValueError("invalid adapter pair")
        self.pair.validate()
        if not isinstance(self.status, Capability):
            raise ValueError("invalid capability status")
        confirmations = (
            self.session_present,
            self.context_equal,
            self.model_equal,
            self.cleanup_confirmed,
            self.result_terminal,
        )
        for value in confirmations:
            if type(value) is not bool:
                raise ValueError("managed observation fields must be booleans")
        if self.error_category is not None and (
            not isinstance(self.error_category, str)
            or _ERROR_CATEGORY.fullmatch(self.error_category) is None
        ):
            raise ValueError("invalid sanitized error category")
        if self.status is Capability.PASS and (
            not all(confirmations) or self.error_category is not None
        ):
            raise ValueError("PASS requires every confirmation and no error category")
