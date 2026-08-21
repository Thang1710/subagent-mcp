from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from spikes.phase0a import live_background
from spikes.phase0a.core import run_argv, write_json_atomic
from spikes.phase0a.fixtures import fixture_envelope, validate_fixture
from spikes.phase0a.live_background import (
    AttachObservation,
    BackgroundObservation,
    ConcurrencyObservation,
    ContextPrerequisite,
    LiveGateError,
    NeedsInputObservation,
    OwnedRosterRow,
    build_concurrency_argv,
    build_group_d_scope,
    build_group_f_scope,
    build_group_c_scope,
    build_needs_input_argv,
    load_context_prerequisite,
    load_model_group_circuit,
    open_model_group_circuit,
    parse_owned_roster,
    pretool_guard,
    project_background_matrix,
    project_background_result,
    require_model_groups_available,
    run_concurrency_canary,
    run_needs_input_canary,
    run_write_race_canary,
)
from spikes.phase0a.live_common import BoundCliIdentity, approval_digest, _verify_private_path
from spikes.phase0a.live_context import ContextPaths, build_context_argv, build_context_scope
from spikes.phase0a.live_host import write_bound_host_identity


_PROOF = "phase0a-proof.txt"
_READY = b"ready\n"
_EXECUTION_ID = "a" * 32


def _row(tmp_path: Path, *, state: str = "working") -> OwnedRosterRow:
    return OwnedRosterRow(
        short_id="short_1",
        session_id="session_1",
        name="subagent-harness-mcp-phase0a-c-test",
        cwd=(tmp_path / "worktrees" / "phase0a-c-test").resolve(),
        state=state,
        model="claude-sonnet-5",
        context_fingerprint="context-v1",
        pid_present=state == "working",
    )


def _observation(
    tmp_path: Path,
    *,
    state: str = "working",
    changed_paths: tuple[str, ...] = (_PROOF,),
    proof_content: bytes | None = _READY,
    stop_event_count: int = 0,
    row: OwnedRosterRow | None = None,
) -> BackgroundObservation:
    owned = (
        _row(tmp_path, state=state)
        if row is None
        else replace(row, state=state, pid_present=state == "working")
    )
    common = (tmp_path / "repo" / ".git").resolve()
    return BackgroundObservation(
        row=owned,
        session_start_observed=True,
        worktree_create_observed=True,
        lease_path=owned.cwd,
        event_path=owned.cwd,
        handoff_path=owned.cwd,
        guard_cwd=owned.cwd,
        roster_path=owned.cwd,
        repository_common_dir=common,
        worktree_common_dir=common,
        event_order=("lease", "WorktreeCreate", "handler_stdout", "first_write"),
        first_write_after_handoff=True,
        remote_count=0,
        base_commit="a" * 40,
        current_commit="a" * 40,
        changed_paths=changed_paths,
        proof_content=proof_content,
        stop_event_count=stop_event_count,
        stop_failure_category=None,
    )


class FakeBackground:
    def __init__(self, observations):
        self.observations = list(observations)
        self.actions: list[object] = []

    def launch(self):
        self.actions.append("launch")

    def observe(self, stage: str):
        self.actions.append(("observe", stage))
        value = self.observations.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def stop(self, row: OwnedRosterRow):
        self.actions.append(("stop", row.short_id))

    def stabilize(self, seconds: float):
        self.actions.append(("stabilize", seconds))

    def respawn(self, row: OwnedRosterRow):
        self.actions.append(("respawn", row.short_id))

    def delete_proof(self, row: OwnedRosterRow):
        self.actions.append(("delete", row.short_id))

    def recover_timeout(self, row: OwnedRosterRow | None):
        self.actions.append(("recover", None if row is None else row.short_id))

    def recover_failure(self, row: OwnedRosterRow | None):
        self.actions.append(("recover-failure", None if row is None else row.short_id))


def _happy_observations(tmp_path: Path) -> list[BackgroundObservation]:
    return [
        _observation(tmp_path, state="working"),
        _observation(tmp_path, state="stopped", stop_event_count=1),
        _observation(tmp_path, state="stopped", stop_event_count=1),
        _observation(tmp_path, state="working", stop_event_count=1),
        _observation(tmp_path, state="done", stop_event_count=2),
        _observation(
            tmp_path,
            state="done",
            changed_paths=(),
            proof_content=None,
            stop_event_count=2,
        ),
    ]


def test_worktree_event_and_lease_precede_first_write(tmp_path: Path):
    fake = FakeBackground(_happy_observations(tmp_path))

    result = run_write_race_canary(fake)

    assert result.event_order[:3] == ("lease", "WorktreeCreate", "handler_stdout")
    assert result.first_write_after_handoff is True
    assert result.active_stop_stable_observations == 2
    assert result.provider_launch_count == 2
    assert result.stop_respawn_action_count == 2
    assert fake.actions == [
        "launch",
        ("observe", "working"),
        ("stop", "short_1"),
        ("observe", "stopped-1"),
        ("stabilize", 0.75),
        ("observe", "stopped-2"),
        ("respawn", "short_1"),
        ("observe", "respawn-working"),
        ("observe", "done"),
        ("delete", "short_1"),
        ("observe", "clean"),
    ]


def test_active_stop_requires_two_stable_observations(tmp_path: Path):
    fake = FakeBackground([
        _observation(tmp_path, state="working"),
        _observation(tmp_path, state="stopped", stop_event_count=1),
        _observation(tmp_path, state="working", stop_event_count=1),
    ])

    with pytest.raises(LiveGateError, match="stop not stable"):
        run_write_race_canary(fake)

    assert not any(action == ("respawn", "short_1") for action in fake.actions)


def test_stopped_state_requires_worker_pid_to_be_absent(tmp_path: Path):
    observations = _happy_observations(tmp_path)
    observations[1] = replace(
        observations[1],
        row=replace(observations[1].row, pid_present=True),
    )

    with pytest.raises(LiveGateError, match="stopped row still has a live PID"):
        run_write_race_canary(FakeBackground(observations))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event_path", Path("different"), "handoff paths disagree"),
        ("lease_path", Path("different-lease"), "handoff paths disagree"),
        ("guard_cwd", Path("different-guard"), "handoff paths disagree"),
        ("roster_path", Path("different-roster"), "handoff paths disagree"),
        ("worktree_common_dir", Path("different-common"), "common-dir mismatch"),
        ("remote_count", 1, "remote"),
        ("current_commit", "b" * 40, "unexpected commit"),
        ("changed_paths", ("other.txt",), "unexpected worktree change"),
        ("proof_content", b"wrong\n", "proof content mismatch"),
        ("session_start_observed", False, "SessionStart"),
        ("worktree_create_observed", False, "WorktreeCreate"),
        ("first_write_after_handoff", False, "first write preceded handoff"),
    ],
)
def test_initial_handoff_and_proof_fail_closed(
    tmp_path: Path, field: str, value: object, message: str,
):
    initial = replace(_observation(tmp_path), **{field: value})
    fake = FakeBackground([initial])

    with pytest.raises(LiveGateError, match=message):
        run_write_race_canary(fake)


def test_respawn_requires_same_owned_identity(tmp_path: Path):
    observations = _happy_observations(tmp_path)
    observations[3] = replace(
        observations[3],
        row=replace(observations[3].row, session_id="different_session"),
    )

    with pytest.raises(LiveGateError, match="respawn identity mismatch"):
        run_write_race_canary(FakeBackground(observations))


def test_final_done_requires_new_stop_hook(tmp_path: Path):
    observations = _happy_observations(tmp_path)
    observations[4] = replace(observations[4], stop_event_count=1)

    with pytest.raises(LiveGateError, match="missing final Stop"):
        run_write_race_canary(FakeBackground(observations))


def test_quota_pause_never_respawns_or_deletes(tmp_path: Path):
    initial = replace(_observation(tmp_path), stop_failure_category="rate_limit")
    fake = FakeBackground([initial])

    with pytest.raises(LiveGateError, match="QUOTA_PAUSED"):
        run_write_race_canary(fake)

    assert not any(isinstance(action, tuple) and action[0] in {"respawn", "delete"} for action in fake.actions)
    assert ("recover-failure", "short_1") in fake.actions


def test_timeout_uses_recovery_path_and_retains_proof(tmp_path: Path):
    fake = FakeBackground([
        _observation(tmp_path, state="working"),
        TimeoutError("poll expired"),
    ])

    with pytest.raises(LiveGateError, match="RECOVERY_REQUIRED"):
        run_write_race_canary(fake)

    assert ("recover", "short_1") in fake.actions
    assert not any(isinstance(action, tuple) and action[0] == "delete" for action in fake.actions)


def test_non_timeout_failure_attempts_bounded_recovery_and_retains_proof(tmp_path: Path):
    fake = FakeBackground([
        replace(_observation(tmp_path), proof_content=b"wrong\n"),
    ])

    with pytest.raises(LiveGateError, match="proof content mismatch"):
        run_write_race_canary(fake)

    assert ("recover-failure", "short_1") in fake.actions
    assert not any(isinstance(action, tuple) and action[0] == "delete" for action in fake.actions)


def test_parse_owned_roster_keeps_opaque_values_only_in_memory(tmp_path: Path):
    cwd = str((tmp_path / "worktree").resolve())
    row = parse_owned_roster([{
        "id": "short_1",
        "sessionId": "session_1",
        "name": "group-c",
        "cwd": cwd,
        "kind": "background",
        "state": "working",
        "model": "claude-sonnet-5",
        "contextFingerprint": "context-v1",
        "pid": 123,
        "future": {"ignored": True},
    }], "group-c")

    assert row.short_id == "short_1"
    assert row.session_id == "session_1"
    assert row.cwd == Path(cwd)
    assert row.pid_present is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "missing owned background row"),
        ([{"name": "group-c"}, {"name": "group-c"}], "duplicate owned background row"),
        ([{"name": "group-c", "id": 1}], "id"),
        ([{
            "name": "group-c", "id": "short", "sessionId": "session",
            "cwd": "relative", "kind": "background", "state": "working",
        }], "absolute"),
        ([{
            "name": "group-c", "id": "short", "sessionId": "session",
            "cwd": "C:/repo", "kind": "background", "state": "future_state",
            "model": "claude-sonnet-5", "contextFingerprint": "context-v1",
        }], "unknown background state"),
        ([{
            "name": "group-c", "id": "short", "sessionId": "session",
            "cwd": "C:/repo", "kind": "background", "state": "working",
            "contextFingerprint": "context-v1",
        }], "model"),
        ([{
            "name": "group-c", "id": "short", "sessionId": "session",
            "cwd": "C:/repo", "kind": "background", "state": "working",
            "model": "claude-sonnet-5",
        }], "contextFingerprint"),
        ([{
            "name": "group-c", "id": "short", "sessionId": "session",
            "cwd": "C:/repo", "kind": "background", "state": "blocked",
            "model": "claude-sonnet-5", "contextFingerprint": "context-v1",
            "waitingFor": {"unsafe": True},
        }], "waitingFor"),
    ],
)
def test_parse_owned_roster_rejects_missing_duplicate_or_unknown_schema(payload, message):
    with pytest.raises(LiveGateError, match=message):
        parse_owned_roster(payload, "group-c")


def test_recovery_can_resolve_unique_owned_id_even_when_state_schema_is_unknown():
    payload = [{
        "id": "short_recovery",
        "name": "group-d",
        "kind": "background",
        "state": "future_state",
    }]

    assert live_background._recovery_short_id_from_payload(
        payload, "group-d",
    ) == "short_recovery"
    assert live_background._recovery_short_id_from_payload(
        payload + [dict(payload[0])], "group-d",
    ) is None


def _guard_files(tmp_path: Path):
    worktree_root = tmp_path / "worktrees"
    cwd = worktree_root / "phase0a-c-test"
    cwd.mkdir(parents=True)
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    lease = tmp_path / "lease.json"
    events = tmp_path / "events.jsonl"
    ack = tmp_path / "guard.jsonl"
    lease.write_text(json.dumps({
        "execution_id": _EXECUTION_ID,
        "repository_common_dir": str(common.resolve()),
        "worktree_path": str(cwd.resolve()),
        "status": "leased",
    }), encoding="utf-8")
    events.write_text(json.dumps({
        "hook_event_name": "WorktreeCreate",
        "execution_id": _EXECUTION_ID,
        "worktree_path": str(cwd.resolve()),
    }) + "\n", encoding="utf-8")
    return worktree_root, cwd, common, lease, events, ack


@pytest.mark.parametrize("payload", [
    {"hook_event_name": "PreToolUse", "cwd": "{cwd}", "tool_name": "Read", "tool_input": {"file_path": _PROOF}},
    {"hook_event_name": "PreToolUse", "cwd": "{cwd}", "tool_name": "Write", "tool_input": {"file_path": _PROOF, "content": "ready\n"}},
    {"hook_event_name": "PreToolUse", "cwd": "{cwd}", "tool_name": "Bash", "tool_input": {"command": "sleep 30"}},
])
def test_pretool_guard_allows_only_exact_inputs_after_handoff(tmp_path: Path, payload):
    worktree_root, cwd, common, lease, events, ack = _guard_files(tmp_path)
    payload = dict(payload)
    payload["cwd"] = str(cwd.resolve())

    assert pretool_guard(
        payload,
        lease_ack=lease,
        event_log=events,
        guard_ack=ack,
        worktree_root=worktree_root,
        execution_id=_EXECUTION_ID,
        proof_relative=_PROOF,
        common_dir_resolver=lambda _cwd: common.resolve(),
    ) is True

    recorded = json.loads(ack.read_text(encoding="utf-8").splitlines()[-1])
    assert recorded["allowed"] is True
    assert recorded["cwd"] == str(cwd.resolve())


@pytest.mark.parametrize("tool_name,tool_input", [
    ("Write", {"file_path": "other.txt", "content": "ready\n"}),
    ("Write", {"file_path": _PROOF, "content": "wrong\n"}),
    ("Read", {"file_path": _PROOF, "offset": 1}),
    ("Bash", {"command": "sleep 31"}),
    ("Agent", {}),
])
def test_pretool_guard_denies_every_other_input(tmp_path: Path, tool_name: str, tool_input: dict):
    worktree_root, cwd, common, lease, events, ack = _guard_files(tmp_path)
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd.resolve()),
        "tool_name": tool_name,
        "tool_input": tool_input,
    }

    with pytest.raises(LiveGateError, match="tool input denied"):
        pretool_guard(
            payload,
            lease_ack=lease,
            event_log=events,
            guard_ack=ack,
            worktree_root=worktree_root,
            execution_id=_EXECUTION_ID,
            proof_relative=_PROOF,
            common_dir_resolver=lambda _cwd: common.resolve(),
        )


def test_pretool_guard_denies_before_durable_lease(tmp_path: Path):
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()

    with pytest.raises(LiveGateError, match="lease unavailable"):
        pretool_guard(
            {"hook_event_name": "PreToolUse"},
            lease_ack=tmp_path / "missing.json",
            event_log=tmp_path / "events.jsonl",
            guard_ack=tmp_path / "guard.jsonl",
            worktree_root=worktree_root,
            execution_id=_EXECUTION_ID,
            proof_relative=_PROOF,
        )


def test_group_c_scope_binds_exact_counters_and_dynamic_lifecycle_id(tmp_path: Path):
    launch = ("claude.exe", "--bg", "prompt")
    worktree_hook = ("python.exe", "worktree_hook.py", "--execution-id", "execution-1")
    proof = str((tmp_path / _PROOF).resolve())
    scope = build_group_c_scope(
        git_head="a" * 40,
        cli_sha256="b" * 64,
        executable_manifest_sha256="c" * 64,
        launch_argv=launch,
        worktree_hook_argv=worktree_hook,
        proof_path=proof,
        exact_targets=(proof,),
    )

    assert scope.max_provider_session_launches == 2
    assert scope.max_worktree_creates == 1
    assert scope.max_stop_respawn_actions == 3
    assert scope.max_file_deletes == 1
    assert scope.max_removals == 0
    assert scope.background_internal_requests_acknowledged is True
    effects = {effect.kind: effect for effect in scope.side_effects}
    assert set(effects) == {"provider_launch", "worktree_create", "stop", "respawn", "file_delete"}
    assert effects["provider_launch"].argv_template == launch
    assert effects["stop"].max_uses == 2
    assert effects["respawn"].max_uses == 1
    for kind in ("stop", "respawn"):
        binding = effects[kind].bindings[0]
        assert binding.state_key == "group.short_id"
        assert binding.pattern == r"^[A-Za-z0-9_-]{1,64}$"
        assert binding.require_group_owned is True


def test_public_background_projection_contains_no_opaque_identity_or_path(tmp_path: Path):
    result = run_write_race_canary(FakeBackground(_happy_observations(tmp_path)))
    prerequisite = ContextPrerequisite(
        observed_cli_version="2.1.224 (Claude Code)",
        source_sha256=hashlib.sha256(b"context").hexdigest(),
        candidate_sha256=hashlib.sha256(b"candidate").hexdigest(),
        scope_sha256=hashlib.sha256(b"scope").hexdigest(),
        missing_fields=("effective_setting_sources",),
    )

    projection = project_background_result(result, prerequisite)
    serialized = json.dumps(projection, sort_keys=True)

    assert "short_1" not in serialized
    assert "session_1" not in serialized
    assert str(tmp_path.resolve()) not in serialized
    assert projection["requested_model"] == "claude-sonnet-5"
    assert projection["requested_effort"] == "low"
    assert projection["requested_setting_sources"] == "user,project,local"
    assert projection["requested_auto_compaction_window_tokens"] == 274000
    assert projection["requested_auto_compaction_trigger_tokens"] == 274000
    assert projection["context_delta_fields"] == ["permission_mode", "tools"]
    fixture = fixture_envelope(
        kind="live_background_lifecycle",
        observed_cli_version=prerequisite.observed_cli_version,
        source_kind="bounded_background_projection",
        source_sha256=hashlib.sha256(b"source").hexdigest(),
        payload=projection,
        observed=sorted(projection),
        missing=list(prerequisite.missing_fields),
    )
    validate_fixture(fixture)


def _write_context_prerequisite(root: Path, cli: Path, identity: BoundCliIdentity) -> None:
    root.mkdir(parents=True)
    repo = root / "repo"
    repo.mkdir()
    settings = root / "settings.json"
    empty_mcp = root / "declared-empty.json"
    settings.write_text("{}\n", encoding="utf-8")
    write_json_atomic(empty_mcp, {"mcpServers": {}})
    paths = ContextPaths(
        cwd=repo,
        settings=settings,
        empty_mcp=empty_mcp,
        event_log=root / "events.jsonl",
    )
    argv = build_context_argv(cli, paths)
    scope = build_context_scope(
        git_head="a" * 40,
        cli_sha256=identity.sha256,
        executable_manifest_sha256="b" * 64,
        context_argv=argv,
        control_argv=None,
    )
    write_json_atomic(root / "pending-scope.json", json.loads(json.dumps(scope.to_dict())))
    write_json_atomic(root / "consumed-side-effects.json", [{
        "kind": "provider_launch",
        "argv": list(argv),
        "targets": [],
    }])
    missing = ["effective_setting_sources"]
    payload = {
        "cli_content_sha256": identity.sha256,
        "init_subset_status": "PASS",
        "requested_model": "claude-sonnet-5",
        "requested_effort": "low",
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_percent": None,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_model": "claude-sonnet-5",
        "tool_count": 0,
        "mcp_server_count": 0,
        "is_using_overage": False,
        "rate_statuses": ["allowed"],
        "final_marker_matched": True,
        "checkout_clean": True,
        "background_eligible": True,
        "usage_credits_off_confirmed": True,
        "hook_error_observed": False,
        "missing_fields": missing,
    }
    candidate = fixture_envelope(
        kind="live_context_attestation",
        observed_cli_version=identity.version,
        source_kind="live_context_projection",
        source_sha256=hashlib.sha256(b"context stream").hexdigest(),
        payload=payload,
        observed=sorted(payload),
        missing=missing,
    )
    write_json_atomic(root / "live-context-candidate.json", candidate)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    commands = (
        ["git", "-C", str(path), "init", "-b", "main"],
        ["git", "-C", str(path), "add", "README.md"],
        [
            "git", "-C", str(path),
            "-c", "user.name=Phase0a Test",
            "-c", "user.email=phase0a@example.invalid",
            "commit", "-m", "test fixture",
        ],
    )
    for index, argv in enumerate(commands):
        result = run_argv(f"git-{index}", argv, timeout_seconds=30)
        assert result.exit_code == 0 and not result.timed_out


def test_context_prerequisite_binds_candidate_pending_scope_and_consumed_launch(tmp_path: Path):
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake standalone cli")
    identity = BoundCliIdentity.capture(cli, version="2.1.224 (Claude Code)")
    context = tmp_path / "context"
    _write_context_prerequisite(context, cli, identity)

    prerequisite = load_context_prerequisite(context, cli=cli, bound_identity=identity)

    assert prerequisite.observed_cli_version == identity.version
    assert prerequisite.missing_fields == ("effective_setting_sources",)
    assert len(prerequisite.candidate_sha256) == 64
    assert len(prerequisite.scope_sha256) == 64


def test_group_c_contract_digest_binds_full_task4_candidate(tmp_path: Path):
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake standalone cli")
    identity = BoundCliIdentity.capture(cli, version="2.1.224 (Claude Code)")
    context = tmp_path / "context"
    _write_context_prerequisite(context, cli, identity)
    before = load_context_prerequisite(context, cli=cli, bound_identity=identity)

    candidate_path = context / "live-context-candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["payload"]["missing_fields"] = ["different_gap"]
    candidate["coverage"]["missing"] = ["different_gap"]
    write_json_atomic(candidate_path, candidate)
    after = load_context_prerequisite(context, cli=cli, bound_identity=identity)

    assert before.scope_sha256 == after.scope_sha256
    assert before.source_sha256 == after.source_sha256
    assert before.candidate_sha256 != after.candidate_sha256
    materialized = live_background.materialize_background(
        tmp_path / "background",
        cli=cli,
        python_exe=Path(sys.executable),
        hook_sink=Path(live_background.__file__).with_name("hook_sink.py"),
        worktree_hook=Path(live_background.__file__).with_name("worktree_hook.py"),
        bound_identity=identity,
    )
    common = {
        "python_exe": Path(sys.executable),
        "hook_sink": Path(live_background.__file__).with_name("hook_sink.py"),
        "worktree_hook": Path(live_background.__file__).with_name("worktree_hook.py"),
    }
    before_digest, _before_manifest, _before_contract = (
        live_background.build_background_execution_manifest(materialized, before, **common)
    )
    after_digest, _after_manifest, _after_contract = (
        live_background.build_background_execution_manifest(materialized, after, **common)
    )
    assert before_digest != after_digest


