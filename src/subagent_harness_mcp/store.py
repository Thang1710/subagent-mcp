from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .paths import ProductPaths


APPLICATION_ID = 0x534D4350  # "SMCP"
DATABASE_SCHEMA_VERSION = 1
_SQLITE_TIMEOUT_SECONDS = 5.0
_PROCESS_INITIALIZE_LOCK = threading.Lock()
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_EXECUTION_STATES = frozenset(
    {"queued", "starting", "running", "needs_input"}
) | _TERMINAL_STATES
_CONVERSATION_STATES = frozenset({"open", "active", "needs_input", "idle", "closed"})
_EVENT_KINDS = frozenset(
    {
        "started",
        "checkpoint",
        "tool_started",
        "tool_finished",
        "permission_denied",
        "needs_input",
        "quota_paused",
        "completed",
        "failed",
        "interrupted",
        "cancelled",
    }
)
_STORE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"starting", "failed", "cancelled"}),
    "starting": frozenset(
        {"running", "needs_input", "succeeded", "failed", "cancelled", "interrupted"}
    ),
    "running": frozenset(
        {"needs_input", "succeeded", "failed", "cancelled", "interrupted"}
    ),
    "needs_input": frozenset(
        {"running", "succeeded", "failed", "cancelled", "interrupted"}
    ),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}
_EXECUTION_SELECT = """
    SELECT e.execution_id, e.conversation_id, c.runtime_id,
           c.state, c.state_revision, e.state, e.state_revision,
           c.external_session_id, e.external_execution_id, c.workspace_key,
           c.descriptor_json, e.requested_json, e.observed_json,
           e.result_json, e.next_event_cursor
    FROM executions AS e
    JOIN conversations AS c ON c.conversation_id = e.conversation_id
"""


class StateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RequestClaim:
    created: bool
    conversation_id: str
    execution_id: str
    response: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActionClaim:
    created: bool
    response: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LaunchClaim:
    should_launch: bool
    state: str
    state_revision: int
    recovery_required: bool


@dataclass(frozen=True, slots=True)
class CircuitRecord:
    runtime_id: str
    variant_id: str
    state: str
    pair_key: str
    revision: int
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CanaryClaim:
    created: bool
    state: str
    revision: int
    response: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class VerifiedCleanupReceipt:
    receipt_id: str
    pair_key: str
    verifier_id: str
    process_identity: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution_id: str
    conversation_id: str
    runtime_id: str
    conversation_state: str
    conversation_revision: int
    execution_state: str
    execution_revision: int
    external_session_id: str | None
    external_execution_id: str | None
    workspace_key: str | None
    descriptor: Mapping[str, Any]
    requested: Mapping[str, Any]
    observed: Mapping[str, Any] | None
    result: Mapping[str, Any] | None
    next_event_cursor: int


@dataclass(frozen=True, slots=True)
class PersistedEvent:
    cursor: int
    kind: str
    payload: Mapping[str, Any]


