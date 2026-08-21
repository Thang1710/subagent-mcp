from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from subagent_harness_mcp import store as store_module
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.store import (
    APPLICATION_ID,
    StateError,
    StateStore,
)


def _paths(tmp_path: Path):
    return resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )


def _claim(
    store: StateStore,
    *,
    request_id: str = "request-1",
    payload: dict[str, object] | None = None,
    conversation_id: str = "conversation-1",
    execution_id: str = "execution-1",
):
    return store.claim_execution_request(
        tool="agent_spawn",
        request_id=request_id,
        request_payload=payload or {"task": "review", "variant_id": "future"},
        conversation_id=conversation_id,
        execution_id=execution_id,
        runtime_id="future-harness",
        requested={"model": "provider/future-model", "reasoning": {"mode": "deep"}},
    )


def test_open_creates_only_owned_state_database_with_expected_schema(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    store = StateStore.open(paths)

    with store.transaction() as database:
        application_id = database.execute("PRAGMA application_id").fetchone()[0]
        foreign_keys = database.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = database.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = database.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert application_id == APPLICATION_ID
    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert version == 1
    assert {
        "schema_migrations",
        "conversations",
        "executions",
        "requests",
        "events",
        "circuits",
        "leases",
    } <= tables
    assert paths.database_file.is_file()
    assert not paths.config_dir.exists()
    assert not paths.data_dir.exists()


def test_failure_before_publish_leaves_final_absent_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)

    def fail_before_publish(_path: Path) -> None:
        raise OSError("simulated fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_fsync_file", fail_before_publish)
        with pytest.raises(StateError) as captured:
            StateStore.open(paths)

    assert captured.value.code == "DATABASE_INITIALIZATION_FAILED"
    assert not paths.database_file.exists()
    assert list(paths.state_dir.glob("state.db.*.tmp")) == []

    abandoned = paths.state_dir / "state.db.abandoned.tmp"
    abandoned.write_bytes(b"belongs-to-an-older-crashed-attempt")
    store = StateStore.open(paths)

    assert paths.database_file.is_file()
    assert abandoned.read_bytes() == b"belongs-to-an-older-crashed-attempt"
    with store.transaction() as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert database.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 1


def test_concurrent_first_open_publishes_one_complete_database(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    start = threading.Barrier(3)
    stores: list[StateStore] = []
    errors: list[Exception] = []
    result_lock = threading.Lock()

    def opener() -> None:
        start.wait()
        try:
            result = StateStore.open(paths)
            with result_lock:
                stores.append(result)
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    assert len(stores) == 2
    assert list(paths.state_dir.glob("state.db.*.tmp")) == []
    with stores[0].transaction() as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert database.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()[0] == 1


def test_foreign_database_is_preserved_and_rejected_before_wal(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    with sqlite3.connect(paths.database_file) as database:
        database.execute("CREATE TABLE user_data(value TEXT NOT NULL)")
        database.execute("INSERT INTO user_data VALUES ('keep')")
    before = paths.database_file.read_bytes()

    with pytest.raises(StateError) as captured:
        StateStore.open(paths)

    assert captured.value.code == "DATABASE_UNOWNED"
    assert paths.database_file.read_bytes() == before
    assert not paths.database_file.with_name("state.db-wal").exists()
    with sqlite3.connect(paths.database_file) as database:
        assert database.execute("SELECT value FROM user_data").fetchone()[0] == "keep"


def test_corrupt_database_bytes_are_preserved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    raw = b"not-a-sqlite-database\x00keep"
    paths.database_file.write_bytes(raw)

    with pytest.raises(StateError) as captured:
        StateStore.open(paths)

    assert captured.value.code == "DATABASE_CORRUPT"
    assert paths.database_file.read_bytes() == raw


def test_same_request_returns_recorded_execution_and_digest_conflict_fails(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))

    first = _claim(store)
    replay = _claim(
        store,
        payload={"variant_id": "future", "task": "review"},
        conversation_id="unused-conversation",
        execution_id="unused-execution",
    )

    assert first.created is True
    assert replay.created is False
    assert replay.conversation_id == first.conversation_id
    assert replay.execution_id == first.execution_id
    with pytest.raises(StateError) as captured:
        _claim(store, payload={"task": "different"})
    assert captured.value.code == "IDEMPOTENCY_CONFLICT"

    with store.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1


def test_concurrent_request_claims_create_exactly_one_execution(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))
    start = threading.Barrier(3)
    claims = []
    errors = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        start.wait()
        try:
            result = _claim(
                store,
                conversation_id=f"conversation-{index}",
                execution_id=f"execution-{index}",
            )
            with lock:
                claims.append(result)
        except Exception as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    assert len(claims) == 2
    assert sum(claim.created for claim in claims) == 1
    assert {claim.conversation_id for claim in claims} == {claims[0].conversation_id}
    assert {claim.execution_id for claim in claims} == {claims[0].execution_id}


def test_write_transaction_rolls_back_all_rows_after_failure(tmp_path: Path) -> None:
    store = StateStore.open(_paths(tmp_path))

    with pytest.raises(RuntimeError, match="simulated crash"):
        with store.transaction(write=True) as database:
            database.execute(
                """
                INSERT INTO conversations(
                    conversation_id, runtime_id, state, state_revision,
                    descriptor_json, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rolled-back",
                    "future-harness",
                    "open",
                    0,
                    "{}",
                    "2026-08-21T00:00:00Z",
                    "2026-08-21T00:00:00Z",
                ),
            )
            raise RuntimeError("simulated crash")

    with store.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_launch_compare_and_swap_has_one_winner_and_crash_never_relaunches(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))
    execution_id = _claim(store).execution_id
    start = threading.Barrier(3)
    claims = []

    def worker() -> None:
        start.wait()
        claims.append(store.claim_execution_start(execution_id))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sum(claim.should_launch for claim in claims) == 1
    assert {claim.state for claim in claims} == {"starting"}
    assert store.claim_execution_start(execution_id).should_launch is False

    recovered = store.recover_incomplete_launch(execution_id)
    retry = store.claim_execution_start(execution_id)

    assert recovered.state == "failed"
    assert recovered.recovery_required is True
    assert retry.should_launch is False
    assert retry.recovery_required is True
    with store.transaction() as database:
        result = database.execute(
            "SELECT result_json FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()[0]
    assert json.loads(result)["error"]["code"] == "RECOVERY_REQUIRED"


def test_newer_owned_database_version_fails_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = StateStore.open(paths)
    with store.transaction(write=True) as database:
        database.execute(
            "INSERT INTO schema_migrations(version, applied_at_utc) VALUES (?, ?)",
            (2, "2026-08-21T00:00:00Z"),
        )

    with pytest.raises(StateError) as captured:
        StateStore.open(paths)

    assert captured.value.code == "DATABASE_VERSION_UNSUPPORTED"
    with sqlite3.connect(paths.database_file) as database:
        assert database.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2


def test_active_writer_lease_is_unique_until_released(tmp_path: Path) -> None:
    store = StateStore.open(_paths(tmp_path))
    execution_id = _claim(store).execution_id
    now = "2026-08-21T00:00:00Z"

    with store.transaction(write=True) as database:
        database.execute(
            """
            INSERT INTO leases(
                lease_id, resource_key, execution_id, kind,
                acquired_at_utc, expires_at_utc, released_at_utc
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """,
            ("lease-1", "workspace:one", execution_id, "writer", now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(write=True) as database:
            database.execute(
                """
                INSERT INTO leases(
                    lease_id, resource_key, execution_id, kind,
                    acquired_at_utc, expires_at_utc, released_at_utc
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                """,
                ("lease-2", "workspace:one", execution_id, "writer", now),
            )
    with store.transaction(write=True) as database:
        database.execute(
            "UPDATE leases SET released_at_utc = ? WHERE lease_id = ?",
            (now, "lease-1"),
        )
        database.execute(
            """
            INSERT INTO leases(
                lease_id, resource_key, execution_id, kind,
                acquired_at_utc, expires_at_utc, released_at_utc
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """,
            ("lease-2", "workspace:one", execution_id, "writer", now),
        )


def test_lifecycle_identity_events_and_terminal_result_are_atomic_and_deduplicated(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))
    execution_id = _claim(store).execution_id
    assert store.claim_execution_start(execution_id).should_launch is True

    running = store.bind_execution(
        execution_id=execution_id,
        external_session_id="native-session-1",
        external_execution_id="native-execution-1",
        workspace_key="workspace:one",
        descriptor={"runtime_id": "future-harness", "model_display_name": "future/model"},
        observed={"model": "future/model", "workspace": "workspace:one"},
    )
    completed = store.transition_execution(
        execution_id=execution_id,
        execution_state="succeeded",
        conversation_state="idle",
        observed={"model": "future/model", "workspace": "workspace:one"},
        result={"text": "first final"},
        event_kind="completed",
        event_payload={"result": {"text": "first final"}},
    )
    duplicate = store.transition_execution(
        execution_id=execution_id,
        execution_state="succeeded",
        conversation_state="idle",
        observed={"model": "future/model", "workspace": "workspace:one"},
        result={"text": "late duplicate must lose"},
        event_kind="completed",
        event_payload={"result": {"text": "late duplicate must lose"}},
    )

    events = store.load_events(execution_id, after_cursor=0)
    loaded = store.load_execution(execution_id)
    latest = store.load_latest_execution(running.conversation_id)
    assert running.execution_state == "running"
    assert completed.result == {"text": "first final"}
    assert duplicate.result == {"text": "first final"}
    assert loaded == latest == duplicate
    assert [event.cursor for event in events] == [1, 2]
    assert [event.kind for event in events] == ["started", "completed"]


def test_writer_lease_helpers_fail_closed_and_release_on_owner_request(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))
    first_execution = _claim(store).execution_id
    second_execution = _claim(
        store,
        request_id="request-2",
        conversation_id="conversation-2",
        execution_id="execution-2",
    ).execution_id

    store.acquire_writer_lease(
        lease_id="lease-1",
        resource_key="workspace:one",
        execution_id=first_execution,
    )
    with pytest.raises(StateError) as captured:
        store.acquire_writer_lease(
            lease_id="lease-2",
            resource_key="workspace:one",
            execution_id=second_execution,
        )
    store.release_execution_leases(first_execution)
    store.acquire_writer_lease(
        lease_id="lease-2",
        resource_key="workspace:one",
        execution_id=second_execution,
    )

    assert captured.value.code == "WORKSPACE_BUSY"


def test_action_request_response_is_replayed_without_repeating_side_effect(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))
    execution_id = _claim(store).execution_id
    payload = {"conversation_id": "conversation-1"}

    first = store.claim_action_request(
        tool="agent_interrupt",
        request_id="interrupt-1",
        request_payload=payload,
        conversation_id="conversation-1",
        execution_id=execution_id,
    )
    store.save_request_response(
        tool="agent_interrupt",
        request_id="interrupt-1",
        response={"execution_state": "interrupted"},
    )
    replay = store.claim_action_request(
        tool="agent_interrupt",
        request_id="interrupt-1",
        request_payload=payload,
        conversation_id="conversation-1",
        execution_id=execution_id,
    )

    assert first.created is True
    assert first.response is None
    assert replay.created is False
    assert replay.response == {"execution_state": "interrupted"}


def test_close_refuses_active_execution_and_preserves_native_session(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))
    execution_id = _claim(store).execution_id
    store.claim_execution_start(execution_id)
    store.bind_execution(
        execution_id=execution_id,
        external_session_id="native-session-keep",
        external_execution_id="native-execution-1",
        workspace_key="workspace:one",
        descriptor={"runtime_id": "future-harness"},
        observed={},
    )

    with pytest.raises(StateError) as active:
        store.close_conversation("conversation-1")
    store.transition_execution(
        execution_id=execution_id,
        execution_state="interrupted",
        conversation_state="idle",
        observed={},
        result={"error": {"code": "INTERRUPTED"}},
        event_kind="interrupted",
        event_payload={},
    )
    closed = store.close_conversation("conversation-1")

    assert active.value.code == "SESSION_BUSY"
    assert closed.conversation_state == "closed"
    assert closed.external_session_id == "native-session-keep"


def test_ready_circuit_pauses_with_cas_and_only_fresh_canary_can_probe(
    tmp_path: Path,
) -> None:
    store = StateStore.open(_paths(tmp_path))
    circuit = store.ensure_circuit_pair(
        runtime_id="claude-code",
        variant_id="future-deep",
        pair_key="a" * 64,
        details={"base_pair_key": "b" * 64},
    )
    claimed = store.claim_canary_request(
        request_id="canary-initial",
        request_payload={"pair_key": "a" * 64},
        runtime_id="claude-code",
        variant_id="future-deep",
        pair_key="a" * 64,
    )
    ready = store.complete_canary(
        runtime_id="claude-code",
        variant_id="future-deep",
        pair_key="a" * 64,
        expected_revision=claimed.revision,
        state="ready",
        details={"is_using_overage": False},
    )

    paused = store.pause_ready_circuit(
        runtime_id="claude-code",
        variant_id="future-deep",
        pair_key=ready.pair_key,
        expected_revision=ready.revision,
        error_code="QUOTA_PAUSED",
    )
    replay = store.claim_canary_request(
        request_id="canary-initial",
        request_payload={"pair_key": "a" * 64},
        runtime_id="claude-code",
        variant_id="future-deep",
        pair_key="a" * 64,
    )
    recovery = store.claim_canary_request(
        request_id="canary-recovery",
        request_payload={"pair_key": "a" * 64},
        runtime_id="claude-code",
        variant_id="future-deep",
        pair_key="a" * 64,
    )

    assert circuit.state == "needs_canary"
    assert paused.state == "auto_paused"
    assert paused.details["error_code"] == "QUOTA_PAUSED"
    assert replay.created is False
    assert recovery.created is True
    assert recovery.state == "probing"