@pytest.mark.parametrize("drift", ["candidate", "pending", "ledger"])
def test_context_prerequisite_rejects_any_direct_binding_drift(tmp_path: Path, drift: str):
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake standalone cli")
    identity = BoundCliIdentity.capture(cli, version="2.1.224 (Claude Code)")
    context = tmp_path / "context"
    _write_context_prerequisite(context, cli, identity)
    if drift == "candidate":
        candidate = json.loads((context / "live-context-candidate.json").read_text(encoding="utf-8"))
        candidate["payload"]["background_eligible"] = False
        write_json_atomic(context / "live-context-candidate.json", candidate)
    elif drift == "pending":
        pending = json.loads((context / "pending-scope.json").read_text(encoding="utf-8"))
        pending["side_effects"][0]["argv_template"].remove("--strict-mcp-config")
        write_json_atomic(context / "pending-scope.json", pending)
    else:
        ledger = json.loads((context / "consumed-side-effects.json").read_text(encoding="utf-8"))
        ledger[0]["argv"][-1] = "different prompt"
        write_json_atomic(context / "consumed-side-effects.json", ledger)

    with pytest.raises(LiveGateError, match="Task 4"):
        load_context_prerequisite(context, cli=cli, bound_identity=identity)


def test_preview_group_c_builds_scope_without_invoking_fake_cli(tmp_path: Path):
    live_root = tmp_path / "live"
    group_root = live_root / "background-main"
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"not an executable")
    evidence = {
        "status": "ready",
        "identity_stable": True,
        "observed_cli_version": "2.1.224 (Claude Code)",
        "cli_content_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
    }
    identity_path = write_bound_host_identity(
        live_root / "host",
        cli,
        evidence,
        capabilities={
            "tools_empty_documented": True,
            "prompt_suggestions_false_documented": True,
            "stop_help_recognized": True,
            "respawn_help_recognized": True,
            "attach_help_recognized": True,
            "rm_help_recognized": True,
        },
    )
    identity = BoundCliIdentity.capture(cli, version=evidence["observed_cli_version"])
    assert identity_path == live_root / "host" / "bound-identity.json"
    _write_context_prerequisite(live_root / "context", cli, identity)
    project = tmp_path / "project"
    _init_repo(project)

    display = live_background.preview_background(
        group_root,
        cli=cli,
        project_root=project,
        python_exe=Path(sys.executable),
        hook_sink=Path(live_background.__file__).with_name("hook_sink.py"),
        worktree_hook=Path(live_background.__file__).with_name("worktree_hook.py"),
    )

    assert display["scope"]["max_provider_session_launches"] == 2
    assert display["scope"]["max_worktree_creates"] == 1
    assert (group_root / "pending-scope.json").is_file()
    assert not (group_root / "consumed-side-effects.json").exists()
    assert list(group_root.glob("phase0a-proof.txt")) == []


def test_task6_previews_build_independent_d_and_f_receipts_without_provider_call(
    tmp_path: Path,
):
    live_root = tmp_path / "live"
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"not an executable")
    evidence = {
        "status": "ready",
        "identity_stable": True,
        "observed_cli_version": "2.1.224 (Claude Code)",
        "cli_content_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
    }
    write_bound_host_identity(
        live_root / "host",
        cli,
        evidence,
        capabilities={
            "tools_empty_documented": True,
            "prompt_suggestions_false_documented": True,
            "stop_help_recognized": True,
            "respawn_help_recognized": True,
            "attach_help_recognized": True,
            "rm_help_recognized": True,
        },
    )
    identity = BoundCliIdentity.capture(cli, version=evidence["observed_cli_version"])
    _write_context_prerequisite(live_root / "context", cli, identity)
    project = tmp_path / "project"
    _init_repo(project)
    common = {
        "cli": cli,
        "project_root": project,
        "python_exe": Path(sys.executable),
        "hook_sink": Path(live_background.__file__).with_name("hook_sink.py"),
        "worktree_hook": Path(live_background.__file__).with_name("worktree_hook.py"),
    }

    group_d = live_background.preview_needs_input(
        live_root / "background-needs-input",
        include_attach=True,
        **common,
    )
    group_f = live_background.preview_concurrency(
        live_root / "background-concurrency",
        **common,
    )

    assert group_d["scope_sha256"] != group_f["scope_sha256"]
    assert group_d["scope"]["max_provider_session_launches"] == 1
    assert group_d["scope"]["max_worktree_creates"] == 1
    assert group_d["scope"]["max_attach_actions"] == 1
    d_launch = next(
        effect for effect in group_d["scope"]["side_effects"]
        if effect["kind"] == "provider_launch"
    )["argv_template"]
    assert d_launch[d_launch.index("--permission-mode") + 1] == "manual"
    assert d_launch.count("--worktree") == 1

    assert group_f["scope"]["max_provider_session_launches"] == 2
    assert group_f["scope"]["max_worktree_creates"] == 0
    assert group_f["scope"]["max_stop_respawn_actions"] == 2
    f_launch = next(
        effect for effect in group_f["scope"]["side_effects"]
        if effect["kind"] == "provider_launch"
    )["argv_template"]
    assert "--worktree" not in f_launch
    assert "{group_name}" in f_launch
    assert not (live_root / "background-needs-input" / "consumed-side-effects.json").exists()
    assert not (live_root / "background-concurrency" / "consumed-side-effects.json").exists()


def test_open_model_group_circuit_prevents_later_preview_before_materialization(
    tmp_path: Path,
):
    live_root = tmp_path / "live"
    live_root.mkdir()
    open_model_group_circuit(
        live_root / "model-group-circuit.json",
        category="schema",
        source_group="D",
    )
    project = tmp_path / "project"
    _init_repo(project)

    with pytest.raises(LiveGateError, match="model-group circuit is open"):
        live_background.preview_concurrency(
            live_root / "background-concurrency",
            cli=tmp_path / "missing-claude.exe",
            project_root=project,
            python_exe=Path(sys.executable),
            hook_sink=Path(live_background.__file__).with_name("hook_sink.py"),
            worktree_hook=Path(live_background.__file__).with_name("worktree_hook.py"),
        )

    assert not (live_root / "background-concurrency").exists()


def test_preview_group_c_requires_bound_stop_and_respawn_capabilities(tmp_path: Path):
    live_root = tmp_path / "live"
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake")
    evidence = {
        "status": "ready",
        "identity_stable": True,
        "observed_cli_version": "2.1.224 (Claude Code)",
        "cli_content_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
    }
    write_bound_host_identity(
        live_root / "host",
        cli,
        evidence,
        capabilities={
            "tools_empty_documented": True,
            "prompt_suggestions_false_documented": True,
            "stop_help_recognized": True,
            "respawn_help_recognized": False,
        },
    )
    project = tmp_path / "project"
    _init_repo(project)

    with pytest.raises(LiveGateError, match="required Group C capabilities"):
        live_background.preview_background(
            live_root / "background-main",
            cli=cli,
            project_root=project,
            python_exe=Path(sys.executable),
            hook_sink=Path(live_background.__file__).with_name("hook_sink.py"),
            worktree_hook=Path(live_background.__file__).with_name("worktree_hook.py"),
        )

    assert not (live_root / "background-main").exists()


def test_pretool_guard_rejects_event_or_common_dir_mismatch(tmp_path: Path):
    worktree_root, cwd, common, lease, events, ack = _guard_files(tmp_path)
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd.resolve()),
        "tool_name": "Read",
        "tool_input": {"file_path": _PROOF},
    }
    event = json.loads(events.read_text(encoding="utf-8"))
    event["worktree_path"] = str((tmp_path / "other").resolve())
    events.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(LiveGateError, match="WorktreeCreate path mismatch"):
        pretool_guard(
            payload,
            lease_ack=lease,
            event_log=events,
            guard_ack=ack,
            worktree_root=worktree_root,
            execution_id=_EXECUTION_ID,
            common_dir_resolver=lambda _cwd: common.resolve(),
        )

    events.write_text(json.dumps({
        "hook_event_name": "WorktreeCreate",
        "execution_id": _EXECUTION_ID,
        "worktree_path": str(cwd.resolve()),
    }) + "\n", encoding="utf-8")
    with pytest.raises(LiveGateError, match="common-dir mismatch"):
        pretool_guard(
            payload,
            lease_ack=lease,
            event_log=events,
            guard_ack=ack,
            worktree_root=worktree_root,
            execution_id=_EXECUTION_ID,
            common_dir_resolver=lambda _cwd: tmp_path / "different-common",
        )


@pytest.mark.parametrize("drift", ["dirty", "remote"])
def test_load_background_rejects_dirty_or_remote_repository(tmp_path: Path, drift: str):
    cli = tmp_path / "claude.exe"
    cli.write_bytes(b"fake standalone cli")
    identity = BoundCliIdentity.capture(cli, version="2.1.224 (Claude Code)")
    root = tmp_path / "background"
    kwargs = {
        "cli": cli,
        "python_exe": Path(sys.executable),
        "hook_sink": Path(live_background.__file__).with_name("hook_sink.py"),
        "worktree_hook": Path(live_background.__file__).with_name("worktree_hook.py"),
        "bound_identity": identity,
    }
    materialized = live_background.materialize_background(root, **kwargs)
    if drift == "dirty":
        (materialized.paths.repo / "README.md").write_text("drift\n", encoding="utf-8")
        message = "dirty or drifted"
    else:
        result = run_argv(
            "git-add-remote",
            ["git", "-C", str(materialized.paths.repo), "remote", "add", "origin", str(tmp_path)],
            timeout_seconds=30,
        )
        assert result.exit_code == 0
        message = "has a remote"

    with pytest.raises(LiveGateError, match=message):
        live_background.load_background(root, **kwargs)


def test_group_d_exact_argv_uses_manual_read_write_and_one_worktree(tmp_path: Path):
    argv = build_needs_input_argv(
        Path("claude.exe"),
        tmp_path / "settings.json",
        tmp_path / "empty.json",
        "group-d",
        "worktree-d",
    )

    assert argv == (
        str(Path("claude.exe").resolve()),
        "--bg", "--name", "group-d",
        "--worktree", "worktree-d",
        "--model", "claude-sonnet-5",
        "--effort", "low",
        "--autocompact", "274000",
        "--setting-sources", "user,project,local",
        "--settings", str((tmp_path / "settings.json").resolve()),
        "--tools", "Read,Write",
        "--disallowedTools",
        "mcp__codex__*", "mcp__agent_bridge__*", "mcp__subagent_harness_mcp__*",
        "--permission-mode", "manual",
        "--strict-mcp-config", "--mcp-config", str((tmp_path / "empty.json").resolve()),
        live_background.NEEDS_INPUT_PROMPT,
    )
    assert argv.count("--worktree") == 1


def test_group_f_exact_argv_omits_worktree_and_allows_only_bash(tmp_path: Path):
    argv = build_concurrency_argv(
        Path("claude.exe"),
        tmp_path / "settings.json",
        tmp_path / "empty.json",
        "group-f-1",
    )

    assert "--worktree" not in argv
    assert argv[argv.index("--tools") + 1] == "Bash"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[-1] == live_background.CONCURRENCY_PROMPT


def test_needs_input_guard_records_exact_denied_write_after_handoff(tmp_path: Path):
    worktree_root, cwd, common, lease, events, ack = _guard_files(tmp_path)
    payload = {
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd.resolve()),
        "tool_name": "Write",
        "tool_input": {
            "file_path": "phase0a-unapproved.txt",
            "content": "denied\n",
        },
    }

    with pytest.raises(LiveGateError, match="tool input denied"):
        pretool_guard(
            payload,
            lease_ack=lease,
            event_log=events,
            guard_ack=ack,
            worktree_root=worktree_root,
            execution_id=_EXECUTION_ID,
            proof_relative="phase0a-unapproved.txt",
            policy="needs-input",
            common_dir_resolver=lambda _cwd: common.resolve(),
        )

    record = json.loads(ack.read_text(encoding="utf-8").splitlines()[-1])
    assert record == {
        "allowed": False,
        "cwd": str(cwd.resolve()),
        "input_matched": True,
        "tool_name": "Write",
    }


def test_concurrency_guard_allows_only_exact_wait_in_disposable_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ack = tmp_path / "guard.jsonl"
    exact = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-guard-test",
        "cwd": str(repo.resolve()),
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 20"},
    }

    assert live_background.concurrency_pretool_guard(
        exact, guard_ack=ack, repo=repo,
    ) is True
    wrong = {**exact, "tool_input": {"command": "sleep 21"}}
    with pytest.raises(LiveGateError, match="tool input denied"):
        live_background.concurrency_pretool_guard(
            wrong,
            guard_ack=ack,
            repo=repo,
        )
    records = [json.loads(line) for line in ack.read_text(encoding="utf-8").splitlines()]
    assert [record["allowed"] for record in records] == [True, False]
    assert [record["input_matched"] for record in records] == [True, False]


def test_concurrency_guard_binds_ack_to_sanitized_session_fingerprint(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ack = tmp_path / "guard.jsonl"
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "opaque-session-1",
        "cwd": str(repo.resolve()),
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 20"},
    }

    assert live_background.concurrency_pretool_guard(
        payload, guard_ack=ack, repo=repo,
    ) is True

    record = json.loads(ack.read_text(encoding="utf-8"))
    assert record["session_fingerprint"] == live_background.fingerprint(
        "opaque-session-1",
    )
    assert "opaque-session-1" not in ack.read_text(encoding="utf-8")


def _needs_observation(tmp_path: Path, *, state: str) -> NeedsInputObservation:
    row = _row(tmp_path, state="working")
    row = replace(row, state=state, pid_present=state != "stopped")
    return NeedsInputObservation(
        row=row,
        handoff_equal=True,
        common_dir_equal=True,
        denied_write_observed=True,
        checkout_clean=True,
        remote_count=0,
        base_commit="a" * 40,
        current_commit="a" * 40,
        stop_event_count=1 if state == "stopped" else 0,
        stop_failure_category=None,
    )


class FakeNeedsInput:
    def __init__(self, observations, attach_observation=None):
        self.observations = list(observations)
        self.attach_observation = attach_observation
        self.actions: list[object] = []

    def launch(self):
        self.actions.append("launch")

    def observe(self, stage):
        self.actions.append(("observe", stage))
        return self.observations.pop(0)

    def attach(self, row):
        self.actions.append(("attach", row.short_id))
        return self.attach_observation

    def stop(self, row):
        self.actions.append(("stop", row.short_id))

    def stabilize(self, seconds):
        self.actions.append(("stabilize", seconds))

    def recover_failure(self, row):
        self.actions.append(("recover", None if row is None else row.short_id))


def test_needs_input_attach_preserves_blocked_row_then_stops_stably(tmp_path: Path):
    blocked = _needs_observation(tmp_path, state="blocked")
    attached = AttachObservation(
        row=blocked.row,
        attach_exit_observed=True,
        same_session_hook_observed=True,
        working_transition_observed=False,
        checkout_clean=True,
    )
    fake = FakeNeedsInput([
        blocked,
        _needs_observation(tmp_path, state="stopped"),
        _needs_observation(tmp_path, state="stopped"),
    ], attached)

    result = run_needs_input_canary(fake, include_attach=True)

    assert result.needs_input_observed is True
    assert result.attach_observed is True
    assert result.attach_same_session is True
    assert result.stable_stop_observation_count == 2
    assert fake.actions == [
        "launch",
        ("observe", "needs-input"),
        ("attach", "short_1"),
        ("stop", "short_1"),
        ("observe", "stopped-1"),
        ("stabilize", 0.75),
        ("observe", "stopped-2"),
    ]


def test_needs_input_without_attach_keeps_lifecycle_capability_blocked(tmp_path: Path):
    fake = FakeNeedsInput([
        _needs_observation(tmp_path, state="needs_input"),
        _needs_observation(tmp_path, state="stopped"),
        _needs_observation(tmp_path, state="stopped"),
    ])

    result = run_needs_input_canary(fake, include_attach=False)

    assert result.attach_observed is False
    assert result.lifecycle_commands_status == "BLOCKED"
    assert not any(isinstance(action, tuple) and action[0] == "attach" for action in fake.actions)


def test_attach_working_transition_is_stopped_and_blocks_gate(tmp_path: Path):
    blocked = _needs_observation(tmp_path, state="blocked")
    resumed = replace(blocked.row, state="working", pid_present=True)
    fake = FakeNeedsInput(
        [blocked],
        AttachObservation(
            row=resumed,
            attach_exit_observed=True,
            same_session_hook_observed=True,
            working_transition_observed=True,
            checkout_clean=True,
        ),
    )

    with pytest.raises(LiveGateError, match="attach resumed work"):
        run_needs_input_canary(fake, include_attach=True)

    assert ("recover", "short_1") in fake.actions


def test_needs_input_requires_denied_write_handoff_and_clean_checkout(tmp_path: Path):
    blocked = replace(
        _needs_observation(tmp_path, state="blocked"),
        denied_write_observed=False,
    )
    fake = FakeNeedsInput([blocked])

    with pytest.raises(LiveGateError, match="denied Write"):
        run_needs_input_canary(fake, include_attach=False)

    assert ("recover", "short_1") in fake.actions


def _concurrency_observation(
    tmp_path: Path,
    names: tuple[str, ...],
    *,
    state: str = "working",
    stop_hook_count: int = 0,
    stop_failure_category: str | None = None,
) -> ConcurrencyObservation:
    rows = tuple(
        replace(
            _row(tmp_path / name, state="working"),
            short_id=f"short_{index}",
            session_id=f"session_{index}",
            name=name,
            state=state,
            pid_present=state != "stopped",
        )
        for index, name in enumerate(names, start=1)
    )
    return ConcurrencyObservation(
        rows=rows,
        session_start_count=len(rows),
        guard_allow_count=len(rows),
        checkout_clean=True,
        stop_hook_count=stop_hook_count,
        stop_failure_category=stop_failure_category,
    )


class FakeConcurrency:
    def __init__(self, observations):
        self.observations = list(observations)
        self.actions: list[object] = []

    def launch(self, name):
        self.actions.append(("launch", name))

    def observe(self, stage, names):
        self.actions.append(("observe", stage, tuple(names)))
        return self.observations.pop(0)

    def stop(self, row):
        self.actions.append(("stop", row.short_id))

    def stabilize(self, seconds):
        self.actions.append(("stabilize", seconds))

    def recover_failure(self, rows):
        self.actions.append(("recover", tuple(row.short_id for row in rows)))


def test_concurrency_requires_two_simultaneous_owned_rows_then_stops_both(tmp_path: Path):
    names = ("group-f-1", "group-f-2")
    fake = FakeConcurrency([
        _concurrency_observation(tmp_path, names[:1]),
        _concurrency_observation(tmp_path, names),
        _concurrency_observation(tmp_path, names, state="stopped", stop_hook_count=2),
        _concurrency_observation(tmp_path, names, state="stopped", stop_hook_count=2),
    ])

    result = run_concurrency_canary(fake, group_names=names)

    assert result.observed_floor == 2
    assert result.provider_ceiling == "UNKNOWN"
    assert result.policy_cap == 2
    assert result.stable_stop_observation_count == 2
    assert [action for action in fake.actions if action[0] == "stop"] == [
        ("stop", "short_1"), ("stop", "short_2"),
    ]


def test_first_concurrency_quota_anomaly_prevents_second_model_launch(tmp_path: Path):
    names = ("group-f-1", "group-f-2")
    fake = FakeConcurrency([
        _concurrency_observation(
            tmp_path,
            names[:1],
            stop_failure_category="rate_limit",
        ),
    ])

    with pytest.raises(LiveGateError, match="QUOTA_PAUSED"):
        run_concurrency_canary(fake, group_names=names)

    assert ("launch", names[1]) not in fake.actions


def test_concurrency_fails_closed_when_two_rows_are_not_simultaneously_visible(tmp_path: Path):
    names = ("group-f-1", "group-f-2")
    fake = FakeConcurrency([
        _concurrency_observation(tmp_path, names[:1]),
        _concurrency_observation(tmp_path, names[:1]),
    ])

    with pytest.raises(LiveGateError, match="simultaneous"):
        run_concurrency_canary(fake, group_names=names)


def test_group_f_failed_stop_never_reuses_that_rows_consumed_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    adapter = object.__new__(live_background._LiveGroupFAdapter)
    adapter.authorization = object()
    adapter.materialized = type("Materialized", (), {
        "paths": type("Paths", (), {
            "consumed_ledger": tmp_path / "ledger.json",
        })(),
    })()
    adapter.consumed_stop_ids = set()
    adapter.stop_actions = 0
    invoked: list[str] = []

    def invoke_cli(argv):
        short_id = argv[-1]
        invoked.append(short_id)
        if short_id == "short_1":
            raise LiveGateError("first stop uncertain")

    adapter._invoke_cli = invoke_cli

    def consume(_authorization, kind, state, _ledger, *, invoke):
        assert kind == "stop"
        short_id = state["group"]["short_id"]
        return invoke(("claude.exe", "stop", short_id))

    monkeypatch.setattr(live_background, "consume_side_effect", consume)

    with pytest.raises(LiveGateError, match="uncertain"):
        adapter._consume_owned_stop_id("short_1")
    adapter._consume_owned_stop_id("short_2")
    with pytest.raises(LiveGateError, match="already consumed"):
        adapter._consume_owned_stop_id("short_1")

    assert invoked == ["short_1", "short_2"]
    assert adapter.consumed_stop_ids == {"short_1", "short_2"}
    assert adapter.stop_actions == 2


def test_groups_d_and_f_have_independent_scope_digests_and_counters(tmp_path: Path):
    common = {
        "git_head": "a" * 40,
        "cli_sha256": "b" * 64,
        "executable_manifest_sha256": "c" * 64,
        "exact_targets": (str(tmp_path.resolve()),),
    }
    d_scope = build_group_d_scope(
        **common,
        launch_argv=("claude.exe", "--bg", "--name", "group-d", "prompt"),
        worktree_hook_argv=("python.exe", "worktree_hook.py"),
        include_attach=True,
    )
    f_scope = build_group_f_scope(
        **common,
        launch_argv=("claude.exe", "--bg", "--name", "group-f-1", "prompt"),
        group_names=("group-f-1", "group-f-2"),
    )

    assert approval_digest(d_scope) != approval_digest(f_scope)
    assert d_scope.max_provider_session_launches == 1
    assert d_scope.max_worktree_creates == 1
    assert d_scope.max_attach_actions == 1
    assert f_scope.max_provider_session_launches == 2
    assert f_scope.max_worktree_creates == 0
    assert f_scope.max_stop_respawn_actions == 2
    f_effects = {effect.kind: effect for effect in f_scope.side_effects}
    assert f_effects["provider_launch"].max_uses == 2
    assert f_effects["provider_launch"].bindings[0].state_key == "group.name"