class StateStore:
    def __init__(self, paths: ProductPaths) -> None:
        self._paths = paths

    @classmethod
    def open(cls, paths: ProductPaths) -> "StateStore":
        store = cls(paths)
        store._initialize()
        return store

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        database = self._connect()
        try:
            database.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield database
            database.commit()
        except BaseException:
            if database.in_transaction:
                database.rollback()
            raise
        finally:
            database.close()

    def claim_execution_request(
        self,
        *,
        tool: str,
        request_id: str,
        request_payload: Mapping[str, Any],
        conversation_id: str,
        execution_id: str,
        runtime_id: str | None,
        requested: Mapping[str, Any],
    ) -> RequestClaim:
        _require_id(tool, "tool", 64)
        _require_id(request_id, "request_id", 256)
        _require_id(conversation_id, "conversation_id", 128)
        _require_id(execution_id, "execution_id", 128)
        if runtime_id is not None:
            _require_id(runtime_id, "runtime_id", 128)
        input_sha256 = hashlib.sha256(_canonical_json_bytes(request_payload)).hexdigest()
        requested_json = _canonical_json_text(requested)
        now = _utc_now()

        with self.transaction(write=True) as database:
            existing = database.execute(
                """
                SELECT input_sha256, conversation_id, execution_id, response_json
                FROM requests
                WHERE tool = ? AND request_id = ?
                """,
                (tool, request_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != input_sha256:
                    raise StateError(
                        "IDEMPOTENCY_CONFLICT",
                        "request_id was already used with different input",
                    )
                if not isinstance(existing[1], str) or not isinstance(existing[2], str):
                    raise StateError("DATABASE_CORRUPT", "request has no recorded execution")
                return RequestClaim(
                    False,
                    existing[1],
                    existing[2],
                    _decode_optional_object(existing[3], "request response"),
                )

            conversation = database.execute(
                "SELECT runtime_id, state FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                if runtime_id is None:
                    raise StateError(
                        "CONVERSATION_NOT_FOUND",
                        "conversation does not exist",
                    )
                database.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, runtime_id, state, state_revision,
                        descriptor_json, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, 'active', 0, '{}', ?, ?)
                    """,
                    (conversation_id, runtime_id, now, now),
                )
            else:
                if runtime_id is not None and conversation[0] != runtime_id:
                    raise StateError(
                        "IDENTITY_CONFLICT",
                        "conversation belongs to another runtime",
                    )
                if conversation[1] == "closed":
                    raise StateError("SESSION_CLOSED", "conversation is closed")
                active = database.execute(
                    """
                    SELECT 1 FROM executions
                    WHERE conversation_id = ?
                      AND state NOT IN ('succeeded', 'failed', 'cancelled', 'interrupted')
                    LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                if active is not None:
                    raise StateError("SESSION_BUSY", "conversation has an active execution")
                database.execute(
                    """
                    UPDATE conversations
                    SET state = 'active', state_revision = state_revision + 1,
                        updated_at_utc = ?
                    WHERE conversation_id = ?
                    """,
                    (now, conversation_id),
                )

            if database.execute(
                "SELECT 1 FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone() is not None:
                raise StateError("IDENTITY_CONFLICT", "execution_id already exists")
            database.execute(
                """
                INSERT INTO executions(
                    execution_id, conversation_id, state, state_revision,
                    requested_json, next_event_cursor, created_at_utc, updated_at_utc
                ) VALUES (?, ?, 'queued', 0, ?, 0, ?, ?)
                """,
                (execution_id, conversation_id, requested_json, now, now),
            )
            database.execute(
                """
                INSERT INTO requests(
                    tool, request_id, input_sha256, conversation_id,
                    execution_id, response_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    tool,
                    request_id,
                    input_sha256,
                    conversation_id,
                    execution_id,
                    now,
                ),
            )
        return RequestClaim(True, conversation_id, execution_id)

    def claim_execution_start(self, execution_id: str) -> LaunchClaim:
        _require_id(execution_id, "execution_id", 128)
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT state, state_revision, result_json
                FROM executions
                WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
            state, revision, result_json = row
            if state == "queued":
                revision += 1
                database.execute(
                    """
                    UPDATE executions
                    SET state = 'starting', state_revision = ?,
                        launch_claimed_at_utc = ?, updated_at_utc = ?
                    WHERE execution_id = ? AND state = 'queued'
                    """,
                    (revision, _utc_now(), _utc_now(), execution_id),
                )
                return LaunchClaim(True, "starting", revision, False)
            return LaunchClaim(
                False,
                str(state),
                int(revision),
                _is_recovery_required(result_json),
            )

    def recover_incomplete_launch(self, execution_id: str) -> LaunchClaim:
        """Fail an attested prior-process start; never turn it back into queued."""

        _require_id(execution_id, "execution_id", 128)
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT e.state, e.state_revision, e.result_json,
                       e.external_execution_id, c.external_session_id
                FROM executions AS e
                JOIN conversations AS c ON c.conversation_id = e.conversation_id
                WHERE e.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
            state, revision, result_json, external_execution, external_session = row
            if state != "starting":
                return LaunchClaim(
                    False,
                    str(state),
                    int(revision),
                    _is_recovery_required(result_json),
                )
            if external_execution is not None or external_session is not None:
                raise StateError(
                    "RECOVERY_UNSAFE",
                    "execution has a native identity and must be reconciled",
                )
            revision += 1
            result_json = _canonical_json_text(
                {
                    "error": {
                        "code": "RECOVERY_REQUIRED",
                        "message": "launch outcome is ambiguous; external work was not repeated",
                    }
                }
            )
            now = _utc_now()
            database.execute(
                """
                UPDATE executions
                SET state = 'failed', state_revision = ?, result_json = ?,
                    terminal_at_utc = ?, updated_at_utc = ?
                WHERE execution_id = ? AND state = 'starting'
                """,
                (revision, result_json, now, now, execution_id),
            )
            return LaunchClaim(False, "failed", revision, True)

    def claim_action_request(
        self,
        *,
        tool: str,
        request_id: str,
        request_payload: Mapping[str, Any],
        conversation_id: str,
        execution_id: str,
    ) -> ActionClaim:
        _require_id(tool, "tool", 64)
        _require_id(request_id, "request_id", 256)
        _require_id(conversation_id, "conversation_id", 128)
        _require_id(execution_id, "execution_id", 128)
        input_sha256 = hashlib.sha256(_canonical_json_bytes(request_payload)).hexdigest()
        with self.transaction(write=True) as database:
            existing = database.execute(
                """
                SELECT input_sha256, conversation_id, execution_id, response_json
                FROM requests WHERE tool = ? AND request_id = ?
                """,
                (tool, request_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != input_sha256:
                    raise StateError(
                        "IDEMPOTENCY_CONFLICT",
                        "request_id was already used with different input",
                    )
                if existing[1] != conversation_id or existing[2] != execution_id:
                    raise StateError("IDENTITY_CONFLICT", "action identity changed")
                return ActionClaim(
                    False,
                    _decode_optional_object(existing[3], "request response"),
                )
            linked = database.execute(
                """
                SELECT 1 FROM executions
                WHERE execution_id = ? AND conversation_id = ?
                """,
                (execution_id, conversation_id),
            ).fetchone()
            if linked is None:
                raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
            database.execute(
                """
                INSERT INTO requests(
                    tool, request_id, input_sha256, conversation_id,
                    execution_id, response_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    tool,
                    request_id,
                    input_sha256,
                    conversation_id,
                    execution_id,
                    _utc_now(),
                ),
            )
        return ActionClaim(True)

    def save_request_response(
        self,
        *,
        tool: str,
        request_id: str,
        response: Mapping[str, Any],
    ) -> None:
        _require_id(tool, "tool", 64)
        _require_id(request_id, "request_id", 256)
        encoded = _canonical_json_text(response)
        with self.transaction(write=True) as database:
            row = database.execute(
                "SELECT response_json FROM requests WHERE tool = ? AND request_id = ?",
                (tool, request_id),
            ).fetchone()
            if row is None:
                raise StateError("REQUEST_NOT_FOUND", "request does not exist")
            if row[0] is not None:
                if row[0] != encoded:
                    raise StateError("RESPONSE_CONFLICT", "request response is already final")
                return
            database.execute(
                """
                UPDATE requests SET response_json = ?
                WHERE tool = ? AND request_id = ? AND response_json IS NULL
                """,
                (encoded, tool, request_id),
            )

    def ensure_circuit_pair(
        self,
        *,
        runtime_id: str,
        variant_id: str,
        pair_key: str,
        details: Mapping[str, Any],
    ) -> CircuitRecord:
        _require_id(runtime_id, "runtime_id", 128)
        _require_id(variant_id, "variant_id", 128)
        _require_id(pair_key, "pair_key", 128)
        safe_details = dict(details)
        safe_details["pair_key"] = pair_key
        encoded = _canonical_json_text(safe_details)
        now = _utc_now()
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT state, revision, details_json FROM circuits
                WHERE runtime_id = ? AND variant_id = ?
                """,
                (runtime_id, variant_id),
            ).fetchone()
            if row is None:
                database.execute(
                    """
                    INSERT INTO circuits(
                        runtime_id, variant_id, state, category, retry_after_utc,
                        revision, details_json, updated_at_utc
                    ) VALUES (?, ?, 'needs_canary', NULL, NULL, 0, ?, ?)
                    """,
                    (runtime_id, variant_id, encoded, now),
                )
            else:
                prior = _decode_object(row[2], "circuit details")
                if prior.get("pair_key") != pair_key:
                    if row[0] in {"probing", "recovery_required"}:
                        retained = dict(prior)
                        retained["pending_pair_key"] = pair_key
                        database.execute(
                            """
                            UPDATE circuits
                            SET state = 'recovery_required',
                                category = 'pair_changed_while_probing',
                                retry_after_utc = NULL, revision = revision + 1,
                                details_json = ?, updated_at_utc = ?
                            WHERE runtime_id = ? AND variant_id = ?
                            """,
                            (
                                _canonical_json_text(retained),
                                now,
                                runtime_id,
                                variant_id,
                            ),
                        )
                    else:
                        database.execute(
                            """
                            UPDATE circuits
                            SET state = 'needs_canary', category = 'pair_changed',
                                retry_after_utc = NULL, revision = revision + 1,
                                details_json = ?, updated_at_utc = ?
                            WHERE runtime_id = ? AND variant_id = ?
                            """,
                            (encoded, now, runtime_id, variant_id),
                        )
        return self.load_circuit(runtime_id, variant_id)

    def load_circuit(self, runtime_id: str, variant_id: str) -> CircuitRecord:
        _require_id(runtime_id, "runtime_id", 128)
        _require_id(variant_id, "variant_id", 128)
        with self.transaction() as database:
            row = database.execute(
                """
                SELECT runtime_id, variant_id, state, revision, details_json
                FROM circuits WHERE runtime_id = ? AND variant_id = ?
                """,
                (runtime_id, variant_id),
            ).fetchone()
        if row is None:
            raise StateError("CIRCUIT_NOT_FOUND", "runtime circuit does not exist")
        return _circuit_record(row)

    def list_circuits(self, runtime_id: str) -> tuple[CircuitRecord, ...]:
        _require_id(runtime_id, "runtime_id", 128)
        with self.transaction() as database:
            rows = database.execute(
                """
                SELECT runtime_id, variant_id, state, revision, details_json
                FROM circuits WHERE runtime_id = ? ORDER BY variant_id
                """,
                (runtime_id,),
            ).fetchall()
        return tuple(_circuit_record(row) for row in rows)

    def claim_canary_request(
        self,
        *,
        request_id: str,
        request_payload: Mapping[str, Any],
        runtime_id: str,
        variant_id: str,
        pair_key: str,
    ) -> CanaryClaim:
        _require_id(request_id, "request_id", 256)
        _require_id(runtime_id, "runtime_id", 128)
        _require_id(variant_id, "variant_id", 128)
        _require_id(pair_key, "pair_key", 128)
        digest = hashlib.sha256(_canonical_json_bytes(request_payload)).hexdigest()
        with self.transaction(write=True) as database:
            existing = database.execute(
                """
                SELECT input_sha256, response_json FROM requests
                WHERE tool = 'runtime_canary' AND request_id = ?
                """,
                (request_id,),
            ).fetchone()
            circuit_row = database.execute(
                """
                SELECT state, revision, details_json FROM circuits
                WHERE runtime_id = ? AND variant_id = ?
                """,
                (runtime_id, variant_id),
            ).fetchone()
            if circuit_row is None:
                raise StateError("CIRCUIT_NOT_FOUND", "runtime circuit does not exist")
            details = _decode_object(circuit_row[2], "circuit details")
            if details.get("pair_key") != pair_key:
                raise StateError("IDENTITY_CONFLICT", "runtime adapter pair changed")
            if existing is not None:
                if existing[0] != digest:
                    raise StateError(
                        "IDEMPOTENCY_CONFLICT",
                        "request_id was already used with different input",
                    )
                return CanaryClaim(
                    False,
                    str(circuit_row[0]),
                    int(circuit_row[1]),
                    _decode_optional_object(existing[1], "request response"),
                )
            if circuit_row[0] not in {"needs_canary", "auto_paused"}:
                return CanaryClaim(False, str(circuit_row[0]), int(circuit_row[1]))
            database.execute(
                """
                INSERT INTO requests(
                    tool, request_id, input_sha256, conversation_id,
                    execution_id, response_json, created_at_utc
                ) VALUES ('runtime_canary', ?, ?, NULL, NULL, NULL, ?)
                """,
                (request_id, digest, _utc_now()),
            )
            revision = int(circuit_row[1]) + 1
            database.execute(
                """
                UPDATE circuits SET state = 'probing', category = NULL,
                    revision = ?, updated_at_utc = ?
                WHERE runtime_id = ? AND variant_id = ?
                  AND state IN ('needs_canary', 'auto_paused') AND revision = ?
                """,
                (
                    revision,
                    _utc_now(),
                    runtime_id,
                    variant_id,
                    int(circuit_row[1]),
                ),
            )
            if database.execute("SELECT changes()").fetchone()[0] != 1:
                raise StateError("CIRCUIT_CONFLICT", "runtime canary was claimed elsewhere")
        return CanaryClaim(True, "probing", revision)

    def complete_canary(
        self,
        *,
        runtime_id: str,
        variant_id: str,
        pair_key: str,
        expected_revision: int,
        state: str,
        details: Mapping[str, Any],
    ) -> CircuitRecord:
        if state not in {"ready", "needs_canary", "auth_required", "auto_paused"}:
            raise StateError("REQUEST_INVALID", "circuit state is invalid")
        safe_details = dict(details)
        safe_details["pair_key"] = pair_key
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT details_json FROM circuits
                WHERE runtime_id = ? AND variant_id = ?
                  AND state = 'probing' AND revision = ?
                """,
                (runtime_id, variant_id, expected_revision),
            ).fetchone()
            if row is None or _decode_object(row[0], "circuit details").get(
                "pair_key"
            ) != pair_key:
                raise StateError("CIRCUIT_CONFLICT", "runtime canary result is stale")
            database.execute(
                """
                UPDATE circuits SET state = ?, category = NULL,
                    revision = revision + 1, details_json = ?, updated_at_utc = ?
                WHERE runtime_id = ? AND variant_id = ?
                  AND state = 'probing' AND revision = ?
                """,
                (
                    state,
                    _canonical_json_text(safe_details),
                    _utc_now(),
                    runtime_id,
                    variant_id,
                    expected_revision,
                ),
            )
            if database.execute("SELECT changes()").fetchone()[0] != 1:
                raise StateError("CIRCUIT_CONFLICT", "runtime canary result is stale")
        return self.load_circuit(runtime_id, variant_id)

    def pause_ready_circuit(
        self,
        *,
        runtime_id: str,
        variant_id: str,
        pair_key: str,
        expected_revision: int,
        error_code: str,
    ) -> CircuitRecord:
        _require_id(runtime_id, "runtime_id", 128)
        _require_id(variant_id, "variant_id", 128)
        _require_id(pair_key, "pair_key", 128)
        _require_id(error_code, "error_code", 128)
        if error_code not in {"QUOTA_PAUSED", "USAGE_CREDITS_FORBIDDEN"}:
            raise StateError("REQUEST_INVALID", "circuit pause error is invalid")
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT state, revision, details_json FROM circuits
                WHERE runtime_id = ? AND variant_id = ?
                """,
                (runtime_id, variant_id),
            ).fetchone()
            if row is None:
                raise StateError("CIRCUIT_NOT_FOUND", "runtime circuit does not exist")
            details = _decode_object(row[2], "circuit details")
            if details.get("pair_key") != pair_key:
                raise StateError("IDENTITY_CONFLICT", "runtime adapter pair changed")
            if row[0] == "auto_paused":
                return _circuit_record(
                    (runtime_id, variant_id, row[0], row[1], row[2])
                )
            if row[0] != "ready" or int(row[1]) != expected_revision:
                raise StateError("CIRCUIT_CONFLICT", "runtime quota pause is stale")
            safe_details = dict(details)
            safe_details["error_code"] = error_code
            database.execute(
                """
                UPDATE circuits SET state = 'auto_paused', category = 'quota',
                    retry_after_utc = NULL, revision = revision + 1,
                    details_json = ?, updated_at_utc = ?
                WHERE runtime_id = ? AND variant_id = ?
                  AND state = 'ready' AND revision = ?
                """,
                (
                    _canonical_json_text(safe_details),
                    _utc_now(),
                    runtime_id,
                    variant_id,
                    expected_revision,
                ),
            )
            if database.execute("SELECT changes()").fetchone()[0] != 1:
                raise StateError("CIRCUIT_CONFLICT", "runtime quota pause is stale")
        return self.load_circuit(runtime_id, variant_id)

    def recover_canary_after_cleanup(
        self,
        *,
        runtime_id: str,
        variant_id: str,
        pair_key: str,
        expected_revision: int,
        receipt: VerifiedCleanupReceipt,
    ) -> CircuitRecord:
        if not isinstance(receipt, VerifiedCleanupReceipt):
            raise StateError(
                "RECOVERY_REQUIRED",
                "canary cleanup requires a verified structured receipt",
            )
        for value, label, limit in (
            (receipt.receipt_id, "receipt_id", 256),
            (receipt.pair_key, "pair_key", 128),
            (receipt.verifier_id, "verifier_id", 256),
            (receipt.process_identity, "process_identity", 512),
        ):
            _require_id(value, label, limit)
        if receipt.pair_key != pair_key:
            raise StateError("IDENTITY_CONFLICT", "cleanup receipt pair changed")
        receipt_summary = {
            "receipt_id": receipt.receipt_id,
            "pair_key": receipt.pair_key,
            "verifier_id": receipt.verifier_id,
            "process_identity": receipt.process_identity,
            "evidence": dict(receipt.evidence),
        }
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT state, revision, details_json FROM circuits
                WHERE runtime_id = ? AND variant_id = ?
                """,
                (runtime_id, variant_id),
            ).fetchone()
            if row is None:
                raise StateError("CIRCUIT_NOT_FOUND", "runtime circuit does not exist")
            details = _decode_object(row[2], "circuit details")
            if (
                row[0] not in {"probing", "recovery_required"}
                or int(row[1]) != expected_revision
                or details.get("pair_key") != pair_key
            ):
                raise StateError("CIRCUIT_CONFLICT", "canary recovery receipt is stale")
            details = dict(details)
            details.pop("pending_pair_key", None)
            details["last_cleanup_receipt"] = receipt_summary
            database.execute(
                """
                UPDATE circuits SET state = 'needs_canary', category = NULL,
                    retry_after_utc = NULL, revision = revision + 1,
                    details_json = ?, updated_at_utc = ?
                WHERE runtime_id = ? AND variant_id = ? AND revision = ?
                  AND state IN ('probing', 'recovery_required')
                """,
                (
                    _canonical_json_text(details),
                    _utc_now(),
                    runtime_id,
                    variant_id,
                    expected_revision,
                ),
            )
            if database.execute("SELECT changes()").fetchone()[0] != 1:
                raise StateError("CIRCUIT_CONFLICT", "canary recovery receipt is stale")
        return self.load_circuit(runtime_id, variant_id)

    def acquire_writer_lease(
        self,
        *,
        lease_id: str,
        resource_key: str,
        execution_id: str,
    ) -> None:
        _require_id(lease_id, "lease_id", 128)
        _require_id(resource_key, "resource_key", 4096)
        _require_id(execution_id, "execution_id", 128)
        with self.transaction(write=True) as database:
            existing = database.execute(
                """
                SELECT lease_id, execution_id FROM leases
                WHERE resource_key = ? AND released_at_utc IS NULL
                """,
                (resource_key,),
            ).fetchone()
            if existing is not None:
                if existing == (lease_id, execution_id):
                    return
                raise StateError("WORKSPACE_BUSY", "workspace already has an active writer")
            if database.execute(
                "SELECT 1 FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone() is None:
                raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
            database.execute(
                """
                INSERT INTO leases(
                    lease_id, resource_key, execution_id, kind,
                    acquired_at_utc, expires_at_utc, released_at_utc
                ) VALUES (?, ?, ?, 'writer', ?, NULL, NULL)
                """,
                (lease_id, resource_key, execution_id, _utc_now()),
            )

    def release_execution_leases(self, execution_id: str) -> None:
        _require_id(execution_id, "execution_id", 128)
        with self.transaction(write=True) as database:
            database.execute(
                """
                UPDATE leases SET released_at_utc = ?
                WHERE execution_id = ? AND released_at_utc IS NULL
                """,
                (_utc_now(), execution_id),
            )

    def bind_execution(
        self,
        *,
        execution_id: str,
        external_session_id: str,
        external_execution_id: str,
        workspace_key: str,
        descriptor: Mapping[str, Any],
        observed: Mapping[str, Any],
    ) -> ExecutionRecord:
        _require_id(execution_id, "execution_id", 128)
        _require_id(external_session_id, "external_session_id", 512)
        _require_id(external_execution_id, "external_execution_id", 512)
        _require_id(workspace_key, "workspace_key", 4096)
        descriptor_json = _canonical_json_text(descriptor)
        observed_json = _canonical_json_text(observed)
        now = _utc_now()
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT e.state, e.state_revision, e.next_event_cursor,
                       c.conversation_id, c.external_session_id, c.workspace_key,
                       c.descriptor_json
                FROM executions AS e
                JOIN conversations AS c ON c.conversation_id = e.conversation_id
                WHERE e.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
            state, revision, cursor, conversation_id, session, workspace, saved_descriptor = row
            if state != "starting":
                if (
                    state == "running"
                    and session == external_session_id
                    and workspace == workspace_key
                ):
                    return _load_execution_from_connection(database, execution_id)
                raise StateError("STATE_CONFLICT", "execution is not starting")
            if session is not None and session != external_session_id:
                raise StateError("IDENTITY_CONFLICT", "native session identity changed")
            if workspace is not None and workspace != workspace_key:
                raise StateError("IDENTITY_CONFLICT", "workspace identity changed")
            if saved_descriptor != "{}" and saved_descriptor != descriptor_json:
                raise StateError("IDENTITY_CONFLICT", "agent descriptor changed")
            cursor += 1
            database.execute(
                """
                UPDATE conversations
                SET state = 'active', state_revision = state_revision + 1,
                    external_session_id = ?, workspace_key = ?, descriptor_json = ?,
                    updated_at_utc = ?
                WHERE conversation_id = ?
                """,
                (
                    external_session_id,
                    workspace_key,
                    descriptor_json,
                    now,
                    conversation_id,
                ),
            )
            database.execute(
                """
                UPDATE executions
                SET state = 'running', state_revision = ?, external_execution_id = ?,
                    observed_json = ?, next_event_cursor = ?, updated_at_utc = ?
                WHERE execution_id = ? AND state = 'starting'
                """,
                (
                    revision + 1,
                    external_execution_id,
                    observed_json,
                    cursor,
                    now,
                    execution_id,
                ),
            )
            database.execute(
                """
                INSERT INTO events(execution_id, cursor, kind, payload_json, created_at_utc)
                VALUES (?, ?, 'started', ?, ?)
                """,
                (execution_id, cursor, _canonical_json_text({}), now),
            )
        return self.load_execution(execution_id)

    def transition_execution(
        self,
        *,
        execution_id: str,
        execution_state: str,
        conversation_state: str,
        observed: Mapping[str, Any],
        result: Mapping[str, Any] | None,
        event_kind: str,
        event_payload: Mapping[str, Any],
        release_leases: bool = True,
    ) -> ExecutionRecord:
        _require_id(execution_id, "execution_id", 128)
        _require_execution_state(execution_state)
        _require_conversation_state(conversation_state)
        _require_event_kind(event_kind)
        observed_json = _canonical_json_text(observed)
        result_json = None if result is None else _canonical_json_text(result)
        event_json = _canonical_json_text(event_payload)
        now = _utc_now()
        with self.transaction(write=True) as database:
            row = database.execute(
                """
                SELECT e.state, e.state_revision, e.next_event_cursor,
                       e.result_json, e.conversation_id
                FROM executions AS e WHERE e.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
            current, revision, cursor, saved_result, conversation_id = row
            if current in _TERMINAL_STATES:
                return _load_execution_from_connection(database, execution_id)
            if current == execution_state:
                return _load_execution_from_connection(database, execution_id)
            if execution_state not in _STORE_TRANSITIONS.get(str(current), frozenset()):
                raise StateError(
                    "STATE_CONFLICT",
                    f"execution cannot transition from {current} to {execution_state}",
                )
            if saved_result is not None:
                raise StateError("DATABASE_CORRUPT", "nonterminal execution has a result")
            cursor += 1
            terminal = execution_state in _TERMINAL_STATES
            database.execute(
                """
                UPDATE executions
                SET state = ?, state_revision = ?, observed_json = ?, result_json = ?,
                    next_event_cursor = ?, updated_at_utc = ?, terminal_at_utc = ?
                WHERE execution_id = ?
                """,
                (
                    execution_state,
                    revision + 1,
                    observed_json,
                    result_json,
                    cursor,
                    now,
                    now if terminal else None,
                    execution_id,
                ),
            )
            database.execute(
                """
                UPDATE conversations
                SET state = ?, state_revision = state_revision + 1,
                    updated_at_utc = ? WHERE conversation_id = ?
                """,
                (conversation_state, now, conversation_id),
            )
            database.execute(
                """
                INSERT INTO events(execution_id, cursor, kind, payload_json, created_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (execution_id, cursor, event_kind, event_json, now),
            )
            if terminal and release_leases:
                database.execute(
                    """
                    UPDATE leases SET released_at_utc = ?
                    WHERE execution_id = ? AND released_at_utc IS NULL
                    """,
                    (now, execution_id),
                )
        return self.load_execution(execution_id)

    def load_execution(self, execution_id: str) -> ExecutionRecord:
        _require_id(execution_id, "execution_id", 128)
        with self.transaction() as database:
            row = database.execute(_EXECUTION_SELECT + " WHERE e.execution_id = ?", (execution_id,)).fetchone()
        if row is None:
            raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
        return _execution_record(row)

    def load_latest_execution(self, conversation_id: str) -> ExecutionRecord:
        _require_id(conversation_id, "conversation_id", 128)
        with self.transaction() as database:
            row = database.execute(
                _EXECUTION_SELECT
                + " WHERE e.conversation_id = ? ORDER BY e.created_at_utc DESC, e.rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise StateError("CONVERSATION_NOT_FOUND", "conversation does not exist")
        return _execution_record(row)

    def load_events(
        self,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[PersistedEvent, ...]:
        _require_id(execution_id, "execution_id", 128)
        if isinstance(after_cursor, bool) or not isinstance(after_cursor, int) or after_cursor < 0:
            raise StateError("REQUEST_INVALID", "after_cursor must be nonnegative")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise StateError("REQUEST_INVALID", "event limit is invalid")
        with self.transaction() as database:
            rows = database.execute(
                """
                SELECT cursor, kind, payload_json FROM events
                WHERE execution_id = ? AND cursor > ?
                ORDER BY cursor LIMIT ?
                """,
                (execution_id, after_cursor, limit),
            ).fetchall()
        return tuple(
            PersistedEvent(
                cursor=int(row[0]),
                kind=str(row[1]),
                payload=_decode_object(row[2], "event payload"),
            )
            for row in rows
        )

    def close_conversation(self, conversation_id: str) -> ExecutionRecord:
        _require_id(conversation_id, "conversation_id", 128)
        with self.transaction(write=True) as database:
            row = database.execute(
                _EXECUTION_SELECT
                + " WHERE e.conversation_id = ? ORDER BY e.created_at_utc DESC, e.rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise StateError("CONVERSATION_NOT_FOUND", "conversation does not exist")
            latest = _execution_record(row)
            if latest.execution_state not in _TERMINAL_STATES:
                raise StateError("SESSION_BUSY", "conversation has an active execution")
            if latest.conversation_state == "closed":
                return latest
            database.execute(
                """
                UPDATE conversations
                SET state = 'closed', state_revision = state_revision + 1,
                    updated_at_utc = ?
                WHERE conversation_id = ? AND state != 'closed'
                """,
                (_utc_now(), conversation_id),
            )
        return self.load_latest_execution(conversation_id)

    def _initialize(self) -> None:
        try:
            self._paths.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError("DATABASE_OPEN_FAILED", "cannot open state database") from exc
        lock_path = self._paths.database_file.with_name("state.db.init.lock")
        with _PROCESS_INITIALIZE_LOCK:
            try:
                with _initialization_lock(lock_path):
                    if not self._paths.database_file.exists():
                        _publish_initial_database(self._paths.database_file)
                    _open_owned_database(self._paths.database_file)
            except StateError:
                raise
            except (OSError, sqlite3.DatabaseError) as exc:
                raise StateError(
                    "DATABASE_OPEN_FAILED",
                    "cannot initialize state database",
                ) from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            database = _raw_connect(self._paths.database_file)
            _configure_connection(database)
            if _read_application_id(database) != APPLICATION_ID:
                raise StateError("DATABASE_UNOWNED", "state database identity changed")
            version = _database_version(database)
            if version != DATABASE_SCHEMA_VERSION:
                raise StateError(
                    "DATABASE_VERSION_UNSUPPORTED",
                    f"database schema version {version} is unsupported",
                )
            mode = database.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).casefold() != "wal":
                raise StateError("DATABASE_MODE_INVALID", "database is not in WAL mode")
            return database
        except StateError:
            if "database" in locals():
                database.close()
            raise
        except sqlite3.DatabaseError as exc:
            if "database" in locals():
                database.close()
            raise StateError("DATABASE_CORRUPT", "state database is malformed") from exc


def _raw_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        path,
        timeout=_SQLITE_TIMEOUT_SECONDS,
        isolation_level=None,
    )


def _configure_connection(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA busy_timeout = 5000")
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA synchronous = FULL")
    if database.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise StateError("DATABASE_MODE_INVALID", "foreign keys could not be enabled")


def _publish_initial_database(path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _build_initial_database(temporary)
        _fsync_file(temporary)
        if path.exists():
            return
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except StateError as exc:
        raise StateError(
            "DATABASE_INITIALIZATION_FAILED",
            "initial database could not be prepared",
        ) from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        raise StateError(
            "DATABASE_INITIALIZATION_FAILED",
            "initial database could not be published",
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _build_initial_database(path: Path) -> None:
    database = _raw_connect(path)
    try:
        _configure_connection(database)
        if _read_application_id(database) != 0:
            raise StateError("DATABASE_CORRUPT", "temporary database is not empty")
        database.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        if _read_application_id(database) != APPLICATION_ID:
            raise StateError("DATABASE_CORRUPT", "database ownership was not recorded")
        _apply_migration_one(database)
        if _database_version(database) != DATABASE_SCHEMA_VERSION:
            raise StateError("DATABASE_CORRUPT", "database migration was not recorded")
        _require_quick_check(database)
    finally:
        database.close()


def _open_owned_database(path: Path) -> None:
    try:
        database = _raw_connect(path)
    except sqlite3.DatabaseError as exc:
        raise StateError("DATABASE_OPEN_FAILED", "cannot open state database") from exc
    try:
        _configure_connection(database)
        if _read_application_id(database) != APPLICATION_ID:
            raise StateError(
                "DATABASE_UNOWNED",
                "state.db is not owned by Subagent MCP",
            )
        _require_quick_check(database)
        version = _database_version(database)
        if version > DATABASE_SCHEMA_VERSION:
            raise StateError(
                "DATABASE_VERSION_UNSUPPORTED",
                f"database schema version {version} is newer than this runtime",
            )
        journal_mode = database.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).casefold() != "wal":
            raise StateError("DATABASE_MODE_INVALID", "WAL mode is unavailable")
        if version < DATABASE_SCHEMA_VERSION:
            _apply_migration_one(database)
        _require_quick_check(database)
    except StateError:
        raise
    except sqlite3.DatabaseError as exc:
        raise StateError("DATABASE_CORRUPT", "state database is malformed") from exc
    finally:
        database.close()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


@contextmanager
def _initialization_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_application_id(database: sqlite3.Connection) -> int:
    row = database.execute("PRAGMA application_id").fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise StateError("DATABASE_CORRUPT", "database application id is invalid")
    return row[0]


def _database_version(database: sqlite3.Connection) -> int:
    exists = database.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if exists is None:
        return 0
    row = database.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    version = 0 if row is None or row[0] is None else row[0]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise StateError("DATABASE_CORRUPT", "database migration version is invalid")
    return version


def _require_quick_check(database: sqlite3.Connection) -> None:
    rows = database.execute("PRAGMA quick_check").fetchall()
    if rows != [("ok",)]:
        raise StateError("DATABASE_CORRUPT", "database integrity check failed")


def _apply_migration_one(database: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE schema_migrations(
            version INTEGER PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE conversations(
            conversation_id TEXT PRIMARY KEY,
            runtime_id TEXT NOT NULL,
            state TEXT NOT NULL,
            state_revision INTEGER NOT NULL DEFAULT 0 CHECK(state_revision >= 0),
            external_session_id TEXT,
            workspace_key TEXT,
            descriptor_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX conversations_external_session
        ON conversations(runtime_id, external_session_id)
        WHERE external_session_id IS NOT NULL
        """,
        """
        CREATE TABLE executions(
            execution_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL
                REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
            state TEXT NOT NULL,
            state_revision INTEGER NOT NULL DEFAULT 0 CHECK(state_revision >= 0),
            launch_claimed_at_utc TEXT,
            external_execution_id TEXT,
            requested_json TEXT NOT NULL,
            observed_json TEXT,
            result_json TEXT,
            next_event_cursor INTEGER NOT NULL DEFAULT 0 CHECK(next_event_cursor >= 0),
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            terminal_at_utc TEXT
        )
        """,
        """
        CREATE TABLE requests(
            tool TEXT NOT NULL,
            request_id TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            conversation_id TEXT
                REFERENCES conversations(conversation_id) ON DELETE RESTRICT,
            execution_id TEXT
                REFERENCES executions(execution_id) ON DELETE RESTRICT,
            response_json TEXT,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY(tool, request_id)
        )
        """,
        "CREATE INDEX requests_execution ON requests(execution_id)",
        """
        CREATE TABLE events(
            event_id INTEGER PRIMARY KEY,
            execution_id TEXT NOT NULL
                REFERENCES executions(execution_id) ON DELETE RESTRICT,
            cursor INTEGER NOT NULL CHECK(cursor > 0),
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(execution_id, cursor)
        )
        """,
        "CREATE INDEX events_recent ON events(event_id DESC)",
        """
        CREATE TABLE circuits(
            runtime_id TEXT NOT NULL,
            variant_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            category TEXT,
            retry_after_utc TEXT,
            revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
            details_json TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY(runtime_id, variant_id)
        )
        """,
        """
        CREATE TABLE leases(
            lease_id TEXT PRIMARY KEY,
            resource_key TEXT NOT NULL,
            execution_id TEXT
                REFERENCES executions(execution_id) ON DELETE RESTRICT,
            kind TEXT NOT NULL,
            acquired_at_utc TEXT NOT NULL,
            expires_at_utc TEXT,
            released_at_utc TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX leases_one_active_writer
        ON leases(resource_key)
        WHERE released_at_utc IS NULL
        """,
    )
    try:
        database.execute("BEGIN IMMEDIATE")
        for statement in statements:
            database.execute(statement)
        database.execute(
            "INSERT INTO schema_migrations(version, applied_at_utc) VALUES (?, ?)",
            (DATABASE_SCHEMA_VERSION, _utc_now()),
        )
        database.commit()
    except BaseException:
        if database.in_transaction:
            database.rollback()
        raise


def _load_execution_from_connection(
    database: sqlite3.Connection,
    execution_id: str,
) -> ExecutionRecord:
    row = database.execute(
        _EXECUTION_SELECT + " WHERE e.execution_id = ?",
        (execution_id,),
    ).fetchone()
    if row is None:
        raise StateError("EXECUTION_NOT_FOUND", "execution does not exist")
    return _execution_record(row)


def _execution_record(row: tuple[Any, ...]) -> ExecutionRecord:
    if len(row) != 15:
        raise StateError("DATABASE_CORRUPT", "execution record has the wrong shape")
    (
        execution_id,
        conversation_id,
        runtime_id,
        conversation_state,
        conversation_revision,
        execution_state,
        execution_revision,
        external_session_id,
        external_execution_id,
        workspace_key,
        descriptor_json,
        requested_json,
        observed_json,
        result_json,
        next_event_cursor,
    ) = row
    for value, label in (
        (execution_id, "execution_id"),
        (conversation_id, "conversation_id"),
        (runtime_id, "runtime_id"),
        (conversation_state, "conversation state"),
        (execution_state, "execution state"),
    ):
        if not isinstance(value, str):
            raise StateError("DATABASE_CORRUPT", f"{label} is invalid")
    for value, label in (
        (conversation_revision, "conversation revision"),
        (execution_revision, "execution revision"),
        (next_event_cursor, "event cursor"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StateError("DATABASE_CORRUPT", f"{label} is invalid")
    if conversation_state not in _CONVERSATION_STATES:
        raise StateError("DATABASE_CORRUPT", "conversation state is unknown")
    if execution_state not in _EXECUTION_STATES:
        raise StateError("DATABASE_CORRUPT", "execution state is unknown")
    for value, label in (
        (external_session_id, "external session id"),
        (external_execution_id, "external execution id"),
        (workspace_key, "workspace key"),
    ):
        if value is not None and not isinstance(value, str):
            raise StateError("DATABASE_CORRUPT", f"{label} is invalid")
    return ExecutionRecord(
        execution_id=execution_id,
        conversation_id=conversation_id,
        runtime_id=runtime_id,
        conversation_state=conversation_state,
        conversation_revision=conversation_revision,
        execution_state=execution_state,
        execution_revision=execution_revision,
        external_session_id=external_session_id,
        external_execution_id=external_execution_id,
        workspace_key=workspace_key,
        descriptor=_decode_object(descriptor_json, "agent descriptor"),
        requested=_decode_object(requested_json, "execution request"),
        observed=_decode_optional_object(observed_json, "execution observation"),
        result=_decode_optional_object(result_json, "execution result"),
        next_event_cursor=next_event_cursor,
    )


def _circuit_record(row: tuple[Any, ...]) -> CircuitRecord:
    if len(row) != 5:
        raise StateError("DATABASE_CORRUPT", "circuit record has the wrong shape")
    runtime_id, variant_id, state, revision, details_json = row
    if not all(isinstance(value, str) for value in (runtime_id, variant_id, state)):
        raise StateError("DATABASE_CORRUPT", "circuit identity is invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise StateError("DATABASE_CORRUPT", "circuit revision is invalid")
    details = _decode_object(details_json, "circuit details")
    pair_key = details.get("pair_key")
    if not isinstance(pair_key, str) or not pair_key:
        raise StateError("DATABASE_CORRUPT", "circuit pair identity is invalid")
    return CircuitRecord(
        runtime_id=runtime_id,
        variant_id=variant_id,
        state=state,
        pair_key=pair_key,
        revision=revision,
        details=details,
    )


def _decode_object(raw: object, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise StateError("DATABASE_CORRUPT", f"{label} is invalid")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError("DATABASE_CORRUPT", f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise StateError("DATABASE_CORRUPT", f"{label} is not an object")
    return value


def _decode_optional_object(raw: object, label: str) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    return _decode_object(raw, label)


def _require_execution_state(state: object) -> None:
    if state not in _EXECUTION_STATES:
        raise StateError("REQUEST_INVALID", "execution state is invalid")


def _require_conversation_state(state: object) -> None:
    if state not in _CONVERSATION_STATES:
        raise StateError("REQUEST_INVALID", "conversation state is invalid")


def _require_event_kind(kind: object) -> None:
    if kind not in _EVENT_KINDS:
        raise StateError("REQUEST_INVALID", "event kind is invalid")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StateError("REQUEST_INVALID", "value is not canonical JSON") from exc


def _canonical_json_text(value: object) -> str:
    return _canonical_json_bytes(value).decode("utf-8").rstrip("\n")


def _require_id(value: object, label: str, max_bytes: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StateError("REQUEST_INVALID", f"{label} must be nonempty")
    if len(value.encode("utf-8")) > max_bytes:
        raise StateError("REQUEST_INVALID", f"{label} is too long")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise StateError("REQUEST_INVALID", f"{label} contains a control character")


def _is_recovery_required(result_json: object) -> bool:
    if not isinstance(result_json, str):
        return False
    try:
        value = json.loads(result_json)
    except json.JSONDecodeError:
        raise StateError("DATABASE_CORRUPT", "execution result is malformed")
    return (
        isinstance(value, dict)
        and isinstance(value.get("error"), dict)
        and value["error"].get("code") == "RECOVERY_REQUIRED"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )
