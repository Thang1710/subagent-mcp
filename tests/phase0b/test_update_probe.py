from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading
import time

import pytest

from spikes.phase0a.fixtures import validate_fixture
from spikes.phase0b.update_probe import (
    HealthCheckFailed,
    ManagedRestartDecision,
    ProcessIdentity,
    PublicRosterAggregate,
    PublicRosterRow,
    RuntimeRecord,
    SimulatedCrash,
    StateQuarantined,
    load_restart_state,
    managed_restart,
    read_pointer,
    recover_pointer,
    rollback_pointer,
    switch_pointer,
    terminate_if_same_process,
    visible_restart,
)


def _runtime(
    tmp_path: Path,
    name: str,
    version: str,
    manifest: bytes,
) -> tuple[Path, RuntimeRecord]:
    runtime_parent = tmp_path / "runtimes"
    root = runtime_parent / name
    root.mkdir(parents=True)
    (root / "manifest.json").write_bytes(manifest)
    return runtime_parent, RuntimeRecord(
        version=version,
        root=root,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
    )


def _initialize_pointer(
    tmp_path: Path,
) -> tuple[Path, Path, RuntimeRecord, RuntimeRecord]:
    runtime_parent, old = _runtime(tmp_path, "runtime-1", "1.0.0", b"old-manifest\n")
    _, candidate = _runtime(tmp_path, "runtime-2", "2.0.0", b"new-manifest\n")
    pointer = tmp_path / "current.json"
    assert switch_pointer(
        pointer,
        old,
        runtime_parent=runtime_parent,
        health_ok=True,
    ) is None
    return pointer, runtime_parent, old, candidate


def test_runtime_and_process_records_are_immutable_and_validate_identity(
    tmp_path: Path,
) -> None:
    runtime_parent, runtime = _runtime(
        tmp_path,
        "runtime-1",
        "1.0.0",
        b"manifest\n",
    )
    runtime.validate(runtime_parent)
    process = ProcessIdentity(
        pid=17,
        creation_identity="created-001",
        executable_sha256="b" * 64,
    )
    process.validate()

    with pytest.raises(FrozenInstanceError):
        runtime.version = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        process.pid = 18  # type: ignore[misc]

    with pytest.raises(ValueError, match="manifest"):
        RuntimeRecord("1.0.0", runtime.root, "0" * 64).validate(runtime_parent)
    with pytest.raises(ValueError, match="direct child"):
        RuntimeRecord(
            "1.0.0",
            runtime.root,
            runtime.manifest_sha256,
        ).validate(tmp_path)
    with pytest.raises(ValueError, match="process identity"):
        ProcessIdentity(True, "created-001", "b" * 64).validate()