def test_model_group_circuit_blocks_later_model_groups_but_retains_recovery_state(tmp_path: Path):
    circuit = tmp_path / "model-group-circuit.json"
    open_model_group_circuit(circuit, category="quota", source_group="D")

    with pytest.raises(LiveGateError, match="model-group circuit is open"):
        require_model_groups_available(circuit)

    state = load_model_group_circuit(circuit)
    assert state == {"category": "quota", "source_group": "D", "status": "OPEN"}
    assert circuit.is_file()


def test_background_matrix_projection_is_blocked_until_every_required_state_is_live_observed(tmp_path: Path):
    blocked = _needs_observation(tmp_path, state="blocked")
    needs = run_needs_input_canary(FakeNeedsInput([
        blocked,
        _needs_observation(tmp_path, state="stopped"),
        _needs_observation(tmp_path, state="stopped"),
    ]), include_attach=False)
    names = ("group-f-1", "group-f-2")
    concurrency = run_concurrency_canary(FakeConcurrency([
        _concurrency_observation(tmp_path, names[:1]),
        _concurrency_observation(tmp_path, names),
        _concurrency_observation(tmp_path, names, state="stopped", stop_hook_count=2),
        _concurrency_observation(tmp_path, names, state="stopped", stop_hook_count=2),
    ]), group_names=names)

    projection = project_background_matrix(
        needs,
        concurrency,
        state_categories={"working", "needs_input_or_blocked", "done", "stopped"},
        stop_failure_observed=False,
    )

    assert projection["status"] == "BLOCKED"
    assert projection["state_presence"]["failed"] is False
    assert projection["agents_json_schema_status"] == "BLOCKED"
    assert projection["stop_failure_hook_status"] == "BLOCKED"
    assert projection["observed_floor"] == 2
    assert projection["provider_ceiling"] == "UNKNOWN"
    assert projection["agent_view_overhead"] == "UNKNOWN"
    assert "short_" not in json.dumps(projection)


def test_task6_projections_carry_context_credit_and_explicit_delta_fields(tmp_path: Path):
    prerequisite = ContextPrerequisite(
        observed_cli_version="2.1.224 (Claude Code)",
        source_sha256="a" * 64,
        candidate_sha256="b" * 64,
        scope_sha256="c" * 64,
        missing_fields=("effective_auto_compaction_window_tokens",),
    )
    blocked = _needs_observation(tmp_path, state="blocked")
    needs = run_needs_input_canary(FakeNeedsInput([
        blocked,
        _needs_observation(tmp_path, state="stopped"),
        _needs_observation(tmp_path, state="stopped"),
    ]), include_attach=False)
    names = ("group-f-1", "group-f-2")
    concurrency = run_concurrency_canary(FakeConcurrency([
        _concurrency_observation(tmp_path, names[:1]),
        _concurrency_observation(tmp_path, names),
        _concurrency_observation(tmp_path, names, state="stopped", stop_hook_count=2),
        _concurrency_observation(tmp_path, names, state="stopped", stop_hook_count=2),
    ]), group_names=names)

    d_projection = live_background.project_needs_input_result(needs, prerequisite)
    f_projection = live_background.project_concurrency_result(concurrency, prerequisite)

    for projection in (d_projection, f_projection):
        assert projection["declared_native_attestation"] == "incomplete"
        assert projection["usage_credits_off_confirmed"] is True
        assert projection["foreground_overage_absent"] is True
        assert projection["effective_auto_compaction_window_tokens"] is None
        assert projection["effective_auto_compaction_trigger_percent"] is None
        assert projection["effective_auto_compaction_trigger_tokens"] is None
    assert d_projection["context_delta_fields"] == ["permission_mode", "tools"]
    assert f_projection["context_delta_fields"] == ["tools"]


def test_model_circuit_wrapper_opens_for_c_and_non_live_gate_failure(tmp_path: Path):
    circuit = tmp_path / "model-group-circuit.json"

    def fail():
        raise OSError("post-authorization handle drift")

    with pytest.raises(OSError, match="handle drift"):
        live_background._run_with_model_circuit(
            fail, circuit_path=circuit, source_group="C",
        )

    assert load_model_group_circuit(circuit) == {
        "category": "schema",
        "source_group": "C",
        "status": "OPEN",
    }


def test_offline_matrix_finalizer_emits_honest_blocked_fixture(tmp_path: Path):
    version = "2.1.224 (Claude Code)"

    def candidate(path: Path, kind: str, source_kind: str, payload: dict):
        payload = {"cli_content_sha256": "a" * 64, **payload}
        value = fixture_envelope(
            kind=kind,
            observed_cli_version=version,
            source_kind=source_kind,
            source_sha256=hashlib.sha256(kind.encode()).hexdigest(),
            payload=payload,
            observed=sorted(payload),
            missing=[],
        )
        write_json_atomic(path, value)

    c_path = tmp_path / "c.json"
    d_path = tmp_path / "d.json"
    f_path = tmp_path / "f.json"
    candidate(c_path, "live_background_lifecycle", "bounded_background_projection", {
        "provider_launch_count": 2,
        "respawn_working_observed": True,
        "active_stop_stable_observation_count": 2,
        "final_state_category": "done",
        "context_missing_fields": ["effective_auto_compaction_window_tokens"],
        "failed_state_observed": False,
        "stop_failure_observed": False,
    })
    candidate(d_path, "live_background_needs_input", "bounded_needs_input_projection", {
        "needs_input_observed": True,
        "stable_stop_observation_count": 2,
        "lifecycle_commands_status": "BLOCKED",
        "denied_write_observed": True,
        "attach_observed": False,
        "attach_same_session": False,
        "checkout_clean": True,
        "context_missing_fields": ["effective_auto_compaction_window_tokens"],
        "failed_state_observed": False,
        "stop_failure_observed": False,
    })
    candidate(f_path, "live_background_concurrency", "bounded_concurrency_projection", {
        "observed_floor": 2,
        "provider_ceiling": "UNKNOWN",
        "policy_cap": 2,
        "simultaneous_active_observed": True,
        "stable_stop_observation_count": 2,
        "stop_hook_observed": True,
        "checkout_clean": True,
        "active_state_categories": ["working"],
        "context_missing_fields": ["effective_auto_compaction_window_tokens"],
        "failed_state_observed": False,
        "stop_failure_observed": False,
    })
    stop_contract = (
        Path(__file__).parents[1]
        / "fixtures" / "phase0a" / "current" / "stop-failure-contract.json"
    )
    output = tmp_path / "live-background-matrix.json"

    fixture = live_background.finalize_background_matrix(
        c_path,
        d_path,
        f_path,
        stop_contract,
        output=output,
    )

    validate_fixture(fixture)
    assert output.is_file()
    assert fixture["payload"]["status"] == "BLOCKED"
    assert fixture["payload"]["state_presence"]["failed"] is False
    assert fixture["payload"]["stop_failure_hook_status"] == "BLOCKED"
    assert "short_" not in json.dumps(fixture)


def _cleanup_observation(
    tmp_path: Path,
    *,
    name: str = "group-c",
    short_id: str = "short_cleanup_1",
    state: str = "done",
) -> live_background.CleanupObservation:
    approved_root = (tmp_path / "worktrees").resolve()
    worktree = (approved_root / name).resolve()
    worktree.mkdir(parents=True, exist_ok=True)
    repository = (tmp_path / "repo").resolve()
    repository.mkdir(exist_ok=True)
    common = (repository / ".git").resolve()
    common.mkdir(exist_ok=True)
    row = OwnedRosterRow(
        short_id=short_id,
        session_id="session_" + short_id,
        name=name,
        cwd=worktree,
        state=state,
        model="claude-sonnet-5",
        context_fingerprint="context-v1",
        pid_present=False,
    )
    return live_background.CleanupObservation(
        row=row,
        approved_disposable_root=approved_root,
        repository_root=repository,
        repository_common_dir=common,
        worktree_common_dir=common,
        lease_path=worktree,
        event_path=worktree,
        base_commit="a" * 40,
        current_commit="a" * 40,
        status_line_count=0,
        commits_above_base=0,
        remote_count=0,
        matching_process_count=0,
        registered_worktree=True,
        creation_scope_sha256="b" * 64,
        pending_scope_sha256="b" * 64,
        receipt_scope_sha256="b" * 64,
        consumed_creation_observed=True,
    )


class FakeCleanup:
    def __init__(self, removal_observations=(), *, refuse_at: int | None = None):
        self.removal_observations = list(removal_observations)
        self.refuse_at = refuse_at
        self.actions: list[object] = []

    def remove(self, target):
        self.actions.append(("remove", target.row.short_id))
        if self.refuse_at == len(self.actions):
            raise LiveGateError("provider rm refused")

    def observe_removed(self, target):
        self.actions.append(("observe", target.row.short_id))
        return self.removal_observations.pop(0)


def _removed(observation: live_background.CleanupObservation):
    return live_background.RemovalObservation(
        row_identity_equal=True,
        worktree_remove_event_match=True,
        path_absent=True,
        worktree_unregistered=True,
        row_absent=True,
        unrelated_rows_unchanged=True,
        unrelated_worktrees_unchanged=True,
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("status_line_count", 1, "dirty"),
        ("commits_above_base", 1, "commits"),
        ("remote_count", 1, "remote"),
        ("matching_process_count", 1, "process"),
        ("registered_worktree", False, "registered"),
        ("consumed_creation_observed", False, "lineage"),
        ("receipt_scope_sha256", "c" * 64, "lineage"),
    ],
)
def test_cleanup_audit_failure_performs_zero_removals(
    tmp_path: Path, field: str, value: object, reason: str,
):
    observation = replace(_cleanup_observation(tmp_path), **{field: value})
    fake = FakeCleanup()

    result = live_background.run_cleanup_canary(fake, (observation,))

    assert result.status == "RECOVERY_REQUIRED"
    assert result.removal_attempt_count == 0
    assert any(reason in item for item in result.residual_reasons)
    assert fake.actions == []


def test_cleanup_audit_rejects_working_pid_path_and_handoff_drift(tmp_path: Path):
    base = _cleanup_observation(tmp_path)
    cases = (
        replace(base, row=replace(base.row, state="working", pid_present=True)),
        replace(base, row=replace(base.row, cwd=(tmp_path / "outside").resolve())),
        replace(base, worktree_common_dir=(tmp_path / "other-common").resolve()),
        replace(base, event_path=(tmp_path / "different-event").resolve()),
    )

    for observation in cases:
        fake = FakeCleanup()
        result = live_background.run_cleanup_canary(fake, (observation,))
        assert result.status == "RECOVERY_REQUIRED"
        assert result.removal_attempt_count == 0
        assert fake.actions == []


def test_cleanup_removes_only_after_complete_audit_and_observes_each_release(tmp_path: Path):
    first = _cleanup_observation(tmp_path / "one", name="group-c", short_id="short_1")
    second = _cleanup_observation(tmp_path / "two", name="group-d", short_id="short_2", state="stopped")
    fake = FakeCleanup((_removed(first), _removed(second)))

    result = live_background.run_cleanup_canary(fake, (first, second))

    assert result.status == "PASS"
    assert result.audited_target_count == 2
    assert result.removal_attempt_count == 2
    assert result.removal_success_count == 2
    assert result.worktree_remove_hook_count == 2
    assert fake.actions == [
        ("remove", "short_1"), ("observe", "short_1"),
        ("remove", "short_2"), ("observe", "short_2"),
    ]


def test_cleanup_rm_refusal_stops_without_touching_later_target(tmp_path: Path):
    first = _cleanup_observation(tmp_path / "one", short_id="short_1")
    second = _cleanup_observation(tmp_path / "two", short_id="short_2")
    fake = FakeCleanup(refuse_at=1)

    result = live_background.run_cleanup_canary(fake, (first, second))

    assert result.status == "RECOVERY_REQUIRED"
    assert result.removal_attempt_count == 1
    assert result.removal_success_count == 0
    assert fake.actions == [("remove", "short_1")]


def test_group_g_scope_binds_exact_owned_ids_targets_and_removal_count(tmp_path: Path):
    contract_sha256 = "d" * 64
    scope = live_background.build_group_g_scope(
        git_head="a" * 40,
        cli_sha256="b" * 64,
        executable_manifest_sha256="c" * 64,
        cli=Path("claude.exe"),
        short_ids=("short_1", "short_2"),
        worktree_targets=(
            str((tmp_path / "one").resolve()),
            str((tmp_path / "two").resolve()),
        ),
        exact_targets=(str(tmp_path.resolve()),),
        cleanup_contract_sha256=contract_sha256,
    )

    assert scope.max_provider_session_launches == 0
    assert scope.max_removals == 2
    assert scope.max_stop_respawn_actions == 0
    assert len(scope.side_effects) == 1
    effect = scope.side_effects[0]
    assert effect.kind == "remove"
    assert effect.max_uses == 2
    assert effect.argv_template == (str(Path("claude.exe").resolve()), "rm", "{short_id}")
    assert effect.bindings[0].state_key == "group.short_id"
    assert "short_1" in effect.bindings[0].pattern
    assert "short_2" in effect.bindings[0].pattern
    assert "cleanup-contract-sha256:" + contract_sha256 in effect.exact_targets


def test_cleanup_projection_is_sanitized_and_reports_residuals(tmp_path: Path):
    observation = _cleanup_observation(tmp_path)
    fake = FakeCleanup((_removed(observation),))
    result = live_background.run_cleanup_canary(fake, (observation,))

    projection = live_background.project_cleanup_result(result)
    serialized = json.dumps(projection, sort_keys=True)

    assert projection["status"] == "PASS"
    assert projection["removal_success_count"] == 1
    assert projection["all_worktree_remove_events_matched"] is True
    assert "short_cleanup" not in serialized
    assert str(tmp_path.resolve()) not in serialized


def test_cleanup_contract_load_rejects_audit_or_target_drift(tmp_path: Path):
    observation = _cleanup_observation(tmp_path / "source")
    identity = BoundCliIdentity.capture(sys.executable, version="2.1.224")
    root = tmp_path / "cleanup"
    materialized = live_background.materialize_cleanup(
        root,
        (observation,),
        bound_identity=identity,
        retained_group_f_count=2,
    )
    _verify_private_path(materialized.paths.root, directory=True)

    loaded = live_background.load_cleanup(
        root,
        (observation,),
        bound_identity=identity,
        retained_group_f_count=2,
    )
    assert loaded.contract_sha256 == materialized.contract_sha256

    drifted = replace(observation, status_line_count=1)
    with pytest.raises(LiveGateError, match="preview audit drifted"):
        live_background.load_cleanup(
            root,
            (drifted,),
            bound_identity=identity,
            retained_group_f_count=2,
        )


def test_plan_ownership_writer_is_idempotent_and_releases_exact_pair(tmp_path: Path):
    observation = _cleanup_observation(tmp_path / "source")
    live_root = tmp_path / "live"
    live_root.mkdir()

    for _ in range(2):
        live_background._record_plan_ownership(
            live_root,
            scope_sha256=observation.creation_scope_sha256,
            rows=(observation.row,),
            common_dir=observation.repository_common_dir,
            include_worktrees=True,
        )

    ownership = json.loads((live_root / "ownership.json").read_text(encoding="utf-8"))
    assert [item["kind"] for item in ownership["records"]] == ["row", "worktree"]
    assert len({item["target_fingerprint"] for item in ownership["records"]}) == 2

    live_background._release_plan_ownership(live_root, observation)
    released = json.loads((live_root / "ownership.json").read_text(encoding="utf-8"))
    assert released == {"schema_version": 1, "records": []}