def test_switch_is_durable_and_rollback_restores_exact_prior_pointer(
    tmp_path: Path,
) -> None:
    pointer, runtime_parent, old, candidate = _initialize_pointer(tmp_path)
    old_payload = json.loads(pointer.read_text(encoding="utf-8"))
    old_payload["future_metadata"] = {"preserve": True}
    pointer.write_text(
        json.dumps(old_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    old_bytes = pointer.read_bytes()

    snapshot = switch_pointer(
        pointer,
        candidate,
        runtime_parent=runtime_parent,
        health_ok=True,
    )

    assert snapshot is not None
    assert read_pointer(pointer, runtime_parent) == candidate
    rollback_pointer(pointer, snapshot, runtime_parent=runtime_parent)
    assert pointer.read_bytes() == old_bytes
    assert read_pointer(pointer, runtime_parent) == old


def test_failed_health_leaves_old_pointer_byte_exact(tmp_path: Path) -> None:
    pointer, runtime_parent, old, candidate = _initialize_pointer(tmp_path)
    old_bytes = pointer.read_bytes()

    with pytest.raises(HealthCheckFailed, match="health"):
        switch_pointer(
            pointer,
            candidate,
            runtime_parent=runtime_parent,
            health_ok=False,
        )

    assert pointer.read_bytes() == old_bytes
    assert read_pointer(pointer, runtime_parent) == old
    assert candidate.root.is_dir()
    assert not pointer.with_name(pointer.name + ".pending").exists()


def test_locked_old_runtime_remains_readable_after_pointer_switch(
    tmp_path: Path,
) -> None:
    pointer, runtime_parent, old, candidate = _initialize_pointer(tmp_path)
    old_manifest = old.root / "manifest.json"

    with old_manifest.open("rb") as locked_reader:
        snapshot = switch_pointer(
            pointer,
            candidate,
            runtime_parent=runtime_parent,
            health_ok=True,
        )
        assert snapshot is not None
        assert read_pointer(pointer, runtime_parent) == candidate
        assert locked_reader.read() == b"old-manifest\n"

    assert old_manifest.read_bytes() == b"old-manifest\n"


def test_crash_after_durable_stage_recovers_without_changing_current(
    tmp_path: Path,
) -> None:
    pointer, runtime_parent, old, candidate = _initialize_pointer(tmp_path)
    old_bytes = pointer.read_bytes()
    quarantine = tmp_path / "quarantine"

    with pytest.raises(SimulatedCrash, match="staged"):
        switch_pointer(
            pointer,
            candidate,
            runtime_parent=runtime_parent,
            health_ok=True,
            crash_after_stage=True,
        )

    pending = pointer.with_name(pointer.name + ".pending")
    assert pending.is_file()
    recovered = recover_pointer(
        pointer,
        runtime_parent=runtime_parent,
        quarantine_dir=quarantine,
    )

    assert recovered == old
    assert pointer.read_bytes() == old_bytes
    assert not pending.exists()
    assert [path.name for path in quarantine.iterdir()] == ["current.json.pending.quarantine"]
    assert candidate.root.is_dir()


def test_corrupt_state_is_quarantined_without_deleting_prior_runtime(
    tmp_path: Path,
) -> None:
    pointer, runtime_parent, old, _candidate = _initialize_pointer(tmp_path)
    state = tmp_path / "restart-state.json"
    quarantine = tmp_path / "state-quarantine"
    state.write_text('{"schema_version":1,"executions":', encoding="utf-8")

    with pytest.raises(StateQuarantined, match="quarantined"):
        load_restart_state(state, quarantine_dir=quarantine)

    assert not state.exists()
    assert (quarantine / "restart-state.json.quarantine").is_file()
    assert old.root.is_dir()
    assert read_pointer(pointer, runtime_parent) == old


def test_additive_state_is_preserved_and_managed_restart_is_idempotent(
    tmp_path: Path,
) -> None:
    state = tmp_path / "restart-state.json"
    quarantine = tmp_path / "quarantine"

    first = managed_restart(
        state,
        request_key="request-a",
        new_execution_key="execution-a",
        new_native_resume_key="resume-a",
        quarantine_dir=quarantine,
    )
    assert first == ManagedRestartDecision(
        action="start",
        execution_key="execution-a",
        native_resume_key="resume-a",
    )

    persisted = json.loads(state.read_text(encoding="utf-8"))
    persisted["future_top_level"] = {"kept": True}
    persisted["executions"][0]["future_execution_field"] = ["kept"]
    state.write_text(
        json.dumps(persisted, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    before_restart = state.read_bytes()

    restarted = managed_restart(
        state,
        request_key="request-a",
        new_execution_key="must-not-start",
        new_native_resume_key="must-not-use",
        quarantine_dir=quarantine,
    )

    assert restarted == ManagedRestartDecision(
        action="resume",
        execution_key="execution-a",
        native_resume_key="resume-a",
    )
    assert state.read_bytes() == before_restart
    reloaded = load_restart_state(state, quarantine_dir=quarantine)
    assert reloaded["future_top_level"] == {"kept": True}
    assert reloaded["executions"][0]["future_execution_field"] == ["kept"]
    assert len(reloaded["executions"]) == 1


def test_concurrent_same_request_starts_once_and_persists_one_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "restart-state.json"
    quarantine = tmp_path / "quarantine"
    import spikes.phase0b.update_probe as update_probe

    original_load = update_probe.load_restart_state
    observation_lock = threading.Lock()
    active_loads = 0
    max_active_loads = 0

    def observed_load(path: Path, *, quarantine_dir: Path) -> dict[str, object]:
        nonlocal active_loads, max_active_loads
        with observation_lock:
            active_loads += 1
            max_active_loads = max(max_active_loads, active_loads)
        try:
            time.sleep(0.05)
            return original_load(path, quarantine_dir=quarantine_dir)
        finally:
            with observation_lock:
                active_loads -= 1

    monkeypatch.setattr(update_probe, "load_restart_state", observed_load)

    def claim(index: int) -> ManagedRestartDecision:
        return managed_restart(
            state,
            request_key="same-request",
            new_execution_key=f"execution-{index}",
            new_native_resume_key=f"resume-{index}",
            quarantine_dir=quarantine,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(claim, (1, 2)))

    assert max_active_loads == 1
    assert sorted(decision.action for decision in decisions) == ["resume", "start"]
    started = next(decision for decision in decisions if decision.action == "start")
    resumed = next(decision for decision in decisions if decision.action == "resume")
    assert resumed.execution_key == started.execution_key
    assert resumed.native_resume_key == started.native_resume_key
    persisted = load_restart_state(state, quarantine_dir=quarantine)
    assert len(persisted["executions"]) == 1
    assert persisted["executions"][0]["request_key"] == "same-request"


def test_pid_reuse_mismatch_is_never_targeted() -> None:
    expected = ProcessIdentity(42, "created-original", "a" * 64)
    reused = ProcessIdentity(42, "created-reused", "a" * 64)
    targeted: list[int] = []

    assert terminate_if_same_process(expected, reused, targeted.append) is False
    assert targeted == []

    exact = ProcessIdentity(42, "created-original", "a" * 64)
    assert terminate_if_same_process(expected, exact, targeted.append) is True
    assert targeted == [42]


def test_visible_restart_reattaches_only_from_supplied_public_roster() -> None:
    owned = "c" * 64
    public = PublicRosterAggregate(
        source="claude_agents_json_all",
        rows=(
            PublicRosterRow(
                ownership_digest=owned,
                status="working",
                native_session_present=True,
                workspace_equal=True,
            ),
        ),
    )

    assert visible_restart(owned, public) == "reattach"
    assert visible_restart(
        owned,
        PublicRosterAggregate(source="claude_agents_json_all", rows=()),
    ) == "recovery_required"
    with pytest.raises(ValueError, match="public roster"):
        visible_restart(
            owned,
            PublicRosterAggregate(source="private_daemon", rows=public.rows),
        )


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_update_fixture_is_deterministic_and_contains_only_sanitized_aggregates() -> None:
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "phase0b"
        / "current"
        / "update-simulation.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    validate_fixture(fixture)
    payload_bytes = json.dumps(
        fixture["payload"],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert fixture["source"] == {
        "kind": "deterministic_tmp_path_simulation",
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
    assert fixture["payload"]["restart_rollback"] == "pass"
    assert all(fixture["payload"]["checks"].values())

    forbidden_keys = {
        "pid",
        "session_id",
        "native_session_id",
        "raw",
        "raw_output",
        "transcript",
    }
    for value in _walk(fixture):
        if isinstance(value, str):
            assert value.casefold() not in forbidden_keys
            assert not PurePosixPath(value).is_absolute()
            assert not PureWindowsPath(value).is_absolute()
