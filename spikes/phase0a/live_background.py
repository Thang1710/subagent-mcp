from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .background_probe import (
        BACKGROUND_PROMPT,
        build_background_argv,
        build_background_hook_settings,
        prepare_background,
    )
    from .contracts import STOP_FAILURE_CATEGORIES, normalize_agents
    from .core import fingerprint, read_fd_bounded, run_argv, write_json_atomic
    from .fixtures import fixture_envelope, validate_fixture
    from .hook_sink import build_hook_settings as build_event_hook_settings
    from .live_common import (
        ApprovalScope,
        BoundCliIdentity,
        BoundExecutableFile,
        BoundExecutableManifest,
        ExecutionAuthorization,
        ExecutionObservations,
        RuntimeBinding,
        SideEffectSpec,
        approval_digest,
        claim_execution_authorization,
        consume_side_effect,
        prepare_private_runtime_group_root,
    )
    from .live_context import ContextPaths, build_context_argv
    from .live_host import load_bound_host_capabilities, load_bound_host_identity
    from .live_init import (
        _expected_python_process_image,
        _git_checkpoint,
        assert_no_credential_overrides,
    )
    from .locking import locked_file
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from spikes.phase0a.background_probe import (
        BACKGROUND_PROMPT,
        build_background_argv,
        build_background_hook_settings,
        prepare_background,
    )
    from spikes.phase0a.contracts import STOP_FAILURE_CATEGORIES, normalize_agents
    from spikes.phase0a.core import fingerprint, read_fd_bounded, run_argv, write_json_atomic
    from spikes.phase0a.fixtures import fixture_envelope, validate_fixture
    from spikes.phase0a.hook_sink import build_hook_settings as build_event_hook_settings
    from spikes.phase0a.live_common import (
        ApprovalScope,
        BoundCliIdentity,
        BoundExecutableFile,
        BoundExecutableManifest,
        ExecutionAuthorization,
        ExecutionObservations,
        RuntimeBinding,
        SideEffectSpec,
        approval_digest,
        claim_execution_authorization,
        consume_side_effect,
        prepare_private_runtime_group_root,
    )
    from spikes.phase0a.live_context import ContextPaths, build_context_argv
    from spikes.phase0a.live_host import load_bound_host_capabilities, load_bound_host_identity
    from spikes.phase0a.live_init import (
        _expected_python_process_image,
        _git_checkpoint,
        assert_no_credential_overrides,
    )
    from spikes.phase0a.locking import locked_file


_MODEL = "claude-sonnet-5"
_EFFORT = "low"
_PROOF_RELATIVE = "phase0a-proof.txt"
_PROOF_BYTES = b"ready\n"
_NEEDS_INPUT_RELATIVE = "phase0a-unapproved.txt"
_CONCURRENCY_COMMAND = "sleep 20"
_CONCURRENCY_MARKER = "PHASE0A_CONCURRENCY_COMPLETE"
_ATTACH_TIMEOUT_SECONDS = 300.0
_MAX_LOCAL_BYTES = 1024 * 1024
_MAX_EVENTS = 256
_ROW_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_GROUP_C_STATES = {
    "working", "blocked", "needs_input", "stopped", "done", "failed",
}
_QUOTA_STOP_FAILURES = {"rate_limit", "billing_error"}
_RECURSION_DENIES = (
    "mcp__codex__*",
    "mcp__agent_bridge__*",
    "mcp__subagent_harness_mcp__*",
)
_CLEANUP_SOURCES = (
    ("C", "background-main", "approved-C.json"),
    ("D", "background-needs-input", "approved-D.json"),
)
_CLEANUP_TIMEOUT_SECONDS = 30.0

NEEDS_INPUT_PROMPT = (
    "In this disposable worktree, use Write to create phase0a-unapproved.txt "
    "containing exactly denied and one newline. Do not use Bash, do not edit any "
    "other file, do not commit, and wait for controller permission if Write is denied."
)

CONCURRENCY_PROMPT = (
    "Run exactly `sleep 20` with Bash, make no file changes, then respond with exactly "
    f"{_CONCURRENCY_MARKER}. Do not run any other command."
)


class LiveGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackgroundPaths:
    root: Path
    repo: Path
    settings: Path
    empty_mcp: Path
    event_log: Path
    worktree_root: Path
    lease_ack: Path
    creation_lock: Path
    guard_ack: Path
    prompt: Path
    local_state: Path
    consumed_ledger: Path
    candidate: Path


@dataclass(frozen=True)
class MaterializedBackground:
    paths: BackgroundPaths
    launch_argv: tuple[str, ...]
    worktree_hook_argv: tuple[str, ...]
    group_name: str
    worktree_name: str
    execution_id: str
    base_commit: str
    repository_common_dir: Path
    cli_sha256: str
    cli_version: str


@dataclass(frozen=True)
class MaterializedConcurrency:
    paths: BackgroundPaths
    launch_argv: tuple[str, ...]
    group_names: tuple[str, str]
    execution_id: str
    base_commit: str
    repository_common_dir: Path
    cli_sha256: str
    cli_version: str


@dataclass(frozen=True)
class OwnedRosterRow:
    short_id: str
    session_id: str
    name: str
    cwd: Path
    state: str
    model: str
    context_fingerprint: str
    pid_present: bool


@dataclass(frozen=True)
class BackgroundObservation:
    row: OwnedRosterRow
    session_start_observed: bool
    worktree_create_observed: bool
    lease_path: Path
    event_path: Path
    handoff_path: Path
    guard_cwd: Path
    roster_path: Path
    repository_common_dir: Path
    worktree_common_dir: Path
    event_order: tuple[str, ...]
    first_write_after_handoff: bool
    remote_count: int
    base_commit: str
    current_commit: str
    changed_paths: tuple[str, ...]
    proof_content: bytes | None
    stop_event_count: int
    stop_failure_category: str | None


@dataclass(frozen=True)
class BackgroundCanaryResult:
    event_order: tuple[str, ...]
    first_write_after_handoff: bool
    active_stop_stable_observations: int
    provider_launch_count: int
    worktree_create_count: int
    stop_respawn_action_count: int
    file_delete_count: int
    session_start_observed: bool
    worktree_create_observed: bool
    handoff_equality_observed: bool
    common_dir_equality_observed: bool
    respawn_identity_equal: bool
    respawn_working_observed: bool
    final_state_category: str
    stop_hook_observed: bool
    proof_only_change: bool
    proof_bytes_matched: bool
    final_checkout_clean: bool


@dataclass(frozen=True)
class ContextPrerequisite:
    observed_cli_version: str
    source_sha256: str
    candidate_sha256: str
    scope_sha256: str
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class NeedsInputObservation:
    row: OwnedRosterRow
    handoff_equal: bool
    common_dir_equal: bool
    denied_write_observed: bool
    checkout_clean: bool
    remote_count: int
    base_commit: str
    current_commit: str
    stop_event_count: int
    stop_failure_category: str | None


@dataclass(frozen=True)
class AttachObservation:
    row: OwnedRosterRow
    attach_exit_observed: bool
    same_session_hook_observed: bool
    working_transition_observed: bool
    checkout_clean: bool


@dataclass(frozen=True)
class NeedsInputCanaryResult:
    needs_input_observed: bool
    attach_observed: bool
    attach_same_session: bool
    stable_stop_observation_count: int
    lifecycle_commands_status: str
    denied_write_observed: bool
    checkout_clean: bool
    stop_hook_observed: bool


@dataclass(frozen=True)
class ConcurrencyObservation:
    rows: tuple[OwnedRosterRow, ...]
    session_start_count: int
    guard_allow_count: int
    checkout_clean: bool
    stop_hook_count: int
    stop_failure_category: str | None


@dataclass(frozen=True)
class ConcurrencyCanaryResult:
    observed_floor: int
    provider_ceiling: str
    policy_cap: int
    stable_stop_observation_count: int
    simultaneous_active_observed: bool
    stop_hook_observed: bool
    checkout_clean: bool
    active_state_categories: tuple[str, ...]


@dataclass(frozen=True)
class CleanupObservation:
    row: OwnedRosterRow
    approved_disposable_root: Path
    repository_root: Path
    repository_common_dir: Path
    worktree_common_dir: Path
    lease_path: Path
    event_path: Path
    base_commit: str
    current_commit: str
    status_line_count: int
    commits_above_base: int
    remote_count: int
    matching_process_count: int
    registered_worktree: bool
    creation_scope_sha256: str
    pending_scope_sha256: str
    receipt_scope_sha256: str
    consumed_creation_observed: bool
    source_group: str = "C"
    source_root: Path | None = None
    source_event_log: Path | None = None


@dataclass(frozen=True)
class RemovalObservation:
    row_identity_equal: bool
    worktree_remove_event_match: bool
    path_absent: bool
    worktree_unregistered: bool
    row_absent: bool
    unrelated_rows_unchanged: bool
    unrelated_worktrees_unchanged: bool


@dataclass(frozen=True)
class CleanupCanaryResult:
    status: str
    audited_target_count: int
    removal_attempt_count: int
    removal_success_count: int
    worktree_remove_hook_count: int
    residual_count: int
    residual_reasons: tuple[str, ...]
    all_worktree_remove_events_matched: bool
    all_paths_absent: bool
    all_rows_absent: bool
    unrelated_state_unchanged: bool


@dataclass(frozen=True)
class CleanupPaths:
    root: Path
    contract: Path
    pending_scope: Path
    consumed_ledger: Path
    residual: Path
    candidate: Path


@dataclass(frozen=True)
class MaterializedCleanup:
    paths: CleanupPaths
    observations: tuple[CleanupObservation, ...]
    contract: dict[str, Any]
    contract_sha256: str
    cli_sha256: str
    cli_version: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bounded(path: Path, label: str) -> bytes:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LiveGateError(f"{label} unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_LOCAL_BYTES:
        raise LiveGateError(f"{label} is not a bounded direct file")
    data = path.read_bytes()
    if len(data) != metadata.st_size:
        raise LiveGateError(f"{label} changed during read")
    return data


def _read_json(path: Path, label: str) -> Any:
    try:
        value = json.loads(_read_bounded(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveGateError(f"{label} is malformed") from exc
    return value


def _read_json_lines(path: Path, label: str, *, missing_ok: bool = False) -> list[dict[str, Any]]:
    if missing_ok and not path.exists():
        return []
    try:
        text = _read_bounded(path, label).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LiveGateError(f"{label} must be UTF-8") from exc
    lines = text.splitlines()
    if len(lines) > _MAX_EVENTS:
        raise LiveGateError(f"{label} has too many records")
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LiveGateError(f"{label} is malformed") from exc
        if not isinstance(item, dict):
            raise LiveGateError(f"{label} record must be an object")
        result.append(item)
    return result


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _git_text(name: str, argv: Sequence[str], *, timeout_seconds: float = 15) -> str:
    result = run_argv(name, list(argv), timeout_seconds=timeout_seconds)
    if result.exit_code != 0 or result.timed_out:
        raise LiveGateError(f"{name} failed")
    return result.stdout.strip()


def _git_common_dir(cwd: Path) -> Path:
    raw = _git_text(
        "git-common-dir",
        ("git", "-C", str(cwd), "rev-parse", "--git-common-dir"),
    )
    common = Path(raw)
    if not common.is_absolute():
        common = cwd / common
    return common.resolve(strict=True)


def _append_guard_ack(
    path: Path,
    *,
    allowed: bool,
    cwd: Path,
    tool_name: str,
    input_matched: bool | None = None,
    session_fingerprint: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or getattr(
            metadata, "st_file_attributes", 0,
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise LiveGateError("PreToolUse guard acknowledgement must be direct")
    record = {
        "allowed": allowed,
        "cwd": str(cwd),
        "tool_name": tool_name,
    }
    if input_matched is not None:
        record["input_matched"] = input_matched
    if session_fingerprint is not None:
        record["session_fingerprint"] = session_fingerprint
    lock_path = path.with_suffix(path.suffix + ".lock")
    with locked_file(lock_path, timeout_seconds=10):
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def pretool_guard(
    payload: Mapping[str, Any],
    *,
    lease_ack: Path,
    event_log: Path,
    guard_ack: Path,
    worktree_root: Path,
    execution_id: str,
    proof_relative: str = _PROOF_RELATIVE,
    policy: str = "main",
    common_dir_resolver: Callable[[Path], Path] = _git_common_dir,
) -> bool:
    if policy not in {"main", "needs-input"}:
        raise LiveGateError("unknown worktree guard policy")
    expected_relative = (
        _PROOF_RELATIVE if policy == "main" else _NEEDS_INPUT_RELATIVE
    )
    if proof_relative != expected_relative:
        raise LiveGateError("proof policy drifted")
    if _HEX32.fullmatch(execution_id) is None:
        raise LiveGateError("guard execution identity is invalid")
    if not lease_ack.exists():
        raise LiveGateError("lease unavailable")
    lease = _read_json(lease_ack, "lease acknowledgement")
    if not isinstance(lease, dict) or set(lease) != {
        "execution_id", "repository_common_dir", "status", "worktree_path",
    }:
        raise LiveGateError("lease acknowledgement schema mismatch")
    if lease.get("execution_id") != execution_id or lease.get("status") != "leased":
        raise LiveGateError("lease acknowledgement identity mismatch")
    raw_worktree = lease.get("worktree_path")
    raw_common = lease.get("repository_common_dir")
    if not isinstance(raw_worktree, str) or not isinstance(raw_common, str):
        raise LiveGateError("lease acknowledgement paths are invalid")
    try:
        root = worktree_root.resolve(strict=True)
        supplied_worktree = Path(raw_worktree)
        supplied_metadata = supplied_worktree.stat(follow_symlinks=False)
        if supplied_worktree.is_symlink() or getattr(
            supplied_metadata, "st_file_attributes", 0,
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise LiveGateError("leased worktree must be direct")
        leased_worktree = supplied_worktree.resolve(strict=True)
        leased_worktree.relative_to(root)
        expected_common = Path(raw_common).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise LiveGateError("lease acknowledgement escaped the worktree root") from exc

    events = _read_json_lines(event_log, "background event log")
    matching_events = [
        item for item in events
        if item.get("hook_event_name") == "WorktreeCreate"
        and item.get("execution_id") == execution_id
    ]
    if len(matching_events) != 1:
        raise LiveGateError("WorktreeCreate acknowledgement mismatch")
    event_path = matching_events[0].get("worktree_path")
    if not isinstance(event_path, str) or not _same_path(Path(event_path), leased_worktree):
        raise LiveGateError("WorktreeCreate path mismatch")

    if payload.get("hook_event_name") != "PreToolUse":
        raise LiveGateError("unexpected guard hook event")
    raw_cwd = payload.get("cwd")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(raw_cwd, str) or not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        raise LiveGateError("PreToolUse payload schema mismatch")
    try:
        supplied_cwd = Path(raw_cwd)
        cwd_metadata = supplied_cwd.stat(follow_symlinks=False)
        if supplied_cwd.is_symlink() or getattr(
            cwd_metadata, "st_file_attributes", 0,
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise LiveGateError("PreToolUse cwd must be direct")
        cwd = supplied_cwd.resolve(strict=True)
    except OSError as exc:
        raise LiveGateError("PreToolUse cwd unavailable") from exc
    if not _same_path(cwd, leased_worktree):
        raise LiveGateError("PreToolUse cwd does not match lease")
    try:
        observed_common = common_dir_resolver(cwd)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LiveGateError("PreToolUse common-dir unavailable") from exc
    if not _same_path(observed_common, expected_common):
        raise LiveGateError("PreToolUse common-dir mismatch")

    if policy == "needs-input":
        input_matched = tool_name == "Write" and tool_input == {
            "file_path": _NEEDS_INPUT_RELATIVE,
            "content": "denied\n",
        }
        _append_guard_ack(
            guard_ack,
            allowed=False,
            cwd=cwd,
            tool_name=tool_name,
            input_matched=input_matched,
        )
        raise LiveGateError("tool input denied")
    allowed_inputs = {
        "Read": {"file_path": proof_relative},
        "Write": {"file_path": proof_relative, "content": _PROOF_BYTES.decode("ascii")},
        "Bash": {"command": "sleep 30"},
    }
    allowed = tool_name in allowed_inputs and tool_input == allowed_inputs[tool_name]
    _append_guard_ack(guard_ack, allowed=allowed, cwd=cwd, tool_name=tool_name)
    if not allowed:
        raise LiveGateError("tool input denied")
    return True


def concurrency_pretool_guard(
    payload: Mapping[str, Any],
    *,
    guard_ack: Path,
    repo: Path,
) -> bool:
    if payload.get("hook_event_name") != "PreToolUse":
        raise LiveGateError("unexpected concurrency guard hook event")
    raw_cwd = payload.get("cwd")
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if (
        not isinstance(raw_cwd, str)
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(tool_name, str)
        or not isinstance(tool_input, dict)
    ):
        raise LiveGateError("concurrency PreToolUse payload schema mismatch")
    try:
        supplied_cwd = Path(raw_cwd)
        metadata = supplied_cwd.stat(follow_symlinks=False)
        if supplied_cwd.is_symlink() or getattr(
            metadata, "st_file_attributes", 0,
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise LiveGateError("concurrency cwd must be direct")
        cwd = supplied_cwd.resolve(strict=True)
        expected_repo = Path(repo).resolve(strict=True)
    except OSError as exc:
        raise LiveGateError("concurrency cwd unavailable") from exc
    if not _same_path(cwd, expected_repo):
        raise LiveGateError("concurrency cwd does not match disposable repo")
    allowed = tool_name == "Bash" and tool_input == {"command": _CONCURRENCY_COMMAND}
    _append_guard_ack(
        guard_ack,
        allowed=allowed,
        cwd=cwd,
        tool_name=tool_name,
        input_matched=allowed,
        session_fingerprint=fingerprint(session_id),
    )
    if not allowed:
        raise LiveGateError("tool input denied")
    return True


def parse_owned_roster(payload: Any, expected_name: str) -> OwnedRosterRow:
    if not isinstance(expected_name, str) or _NAME.fullmatch(expected_name) is None:
        raise LiveGateError("expected background name is invalid")
    try:
        normalize_agents(payload)
    except ValueError as exc:
        raise LiveGateError(str(exc)) from exc
    matching = [item for item in payload if item.get("name") == expected_name]
    if not matching:
        raise LiveGateError("missing owned background row")
    if len(matching) != 1:
        raise LiveGateError("duplicate owned background row")
    item = matching[0]
    values: dict[str, str] = {}
    for key in ("id", "sessionId", "name", "cwd", "kind", "state"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            raise LiveGateError(f"background row {key} must be a nonempty string")
        values[key] = value
    if _ROW_ID.fullmatch(values["id"]) is None or _ROW_ID.fullmatch(values["sessionId"]) is None:
        raise LiveGateError("background row id schema mismatch")
    if values["kind"] != "background":
        raise LiveGateError("owned row is not a background row")
    if values["state"] not in _KNOWN_GROUP_C_STATES:
        raise LiveGateError("unknown background state")
    supplied_cwd = Path(values["cwd"])
    if not supplied_cwd.is_absolute():
        raise LiveGateError("background cwd must be absolute")
    model = item.get("model")
    fingerprint = item.get("contextFingerprint")
    waiting_for = item.get("waitingFor")
    if waiting_for is not None and not isinstance(waiting_for, str):
        raise LiveGateError("background waitingFor must be a string or null")
    if not isinstance(model, str) or not model:
        raise LiveGateError("background model must be a nonempty string")
    if model != _MODEL:
        raise LiveGateError("background model mismatch")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise LiveGateError("background contextFingerprint must be a nonempty string")
    pid = item.get("pid")
    if pid is not None and (
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
    ):
        raise LiveGateError("background pid schema mismatch")
    return OwnedRosterRow(
        short_id=values["id"],
        session_id=values["sessionId"],
        name=values["name"],
        cwd=supplied_cwd.resolve(strict=False),
        state=values["state"],
        model=model,
        context_fingerprint=fingerprint,
        pid_present=pid is not None,
    )


def _recovery_short_id_from_payload(payload: Any, expected_name: str) -> str | None:
    if _NAME.fullmatch(expected_name) is None or not isinstance(payload, list):
        return None
    matches = [
        item for item in payload
        if isinstance(item, dict) and item.get("name") == expected_name
    ]
    if len(matches) != 1:
        return None
    item = matches[0]
    short_id = item.get("id")
    if (
        item.get("kind") != "background"
        or not isinstance(short_id, str)
        or _ROW_ID.fullmatch(short_id) is None
    ):
        return None
    return short_id


def _check_terminal(observation: BackgroundObservation) -> None:
    category = observation.stop_failure_category
    if category in _QUOTA_STOP_FAILURES:
        raise LiveGateError("QUOTA_PAUSED: background terminal quota or billing condition")
    if category is not None:
        raise LiveGateError("background StopFailure blocked the lifecycle canary")


def _same_owned_identity(left: OwnedRosterRow, right: OwnedRosterRow) -> bool:
    return (
        left.short_id == right.short_id
        and left.session_id == right.session_id
        and left.name == right.name
        and _same_path(left.cwd, right.cwd)
        and left.model == right.model
        and left.context_fingerprint == right.context_fingerprint
    )


def _verify_repository(observation: BackgroundObservation, *, clean: bool) -> None:
    if observation.remote_count != 0:
        raise LiveGateError("disposable repository gained a remote")
    if observation.current_commit != observation.base_commit:
        raise LiveGateError("unexpected commit observed")
    if clean:
        if observation.changed_paths or observation.proof_content is not None:
            raise LiveGateError("final checkout is not clean")
        return
    if observation.changed_paths != (_PROOF_RELATIVE,):
        raise LiveGateError("unexpected worktree change")
    if observation.proof_content != _PROOF_BYTES:
        raise LiveGateError("proof content mismatch")


def _verify_handoff(observation: BackgroundObservation) -> None:
    if not observation.session_start_observed:
        raise LiveGateError("SessionStart was not observed")
    if not observation.worktree_create_observed:
        raise LiveGateError("WorktreeCreate was not observed")
    paths = (
        observation.row.cwd,
        observation.lease_path,
        observation.event_path,
        observation.handoff_path,
        observation.guard_cwd,
        observation.roster_path,
    )
    if len({_path_key(path) for path in paths}) != 1:
        raise LiveGateError("handoff paths disagree")
    if not _same_path(
        observation.repository_common_dir, observation.worktree_common_dir,
    ):
        raise LiveGateError("common-dir mismatch")
    if observation.event_order[:3] != (
        "lease", "WorktreeCreate", "handler_stdout",
    ):
        raise LiveGateError("handoff event order mismatch")
    if not observation.first_write_after_handoff:
        raise LiveGateError("first write preceded handoff")


def _verify_state(
    observation: BackgroundObservation,
    *,
    expected_state: str,
    identity: OwnedRosterRow,
    clean: bool = False,
) -> None:
    _check_terminal(observation)
    if not _same_owned_identity(identity, observation.row):
        raise LiveGateError("respawn identity mismatch")
    if observation.row.state != expected_state:
        if expected_state == "stopped":
            raise LiveGateError("stop not stable")
        raise LiveGateError(f"background did not reach {expected_state}")
    if expected_state == "stopped" and observation.row.pid_present:
        raise LiveGateError("stopped row still has a live PID")
    if expected_state == "done" and observation.row.pid_present:
        raise LiveGateError("done row still has a live PID")
    _verify_repository(observation, clean=clean)


def run_write_race_canary(adapter: Any) -> BackgroundCanaryResult:
    last_row: OwnedRosterRow | None = None
    launch_attempted = False
    try:
        launch_attempted = True
        adapter.launch()
        working = adapter.observe("working")
        last_row = working.row
        _check_terminal(working)
        if working.row.state != "working":
            raise LiveGateError("background did not reach working")
        if working.row.model != _MODEL:
            raise LiveGateError("background model mismatch")
        _verify_handoff(working)
        _verify_repository(working, clean=False)

        adapter.stop(working.row)
        stopped_one = adapter.observe("stopped-1")
        last_row = stopped_one.row
        _verify_state(stopped_one, expected_state="stopped", identity=working.row)
        if stopped_one.stop_event_count < 1:
            raise LiveGateError("missing active Stop hook")

        adapter.stabilize(0.75)
        stopped_two = adapter.observe("stopped-2")
        last_row = stopped_two.row
        _verify_state(stopped_two, expected_state="stopped", identity=working.row)
        if stopped_two.stop_event_count < stopped_one.stop_event_count:
            raise LiveGateError("Stop hook count regressed")

        adapter.respawn(stopped_two.row)
        respawned = adapter.observe("respawn-working")
        last_row = respawned.row
        _verify_state(respawned, expected_state="working", identity=working.row)

        done = adapter.observe("done")
        last_row = done.row
        _verify_state(done, expected_state="done", identity=working.row)
        if done.stop_event_count <= stopped_two.stop_event_count:
            raise LiveGateError("missing final Stop hook")

        adapter.delete_proof(done.row)
        clean = adapter.observe("clean")
        last_row = clean.row
        _verify_state(clean, expected_state="done", identity=working.row, clean=True)
        return BackgroundCanaryResult(
            event_order=working.event_order,
            first_write_after_handoff=working.first_write_after_handoff,
            active_stop_stable_observations=2,
            provider_launch_count=2,
            worktree_create_count=1,
            stop_respawn_action_count=2,
            file_delete_count=1,
            session_start_observed=True,
            worktree_create_observed=True,
            handoff_equality_observed=True,
            common_dir_equality_observed=True,
            respawn_identity_equal=True,
            respawn_working_observed=True,
            final_state_category="done",
            stop_hook_observed=True,
            proof_only_change=True,
            proof_bytes_matched=True,
            final_checkout_clean=True,
        )
    except TimeoutError as exc:
        adapter.recover_timeout(last_row)
        raise LiveGateError("RECOVERY_REQUIRED: background lifecycle timed out") from exc
    except BaseException:
        if launch_attempted:
            adapter.recover_failure(last_row)
        raise


def _check_stop_failure(category: str | None, label: str) -> None:
    if category in _QUOTA_STOP_FAILURES:
        raise LiveGateError(f"QUOTA_PAUSED: {label} quota or billing condition")
    if category is not None:
        raise LiveGateError(f"{label} StopFailure blocked the lifecycle canary")


def _verify_needs_input_repository(observation: NeedsInputObservation) -> None:
    if observation.remote_count != 0:
        raise LiveGateError("needs-input repository gained a remote")
    if observation.current_commit != observation.base_commit:
        raise LiveGateError("needs-input repository commit changed")
    if not observation.checkout_clean:
        raise LiveGateError("needs-input checkout changed")


def _verify_needs_input_state(
    observation: NeedsInputObservation,
    *,
    identity: OwnedRosterRow,
    expected_state: str,
) -> None:
    _check_stop_failure(observation.stop_failure_category, "needs-input")
    if not _same_owned_identity(identity, observation.row):
        raise LiveGateError("needs-input identity mismatch")
    if observation.row.state != expected_state:
        if expected_state == "stopped":
            raise LiveGateError("needs-input stop not stable")
        raise LiveGateError(f"needs-input row did not reach {expected_state}")
    if expected_state == "stopped" and observation.row.pid_present:
        raise LiveGateError("needs-input stopped row still has a live PID")
    _verify_needs_input_repository(observation)


def run_needs_input_canary(
    adapter: Any,
    *,
    include_attach: bool,
) -> NeedsInputCanaryResult:
    last_row: OwnedRosterRow | None = None
    launch_attempted = False
    try:
        launch_attempted = True
        adapter.launch()
        blocked = adapter.observe("needs-input")
        last_row = blocked.row
        _check_stop_failure(blocked.stop_failure_category, "needs-input")
        if blocked.row.state not in {"blocked", "needs_input"}:
            raise LiveGateError("needs-input row exposed an undocumented state")
        if blocked.row.model != _MODEL:
            raise LiveGateError("needs-input model mismatch")
        if not blocked.handoff_equal:
            raise LiveGateError("needs-input handoff paths disagree")
        if not blocked.common_dir_equal:
            raise LiveGateError("needs-input common-dir mismatch")
        if not blocked.denied_write_observed:
            raise LiveGateError("denied Write acknowledgement was not observed")
        _verify_needs_input_repository(blocked)

        attach_observed = False
        attach_same_session = False
        if include_attach:
            attached = adapter.attach(blocked.row)
            last_row = attached.row
            attach_observed = attached.attach_exit_observed
            attach_same_session = attached.same_session_hook_observed
            if not _same_owned_identity(blocked.row, attached.row):
                raise LiveGateError("attach identity mismatch")
            if not attached.attach_exit_observed:
                raise LiveGateError("attach exit was not observed")
            if not attached.same_session_hook_observed:
                raise LiveGateError("attach same-session hook was not observed")
            if attached.working_transition_observed or attached.row.state == "working":
                raise LiveGateError("attach resumed work")
            if attached.row.state not in {"blocked", "needs_input"}:
                raise LiveGateError("attach changed the blocked state")
            if not attached.checkout_clean:
                raise LiveGateError("attach changed the checkout")

        adapter.stop(blocked.row)
        stopped_one = adapter.observe("stopped-1")
        last_row = stopped_one.row
        _verify_needs_input_state(
            stopped_one, identity=blocked.row, expected_state="stopped",
        )
        if stopped_one.stop_event_count < 1:
            raise LiveGateError("needs-input Stop hook was not observed")

        adapter.stabilize(0.75)
        stopped_two = adapter.observe("stopped-2")
        last_row = stopped_two.row
        _verify_needs_input_state(
            stopped_two, identity=blocked.row, expected_state="stopped",
        )
        if stopped_two.stop_event_count < stopped_one.stop_event_count:
            raise LiveGateError("needs-input Stop hook count regressed")

        return NeedsInputCanaryResult(
            needs_input_observed=True,
            attach_observed=attach_observed,
            attach_same_session=attach_same_session,
            stable_stop_observation_count=2,
            lifecycle_commands_status="PASS" if include_attach else "BLOCKED",
            denied_write_observed=True,
            checkout_clean=True,
            stop_hook_observed=True,
        )
    except TimeoutError as exc:
        if launch_attempted:
            adapter.recover_failure(last_row)
        raise LiveGateError("RECOVERY_REQUIRED: needs-input lifecycle timed out") from exc
    except BaseException:
        if launch_attempted:
            adapter.recover_failure(last_row)
        raise


def _validate_concurrency_rows(
    observation: ConcurrencyObservation,
    *,
    expected_names: tuple[str, ...],
    expected_state: str | None = None,
    identities: Mapping[str, OwnedRosterRow] | None = None,
) -> dict[str, OwnedRosterRow]:
    _check_stop_failure(observation.stop_failure_category, "concurrency")
    if not observation.checkout_clean:
        raise LiveGateError("concurrency checkout changed")
    if len(observation.rows) != len(expected_names):
        raise LiveGateError("required simultaneous background rows were not visible")
    by_name = {row.name: row for row in observation.rows}
    if len(by_name) != len(observation.rows) or set(by_name) != set(expected_names):
        raise LiveGateError("concurrency rows are duplicate or cross-owned")
    if len({row.short_id for row in observation.rows}) != len(observation.rows):
        raise LiveGateError("concurrency row IDs are not unique")
    if len({row.session_id for row in observation.rows}) != len(observation.rows):
        raise LiveGateError("concurrency session IDs are not unique")
    for name, row in by_name.items():
        if row.model != _MODEL:
            raise LiveGateError("concurrency model mismatch")
        if expected_state is None:
            if row.state not in {"working", "blocked"}:
                raise LiveGateError("concurrency row is not active")
        elif row.state != expected_state:
            raise LiveGateError("concurrency stop not stable")
        if expected_state == "stopped" and row.pid_present:
            raise LiveGateError("concurrency stopped row still has a live PID")
        if identities is not None and not _same_owned_identity(identities[name], row):
            raise LiveGateError("concurrency identity mismatch")
    return by_name


def run_concurrency_canary(
    adapter: Any,
    *,
    group_names: Sequence[str],
) -> ConcurrencyCanaryResult:
    names = tuple(group_names)
    if (
        len(names) != 2
        or len(set(names)) != 2
        or any(_NAME.fullmatch(name) is None for name in names)
    ):
        raise LiveGateError("concurrency requires exactly two safe unique names")
    owned: dict[str, OwnedRosterRow] = {}
    launch_attempted = False
    try:
        launch_attempted = True
        adapter.launch(names[0])
        first = adapter.observe("first-active", names[:1])
        owned.update(_validate_concurrency_rows(first, expected_names=names[:1]))
        if first.session_start_count < 1 or first.guard_allow_count < 1:
            raise LiveGateError("first concurrency launch was not fully observed")

        adapter.launch(names[1])
        simultaneous = adapter.observe("simultaneous", names)
        owned = _validate_concurrency_rows(simultaneous, expected_names=names)
        if simultaneous.session_start_count < 2 or simultaneous.guard_allow_count < 2:
            raise LiveGateError("two simultaneous launches were not fully observed")

        for name in names:
            adapter.stop(owned[name])
        stopped_one = adapter.observe("stopped-1", names)
        _validate_concurrency_rows(
            stopped_one,
            expected_names=names,
            expected_state="stopped",
            identities=owned,
        )
        if stopped_one.stop_hook_count < 2:
            raise LiveGateError("concurrency Stop hooks were not observed")

        adapter.stabilize(0.75)
        stopped_two = adapter.observe("stopped-2", names)
        _validate_concurrency_rows(
            stopped_two,
            expected_names=names,
            expected_state="stopped",
            identities=owned,
        )
        if stopped_two.stop_hook_count < stopped_one.stop_hook_count:
            raise LiveGateError("concurrency Stop hook count regressed")

        return ConcurrencyCanaryResult(
            observed_floor=2,
            provider_ceiling="UNKNOWN",
            policy_cap=2,
            stable_stop_observation_count=2,
            simultaneous_active_observed=True,
            stop_hook_observed=True,
            checkout_clean=True,
            active_state_categories=tuple(sorted({
                "needs_input_or_blocked" if row.state == "blocked" else row.state
                for row in simultaneous.rows
            })),
        )
    except TimeoutError as exc:
        if launch_attempted:
            adapter.recover_failure(tuple(owned.values()))
        raise LiveGateError("RECOVERY_REQUIRED: concurrency lifecycle timed out") from exc
    except BaseException:
        if launch_attempted:
            adapter.recover_failure(tuple(owned.values()))
        raise


def _cleanup_path_is_owned(path: Path, approved_root: Path) -> bool:
    try:
        candidate_metadata = path.stat(follow_symlinks=False)
        root_metadata = approved_root.stat(follow_symlinks=False)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            path.is_symlink()
            or approved_root.is_symlink()
            or getattr(candidate_metadata, "st_file_attributes", 0) & reparse
            or getattr(root_metadata, "st_file_attributes", 0) & reparse
        ):
            return False
        candidate = path.resolve(strict=True)
        root = approved_root.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return False
    return candidate != root


def _cleanup_audit_reasons(
    observations: Sequence[CleanupObservation],
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if not observations:
        reasons.add("no_plan_owned_targets")
        return tuple(sorted(reasons))
    short_ids: set[str] = set()
    worktrees: set[str] = set()
    for item in observations:
        row = item.row
        if row.short_id in short_ids or _path_key(row.cwd) in worktrees:
            reasons.add("duplicate_target")
        short_ids.add(row.short_id)
        worktrees.add(_path_key(row.cwd))
        if not _cleanup_path_is_owned(row.cwd, item.approved_disposable_root):
            reasons.add("path_outside_approved_root")
        if row.state not in {"stopped", "done", "failed"} or row.pid_present:
            reasons.add("nonterminal_row")
        if not _same_path(item.repository_common_dir, item.worktree_common_dir):
            reasons.add("common_dir_mismatch")
        if len({
            _path_key(row.cwd),
            _path_key(item.lease_path),
            _path_key(item.event_path),
        }) != 1:
            reasons.add("handoff_path_mismatch")
        counts = (
            item.status_line_count,
            item.commits_above_base,
            item.remote_count,
            item.matching_process_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            reasons.add("invalid_audit_count")
        else:
            if item.status_line_count:
                reasons.add("dirty_checkout")
            if item.commits_above_base:
                reasons.add("commits_above_base")
            if item.remote_count:
                reasons.add("remote_present")
            if item.matching_process_count:
                reasons.add("matching_process_present")
        if item.current_commit != item.base_commit:
            reasons.add("base_commit_drift")
        if item.registered_worktree is not True:
            reasons.add("worktree_not_registered")
        scope_values = (
            item.creation_scope_sha256,
            item.pending_scope_sha256,
            item.receipt_scope_sha256,
        )
        if (
            any(_HEX64.fullmatch(value) is None for value in scope_values)
            or len(set(scope_values)) != 1
            or item.consumed_creation_observed is not True
        ):
            reasons.add("creation_lineage_mismatch")
    return tuple(sorted(reasons))


def run_cleanup_canary(
    adapter: Any,
    observations: Sequence[CleanupObservation],
) -> CleanupCanaryResult:
    targets = tuple(observations)
    reasons = _cleanup_audit_reasons(targets)
    if reasons:
        return CleanupCanaryResult(
            status="RECOVERY_REQUIRED",
            audited_target_count=len(targets),
            removal_attempt_count=0,
            removal_success_count=0,
            worktree_remove_hook_count=0,
            residual_count=len(targets),
            residual_reasons=reasons,
            all_worktree_remove_events_matched=False,
            all_paths_absent=False,
            all_rows_absent=False,
            unrelated_state_unchanged=True,
        )

    attempts = 0
    successes = 0
    hook_count = 0
    for index, target in enumerate(targets):
        attempts += 1
        try:
            adapter.remove(target)
        except Exception:
            return CleanupCanaryResult(
                status="RECOVERY_REQUIRED",
                audited_target_count=len(targets),
                removal_attempt_count=attempts,
                removal_success_count=successes,
                worktree_remove_hook_count=hook_count,
                residual_count=len(targets) - successes,
                residual_reasons=("provider_remove_refused",),
                all_worktree_remove_events_matched=False,
                all_paths_absent=False,
                all_rows_absent=False,
                unrelated_state_unchanged=True,
            )
        try:
            removed = adapter.observe_removed(target)
        except Exception:
            removed = None
        if removed is None or not all((
            removed.row_identity_equal,
            removed.worktree_remove_event_match,
            removed.path_absent,
            removed.worktree_unregistered,
            removed.row_absent,
            removed.unrelated_rows_unchanged,
            removed.unrelated_worktrees_unchanged,
        )):
            return CleanupCanaryResult(
                status="RECOVERY_REQUIRED",
                audited_target_count=len(targets),
                removal_attempt_count=attempts,
                removal_success_count=successes,
                worktree_remove_hook_count=hook_count,
                residual_count=len(targets) - successes,
                residual_reasons=("removal_verification_failed",),
                all_worktree_remove_events_matched=False,
                all_paths_absent=False,
                all_rows_absent=False,
                unrelated_state_unchanged=False,
            )
        successes += 1
        hook_count += 1
    return CleanupCanaryResult(
        status="PASS",
        audited_target_count=len(targets),
        removal_attempt_count=attempts,
        removal_success_count=successes,
        worktree_remove_hook_count=hook_count,
        residual_count=0,
        residual_reasons=(),
        all_worktree_remove_events_matched=True,
        all_paths_absent=True,
        all_rows_absent=True,
        unrelated_state_unchanged=True,
    )


def project_background_result(
    result: BackgroundCanaryResult,
    prerequisite: ContextPrerequisite,
) -> dict[str, Any]:
    return {
        "status": "PASS",
        "declared_native_attestation": "incomplete",
        "context_missing_fields": list(prerequisite.missing_fields),
        "requested_model": _MODEL,
        "requested_effort": _EFFORT,
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_tokens": 274000,
        "foreground_tool_count": 0,
        "background_tool_count": 3,
        "foreground_permission_mode_category": "dontAsk",
        "background_permission_mode_category": "acceptEdits",
        "context_delta_fields": ["permission_mode", "tools"],
        "strict_empty_mcp_observed": True,
        "recursion_denies_complete": True,
        "usage_credits_off_confirmed": True,
        "foreground_overage_absent": True,
        "provider_launch_count": result.provider_launch_count,
        "worktree_create_count": result.worktree_create_count,
        "stop_respawn_action_count": result.stop_respawn_action_count,
        "file_delete_count": result.file_delete_count,
        "unique_row_count": 1,
        "session_start_observed": result.session_start_observed,
        "worktree_create_observed": result.worktree_create_observed,
        "handoff_equality_observed": result.handoff_equality_observed,
        "handoff_precedes_first_write": result.first_write_after_handoff,
        "common_dir_equality_observed": result.common_dir_equality_observed,
        "active_stop_stable_observation_count": result.active_stop_stable_observations,
        "respawn_identity_equal": result.respawn_identity_equal,
        "respawn_working_observed": result.respawn_working_observed,
        "final_state_category": result.final_state_category,
        "stop_hook_observed": result.stop_hook_observed,
        "proof_only_change": result.proof_only_change,
        "proof_bytes_matched": result.proof_bytes_matched,
        "proof_delete_count": result.file_delete_count,
        "final_checkout_clean": result.final_checkout_clean,
        "state_categories_observed": ["done", "stopped", "working"],
        "failed_state_observed": False,
        "stop_failure_observed": False,
        "opaque_values_persisted": False,
    }


def project_background_matrix(
    needs_input: NeedsInputCanaryResult,
    concurrency: ConcurrencyCanaryResult,
    *,
    state_categories: set[str],
    stop_failure_observed: bool,
    context_missing_fields: Sequence[str] = (),
) -> dict[str, Any]:
    required = (
        "working", "needs_input_or_blocked", "done", "failed", "stopped",
    )
    state_presence = {category: category in state_categories for category in required}
    matrix_complete = all(state_presence.values())
    lifecycle_complete = (
        matrix_complete
        and stop_failure_observed
        and needs_input.lifecycle_commands_status == "PASS"
        and concurrency.simultaneous_active_observed
    )
    return {
        "status": "PASS" if lifecycle_complete else "BLOCKED",
        "declared_native_attestation": "incomplete",
        "context_missing_fields": list(context_missing_fields),
        "requested_model": _MODEL,
        "requested_effort": _EFFORT,
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_auto_compaction_window_tokens": None,
        "effective_auto_compaction_trigger_percent": None,
        "effective_auto_compaction_trigger_tokens": None,
        "needs_input_tool_count": 2,
        "needs_input_permission_mode_category": "manual",
        "concurrency_tool_count": 1,
        "concurrency_permission_mode_category": "dontAsk",
        "strict_empty_mcp_observed": True,
        "recursion_denies_complete": True,
        "usage_credits_off_confirmed": True,
        "foreground_overage_absent": True,
        "background_overage_status": "UNKNOWN",
        "state_presence": state_presence,
        "agents_json_schema_status": "PASS" if matrix_complete else "BLOCKED",
        "lifecycle_commands_status": "PASS" if lifecycle_complete else "BLOCKED",
        "stop_failure_hook_status": "PASS" if stop_failure_observed else "BLOCKED",
        "needs_input_observed": needs_input.needs_input_observed,
        "denied_write_observed": needs_input.denied_write_observed,
        "attach_observed": needs_input.attach_observed,
        "attach_same_session": needs_input.attach_same_session,
        "needs_input_checkout_clean": needs_input.checkout_clean,
        "needs_input_stable_stop_observation_count": (
            needs_input.stable_stop_observation_count
        ),
        "needs_input_stop_hook_observed": needs_input.stop_hook_observed,
        "observed_floor": concurrency.observed_floor,
        "provider_ceiling": concurrency.provider_ceiling,
        "policy_cap": concurrency.policy_cap,
        "simultaneous_active_observed": concurrency.simultaneous_active_observed,
        "concurrency_checkout_clean": concurrency.checkout_clean,
        "concurrency_stable_stop_observation_count": (
            concurrency.stable_stop_observation_count
        ),
        "concurrency_stop_hook_observed": concurrency.stop_hook_observed,
        "active_state_categories": list(concurrency.active_state_categories),
        "agent_view_overhead": "UNKNOWN",
        "agent_view_source": "https://code.claude.com/docs/en/agent-view",
        "agent_view_retrieved_on": "2026-08-20",
        "opaque_values_persisted": False,
    }


def project_cleanup_result(result: CleanupCanaryResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "audited_target_count": result.audited_target_count,
        "removal_attempt_count": result.removal_attempt_count,
        "removal_success_count": result.removal_success_count,
        "worktree_remove_hook_count": result.worktree_remove_hook_count,
        "residual_count": result.residual_count,
        "residual_reason_categories": list(result.residual_reasons),
        "all_worktree_remove_events_matched": (
            result.all_worktree_remove_events_matched
        ),
        "all_paths_absent": result.all_paths_absent,
        "all_rows_absent": result.all_rows_absent,
        "unrelated_state_unchanged": result.unrelated_state_unchanged,
        "provider_native_remove_only": True,
        "direct_transcript_edit_count": 0,
        "fallback_git_or_filesystem_remove_count": 0,
        "opaque_values_persisted": False,
    }


def project_needs_input_result(
    result: NeedsInputCanaryResult,
    prerequisite: ContextPrerequisite,
) -> dict[str, Any]:
    return {
        "status": (
            "PASS" if result.lifecycle_commands_status == "PASS" else "BLOCKED"
        ),
        "declared_native_attestation": "incomplete",
        "context_missing_fields": list(prerequisite.missing_fields),
        "requested_model": _MODEL,
        "requested_effort": _EFFORT,
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_auto_compaction_window_tokens": None,
        "effective_auto_compaction_trigger_percent": None,
        "effective_auto_compaction_trigger_tokens": None,
        "foreground_tool_count": 0,
        "tool_count": 2,
        "foreground_permission_mode_category": "dontAsk",
        "permission_mode_category": "manual",
        "context_delta_fields": ["permission_mode", "tools"],
        "strict_empty_mcp_observed": True,
        "recursion_denies_complete": True,
        "usage_credits_off_confirmed": True,
        "foreground_overage_absent": True,
        "background_overage_status": "UNKNOWN",
        "provider_launch_count": 1,
        "worktree_create_count": 1,
        "stop_action_count": 1,
        "attach_action_count": 1 if result.attach_observed else 0,
        "needs_input_observed": result.needs_input_observed,
        "denied_write_observed": result.denied_write_observed,
        "attach_observed": result.attach_observed,
        "attach_same_session": result.attach_same_session,
        "stable_stop_observation_count": result.stable_stop_observation_count,
        "stop_hook_observed": result.stop_hook_observed,
        "checkout_clean": result.checkout_clean,
        "lifecycle_commands_status": result.lifecycle_commands_status,
        "state_categories_observed": ["needs_input_or_blocked", "stopped"],
        "failed_state_observed": False,
        "stop_failure_observed": False,
        "opaque_values_persisted": False,
    }


def project_concurrency_result(
    result: ConcurrencyCanaryResult,
    prerequisite: ContextPrerequisite,
) -> dict[str, Any]:
    return {
        "status": "PASS",
        "declared_native_attestation": "incomplete",
        "context_missing_fields": list(prerequisite.missing_fields),
        "requested_model": _MODEL,
        "requested_effort": _EFFORT,
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_auto_compaction_window_tokens": None,
        "effective_auto_compaction_trigger_percent": None,
        "effective_auto_compaction_trigger_tokens": None,
        "foreground_tool_count": 0,
        "tool_count": 1,
        "foreground_permission_mode_category": "dontAsk",
        "permission_mode_category": "dontAsk",
        "context_delta_fields": ["tools"],
        "strict_empty_mcp_observed": True,
        "recursion_denies_complete": True,
        "usage_credits_off_confirmed": True,
        "foreground_overage_absent": True,
        "background_overage_status": "UNKNOWN",
        "provider_launch_count": 2,
        "worktree_create_count": 0,
        "stop_action_count": 2,
        "simultaneous_active_observed": result.simultaneous_active_observed,
        "observed_floor": result.observed_floor,
        "provider_ceiling": result.provider_ceiling,
        "policy_cap": result.policy_cap,
        "stable_stop_observation_count": result.stable_stop_observation_count,
        "stop_hook_observed": result.stop_hook_observed,
        "checkout_clean": result.checkout_clean,
        "active_state_categories": list(result.active_state_categories),
        "state_categories_observed": sorted({
            *result.active_state_categories, "stopped",
        }),
        "failed_state_observed": False,
        "stop_failure_observed": False,
        "agent_view_overhead": "UNKNOWN",
        "agent_view_source": "https://code.claude.com/docs/en/agent-view",
        "agent_view_retrieved_on": "2026-08-20",
        "opaque_values_persisted": False,
    }


def _load_background_candidate(
    path: str | Path,
    *,
    expected_kind: str,
    expected_source_kind: str,
) -> dict[str, Any]:
    candidate = _read_json(Path(path), expected_kind)
    try:
        validate_fixture(candidate)
    except ValueError as exc:
        raise LiveGateError(f"{expected_kind} candidate is invalid") from exc
    if (
        candidate.get("kind") != expected_kind
        or candidate.get("source", {}).get("kind") != expected_source_kind
        or not isinstance(candidate.get("payload"), dict)
    ):
        raise LiveGateError(f"{expected_kind} candidate identity mismatch")
    return candidate


def _required_projection_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if type(value) is not bool:
        raise LiveGateError(f"background candidate {key} must be boolean")
    return value


def _context_missing_record(payload: Mapping[str, Any]) -> tuple[str, ...]:
    value = payload.get("context_missing_fields")
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        raise LiveGateError("background context missing-field record is invalid")
    return tuple(value)


def finalize_background_matrix(
    group_c_candidate: str | Path,
    group_d_candidate: str | Path,
    group_f_candidate: str | Path,
    stop_failure_contract: str | Path,
    *,
    output: str | Path,
) -> dict[str, Any]:
    candidates = (
        _load_background_candidate(
            group_c_candidate,
            expected_kind="live_background_lifecycle",
            expected_source_kind="bounded_background_projection",
        ),
        _load_background_candidate(
            group_d_candidate,
            expected_kind="live_background_needs_input",
            expected_source_kind="bounded_needs_input_projection",
        ),
        _load_background_candidate(
            group_f_candidate,
            expected_kind="live_background_concurrency",
            expected_source_kind="bounded_concurrency_projection",
        ),
    )
    versions = {candidate.get("observed_cli_version") for candidate in candidates}
    if len(versions) != 1 or not all(isinstance(version, str) and version for version in versions):
        raise LiveGateError("background candidate CLI versions disagree")
    version = next(iter(versions))
    cli_digests = {
        candidate.get("payload", {}).get("cli_content_sha256")
        for candidate in candidates
    }
    if (
        len(cli_digests) != 1
        or not all(isinstance(value, str) and _HEX64.fullmatch(value) for value in cli_digests)
    ):
        raise LiveGateError("background candidate CLI identities disagree")
    cli_digest = next(iter(cli_digests))

    stop_contract = _read_json(Path(stop_failure_contract), "StopFailure contract")
    try:
        validate_fixture(stop_contract)
    except ValueError as exc:
        raise LiveGateError("StopFailure contract is invalid") from exc
    if (
        stop_contract.get("kind") != "stop_failure_contract"
        or stop_contract.get("observed_cli_version") != version
        or stop_contract.get("payload", {}).get("documented_categories")
        != list(STOP_FAILURE_CATEGORIES)
    ):
        raise LiveGateError("StopFailure contract identity mismatch")

    c_payload, d_payload, f_payload = (
        candidate["payload"] for candidate in candidates
    )
    missing_records = {
        _context_missing_record(payload)
        for payload in (c_payload, d_payload, f_payload)
    }
    if len(missing_records) != 1:
        raise LiveGateError("background context missing fields disagree")
    context_missing = next(iter(missing_records))

    state_categories: set[str] = set()
    if c_payload.get("respawn_working_observed") is True:
        state_categories.add("working")
    if c_payload.get("final_state_category") == "done":
        state_categories.add("done")
    if c_payload.get("active_stop_stable_observation_count") == 2:
        state_categories.add("stopped")
    if d_payload.get("needs_input_observed") is True:
        state_categories.add("needs_input_or_blocked")
    if d_payload.get("stable_stop_observation_count") == 2:
        state_categories.add("stopped")
    active_states = f_payload.get("active_state_categories")
    if (
        not isinstance(active_states, list)
        or any(state not in {"working", "needs_input_or_blocked"} for state in active_states)
    ):
        raise LiveGateError("Group F active state categories are invalid")
    state_categories.update(active_states)
    if f_payload.get("stable_stop_observation_count") == 2:
        state_categories.add("stopped")
    if any(payload.get("failed_state_observed") is True for payload in (
        c_payload, d_payload, f_payload,
    )):
        state_categories.add("failed")
    stop_failure_observed = any(
        payload.get("stop_failure_observed") is True
        for payload in (c_payload, d_payload, f_payload)
    )

    needs = NeedsInputCanaryResult(
        needs_input_observed=_required_projection_bool(
            d_payload, "needs_input_observed",
        ),
        attach_observed=_required_projection_bool(d_payload, "attach_observed"),
        attach_same_session=_required_projection_bool(d_payload, "attach_same_session"),
        stable_stop_observation_count=int(
            d_payload.get("stable_stop_observation_count", 0)
        ),
        lifecycle_commands_status=str(d_payload.get("lifecycle_commands_status")),
        denied_write_observed=_required_projection_bool(
            d_payload, "denied_write_observed",
        ),
        checkout_clean=_required_projection_bool(d_payload, "checkout_clean"),
        stop_hook_observed=d_payload.get("stable_stop_observation_count") == 2,
    )
    concurrency = ConcurrencyCanaryResult(
        observed_floor=int(f_payload.get("observed_floor", 0)),
        provider_ceiling=str(f_payload.get("provider_ceiling")),
        policy_cap=int(f_payload.get("policy_cap", 0)),
        stable_stop_observation_count=int(
            f_payload.get("stable_stop_observation_count", 0)
        ),
        simultaneous_active_observed=_required_projection_bool(
            f_payload, "simultaneous_active_observed",
        ),
        stop_hook_observed=_required_projection_bool(
            f_payload, "stop_hook_observed",
        ),
        checkout_clean=_required_projection_bool(f_payload, "checkout_clean"),
        active_state_categories=tuple(active_states),
    )
    projection = project_background_matrix(
        needs,
        concurrency,
        state_categories=state_categories,
        stop_failure_observed=stop_failure_observed,
        context_missing_fields=context_missing,
    )
    projection["cli_content_sha256"] = cli_digest
    missing = list(context_missing)
    if "failed" not in state_categories:
        missing.append("failed_state_observation")
    if not stop_failure_observed:
        missing.append("live_stop_failure_observation")
    missing = sorted(set(missing))
    source_paths = (
        Path(group_c_candidate),
        Path(group_d_candidate),
        Path(group_f_candidate),
        Path(stop_failure_contract),
    )
    fixture = fixture_envelope(
        kind="live_background_matrix",
        observed_cli_version=version,
        source_kind="bounded_background_candidate_set",
        source_sha256=_combined_source_sha256(source_paths),
        payload=projection,
        observed=sorted(key for key in projection if key not in missing),
        missing=missing,
    )
    write_json_atomic(output, fixture)
    return fixture


def load_model_group_circuit(path: str | Path) -> dict[str, str]:
    target = Path(path)
    if not target.exists():
        return {"category": "none", "source_group": "none", "status": "READY"}
    payload = _read_json(target, "model-group circuit")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"category", "source_group", "status"}
        or payload.get("status") != "OPEN"
        or payload.get("category") not in {"quota", "schema", "worktree"}
        or payload.get("source_group") not in {"C", "D", "F"}
    ):
        raise LiveGateError("model-group circuit state is invalid")
    return {
        "category": payload["category"],
        "source_group": payload["source_group"],
        "status": payload["status"],
    }


def open_model_group_circuit(
    path: str | Path,
    *,
    category: str,
    source_group: str,
) -> None:
    if category not in {"quota", "schema", "worktree"}:
        raise ValueError("model-group circuit category is invalid")
    if source_group not in {"C", "D", "F"}:
        raise ValueError("model-group circuit source is invalid")
    target = Path(path)
    if target.exists():
        current = load_model_group_circuit(target)
        if current != {
            "category": category,
            "source_group": source_group,
            "status": "OPEN",
        }:
            raise LiveGateError("model-group circuit is already open")
        return
    write_json_atomic(target, {
        "category": category,
        "source_group": source_group,
        "status": "OPEN",
    })


def require_model_groups_available(path: str | Path) -> None:
    state = load_model_group_circuit(path)
    if state["status"] == "OPEN":
        raise LiveGateError(
            "model-group circuit is open: "
            f"{state['category']} from Group {state['source_group']}"
        )


def build_group_c_scope(
    *,
    git_head: str,
    cli_sha256: str,
    executable_manifest_sha256: str,
    launch_argv: Sequence[str],
    worktree_hook_argv: Sequence[str],
    proof_path: str,
    exact_targets: Sequence[str],
    trust_revision: int = 1,
) -> ApprovalScope:
    cli = str(launch_argv[0])
    binding = RuntimeBinding(
        token="{short_id}",
        state_key="group.short_id",
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        require_group_owned=True,
    )
    targets = tuple(exact_targets)
    effects = (
        SideEffectSpec(
            kind="provider_launch",
            argv_template=tuple(launch_argv),
            bindings=(),
            max_uses=1,
            exact_targets=targets,
        ),
        SideEffectSpec(
            kind="worktree_create",
            argv_template=tuple(worktree_hook_argv),
            bindings=(),
            max_uses=1,
            exact_targets=targets,
        ),
        SideEffectSpec(
            kind="stop",
            argv_template=(cli, "stop", "{short_id}"),
            bindings=(binding,),
            max_uses=2,
            exact_targets=targets,
        ),
        SideEffectSpec(
            kind="respawn",
            argv_template=(cli, "respawn", "{short_id}"),
            bindings=(binding,),
            max_uses=1,
            exact_targets=targets,
        ),
        SideEffectSpec(
            kind="file_delete",
            argv_template=("internal:file-delete", proof_path),
            bindings=(),
            max_uses=1,
            exact_targets=(proof_path,),
        ),
    )
    return ApprovalScope(
        schema_version=1,
        git_head=git_head,
        cli_sha256=cli_sha256,
        gate_ids=(
            "agents_json_schema",
            "lifecycle_commands",
            "session_start_hook",
            "worktree_create_hook",
            "stop_hook",
            "daemon_stop_race",
        ),
        side_effects=effects,
        max_provider_session_launches=2,
        max_worktree_creates=1,
        max_stop_respawn_actions=3,
        max_attach_actions=0,
        max_file_deletes=1,
        max_removals=0,
        background_internal_requests_acknowledged=True,
        executable_manifest_sha256=executable_manifest_sha256,
        trust_revision=trust_revision,
    )


def _short_id_binding() -> RuntimeBinding:
    return RuntimeBinding(
        token="{short_id}",
        state_key="group.short_id",
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        require_group_owned=True,
    )


def build_group_d_scope(
    *,
    git_head: str,
    cli_sha256: str,
    executable_manifest_sha256: str,
    launch_argv: Sequence[str],
    worktree_hook_argv: Sequence[str],
    include_attach: bool,
    exact_targets: Sequence[str],
    trust_revision: int = 1,
) -> ApprovalScope:
    cli = str(launch_argv[0])
    binding = _short_id_binding()
    targets = tuple(exact_targets)
    effects: list[SideEffectSpec] = [
        SideEffectSpec(
            kind="provider_launch",
            argv_template=tuple(launch_argv),
            bindings=(),
            max_uses=1,
            exact_targets=targets,
        ),
        SideEffectSpec(
            kind="worktree_create",
            argv_template=tuple(worktree_hook_argv),
            bindings=(),
            max_uses=1,
            exact_targets=targets,
        ),
        SideEffectSpec(
            kind="stop",
            argv_template=(cli, "stop", "{short_id}"),
            bindings=(binding,),
            max_uses=1,
            exact_targets=targets,
        ),
    ]
    if include_attach:
        effects.append(SideEffectSpec(
            kind="attach",
            argv_template=(cli, "attach", "{short_id}"),
            bindings=(binding,),
            max_uses=1,
            exact_targets=targets,
        ))
    return ApprovalScope(
        schema_version=1,
        git_head=git_head,
        cli_sha256=cli_sha256,
        gate_ids=(
            "agents_json_schema",
            "lifecycle_commands",
            "session_start_hook",
            "worktree_create_hook",
            "stop_hook",
        ),
        side_effects=tuple(effects),
        max_provider_session_launches=1,
        max_worktree_creates=1,
        max_stop_respawn_actions=1,
        max_attach_actions=1 if include_attach else 0,
        max_file_deletes=0,
        max_removals=0,
        background_internal_requests_acknowledged=True,
        executable_manifest_sha256=executable_manifest_sha256,
        trust_revision=trust_revision,
    )


def build_group_f_scope(
    *,
    git_head: str,
    cli_sha256: str,
    executable_manifest_sha256: str,
    launch_argv: Sequence[str],
    group_names: Sequence[str],
    exact_targets: Sequence[str],
    trust_revision: int = 1,
) -> ApprovalScope:
    names = tuple(group_names)
    if (
        len(names) != 2
        or len(set(names)) != 2
        or any(_NAME.fullmatch(name) is None for name in names)
    ):
        raise ValueError("Group F requires exactly two safe unique names")
    template = list(launch_argv)
    try:
        name_index = template.index("--name") + 1
    except ValueError as exc:
        raise ValueError("Group F launch argv is missing --name") from exc
    if name_index >= len(template) or template[name_index] != names[0]:
        raise ValueError("Group F launch argv does not bind the first name")
    template[name_index] = "{group_name}"
    name_pattern = "^(?:" + "|".join(re.escape(name) for name in names) + ")$"
    name_binding = RuntimeBinding(
        token="{group_name}",
        state_key="group.name",
        pattern=name_pattern,
        require_group_owned=True,
    )
    short_binding = _short_id_binding()
    cli = str(launch_argv[0])
    targets = tuple(exact_targets)
    effects = (
        SideEffectSpec(
            kind="provider_launch",
            argv_template=tuple(template),
            bindings=(name_binding,),
            max_uses=2,
            exact_targets=targets,
        ),
        SideEffectSpec(
            kind="stop",
            argv_template=(cli, "stop", "{short_id}"),
            bindings=(short_binding,),
            max_uses=2,
            exact_targets=targets,
        ),
    )
    return ApprovalScope(
        schema_version=1,
        git_head=git_head,
        cli_sha256=cli_sha256,
        gate_ids=("agents_json_schema", "lifecycle_commands", "stop_hook"),
        side_effects=effects,
        max_provider_session_launches=2,
        max_worktree_creates=0,
        max_stop_respawn_actions=2,
        max_attach_actions=0,
        max_file_deletes=0,
        max_removals=0,
        background_internal_requests_acknowledged=True,
        executable_manifest_sha256=executable_manifest_sha256,
        trust_revision=trust_revision,
    )


def build_group_g_scope(
    *,
    git_head: str,
    cli_sha256: str,
    executable_manifest_sha256: str,
    cli: str | Path,
    short_ids: Sequence[str],
    worktree_targets: Sequence[str],
    exact_targets: Sequence[str],
    cleanup_contract_sha256: str | None = None,
    trust_revision: int = 1,
) -> ApprovalScope:
    ids = tuple(short_ids)
    worktrees = tuple(worktree_targets)
    if (
        not ids
        or len(ids) != len(worktrees)
        or len(ids) > 16
        or len(set(ids)) != len(ids)
        or any(_ROW_ID.fullmatch(value) is None for value in ids)
    ):
        raise ValueError("Group G requires bounded unique row IDs and worktree targets")
    canonical_worktrees = tuple(
        str(Path(value).resolve(strict=False)) for value in worktrees
    )
    if len(set(map(os.path.normcase, canonical_worktrees))) != len(canonical_worktrees):
        raise ValueError("Group G worktree targets must be unique")
    digest = cleanup_contract_sha256
    if digest is None:
        digest = hashlib.sha256(_canonical_json({
            "short_ids": list(ids),
            "worktree_targets": list(canonical_worktrees),
            "exact_targets": list(exact_targets),
        })).hexdigest()
    if _HEX64.fullmatch(digest) is None:
        raise ValueError("Group G cleanup contract digest is invalid")
    binding = RuntimeBinding(
        token="{short_id}",
        state_key="group.short_id",
        pattern="^(?:" + "|".join(re.escape(value) for value in ids) + ")$",
        require_group_owned=True,
    )
    targets_list = list(canonical_worktrees)
    seen_targets = {os.path.normcase(value) for value in canonical_worktrees}
    for value in exact_targets:
        rendered = str(value)
        key = os.path.normcase(rendered)
        if key not in seen_targets:
            targets_list.append(rendered)
            seen_targets.add(key)
    targets = (*targets_list, "cleanup-contract-sha256:" + digest)
    effect = SideEffectSpec(
        kind="remove",
        argv_template=(str(Path(cli).resolve()), "rm", "{short_id}"),
        bindings=(binding,),
        max_uses=len(ids),
        exact_targets=targets,
    )
    return ApprovalScope(
        schema_version=1,
        git_head=git_head,
        cli_sha256=cli_sha256,
        gate_ids=("worktree_remove_hook",),
        side_effects=(effect,),
        max_provider_session_launches=0,
        max_worktree_creates=0,
        max_stop_respawn_actions=0,
        max_attach_actions=0,
        max_file_deletes=0,
        max_removals=len(ids),
        background_internal_requests_acknowledged=False,
        executable_manifest_sha256=executable_manifest_sha256,
        trust_revision=trust_revision,
    )


def _task2_identity_path(root: Path) -> Path:
    return root.absolute().parent / "host" / "bound-identity.json"


def _context_root(root: Path) -> Path:
    return root.absolute().parent / "context"


def _canonical_payload_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def load_context_prerequisite(
    context_root: str | Path,
    *,
    cli: str | Path,
    bound_identity: BoundCliIdentity,
) -> ContextPrerequisite:
    root = Path(context_root).resolve(strict=True)
    candidate_path = root / "live-context-candidate.json"
    candidate_bytes = _read_bounded(candidate_path, "Task 4 candidate")
    try:
        candidate = json.loads(candidate_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveGateError("Task 4 candidate is malformed") from exc
    try:
        validate_fixture(candidate)
    except ValueError as exc:
        raise LiveGateError("Task 4 candidate fixture is invalid") from exc
    if (
        candidate.get("kind") != "live_context_attestation"
        or candidate.get("observed_cli_version") != bound_identity.version
        or candidate.get("source", {}).get("kind") != "live_context_projection"
    ):
        raise LiveGateError("Task 4 candidate identity mismatch")
    payload = candidate.get("payload")
    if not isinstance(payload, dict):
        raise LiveGateError("Task 4 candidate payload is invalid")
    exact_requirements = {
        "cli_content_sha256": bound_identity.sha256,
        "init_subset_status": "PASS",
        "requested_model": _MODEL,
        "requested_effort": _EFFORT,
        "requested_setting_sources": "user,project,local",
        "requested_auto_compaction_window_tokens": 274000,
        "requested_auto_compaction_trigger_percent": None,
        "requested_auto_compaction_trigger_tokens": 274000,
        "effective_model": _MODEL,
        "tool_count": 0,
        "mcp_server_count": 0,
        "is_using_overage": False,
        "final_marker_matched": True,
        "checkout_clean": True,
        "background_eligible": True,
        "usage_credits_off_confirmed": True,
        "hook_error_observed": False,
    }
    if any(payload.get(key) != value for key, value in exact_requirements.items()):
        raise LiveGateError("Task 4 context prerequisite is not eligible")
    rate_statuses = payload.get("rate_statuses")
    if (
        not isinstance(rate_statuses, list)
        or any(status not in {"allowed", "allowed_warning"} for status in rate_statuses)
    ):
        raise LiveGateError("Task 4 rate status is not eligible")
    missing = payload.get("missing_fields")
    if (
        not isinstance(missing, list)
        or any(not isinstance(item, str) or not item for item in missing)
        or missing != sorted(set(missing))
    ):
        raise LiveGateError("Task 4 missing-field record is invalid")
    if candidate.get("coverage", {}).get("missing") != missing:
        raise LiveGateError("Task 4 missing-field coverage drifted")

    paths = ContextPaths(
        cwd=(root / "repo").resolve(strict=True),
        settings=(root / "settings.json").resolve(strict=True),
        empty_mcp=(root / "declared-empty.json").resolve(strict=True),
        event_log=root / "events.jsonl",
    )
    if _read_json(paths.empty_mcp, "Task 4 empty MCP") != {"mcpServers": {}}:
        raise LiveGateError("Task 4 MCP configuration drifted")
    expected_argv = list(build_context_argv(cli, paths))
    pending = _read_json(root / "pending-scope.json", "Task 4 pending scope")
    if not isinstance(pending, dict) or pending.get("cli_sha256") != bound_identity.sha256:
        raise LiveGateError("Task 4 pending scope identity mismatch")
    effects = pending.get("side_effects")
    if not isinstance(effects, list):
        raise LiveGateError("Task 4 pending scope side effects are invalid")
    launches = [effect for effect in effects if effect.get("kind") == "provider_launch"]
    if (
        len(effects) != 1
        or len(launches) != 1
        or launches[0].get("argv_template") != expected_argv
        or launches[0].get("max_uses") != 1
        or pending.get("max_provider_session_launches") != 1
        or any(pending.get(key) != 0 for key in (
            "max_worktree_creates",
            "max_stop_respawn_actions",
            "max_attach_actions",
            "max_file_deletes",
            "max_removals",
        ))
        or pending.get("background_internal_requests_acknowledged") is not False
    ):
        raise LiveGateError("Task 4 exact foreground argv is not bound")
    ledger = _read_json(root / "consumed-side-effects.json", "Task 4 consumed ledger")
    if not isinstance(ledger, list):
        raise LiveGateError("Task 4 consumed ledger is invalid")
    consumed = [item for item in ledger if item.get("kind") == "provider_launch"]
    if len(ledger) != 1 or len(consumed) != 1 or consumed[0].get("argv") != expected_argv:
        raise LiveGateError("Task 4 foreground launch was not consumed")
    source = candidate.get("source")
    if not isinstance(source, dict) or _HEX64.fullmatch(str(source.get("sha256", ""))) is None:
        raise LiveGateError("Task 4 candidate source is invalid")
    return ContextPrerequisite(
        observed_cli_version=bound_identity.version,
        source_sha256=source["sha256"],
        candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        scope_sha256=_canonical_payload_sha256(pending),
        missing_fields=tuple(missing),
    )


def build_group_c_argv(
    cli: str | Path,
    paths: BackgroundPaths,
    *,
    group_name: str,
    worktree_name: str,
) -> tuple[str, ...]:
    return tuple(build_background_argv(
        Path(cli),
        paths.settings,
        paths.empty_mcp,
        group_name,
        worktree_name,
        _MODEL,
        _EFFORT,
        BACKGROUND_PROMPT,
    ))


def _build_task6_argv(
    cli: str | Path,
    settings: str | Path,
    empty_mcp: str | Path,
    group_name: str,
    *,
    tools: str,
    permission_mode: str,
    prompt: str,
    worktree_name: str | None,
) -> tuple[str, ...]:
    if _NAME.fullmatch(group_name) is None:
        raise ValueError("background group name is invalid")
    argv = [
        str(Path(cli).resolve()),
        "--bg", "--name", group_name,
    ]
    if worktree_name is not None:
        if _NAME.fullmatch(worktree_name) is None:
            raise ValueError("background worktree name is invalid")
        argv.extend(("--worktree", worktree_name))
    argv.extend((
        "--model", _MODEL,
        "--effort", _EFFORT,
        "--autocompact", "274000",
        "--setting-sources", "user,project,local",
        "--settings", str(Path(settings).resolve()),
        "--tools", tools,
        "--disallowedTools", *_RECURSION_DENIES,
        "--permission-mode", permission_mode,
        "--strict-mcp-config",
        "--mcp-config", str(Path(empty_mcp).resolve()),
        prompt,
    ))
    return tuple(argv)


def build_needs_input_argv(
    cli: str | Path,
    settings: str | Path,
    empty_mcp: str | Path,
    group_name: str,
    worktree_name: str,
) -> tuple[str, ...]:
    return _build_task6_argv(
        cli,
        settings,
        empty_mcp,
        group_name,
        tools="Read,Write",
        permission_mode="manual",
        prompt=NEEDS_INPUT_PROMPT,
        worktree_name=worktree_name,
    )


def build_concurrency_argv(
    cli: str | Path,
    settings: str | Path,
    empty_mcp: str | Path,
    group_name: str,
) -> tuple[str, ...]:
    return _build_task6_argv(
        cli,
        settings,
        empty_mcp,
        group_name,
        tools="Bash",
        permission_mode="dontAsk",
        prompt=CONCURRENCY_PROMPT,
        worktree_name=None,
    )


def build_needs_input_hook_settings(
    python_exe: Path,
    hook_sink: Path,
    worktree_hook: Path,
    paths: BackgroundPaths,
    execution_id: str,
) -> dict[str, Any]:
    settings = build_background_hook_settings(
        python_exe,
        hook_sink,
        worktree_hook,
        Path(__file__),
        paths.event_log,
        paths.repo,
        paths.worktree_root,
        paths.lease_ack,
        paths.creation_lock,
        paths.guard_ack,
        execution_id,
        proof_relative=_NEEDS_INPUT_RELATIVE,
    )
    guard = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    guard["args"].extend(("--policy", "needs-input"))
    return settings


def build_concurrency_hook_settings(
    python_exe: Path,
    hook_sink: Path,
    paths: BackgroundPaths,
) -> dict[str, Any]:
    settings = build_event_hook_settings(
        python_exe,
        hook_sink,
        paths.event_log,
        events=("SessionStart", "Stop", "StopFailure"),
    )
    settings["hooks"]["PreToolUse"] = [{
        "hooks": [{
            "type": "command",
            "command": str(python_exe.resolve()),
            "args": [
                str(Path(__file__).resolve()),
                "concurrency-guard",
                "--guard-ack", str(paths.guard_ack.resolve()),
                "--repo", str(paths.repo.resolve()),
            ],
            "timeout": 30,
        }],
    }]
    return settings


def _validate_task6_layout_paths(
    target: Path,
    *,
    expected_mode: str,
) -> tuple[BackgroundPaths, dict[str, Any]]:
    layout = _read_json(target / "layout.json", f"Group {expected_mode} layout")
    if not isinstance(layout, dict) or layout.get("mode") != expected_mode:
        raise LiveGateError(f"Group {expected_mode} layout is invalid")
    paths = _paths_from_root(target)
    expected_paths = {
        "root": paths.root,
        "repo": paths.repo,
        "events": paths.event_log,
        "settings": paths.settings,
        "declared_config": paths.empty_mcp,
        "worktree_root": paths.worktree_root,
        "lease_ack": paths.lease_ack,
        "creation_lock": paths.creation_lock,
        "guard_ack": paths.guard_ack,
        "prompt": paths.prompt,
    }
    if any(layout.get(key) != str(path) for key, path in expected_paths.items()):
        raise LiveGateError(f"Group {expected_mode} layout path drifted")
    if layout.get("remote_count") != 0 or layout.get("root_contained") is not True:
        raise LiveGateError(f"Group {expected_mode} repository preparation is unsafe")
    if _read_json(paths.empty_mcp, "Task 6 empty MCP") != {"mcpServers": {}}:
        raise LiveGateError(f"Group {expected_mode} empty MCP drifted")
    return paths, layout


def _verify_task6_repository(
    paths: BackgroundPaths,
    layout: Mapping[str, Any],
) -> tuple[str, Path]:
    base_commit = layout.get("base_commit")
    raw_common = layout.get("repository_common_dir")
    if not isinstance(base_commit, str) or _HEX40.fullmatch(base_commit) is None:
        raise LiveGateError("Task 6 base commit is invalid")
    if not isinstance(raw_common, str):
        raise LiveGateError("Task 6 common-dir is invalid")
    common = Path(raw_common).resolve(strict=True)
    current_head, dirty = _git_checkpoint(paths.repo)
    if dirty or current_head != base_commit:
        raise LiveGateError("Task 6 base repository is dirty or drifted")
    if _git_text("git-remotes", ("git", "-C", str(paths.repo), "remote")):
        raise LiveGateError("Task 6 base repository has a remote")
    if not _same_path(_git_common_dir(paths.repo), common):
        raise LiveGateError("Task 6 repository common-dir drifted")
    return base_commit, common


def load_needs_input(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedBackground:
    target = Path(root).resolve(strict=True)
    if bound_identity.version == "unverified" or not bound_identity.matches(cli):
        raise LiveGateError("Task 2 bound CLI identity is required")
    paths, layout = _validate_task6_layout_paths(target, expected_mode="D")
    execution_id = layout.get("execution_id")
    group_name = layout.get("name")
    worktree_name = layout.get("worktree_name")
    if not isinstance(execution_id, str) or _HEX32.fullmatch(execution_id) is None:
        raise LiveGateError("Group D execution identity is invalid")
    if not isinstance(group_name, str) or _NAME.fullmatch(group_name) is None:
        raise LiveGateError("Group D name is invalid")
    if not isinstance(worktree_name, str) or _NAME.fullmatch(worktree_name) is None:
        raise LiveGateError("Group D worktree name is invalid")
    if paths.prompt.read_text(encoding="utf-8") != NEEDS_INPUT_PROMPT + "\n":
        raise LiveGateError("Group D prompt drifted")
    expected_settings = build_needs_input_hook_settings(
        Path(python_exe), Path(hook_sink), Path(worktree_hook), paths, execution_id,
    )
    if _read_json(paths.settings, "Group D settings") != expected_settings:
        raise LiveGateError("Group D settings drifted")
    handler = expected_settings["hooks"]["WorktreeCreate"][0]["hooks"][0]
    hook_argv = (handler["command"], *handler["args"])
    base_commit, common = _verify_task6_repository(paths, layout)
    return MaterializedBackground(
        paths=paths,
        launch_argv=build_needs_input_argv(
            cli, paths.settings, paths.empty_mcp, group_name, worktree_name,
        ),
        worktree_hook_argv=hook_argv,
        group_name=group_name,
        worktree_name=worktree_name,
        execution_id=execution_id,
        base_commit=base_commit,
        repository_common_dir=common,
        cli_sha256=bound_identity.sha256,
        cli_version=bound_identity.version,
    )


def materialize_needs_input(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedBackground:
    layout = prepare_background(
        Path(root), Path(python_exe), Path(hook_sink), Path(worktree_hook), Path(__file__),
    )
    target = Path(layout["root"])
    paths = _paths_from_root(target)
    execution_id = layout["execution_id"]
    layout.update({
        "mode": "D",
        "name": "subagent-harness-mcp-phase0a-d-" + execution_id[:16],
        "worktree_name": "phase0a-d-" + execution_id[:16],
        "approval_scope_sha256": None,
    })
    write_json_atomic(
        paths.settings,
        build_needs_input_hook_settings(
            Path(python_exe), Path(hook_sink), Path(worktree_hook), paths, execution_id,
        ),
    )
    paths.prompt.write_text(NEEDS_INPUT_PROMPT + "\n", encoding="utf-8", newline="\n")
    write_json_atomic(target / "layout.json", layout)
    return load_needs_input(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
    )


def load_concurrency(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedConcurrency:
    target = Path(root).resolve(strict=True)
    if bound_identity.version == "unverified" or not bound_identity.matches(cli):
        raise LiveGateError("Task 2 bound CLI identity is required")
    paths, layout = _validate_task6_layout_paths(target, expected_mode="F")
    execution_id = layout.get("execution_id")
    raw_names = layout.get("names")
    if not isinstance(execution_id, str) or _HEX32.fullmatch(execution_id) is None:
        raise LiveGateError("Group F execution identity is invalid")
    if (
        not isinstance(raw_names, list)
        or len(raw_names) != 2
        or len(set(raw_names)) != 2
        or any(not isinstance(name, str) or _NAME.fullmatch(name) is None for name in raw_names)
    ):
        raise LiveGateError("Group F names are invalid")
    names = (raw_names[0], raw_names[1])
    if paths.prompt.read_text(encoding="utf-8") != CONCURRENCY_PROMPT + "\n":
        raise LiveGateError("Group F prompt drifted")
    expected_settings = build_concurrency_hook_settings(
        Path(python_exe), Path(hook_sink), paths,
    )
    if _read_json(paths.settings, "Group F settings") != expected_settings:
        raise LiveGateError("Group F settings drifted")
    base_commit, common = _verify_task6_repository(paths, layout)
    return MaterializedConcurrency(
        paths=paths,
        launch_argv=build_concurrency_argv(
            cli, paths.settings, paths.empty_mcp, names[0],
        ),
        group_names=names,
        execution_id=execution_id,
        base_commit=base_commit,
        repository_common_dir=common,
        cli_sha256=bound_identity.sha256,
        cli_version=bound_identity.version,
    )


def materialize_concurrency(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedConcurrency:
    layout = prepare_background(
        Path(root), Path(python_exe), Path(hook_sink), Path(worktree_hook), Path(__file__),
    )
    target = Path(layout["root"])
    paths = _paths_from_root(target)
    execution_id = layout["execution_id"]
    names = [
        "subagent-harness-mcp-phase0a-f1-" + execution_id[:12],
        "subagent-harness-mcp-phase0a-f2-" + execution_id[:12],
    ]
    layout.update({
        "mode": "F",
        "name": names[0],
        "names": names,
        "worktree_name": None,
        "approval_scope_sha256": None,
    })
    write_json_atomic(
        paths.settings,
        build_concurrency_hook_settings(Path(python_exe), Path(hook_sink), paths),
    )
    paths.prompt.write_text(CONCURRENCY_PROMPT + "\n", encoding="utf-8", newline="\n")
    write_json_atomic(target / "layout.json", layout)
    return load_concurrency(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        bound_identity=bound_identity,
    )


def _paths_from_root(root: Path) -> BackgroundPaths:
    return BackgroundPaths(
        root=root,
        repo=root / "repo",
        settings=root / "settings.json",
        empty_mcp=root / "declared-empty.json",
        event_log=root / "events.jsonl",
        worktree_root=root / "worktrees",
        lease_ack=root / "worktree-lease.json",
        creation_lock=root / "repository-create.lock",
        guard_ack=root / "pretool-guard.jsonl",
        prompt=root / "prompt.txt",
        local_state=root / "local-state.json",
        consumed_ledger=root / "consumed-side-effects.json",
        candidate=root / "live-background-candidate.json",
    )


def load_background(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedBackground:
    target = Path(root).resolve(strict=True)
    if bound_identity.version == "unverified" or not bound_identity.matches(cli):
        raise LiveGateError("Task 2 bound CLI identity is required")
    layout = _read_json(target / "layout.json", "background layout")
    if not isinstance(layout, dict):
        raise LiveGateError("background layout must be an object")
    paths = _paths_from_root(target)
    for key, path in {
        "root": paths.root,
        "repo": paths.repo,
        "events": paths.event_log,
        "settings": paths.settings,
        "declared_config": paths.empty_mcp,
        "worktree_root": paths.worktree_root,
        "lease_ack": paths.lease_ack,
        "creation_lock": paths.creation_lock,
        "guard_ack": paths.guard_ack,
        "prompt": paths.prompt,
    }.items():
        if layout.get(key) != str(path):
            raise LiveGateError("background layout path drifted")
    execution_id = layout.get("execution_id")
    group_name = layout.get("name")
    worktree_name = layout.get("worktree_name")
    base_commit = layout.get("base_commit")
    raw_common = layout.get("repository_common_dir")
    if not isinstance(execution_id, str) or _HEX32.fullmatch(execution_id) is None:
        raise LiveGateError("background execution identity is invalid")
    if not isinstance(group_name, str) or _NAME.fullmatch(group_name) is None:
        raise LiveGateError("background group name is invalid")
    if not isinstance(worktree_name, str) or _NAME.fullmatch(worktree_name) is None:
        raise LiveGateError("background worktree name is invalid")
    if not isinstance(base_commit, str) or _HEX40.fullmatch(base_commit) is None:
        raise LiveGateError("background base commit is invalid")
    if not isinstance(raw_common, str):
        raise LiveGateError("background common-dir is invalid")
    common = Path(raw_common).resolve(strict=True)
    if layout.get("remote_count") != 0 or layout.get("root_contained") is not True:
        raise LiveGateError("background repository preparation is unsafe")
    if paths.prompt.read_text(encoding="utf-8") != BACKGROUND_PROMPT + "\n":
        raise LiveGateError("background prompt drifted")
    if _read_json(paths.empty_mcp, "background empty MCP") != {"mcpServers": {}}:
        raise LiveGateError("background empty MCP drifted")
    expected_settings = build_background_hook_settings(
        Path(python_exe),
        Path(hook_sink),
        Path(worktree_hook),
        Path(__file__),
        paths.event_log,
        paths.repo,
        paths.worktree_root,
        paths.lease_ack,
        paths.creation_lock,
        paths.guard_ack,
        execution_id,
    )
    if _read_json(paths.settings, "background settings") != expected_settings:
        raise LiveGateError("background settings drifted or generic hook was substituted")
    worktree_handler = expected_settings["hooks"]["WorktreeCreate"][0]["hooks"][0]
    hook_argv = (worktree_handler["command"], *worktree_handler["args"])
    current_head, dirty = _git_checkpoint(paths.repo)
    if dirty or current_head != base_commit:
        raise LiveGateError("background base repository is dirty or drifted")
    if _git_text("git-remotes", ("git", "-C", str(paths.repo), "remote")):
        raise LiveGateError("background base repository has a remote")
    if not _same_path(_git_common_dir(paths.repo), common):
        raise LiveGateError("background repository common-dir drifted")
    return MaterializedBackground(
        paths=paths,
        launch_argv=build_group_c_argv(
            cli, paths, group_name=group_name, worktree_name=worktree_name,
        ),
        worktree_hook_argv=hook_argv,
        group_name=group_name,
        worktree_name=worktree_name,
        execution_id=execution_id,
        base_commit=base_commit,
        repository_common_dir=common,
        cli_sha256=bound_identity.sha256,
        cli_version=bound_identity.version,
    )


def materialize_background(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedBackground:
    prepare_background(
        Path(root),
        Path(python_exe),
        Path(hook_sink),
        Path(worktree_hook),
        Path(__file__),
    )
    return load_background(
        root,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
    )


def _background_exact_targets(materialized: MaterializedBackground) -> tuple[str, ...]:
    paths = materialized.paths
    proof = paths.worktree_root / materialized.worktree_name / _PROOF_RELATIVE
    targets = (
        paths.root,
        paths.repo,
        paths.settings,
        paths.empty_mcp,
        paths.event_log,
        paths.event_log.with_suffix(paths.event_log.suffix + ".lock"),
        paths.worktree_root,
        paths.lease_ack,
        paths.creation_lock,
        paths.guard_ack,
        paths.guard_ack.with_suffix(paths.guard_ack.suffix + ".lock"),
        paths.local_state,
        paths.consumed_ledger,
        paths.candidate,
        paths.root.parent / "model-group-circuit.json",
        paths.root.parent / "ownership.json",
        paths.root.parent / "ownership.json.lock",
        proof,
    )
    return tuple(str(path.resolve(strict=False)) for path in targets)


def _task6_exact_targets(
    materialized: MaterializedBackground | MaterializedConcurrency,
    *,
    group: str,
) -> tuple[str, ...]:
    paths = materialized.paths
    common = [
        paths.root,
        paths.repo,
        paths.settings,
        paths.empty_mcp,
        paths.event_log,
        paths.event_log.with_suffix(paths.event_log.suffix + ".lock"),
        paths.guard_ack,
        paths.guard_ack.with_suffix(paths.guard_ack.suffix + ".lock"),
        paths.local_state,
        paths.consumed_ledger,
        paths.candidate,
        paths.root.parent / "model-group-circuit.json",
        paths.root.parent / "ownership.json",
        paths.root.parent / "ownership.json.lock",
    ]
    if group == "D":
        assert isinstance(materialized, MaterializedBackground)
        common.extend((
            paths.worktree_root,
            paths.lease_ack,
            paths.creation_lock,
            paths.worktree_root / materialized.worktree_name / _NEEDS_INPUT_RELATIVE,
        ))
    elif group != "F":
        raise ValueError("unknown Task 6 group")
    return tuple(str(path.resolve(strict=False)) for path in common)


def build_task6_execution_manifest(
    materialized: MaterializedBackground | MaterializedConcurrency,
    prerequisite: ContextPrerequisite,
    *,
    group: str,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
) -> tuple[str, BoundExecutableManifest, dict[str, Any]]:
    if group not in {"D", "F"}:
        raise ValueError("unknown Task 6 group")
    generated = {
        "README.md": materialized.paths.repo / "README.md",
        "declared-empty.json": materialized.paths.empty_mcp,
        "prompt.txt": materialized.paths.prompt,
        "settings.json": materialized.paths.settings,
    }
    source_paths = {
        Path(materialized.launch_argv[0]),
        Path(python_exe),
        _expected_python_process_image(Path(python_exe)),
        Path(hook_sink),
        Path(worktree_hook),
        Path(__file__),
        Path(__file__).with_name("background_probe.py"),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("core.py"),
        Path(__file__).with_name("fixtures.py"),
        Path(__file__).with_name("hook_sink.py"),
        Path(__file__).with_name("live_common.py"),
        Path(__file__).with_name("live_context.py"),
        Path(__file__).with_name("live_host.py"),
        Path(__file__).with_name("live_init.py"),
        Path(__file__).with_name("locking.py"),
        Path(__file__).with_name("worktree_hook.py"),
        *generated.values(),
    }
    entries: list[BoundExecutableFile] = []
    for path in sorted((path.resolve(strict=True) for path in source_paths), key=str):
        identity = BoundCliIdentity.capture(path, version="unverified")
        entries.append(BoundExecutableFile(
            canonical_path=identity.canonical_path,
            sha256=identity.sha256,
            file_identity=identity.file_identity,
        ))
    manifest = BoundExecutableManifest(
        repository_id=f"group-{group.lower()}-generated",
        trust_revision=1,
        entries=tuple(entries),
    )
    contract: dict[str, Any] = {
        "schema_version": 1,
        "group": group,
        "file_manifest_sha256": manifest.sha256,
        "launch_argv": list(materialized.launch_argv),
        "base_commit": materialized.base_commit,
        "repository_common_dir": str(materialized.repository_common_dir),
        "context_source_sha256": prerequisite.source_sha256,
        "context_candidate_sha256": prerequisite.candidate_sha256,
        "context_scope_sha256": prerequisite.scope_sha256,
        "generated_file_sha256": {
            name: _sha256_file(path) for name, path in sorted(generated.items())
        },
        "mutable_targets": list(_task6_exact_targets(materialized, group=group)),
    }
    if group == "D":
        assert isinstance(materialized, MaterializedBackground)
        contract.update({
            "group_name": materialized.group_name,
            "worktree_name": materialized.worktree_name,
            "worktree_hook_argv": list(materialized.worktree_hook_argv),
            "attempted_write_relative": _NEEDS_INPUT_RELATIVE,
        })
    else:
        assert isinstance(materialized, MaterializedConcurrency)
        contract["group_names"] = list(materialized.group_names)
        contract["allowed_command"] = _CONCURRENCY_COMMAND
    return _execution_contract_digest(contract), manifest, contract


def _execution_contract_digest(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(contract)).hexdigest()


def build_background_execution_manifest(
    materialized: MaterializedBackground,
    prerequisite: ContextPrerequisite,
    *,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
) -> tuple[str, BoundExecutableManifest, dict[str, Any]]:
    generated = {
        "README.md": materialized.paths.repo / "README.md",
        "declared-empty.json": materialized.paths.empty_mcp,
        "prompt.txt": materialized.paths.prompt,
        "settings.json": materialized.paths.settings,
    }
    source_paths = {
        Path(materialized.launch_argv[0]),
        Path(python_exe),
        _expected_python_process_image(Path(python_exe)),
        Path(hook_sink),
        Path(worktree_hook),
        Path(__file__),
        Path(__file__).with_name("background_probe.py"),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("core.py"),
        Path(__file__).with_name("fixtures.py"),
        Path(__file__).with_name("hook_sink.py"),
        Path(__file__).with_name("live_common.py"),
        Path(__file__).with_name("live_context.py"),
        Path(__file__).with_name("live_host.py"),
        Path(__file__).with_name("live_init.py"),
        Path(__file__).with_name("locking.py"),
        Path(__file__).with_name("worktree_hook.py"),
        *generated.values(),
    }
    entries: list[BoundExecutableFile] = []
    for path in sorted((path.resolve(strict=True) for path in source_paths), key=str):
        identity = BoundCliIdentity.capture(path, version="unverified")
        entries.append(BoundExecutableFile(
            canonical_path=identity.canonical_path,
            sha256=identity.sha256,
            file_identity=identity.file_identity,
        ))
    manifest = BoundExecutableManifest(
        repository_id="group-c-generated",
        trust_revision=1,
        entries=tuple(entries),
    )
    contract = {
        "schema_version": 1,
        "file_manifest_sha256": manifest.sha256,
        "launch_argv": list(materialized.launch_argv),
        "worktree_hook_argv": list(materialized.worktree_hook_argv),
        "group_name": materialized.group_name,
        "worktree_name": materialized.worktree_name,
        "base_commit": materialized.base_commit,
        "repository_common_dir": str(materialized.repository_common_dir),
        "proof_relative": _PROOF_RELATIVE,
        "proof_sha256": hashlib.sha256(_PROOF_BYTES).hexdigest(),
        "context_source_sha256": prerequisite.source_sha256,
        "context_candidate_sha256": prerequisite.candidate_sha256,
        "context_scope_sha256": prerequisite.scope_sha256,
        "generated_file_sha256": {
            name: _sha256_file(path) for name, path in sorted(generated.items())
        },
        "mutable_targets": list(_background_exact_targets(materialized)),
    }
    return _execution_contract_digest(contract), manifest, contract


def _scope_payload(scope: ApprovalScope) -> dict[str, Any]:
    return json.loads(json.dumps(scope.to_dict()))


def _require_background_capabilities(capabilities: Mapping[str, bool]) -> None:
    required = (
        "tools_empty_documented",
        "prompt_suggestions_false_documented",
        "stop_help_recognized",
        "respawn_help_recognized",
    )
    if any(capabilities.get(key) is not True for key in required):
        raise LiveGateError("Task 2 did not bind required Group C capabilities")


def _require_task6_capabilities(
    capabilities: Mapping[str, bool],
    *,
    group: str,
    include_attach: bool = False,
) -> None:
    required = [
        "tools_empty_documented",
        "prompt_suggestions_false_documented",
        "stop_help_recognized",
    ]
    if group == "D" and include_attach:
        required.append("attach_help_recognized")
    if group not in {"D", "F"}:
        raise ValueError("unknown Task 6 capability group")
    if any(capabilities.get(key) is not True for key in required):
        raise LiveGateError(f"Task 2 did not bind required Group {group} capabilities")


def preview_background(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    context_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    assert_no_credential_overrides(os.environ if env is None else env)
    git_head, dirty = _git_checkpoint(project_root)
    if dirty:
        raise LiveGateError("tracked checkout must be clean before Group C preview")
    target = Path(root).absolute()
    require_model_groups_available(_model_circuit_path(target))
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_background_capabilities(load_bound_host_capabilities(identity_path))
    prerequisite = load_context_prerequisite(
        _context_root(target) if context_root is None else context_root,
        cli=cli,
        bound_identity=bound_identity,
    )
    materialized = materialize_background(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
    )
    manifest_sha256, _manifest, contract = build_background_execution_manifest(
        materialized,
        prerequisite,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
    )
    proof = materialized.paths.worktree_root / materialized.worktree_name / _PROOF_RELATIVE
    scope = build_group_c_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        launch_argv=materialized.launch_argv,
        worktree_hook_argv=materialized.worktree_hook_argv,
        proof_path=str(proof.resolve(strict=False)),
        exact_targets=_background_exact_targets(materialized),
    )
    payload = _scope_payload(scope)
    write_json_atomic(materialized.paths.root / "pending-scope.json", payload)
    layout = _read_json(materialized.paths.root / "layout.json", "background layout")
    layout["approval_scope_sha256"] = approval_digest(scope)
    write_json_atomic(materialized.paths.root / "layout.json", layout)
    return {
        "scope": payload,
        "scope_sha256": approval_digest(scope),
        "execution_contract": contract,
        "context_attestation": "incomplete",
        "credit_control": (
            "foreground isUsingOverage=false plus immediate usage-credits-off confirmation; "
            "background has no per-turn overage stream"
        ),
    }


def _model_circuit_path(target: Path) -> Path:
    return target.absolute().parent / "model-group-circuit.json"


def preview_needs_input(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    include_attach: bool,
    context_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    assert_no_credential_overrides(os.environ if env is None else env)
    git_head, dirty = _git_checkpoint(project_root)
    if dirty:
        raise LiveGateError("tracked checkout must be clean before Group D preview")
    target = Path(root).absolute()
    require_model_groups_available(_model_circuit_path(target))
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_task6_capabilities(
        load_bound_host_capabilities(identity_path),
        group="D",
        include_attach=include_attach,
    )
    prerequisite = load_context_prerequisite(
        _context_root(target) if context_root is None else context_root,
        cli=cli,
        bound_identity=bound_identity,
    )
    materialized = materialize_needs_input(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
    )
    manifest_sha256, _manifest, contract = build_task6_execution_manifest(
        materialized,
        prerequisite,
        group="D",
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
    )
    scope = build_group_d_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        launch_argv=materialized.launch_argv,
        worktree_hook_argv=materialized.worktree_hook_argv,
        include_attach=include_attach,
        exact_targets=_task6_exact_targets(materialized, group="D"),
    )
    payload = _scope_payload(scope)
    write_json_atomic(materialized.paths.root / "pending-scope.json", payload)
    layout = _read_json(materialized.paths.root / "layout.json", "Group D layout")
    layout["approval_scope_sha256"] = approval_digest(scope)
    layout["include_attach"] = include_attach
    write_json_atomic(materialized.paths.root / "layout.json", layout)
    return {
        "group": "D",
        "scope": payload,
        "scope_sha256": approval_digest(scope),
        "execution_contract": contract,
        "context_attestation": "incomplete",
        "credit_control": (
            "foreground usage credits off was confirmed; background has no per-turn "
            "overage stream"
        ),
    }


def preview_concurrency(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    context_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    assert_no_credential_overrides(os.environ if env is None else env)
    git_head, dirty = _git_checkpoint(project_root)
    if dirty:
        raise LiveGateError("tracked checkout must be clean before Group F preview")
    target = Path(root).absolute()
    require_model_groups_available(_model_circuit_path(target))
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_task6_capabilities(
        load_bound_host_capabilities(identity_path), group="F",
    )
    prerequisite = load_context_prerequisite(
        _context_root(target) if context_root is None else context_root,
        cli=cli,
        bound_identity=bound_identity,
    )
    materialized = materialize_concurrency(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
    )
    manifest_sha256, _manifest, contract = build_task6_execution_manifest(
        materialized,
        prerequisite,
        group="F",
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
    )
    scope = build_group_f_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        launch_argv=materialized.launch_argv,
        group_names=materialized.group_names,
        exact_targets=_task6_exact_targets(materialized, group="F"),
    )
    payload = _scope_payload(scope)
    write_json_atomic(materialized.paths.root / "pending-scope.json", payload)
    layout = _read_json(materialized.paths.root / "layout.json", "Group F layout")
    layout["approval_scope_sha256"] = approval_digest(scope)
    write_json_atomic(materialized.paths.root / "layout.json", layout)
    return {
        "group": "F",
        "scope": payload,
        "scope_sha256": approval_digest(scope),
        "execution_contract": contract,
        "context_attestation": "incomplete",
        "observed_floor": 0,
        "provider_ceiling": "UNKNOWN",
        "policy_cap": 2,
        "credit_control": (
            "foreground usage credits off was confirmed; background has no per-turn "
            "overage stream"
        ),
    }


def _cleanup_paths(root: Path) -> CleanupPaths:
    return CleanupPaths(
        root=root,
        contract=root / "cleanup-contract.json",
        pending_scope=root / "pending-scope.json",
        consumed_ledger=root / "consumed-side-effects.json",
        residual=root / "recovery-residual.json",
        candidate=root / "live-worktree-remove-candidate.json",
    )


def _cleanup_roster_payload(
    cli: str | Path,
    *,
    env: Mapping[str, str],
) -> Any:
    result = run_argv(
        "cleanup-agents-json",
        [str(Path(cli).resolve()), "agents", "--json", "--all"],
        timeout_seconds=15,
        env=env,
    )
    if result.exit_code != 0 or result.timed_out:
        raise LiveGateError("cleanup agents JSON query failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LiveGateError("cleanup agents JSON query returned malformed data") from exc
    try:
        normalize_agents(payload)
    except ValueError as exc:
        raise LiveGateError(str(exc)) from exc
    return payload


def _cleanup_row_fingerprint(row: OwnedRosterRow) -> str:
    return hashlib.sha256(_canonical_json({
        "short_id": row.short_id,
        "session_id": row.session_id,
        "name": row.name,
        "cwd": str(row.cwd.resolve(strict=False)),
        "state": row.state,
        "model": row.model,
        "context_fingerprint": row.context_fingerprint,
        "pid_present": row.pid_present,
    })).hexdigest()


def _cleanup_worktree_inventory(repository: Path) -> tuple[Path, ...]:
    raw = _git_text(
        "cleanup-worktree-list",
        ("git", "-C", str(repository), "worktree", "list", "--porcelain"),
    )
    paths: list[Path] = []
    for line in raw.splitlines():
        if line.startswith("worktree "):
            value = line[len("worktree "):]
            candidate = Path(value)
            if not candidate.is_absolute():
                raise LiveGateError("Git worktree inventory returned a relative path")
            paths.append(candidate.resolve(strict=False))
    return tuple(paths)


def _cleanup_registered(repository: Path, target: Path) -> bool:
    return any(_same_path(path, target) for path in _cleanup_worktree_inventory(repository))


def _cleanup_process_count(
    row: OwnedRosterRow,
    *,
    env: Mapping[str, str],
) -> int:
    if row.pid_present:
        return 1
    task_env = dict(env)
    task_env["SUBAGENT_PHASE0A_CLEANUP_MARKER"] = row.name
    if os.name == "nt":
        system_root = task_env.get("SystemRoot", r"C:\Windows")
        powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        script = (
            "$m=$env:SUBAGENT_PHASE0A_CLEANUP_MARKER;"
            "$n=@(Get-CimInstance Win32_Process | Where-Object {"
            "$_.CommandLine -and $_.CommandLine.Contains($m)}).Count;"
            "[Console]::Out.Write($n)"
        )
        result = run_argv(
            "cleanup-process-snapshot",
            (str(powershell), "-NoProfile", "-NonInteractive", "-Command", script),
            timeout_seconds=15,
            env=task_env,
        )
        if result.exit_code != 0 or result.timed_out:
            raise LiveGateError("cleanup process snapshot failed")
        try:
            count = int(result.stdout.strip())
        except ValueError as exc:
            raise LiveGateError("cleanup process snapshot was malformed") from exc
        if count < 0:
            raise LiveGateError("cleanup process snapshot was invalid")
        return count
    result = run_argv(
        "cleanup-process-snapshot",
        ("ps", "-eo", "args="),
        timeout_seconds=15,
        env=task_env,
    )
    if result.exit_code != 0 or result.timed_out:
        raise LiveGateError("cleanup process snapshot failed")
    return sum(row.name in line for line in result.stdout.splitlines())


def _cleanup_creation_lineage(
    source_root: Path,
    *,
    receipt: Path,
) -> tuple[str, str, str, bool]:
    pending = _read_json(source_root / "pending-scope.json", "creation pending scope")
    if not isinstance(pending, dict):
        raise LiveGateError("creation pending scope is invalid")
    pending_sha = _canonical_payload_sha256(pending)
    layout = _read_json(source_root / "layout.json", "creation layout")
    if not isinstance(layout, dict):
        raise LiveGateError("creation layout is invalid")
    creation_sha = layout.get("approval_scope_sha256")
    if not isinstance(creation_sha, str):
        creation_sha = ""

    receipt_payload = _read_json(receipt, "creation approval receipt")
    if (
        not isinstance(receipt_payload, dict)
        or set(receipt_payload) != {
            "scope_sha256", "approved_at", "expires_at", "consumed_at",
            "claimed_execution_id",
        }
    ):
        raise LiveGateError("creation approval receipt is invalid")
    receipt_sha = receipt_payload.get("scope_sha256")
    if not isinstance(receipt_sha, str):
        receipt_sha = ""
    claim = _read_json(
        receipt.with_name(receipt.name + ".claim"),
        "creation approval claim",
    )
    claimed_id = receipt_payload.get("claimed_execution_id")
    claim_equal = (
        isinstance(claim, dict)
        and set(claim) == {"execution_id", "scope_sha256", "claimed_at"}
        and isinstance(claimed_id, str)
        and claimed_id
        and isinstance(claim.get("claimed_at"), str)
        and bool(claim.get("claimed_at"))
        and claim.get("execution_id") == claimed_id
        and claim.get("scope_sha256") == receipt_sha
        and receipt_payload.get("consumed_at") is not None
    )

    effects = pending.get("side_effects")
    if not isinstance(effects, list):
        raise LiveGateError("creation scope side effects are invalid")
    worktree_effects = [
        item for item in effects
        if isinstance(item, dict) and item.get("kind") == "worktree_create"
    ]
    launch_effects = [
        item for item in effects
        if isinstance(item, dict) and item.get("kind") == "provider_launch"
    ]
    ledger = _read_json(source_root / "consumed-side-effects.json", "creation ledger")
    if not isinstance(ledger, list) or any(not isinstance(item, dict) for item in ledger):
        raise LiveGateError("creation ledger is invalid")
    consumed_worktrees = [item for item in ledger if item.get("kind") == "worktree_create"]
    consumed_launches = [item for item in ledger if item.get("kind") == "provider_launch"]
    consumed_equal = (
        len(worktree_effects) == 1
        and len(launch_effects) == 1
        and len(consumed_worktrees) == 1
        and len(consumed_launches) >= 1
        and consumed_worktrees[0].get("argv") == worktree_effects[0].get("argv_template")
        and consumed_worktrees[0].get("targets") == worktree_effects[0].get("exact_targets")
        and all(
            item.get("targets") == launch_effects[0].get("exact_targets")
            and item.get("argv") == launch_effects[0].get("argv_template")
            for item in consumed_launches
        )
    )
    return creation_sha, pending_sha, receipt_sha, bool(claim_equal and consumed_equal)


def _cleanup_status_line_count(worktree: Path) -> int:
    raw = _git_text(
        "cleanup-git-status",
        ("git", "-C", str(worktree), "status", "--porcelain=v1"),
    )
    return len(raw.splitlines()) if raw else 0


def _cleanup_commits_above_base(worktree: Path, base_commit: str) -> int:
    raw = _git_text(
        "cleanup-commits-above-base",
        ("git", "-C", str(worktree), "rev-list", "--count", f"{base_commit}..HEAD"),
    )
    try:
        count = int(raw)
    except ValueError as exc:
        raise LiveGateError("cleanup commit count is malformed") from exc
    if count < 0:
        raise LiveGateError("cleanup commit count is invalid")
    return count


def discover_cleanup_observations(
    live_root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    bound_identity: BoundCliIdentity,
    env: Mapping[str, str],
) -> tuple[CleanupObservation, ...]:
    root = Path(live_root).resolve(strict=True)
    roster = _cleanup_roster_payload(cli, env=env)
    observations: list[CleanupObservation] = []
    for source_group, directory, receipt_name in _CLEANUP_SOURCES:
        source_root = root / directory
        if not source_root.exists():
            continue
        if source_group == "C":
            materialized = load_background(
                source_root,
                cli=cli,
                python_exe=python_exe,
                hook_sink=hook_sink,
                worktree_hook=worktree_hook,
                bound_identity=bound_identity,
            )
        else:
            materialized = load_needs_input(
                source_root,
                cli=cli,
                python_exe=python_exe,
                hook_sink=hook_sink,
                worktree_hook=worktree_hook,
                bound_identity=bound_identity,
            )
        row = parse_owned_roster(roster, materialized.group_name)
        local = _read_json(materialized.paths.local_state, f"Group {source_group} local state")
        expected_local = {
            "group": {
                "short_id": row.short_id,
                "session_id": row.session_id,
                "worktree_path": str(row.cwd),
            }
        }
        if local != expected_local:
            raise LiveGateError(f"Group {source_group} local state drifted")
        lease = _read_json(materialized.paths.lease_ack, f"Group {source_group} lease")
        if (
            not isinstance(lease, dict)
            or set(lease) != {
                "execution_id", "repository_common_dir", "status", "worktree_path",
            }
            or lease.get("execution_id") != materialized.execution_id
            or lease.get("status") != "leased"
            or not _same_path(
                Path(str(lease.get("repository_common_dir", ""))),
                materialized.repository_common_dir,
            )
        ):
            raise LiveGateError(f"Group {source_group} lease is invalid")
        lease_path = Path(str(lease.get("worktree_path", "")))
        events = _read_json_lines(
            materialized.paths.event_log,
            f"Group {source_group} event log",
        )
        worktree_events = [
            event for event in events
            if event.get("hook_event_name") == "WorktreeCreate"
            and event.get("execution_id") == materialized.execution_id
        ]
        if len(worktree_events) != 1:
            raise LiveGateError(f"Group {source_group} WorktreeCreate lineage mismatch")
        event_session = worktree_events[0].get("session_fingerprint")
        if event_session is not None and event_session != fingerprint(row.session_id):
            raise LiveGateError(f"Group {source_group} WorktreeCreate session mismatch")
        event_path = Path(str(worktree_events[0].get("worktree_path", "")))
        creation_sha, pending_sha, receipt_sha, consumed = _cleanup_creation_lineage(
            source_root,
            receipt=root / "approvals" / receipt_name,
        )
        current_commit, _dirty = _git_checkpoint(row.cwd)
        remotes = _git_text(
            "cleanup-git-remotes", ("git", "-C", str(row.cwd), "remote"),
        )
        observations.append(CleanupObservation(
            row=row,
            approved_disposable_root=materialized.paths.worktree_root,
            repository_root=materialized.paths.repo,
            repository_common_dir=materialized.repository_common_dir,
            worktree_common_dir=_git_common_dir(row.cwd),
            lease_path=lease_path,
            event_path=event_path,
            base_commit=materialized.base_commit,
            current_commit=current_commit,
            status_line_count=_cleanup_status_line_count(row.cwd),
            commits_above_base=_cleanup_commits_above_base(
                row.cwd, materialized.base_commit,
            ),
            remote_count=len(remotes.splitlines()) if remotes else 0,
            matching_process_count=_cleanup_process_count(row, env=env),
            registered_worktree=_cleanup_registered(materialized.paths.repo, row.cwd),
            creation_scope_sha256=creation_sha,
            pending_scope_sha256=pending_sha,
            receipt_scope_sha256=receipt_sha,
            consumed_creation_observed=consumed,
            source_group=source_group,
            source_root=source_root,
            source_event_log=materialized.paths.event_log,
        ))
    return tuple(observations)


def _retained_group_f_count(live_root: Path) -> int:
    state = live_root / "background-concurrency" / "local-state.json"
    if not state.exists():
        return 0
    payload = _read_json(state, "Group F local state")
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if (
        not isinstance(groups, list)
        or len(groups) > 2
        or any(not isinstance(item, dict) for item in groups)
    ):
        raise LiveGateError("Group F local state is invalid")
    return len(groups)


def _cleanup_contract_payload(
    observations: Sequence[CleanupObservation],
    *,
    retained_group_f_count: int,
) -> dict[str, Any]:
    targets = []
    for item in observations:
        targets.append({
            "source_group": item.source_group,
            "short_id": item.row.short_id,
            "row_fingerprint": _cleanup_row_fingerprint(item.row),
            "worktree_path": str(item.row.cwd.resolve(strict=False)),
            "repository_root": str(item.repository_root.resolve(strict=False)),
            "repository_common_dir": str(item.repository_common_dir.resolve(strict=False)),
            "base_commit": item.base_commit,
            "creation_scope_sha256": item.creation_scope_sha256,
            "audit": {
                "state": item.row.state,
                "pid_present": item.row.pid_present,
                "status_line_count": item.status_line_count,
                "commits_above_base": item.commits_above_base,
                "remote_count": item.remote_count,
                "matching_process_count": item.matching_process_count,
                "registered_worktree": item.registered_worktree,
                "lease_event_equal": _same_path(item.lease_path, item.event_path),
                "common_dir_equal": _same_path(
                    item.repository_common_dir, item.worktree_common_dir,
                ),
                "creation_lineage_equal": len({
                    item.creation_scope_sha256,
                    item.pending_scope_sha256,
                    item.receipt_scope_sha256,
                }) == 1 and item.consumed_creation_observed,
            },
        })
    return {
        "schema_version": 1,
        "mode": "G",
        "target_count": len(targets),
        "retained_group_f_row_only_count": retained_group_f_count,
        "targets": targets,
    }


def _cleanup_exact_targets(materialized: MaterializedCleanup) -> tuple[str, ...]:
    paths: list[Path] = [
        materialized.paths.root,
        materialized.paths.contract,
        materialized.paths.pending_scope,
        materialized.paths.consumed_ledger,
        materialized.paths.residual,
        materialized.paths.candidate,
        materialized.paths.root.parent / "ownership.json",
        materialized.paths.root.parent / "ownership.json.lock",
    ]
    for item in materialized.observations:
        if item.source_root is not None:
            paths.extend((
                item.source_root / "layout.json",
                item.source_root / "pending-scope.json",
                item.source_root / "consumed-side-effects.json",
                item.source_root / "local-state.json",
                item.source_root / "worktree-lease.json",
            ))
        if item.source_event_log is not None:
            paths.append(item.source_event_log)
        paths.append(item.row.cwd)
    return tuple(str(path.resolve(strict=False)) for path in paths)


def build_cleanup_execution_manifest(
    materialized: MaterializedCleanup,
    *,
    cli: str | Path,
    python_exe: str | Path,
) -> tuple[str, BoundExecutableManifest, dict[str, Any]]:
    source_paths = {
        Path(cli),
        Path(python_exe),
        _expected_python_process_image(Path(python_exe)),
        Path(__file__),
        Path(__file__).with_name("background_probe.py"),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("core.py"),
        Path(__file__).with_name("fixtures.py"),
        Path(__file__).with_name("hook_sink.py"),
        Path(__file__).with_name("live_common.py"),
        Path(__file__).with_name("live_host.py"),
        Path(__file__).with_name("live_init.py"),
        Path(__file__).with_name("locking.py"),
        materialized.paths.contract,
    }
    entries: list[BoundExecutableFile] = []
    for path in sorted((path.resolve(strict=True) for path in source_paths), key=str):
        identity = BoundCliIdentity.capture(path, version="unverified")
        entries.append(BoundExecutableFile(
            canonical_path=identity.canonical_path,
            sha256=identity.sha256,
            file_identity=identity.file_identity,
        ))
    manifest = BoundExecutableManifest(
        repository_id="group-g-cleanup",
        trust_revision=1,
        entries=tuple(entries),
    )
    contract = {
        "schema_version": 1,
        "group": "G",
        "file_manifest_sha256": manifest.sha256,
        "cleanup_contract_sha256": materialized.contract_sha256,
        "ordered_row_fingerprints": [
            _cleanup_row_fingerprint(item.row) for item in materialized.observations
        ],
        "ordered_worktree_targets": [
            str(item.row.cwd.resolve(strict=False)) for item in materialized.observations
        ],
        "creation_approval_lineage": [
            item.creation_scope_sha256 for item in materialized.observations
        ],
        "mutable_targets": list(_cleanup_exact_targets(materialized)),
    }
    return _execution_contract_digest(contract), manifest, contract


def materialize_cleanup(
    root: str | Path,
    observations: Sequence[CleanupObservation],
    *,
    bound_identity: BoundCliIdentity,
    retained_group_f_count: int = 0,
) -> MaterializedCleanup:
    target = prepare_private_runtime_group_root(root)
    paths = _cleanup_paths(target)
    ordered = tuple(observations)
    contract = _cleanup_contract_payload(
        ordered, retained_group_f_count=retained_group_f_count,
    )
    contract_sha256 = _execution_contract_digest(contract)
    write_json_atomic(paths.contract, contract)
    return MaterializedCleanup(
        paths=paths,
        observations=ordered,
        contract=contract,
        contract_sha256=contract_sha256,
        cli_sha256=bound_identity.sha256,
        cli_version=bound_identity.version,
    )


def load_cleanup(
    root: str | Path,
    observations: Sequence[CleanupObservation],
    *,
    bound_identity: BoundCliIdentity,
    retained_group_f_count: int = 0,
) -> MaterializedCleanup:
    target = Path(root).resolve(strict=True)
    paths = _cleanup_paths(target)
    ordered = tuple(observations)
    contract = _cleanup_contract_payload(
        ordered, retained_group_f_count=retained_group_f_count,
    )
    if _read_json(paths.contract, "cleanup contract") != contract:
        raise LiveGateError("cleanup preview audit drifted")
    return MaterializedCleanup(
        paths=paths,
        observations=ordered,
        contract=contract,
        contract_sha256=_execution_contract_digest(contract),
        cli_sha256=bound_identity.sha256,
        cli_version=bound_identity.version,
    )


def _require_cleanup_capabilities(capabilities: Mapping[str, bool]) -> None:
    if capabilities.get("rm_help_recognized") is not True:
        raise LiveGateError("Task 2 did not bind the required Group G rm capability")


def preview_cleanup(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    execution_env = os.environ if env is None else env
    assert_no_credential_overrides(execution_env)
    git_head, dirty = _git_checkpoint(project_root)
    if dirty:
        raise LiveGateError("tracked checkout must be clean before Group G preview")
    requested = Path(root).absolute()
    live_root = requested.parent.resolve(strict=True)
    identity_path = _task2_identity_path(requested)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_cleanup_capabilities(load_bound_host_capabilities(identity_path))
    observations = discover_cleanup_observations(
        live_root,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
        env=execution_env,
    )
    reasons = _cleanup_audit_reasons(observations)
    if reasons:
        raise LiveGateError("RECOVERY_REQUIRED: " + ",".join(reasons))
    retained_f = _retained_group_f_count(live_root)
    materialized = materialize_cleanup(
        requested,
        observations,
        bound_identity=bound_identity,
        retained_group_f_count=retained_f,
    )
    manifest_sha256, _manifest, execution_contract = build_cleanup_execution_manifest(
        materialized, cli=cli, python_exe=python_exe,
    )
    scope = build_group_g_scope(
        git_head=git_head,
        cli_sha256=bound_identity.sha256,
        executable_manifest_sha256=manifest_sha256,
        cli=cli,
        short_ids=tuple(item.row.short_id for item in observations),
        worktree_targets=tuple(str(item.row.cwd) for item in observations),
        exact_targets=_cleanup_exact_targets(materialized),
        cleanup_contract_sha256=materialized.contract_sha256,
    )
    payload = _scope_payload(scope)
    write_json_atomic(materialized.paths.pending_scope, payload)
    return {
        "group": "G",
        "scope": payload,
        "scope_sha256": approval_digest(scope),
        "execution_contract": execution_contract,
        "audit": {
            "eligible": True,
            "target_count": len(observations),
            "retained_group_f_row_only_count": retained_f,
            "targets": [
                {
                    "source_group": item.source_group,
                    "short_id": item.row.short_id,
                    "row_fingerprint": _cleanup_row_fingerprint(item.row),
                    "worktree_path": str(item.row.cwd),
                    "creation_scope_sha256": item.creation_scope_sha256,
                    "status": item.row.state,
                }
                for item in observations
            ],
        },
        "destructive_effect": "provider-native claude rm",
        "fallback_remove_enabled": False,
    }


def _write_local_state(path: Path, row: OwnedRosterRow) -> None:
    write_json_atomic(path, {
        "group": {
            "short_id": row.short_id,
            "session_id": row.session_id,
            "worktree_path": str(row.cwd),
        }
    })


def _ownership_target_fingerprint(
    kind: str,
    row: OwnedRosterRow,
    *,
    common_dir: Path,
) -> str:
    if kind == "row":
        payload = {
            "kind": kind,
            "name": row.name,
            "short_id": row.short_id,
            "session_id": row.session_id,
            "cwd": str(row.cwd.resolve(strict=False)),
        }
    elif kind == "worktree":
        payload = {
            "kind": kind,
            "cwd": str(row.cwd.resolve(strict=False)),
            "common_dir": str(common_dir.resolve(strict=False)),
        }
    else:
        raise ValueError("unsupported ownership record kind")
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _load_ownership_records(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    payload = _read_json(path, "plan ownership state")
    records = payload.get("records") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "records"}
        or payload.get("schema_version") != 1
        or not isinstance(records, list)
        or len(records) > 128
    ):
        raise LiveGateError("plan ownership state is invalid")
    result: list[dict[str, str]] = []
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"kind", "approval_digest", "target_fingerprint"}
            or record.get("kind") not in {"row", "process", "worktree"}
            or _HEX64.fullmatch(str(record.get("approval_digest", ""))) is None
            or _HEX64.fullmatch(str(record.get("target_fingerprint", ""))) is None
        ):
            raise LiveGateError("plan ownership record is invalid")
        result.append({
            "kind": record["kind"],
            "approval_digest": record["approval_digest"],
            "target_fingerprint": record["target_fingerprint"],
        })
    if len({record["target_fingerprint"] for record in result}) != len(result):
        raise LiveGateError("plan ownership fingerprint is duplicated")
    return result


def _record_plan_ownership(
    live_root: Path,
    *,
    scope_sha256: str,
    rows: Sequence[OwnedRosterRow],
    common_dir: Path,
    include_worktrees: bool,
) -> None:
    if _HEX64.fullmatch(scope_sha256) is None:
        raise LiveGateError("plan ownership approval digest is invalid")
    path = live_root / "ownership.json"
    lock = live_root / "ownership.json.lock"
    with locked_file(lock, timeout_seconds=10):
        records = _load_ownership_records(path)
        by_fingerprint = {record["target_fingerprint"]: record for record in records}
        for row in rows:
            kinds = ("row", "worktree") if include_worktrees else ("row",)
            for kind in kinds:
                target_fingerprint = _ownership_target_fingerprint(
                    kind, row, common_dir=common_dir,
                )
                desired = {
                    "kind": kind,
                    "approval_digest": scope_sha256,
                    "target_fingerprint": target_fingerprint,
                }
                current = by_fingerprint.get(target_fingerprint)
                if current is not None and current != desired:
                    raise LiveGateError("plan ownership lineage drifted")
                if current is None:
                    records.append(desired)
                    by_fingerprint[target_fingerprint] = desired
        write_json_atomic(path, {"schema_version": 1, "records": records})


def _release_plan_ownership(
    live_root: Path,
    target: CleanupObservation,
) -> None:
    path = live_root / "ownership.json"
    lock = live_root / "ownership.json.lock"
    with locked_file(lock, timeout_seconds=10):
        records = _load_ownership_records(path)
        fingerprints = {
            _ownership_target_fingerprint(
                kind, target.row, common_dir=target.repository_common_dir,
            )
            for kind in ("row", "worktree")
        }
        matching = [
            record for record in records
            if record["target_fingerprint"] in fingerprints
            and record["approval_digest"] == target.creation_scope_sha256
        ]
        if len(matching) != 2:
            raise LiveGateError("cleanup ownership release lineage mismatch")
        remaining = [
            record for record in records
            if record["target_fingerprint"] not in fingerprints
        ]
        write_json_atomic(path, {"schema_version": 1, "records": remaining})


class _LiveGroupCAdapter:
    def __init__(
        self,
        materialized: MaterializedBackground,
        authorization: ExecutionAuthorization,
        *,
        env: Mapping[str, str],
    ) -> None:
        self.materialized = materialized
        self.authorization = authorization
        self.env = dict(env)
        self.provider_launches = 0
        self.stop_actions = 0
        self.last_row: OwnedRosterRow | None = None
        self.active_stop_event_count = 0

    @property
    def _ledger(self) -> Path:
        return self.materialized.paths.consumed_ledger

    def _invoke_cli(self, argv: tuple[str, ...], *, timeout_seconds: float = 60) -> None:
        result = run_argv(
            "group-c-cli",
            argv,
            cwd=self.materialized.paths.repo,
            timeout_seconds=timeout_seconds,
            env=self.env,
        )
        if result.timed_out:
            raise TimeoutError("Group C CLI action timed out")
        if result.exit_code != 0:
            raise LiveGateError("Group C CLI action failed")

    def launch(self) -> None:
        consume_side_effect(
            self.authorization,
            "worktree_create",
            {},
            self._ledger,
            invoke=None,
        )
        consume_side_effect(
            self.authorization,
            "provider_launch",
            {},
            self._ledger,
            invoke=self._invoke_cli,
        )
        self.provider_launches = 1

    def _roster_payload(self) -> Any:
        result = run_argv(
            "group-c-agents-json",
            [self.materialized.launch_argv[0], "agents", "--json", "--all"],
            timeout_seconds=15,
            env=self.env,
        )
        if result.exit_code != 0 or result.timed_out:
            raise LiveGateError("agents JSON query failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LiveGateError("agents JSON query returned malformed data") from exc

    def _roster(self) -> OwnedRosterRow:
        return parse_owned_roster(
            self._roster_payload(), self.materialized.group_name,
        )

    def _recovery_short_id(self) -> str | None:
        payload = self._roster_payload()
        if not isinstance(payload, list):
            return None
        matches = [
            item for item in payload
            if isinstance(item, dict)
            and item.get("name") == self.materialized.group_name
        ]
        if len(matches) != 1:
            return None
        item = matches[0]
        short_id = item.get("id")
        if (
            item.get("kind") != "background"
            or item.get("state") != "working"
            or not isinstance(short_id, str)
            or _ROW_ID.fullmatch(short_id) is None
        ):
            return None
        return short_id

    def _git_status_paths(self, cwd: Path) -> tuple[str, ...]:
        raw = _git_text(
            "git-status",
            ("git", "-C", str(cwd), "status", "--porcelain=v1", "-z"),
        )
        if not raw:
            return ()
        records = raw.rstrip("\0").split("\0")
        paths: list[str] = []
        for record in records:
            if len(record) < 4 or record[2] != " ":
                raise LiveGateError("unexpected Git status schema")
            paths.append(record[3:])
        return tuple(paths)

    def _snapshot(self, row: OwnedRosterRow, *, require_write: bool) -> BackgroundObservation:
        paths = self.materialized.paths
        lease = _read_json(paths.lease_ack, "worktree lease")
        if not isinstance(lease, dict):
            raise LiveGateError("worktree lease is invalid")
        lease_path = Path(str(lease.get("worktree_path", "")))
        common = Path(str(lease.get("repository_common_dir", "")))
        events = _read_json_lines(paths.event_log, "background event log")
        worktree_events = [
            event for event in events
            if event.get("hook_event_name") == "WorktreeCreate"
            and event.get("execution_id") == self.materialized.execution_id
        ]
        if not worktree_events:
            raise LiveGateError("WorktreeCreate event is unavailable")
        if len(worktree_events) != 1:
            raise LiveGateError("WorktreeCreate event is duplicated")
        event_path = Path(str(worktree_events[0].get("worktree_path", "")))
        guard_records = _read_json_lines(paths.guard_ack, "PreToolUse guard log", missing_ok=True)
        writes = [
            record for record in guard_records
            if record.get("allowed") is True and record.get("tool_name") == "Write"
        ]
        if require_write and len(writes) != 1:
            raise LiveGateError("first approved Write acknowledgement is unavailable")
        guard_cwd = Path(str(writes[-1].get("cwd", ""))) if writes else row.cwd
        proof = row.cwd / _PROOF_RELATIVE
        proof_content = proof.read_bytes() if proof.exists() else None
        stop_failures = [
            event.get("stop_failure", {}).get("category")
            for event in events
            if event.get("hook_event_name") == "StopFailure"
            and isinstance(event.get("stop_failure"), dict)
        ]
        stop_failure = stop_failures[-1] if stop_failures else None
        if stop_failure is not None and not isinstance(stop_failure, str):
            raise LiveGateError("StopFailure category schema mismatch")
        session_start = any(event.get("hook_event_name") == "SessionStart" for event in events)
        stop_count = sum(event.get("hook_event_name") == "Stop" for event in events)
        current_commit, _dirty = _git_checkpoint(row.cwd)
        remotes = _git_text("git-remotes", ("git", "-C", str(row.cwd), "remote"))
        return BackgroundObservation(
            row=row,
            session_start_observed=session_start,
            worktree_create_observed=True,
            lease_path=lease_path,
            event_path=event_path,
            handoff_path=lease_path,
            guard_cwd=guard_cwd,
            roster_path=row.cwd,
            repository_common_dir=common,
            worktree_common_dir=_git_common_dir(row.cwd),
            event_order=("lease", "WorktreeCreate", "handler_stdout", "first_write"),
            first_write_after_handoff=bool(writes),
            remote_count=len(remotes.splitlines()) if remotes else 0,
            base_commit=self.materialized.base_commit,
            current_commit=current_commit,
            changed_paths=self._git_status_paths(row.cwd),
            proof_content=proof_content,
            stop_event_count=stop_count,
            stop_failure_category=stop_failure,
        )

    def observe(self, stage: str) -> BackgroundObservation:
        deadlines = {
            "working": 120.0,
            "stopped-1": 30.0,
            "stopped-2": 5.0,
            "respawn-working": 120.0,
            "done": 300.0,
            "clean": 15.0,
        }
        expected = {
            "working": "working",
            "stopped-1": "stopped",
            "stopped-2": None,
            "respawn-working": "working",
            "done": "done",
            "clean": "done",
        }
        if stage not in deadlines:
            raise ValueError("unknown Group C observation stage")
        deadline = time.monotonic() + deadlines[stage]
        while True:
            try:
                row = self._roster()
                self.last_row = row
                require_write = stage != "clean"
                observation = self._snapshot(row, require_write=require_write)
                desired = expected[stage]
                if stage == "working" and (
                    not observation.session_start_observed
                    or observation.proof_content is None
                    or observation.changed_paths == ()
                ):
                    raise LiveGateError("initial handoff is not complete")
                if stage == "stopped-1" and observation.stop_event_count < 1:
                    raise LiveGateError("active Stop hook is unavailable")
                if stage == "done" and observation.stop_event_count <= self.active_stop_event_count:
                    raise LiveGateError("final Stop hook is unavailable")
                if desired is None or row.state == desired or row.state == "failed":
                    if stage in {"stopped-1", "stopped-2"}:
                        self.active_stop_event_count = observation.stop_event_count
                    _write_local_state(self.materialized.paths.local_state, row)
                    _record_plan_ownership(
                        self.materialized.paths.root.parent,
                        scope_sha256=approval_digest(self.authorization.scope),
                        rows=(row,),
                        common_dir=self.materialized.repository_common_dir,
                        include_worktrees=True,
                    )
                    return observation
            except LiveGateError as exc:
                retryable = any(fragment in str(exc) for fragment in (
                    "missing owned background row",
                    "lease unavailable",
                    "background event log unavailable",
                    "WorktreeCreate event is unavailable",
                    "first approved Write acknowledgement is unavailable",
                    "initial handoff is not complete",
                    "active Stop hook is unavailable",
                    "final Stop hook is unavailable",
                ))
                if not retryable:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Group C {stage} observation timed out")
            time.sleep(min(0.25, remaining))

    def _runtime_state(self, row: OwnedRosterRow) -> dict[str, Any]:
        if self.last_row is None or not _same_owned_identity(self.last_row, row):
            raise LiveGateError("lifecycle action row is not group-owned")
        return {"group": {"short_id": row.short_id}}

    def stop(self, row: OwnedRosterRow) -> None:
        self._consume_stop(self._runtime_state(row))

    def _consume_stop(self, state: Mapping[str, Any]) -> None:
        consume_side_effect(
            self.authorization,
            "stop",
            state,
            self._ledger,
            invoke=self._invoke_cli,
        )
        self.stop_actions += 1

    def stabilize(self, seconds: float) -> None:
        if seconds != 0.75:
            raise LiveGateError("unexpected stop stabilization interval")
        time.sleep(seconds)

    def respawn(self, row: OwnedRosterRow) -> None:
        if self.provider_launches != 1:
            raise LiveGateError("respawn would exceed the provider launch contract")
        consume_side_effect(
            self.authorization,
            "respawn",
            self._runtime_state(row),
            self._ledger,
            invoke=self._invoke_cli,
        )
        self.provider_launches += 1

    def delete_proof(self, row: OwnedRosterRow) -> None:
        proof = self.materialized.paths.worktree_root / self.materialized.worktree_name / _PROOF_RELATIVE
        if not _same_path(proof.parent, row.cwd):
            raise LiveGateError("proof delete target does not match owned worktree")

        def delete(_argv: tuple[str, ...]) -> None:
            if proof.is_symlink() or proof.read_bytes() != _PROOF_BYTES:
                raise LiveGateError("proof delete target drifted")
            proof.unlink()

        consume_side_effect(
            self.authorization,
            "file_delete",
            {},
            self._ledger,
            invoke=delete,
        )

    def _recover_owned_working(self, row: OwnedRosterRow | None) -> None:
        candidate = self.last_row if row is None else row
        short_id: str | None = None
        try:
            current = self._roster()
            candidate = current
            self.last_row = current
            if current.state == "working":
                short_id = current.short_id
        except Exception:
            try:
                short_id = self._recovery_short_id()
            except Exception:
                short_id = None
        if self.stop_actions >= 2:
            return
        try:
            if candidate is not None and candidate.state == "working":
                self.stop(candidate)
            elif short_id is not None:
                self._consume_stop({"group": {"short_id": short_id}})
        except Exception:
            return

    def recover_timeout(self, row: OwnedRosterRow | None) -> None:
        self._recover_owned_working(row)

    def recover_failure(self, row: OwnedRosterRow | None) -> None:
        self._recover_owned_working(row)


class _LiveGroupDAdapter(_LiveGroupCAdapter):
    def _snapshot_needs(self, row: OwnedRosterRow) -> NeedsInputObservation:
        paths = self.materialized.paths
        lease = _read_json(paths.lease_ack, "Group D worktree lease")
        if not isinstance(lease, dict):
            raise LiveGateError("Group D worktree lease is invalid")
        lease_path = Path(str(lease.get("worktree_path", "")))
        common = Path(str(lease.get("repository_common_dir", "")))
        events = _read_json_lines(paths.event_log, "Group D event log")
        worktree_events = [
            event for event in events
            if event.get("hook_event_name") == "WorktreeCreate"
            and event.get("execution_id") == self.materialized.execution_id
        ]
        if len(worktree_events) != 1:
            raise LiveGateError("Group D WorktreeCreate event is unavailable")
        event_path = Path(str(worktree_events[0].get("worktree_path", "")))
        guards = _read_json_lines(
            paths.guard_ack, "Group D guard log", missing_ok=True,
        )
        denied_writes = [
            record for record in guards
            if record.get("allowed") is False
            and record.get("tool_name") == "Write"
            and record.get("input_matched") is True
        ]
        guard_path = (
            Path(str(denied_writes[-1].get("cwd", "")))
            if denied_writes else row.cwd
        )
        supplied_paths = (row.cwd, lease_path, event_path, guard_path)
        handoff_equal = len({_path_key(path) for path in supplied_paths}) == 1
        common_equal = _same_path(common, _git_common_dir(row.cwd))
        changed_paths = self._git_status_paths(row.cwd)
        forbidden = row.cwd / _NEEDS_INPUT_RELATIVE
        current_commit, _dirty = _git_checkpoint(row.cwd)
        remotes = _git_text("git-remotes", ("git", "-C", str(row.cwd), "remote"))
        stop_failures = [
            event.get("stop_failure", {}).get("category")
            for event in events
            if event.get("hook_event_name") == "StopFailure"
            and isinstance(event.get("stop_failure"), dict)
        ]
        stop_failure = stop_failures[-1] if stop_failures else None
        if stop_failure is not None and not isinstance(stop_failure, str):
            raise LiveGateError("Group D StopFailure category schema mismatch")
        return NeedsInputObservation(
            row=row,
            handoff_equal=handoff_equal,
            common_dir_equal=common_equal,
            denied_write_observed=len(denied_writes) == 1,
            checkout_clean=not changed_paths and not forbidden.exists(),
            remote_count=len(remotes.splitlines()) if remotes else 0,
            base_commit=self.materialized.base_commit,
            current_commit=current_commit,
            stop_event_count=sum(
                event.get("hook_event_name") == "Stop" for event in events
            ),
            stop_failure_category=stop_failure,
        )

    def observe(self, stage: str) -> NeedsInputObservation:
        deadlines = {
            "needs-input": 180.0,
            "stopped-1": 30.0,
            "stopped-2": 5.0,
        }
        if stage not in deadlines:
            raise ValueError("unknown Group D observation stage")
        deadline = time.monotonic() + deadlines[stage]
        while True:
            try:
                row = self._roster()
                self.last_row = row
                observation = self._snapshot_needs(row)
                if observation.stop_failure_category is not None:
                    return observation
                if stage == "needs-input" and (
                    row.state in {"blocked", "needs_input"}
                    and observation.denied_write_observed
                ):
                    _write_local_state(self.materialized.paths.local_state, row)
                    _record_plan_ownership(
                        self.materialized.paths.root.parent,
                        scope_sha256=approval_digest(self.authorization.scope),
                        rows=(row,),
                        common_dir=self.materialized.repository_common_dir,
                        include_worktrees=True,
                    )
                    return observation
                if stage.startswith("stopped") and row.state == "stopped":
                    _write_local_state(self.materialized.paths.local_state, row)
                    _record_plan_ownership(
                        self.materialized.paths.root.parent,
                        scope_sha256=approval_digest(self.authorization.scope),
                        rows=(row,),
                        common_dir=self.materialized.repository_common_dir,
                        include_worktrees=True,
                    )
                    return observation
            except LiveGateError as exc:
                retryable = any(fragment in str(exc) for fragment in (
                    "missing owned background row",
                    "worktree lease unavailable",
                    "event log unavailable",
                    "WorktreeCreate event is unavailable",
                ))
                if not retryable:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Group D {stage} observation timed out")
            time.sleep(min(0.25, remaining))

    def attach(self, row: OwnedRosterRow) -> AttachObservation:
        if self.last_row is None or not _same_owned_identity(self.last_row, row):
            raise LiveGateError("attach row is not Group D owned")
        streams = (sys.stdin, sys.stdout, sys.stderr)
        if any(not hasattr(stream, "isatty") or not stream.isatty() for stream in streams):
            raise LiveGateError("attach requires an inherited visible TTY")
        before_events = _read_json_lines(
            self.materialized.paths.event_log, "Group D event log",
        )
        working_transition = False
        concrete: list[tuple[str, ...]] = []
        consume_side_effect(
            self.authorization,
            "attach",
            self._runtime_state(row),
            self._ledger,
            invoke=lambda argv: concrete.append(argv),
        )
        if len(concrete) != 1:
            raise LiveGateError("Group D attach argv was not materialized")
        deadline = time.monotonic() + _ATTACH_TIMEOUT_SECONDS
        with subprocess.Popen(
            concrete[0],
            cwd=self.materialized.paths.repo,
            env=self.env,
        ) as process:
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Group D attach timed out")
                    try:
                        current = self._roster()
                        self.last_row = current
                        if current.state == "working":
                            working_transition = True
                    except LiveGateError:
                        pass
                    time.sleep(0.25)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                raise
            if process.returncode != 0:
                raise LiveGateError("Group D attach command failed")
        current = self._roster()
        self.last_row = current
        after_events = _read_json_lines(
            self.materialized.paths.event_log, "Group D event log",
        )
        new_events = after_events[len(before_events):]
        expected_fingerprint = fingerprint(row.session_id)
        same_session_hook = any(
            event.get("session_fingerprint") == expected_fingerprint
            for event in new_events
        )
        return AttachObservation(
            row=current,
            attach_exit_observed=True,
            same_session_hook_observed=same_session_hook,
            working_transition_observed=working_transition,
            checkout_clean=(
                not self._git_status_paths(current.cwd)
                and not (current.cwd / _NEEDS_INPUT_RELATIVE).exists()
            ),
        )

    def _recover_group_d(self, row: OwnedRosterRow | None) -> None:
        if self.stop_actions >= 1:
            return
        candidate = row
        try:
            current = self._roster()
            self.last_row = current
            candidate = current
        except Exception:
            pass
        try:
            if candidate is not None:
                if candidate.state in {"working", "blocked", "needs_input"} or candidate.pid_present:
                    self.stop(candidate)
                return
            short_id = _recovery_short_id_from_payload(
                self._roster_payload(), self.materialized.group_name,
            )
            if short_id is not None:
                self._consume_stop({"group": {"short_id": short_id}})
        except Exception:
            return

    def recover_timeout(self, row: OwnedRosterRow | None) -> None:
        self._recover_group_d(row)

    def recover_failure(self, row: OwnedRosterRow | None) -> None:
        self._recover_group_d(row)


class _LiveGroupFAdapter:
    def __init__(
        self,
        materialized: MaterializedConcurrency,
        authorization: ExecutionAuthorization,
        *,
        env: Mapping[str, str],
    ) -> None:
        self.materialized = materialized
        self.authorization = authorization
        self.env = dict(env)
        self.launched: set[str] = set()
        self.owned: dict[str, OwnedRosterRow] = {}
        self.stop_actions = 0
        self.consumed_stop_ids: set[str] = set()

    @property
    def _ledger(self) -> Path:
        return self.materialized.paths.consumed_ledger

    def _invoke_cli(self, argv: tuple[str, ...], *, timeout_seconds: float = 60) -> None:
        result = run_argv(
            "group-f-cli",
            argv,
            cwd=self.materialized.paths.repo,
            timeout_seconds=timeout_seconds,
            env=self.env,
        )
        if result.timed_out:
            raise TimeoutError("Group F CLI action timed out")
        if result.exit_code != 0:
            raise LiveGateError("Group F CLI action failed")

    def _roster_payload(self) -> Any:
        result = run_argv(
            "group-f-agents-json",
            [self.materialized.launch_argv[0], "agents", "--json", "--all"],
            timeout_seconds=15,
            env=self.env,
        )
        if result.exit_code != 0 or result.timed_out:
            raise LiveGateError("Group F agents JSON query failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LiveGateError("Group F agents JSON query returned malformed data") from exc

    def _row(self, name: str) -> OwnedRosterRow:
        row = parse_owned_roster(self._roster_payload(), name)
        if name not in self.materialized.group_names:
            raise LiveGateError("Group F row is cross-owned")
        return row

    def launch(self, name: str) -> None:
        if name not in self.materialized.group_names or name in self.launched:
            raise LiveGateError("Group F launch name is not approved")
        consume_side_effect(
            self.authorization,
            "provider_launch",
            {"group": {"name": name}},
            self._ledger,
            invoke=self._invoke_cli,
        )
        self.launched.add(name)

    def _snapshot(self, names: Sequence[str]) -> ConcurrencyObservation:
        payload = self._roster_payload()
        rows = tuple(parse_owned_roster(payload, name) for name in names)
        for row in rows:
            self.owned[row.name] = row
        paths = self.materialized.paths
        events = _read_json_lines(paths.event_log, "Group F event log", missing_ok=True)
        guards = _read_json_lines(paths.guard_ack, "Group F guard log", missing_ok=True)
        stop_failures = [
            event.get("stop_failure", {}).get("category")
            for event in events
            if event.get("hook_event_name") == "StopFailure"
            and isinstance(event.get("stop_failure"), dict)
        ]
        stop_failure = stop_failures[-1] if stop_failures else None
        if stop_failure is not None and not isinstance(stop_failure, str):
            raise LiveGateError("Group F StopFailure category schema mismatch")
        current_head, dirty = _git_checkpoint(paths.repo)
        remotes = _git_text("git-remotes", ("git", "-C", str(paths.repo), "remote"))
        expected_sessions = {fingerprint(row.session_id) for row in rows}
        started_sessions = {
            str(event.get("session_fingerprint"))
            for event in events
            if event.get("hook_event_name") == "SessionStart"
            and isinstance(event.get("session_fingerprint"), str)
        }
        guarded_sessions = {
            str(record.get("session_fingerprint"))
            for record in guards
            if record.get("allowed") is True
            and record.get("tool_name") == "Bash"
            and record.get("input_matched") is True
            and isinstance(record.get("session_fingerprint"), str)
        }
        stopped_sessions = {
            str(event.get("session_fingerprint"))
            for event in events
            if event.get("hook_event_name") == "Stop"
            and isinstance(event.get("session_fingerprint"), str)
        }
        checkout_clean = (
            not dirty
            and current_head == self.materialized.base_commit
            and not remotes
            and all(_same_path(row.cwd, paths.repo) for row in rows)
        )
        return ConcurrencyObservation(
            rows=rows,
            session_start_count=len(expected_sessions & started_sessions),
            guard_allow_count=len(expected_sessions & guarded_sessions),
            checkout_clean=checkout_clean,
            stop_hook_count=len(expected_sessions & stopped_sessions),
            stop_failure_category=stop_failure,
        )

    def observe(self, stage: str, names: Sequence[str]) -> ConcurrencyObservation:
        deadlines = {
            "first-active": 120.0,
            "simultaneous": 120.0,
            "stopped-1": 30.0,
            "stopped-2": 5.0,
        }
        if stage not in deadlines:
            raise ValueError("unknown Group F observation stage")
        deadline = time.monotonic() + deadlines[stage]
        while True:
            try:
                observation = self._snapshot(names)
                if observation.stop_failure_category is not None:
                    return observation
                states = {row.state for row in observation.rows}
                if stage in {"first-active", "simultaneous"}:
                    ready = states <= {"working", "blocked"}
                    expected_count = len(tuple(names))
                    ready = (
                        ready
                        and observation.session_start_count >= expected_count
                        and observation.guard_allow_count >= expected_count
                    )
                else:
                    ready = states == {"stopped"} and observation.stop_hook_count >= len(tuple(names))
                if ready:
                    write_json_atomic(self.materialized.paths.local_state, {
                        "groups": [
                            {"name": row.name, "short_id": row.short_id}
                            for row in observation.rows
                        ],
                    })
                    _record_plan_ownership(
                        self.materialized.paths.root.parent,
                        scope_sha256=approval_digest(self.authorization.scope),
                        rows=observation.rows,
                        common_dir=self.materialized.repository_common_dir,
                        include_worktrees=False,
                    )
                    return observation
            except LiveGateError as exc:
                retryable = any(fragment in str(exc) for fragment in (
                    "missing owned background row",
                    "event log unavailable",
                ))
                if not retryable:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Group F {stage} observation timed out")
            time.sleep(min(0.25, remaining))

    def stop(self, row: OwnedRosterRow) -> None:
        owned = self.owned.get(row.name)
        if owned is None or not _same_owned_identity(owned, row):
            raise LiveGateError("Group F stop row is not owned")
        self._consume_owned_stop_id(row.short_id)

    def _consume_owned_stop_id(self, short_id: str) -> None:
        if short_id in self.consumed_stop_ids:
            raise LiveGateError("Group F stop allowance was already consumed for this row")

        def invoke(argv: tuple[str, ...]) -> None:
            self.consumed_stop_ids.add(short_id)
            self.stop_actions = len(self.consumed_stop_ids)
            self._invoke_cli(argv)

        consume_side_effect(
            self.authorization,
            "stop",
            {"group": {"short_id": short_id}},
            self._ledger,
            invoke=invoke,
        )

    def stabilize(self, seconds: float) -> None:
        if seconds != 0.75:
            raise LiveGateError("unexpected Group F stabilization interval")
        time.sleep(seconds)

    def recover_failure(self, rows: Sequence[OwnedRosterRow]) -> None:
        candidates = {row.name: row for row in rows}
        for name in self.materialized.group_names:
            try:
                candidates[name] = self._row(name)
            except Exception:
                pass
        for name in self.materialized.group_names:
            row = candidates.get(name)
            if row is None or (
                row.state not in {"working", "blocked", "needs_input"}
                and not row.pid_present
            ) or row.short_id in self.consumed_stop_ids:
                continue
            try:
                self.owned[name] = row
                self.stop(row)
            except Exception:
                continue
        if self.stop_actions >= 2:
            return
        try:
            payload = self._roster_payload()
        except Exception:
            return
        for name in self.materialized.group_names:
            if self.stop_actions >= 2 or name in candidates:
                continue
            short_id = _recovery_short_id_from_payload(payload, name)
            if short_id is None or short_id in self.consumed_stop_ids:
                continue
            try:
                self._consume_owned_stop_id(short_id)
            except Exception:
                continue


def _combined_source_sha256(paths: Sequence[Path]) -> str:
    digests = [
        _sha256_file(path) if path.exists() and path.is_file()
        else hashlib.sha256(b"").hexdigest()
        for path in paths
    ]
    return hashlib.sha256(("\n".join(digests) + "\n").encode("ascii")).hexdigest()


def _cleanup_unrelated_roster_snapshot(
    payload: Any,
    *,
    excluded_names: set[str],
) -> tuple[str, ...]:
    try:
        normalize_agents(payload)
    except ValueError as exc:
        raise LiveGateError(str(exc)) from exc
    records: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            raise LiveGateError("cleanup roster entry is invalid")
        if item.get("name") in excluded_names:
            continue
        selected = {
            key: item.get(key)
            for key in (
                "id", "sessionId", "name", "cwd", "kind", "state", "status",
                "waitingFor", "pid", "model", "contextFingerprint",
            )
        }
        records.append(hashlib.sha256(_canonical_json(selected)).hexdigest())
    return tuple(sorted(records))


def _cleanup_event_match_count(
    target: CleanupObservation,
    events: Sequence[Mapping[str, Any]],
) -> int:
    expected_session = fingerprint(target.row.session_id)
    count = 0
    for event in events:
        if event.get("hook_event_name") != "WorktreeRemove":
            continue
        raw_path = event.get("worktree_path")
        if not isinstance(raw_path, str) or not _same_path(Path(raw_path), target.row.cwd):
            continue
        session = event.get("session_fingerprint")
        if session is not None and session != expected_session:
            continue
        count += 1
    return count


class _LiveCleanupAdapter:
    def __init__(
        self,
        materialized: MaterializedCleanup,
        authorization: ExecutionAuthorization,
        *,
        cli: str | Path,
        env: Mapping[str, str],
    ) -> None:
        self.materialized = materialized
        self.authorization = authorization
        self.cli = str(Path(cli).resolve())
        self.env = dict(env)
        self.approved_names = {item.row.name for item in materialized.observations}
        self.approved_paths = {
            _path_key(item.row.cwd) for item in materialized.observations
        }
        roster = _cleanup_roster_payload(self.cli, env=self.env)
        self.unrelated_roster = _cleanup_unrelated_roster_snapshot(
            roster, excluded_names=self.approved_names,
        )
        repositories = {
            _path_key(item.repository_root): item.repository_root
            for item in materialized.observations
        }
        self.unrelated_worktrees = {
            key: tuple(sorted(
                _path_key(path)
                for path in _cleanup_worktree_inventory(repository)
                if _path_key(path) not in self.approved_paths
            ))
            for key, repository in repositories.items()
        }
        self.initial_event_counts: dict[str, int] = {}
        for item in materialized.observations:
            if item.source_event_log is None:
                raise LiveGateError("cleanup source event log is unavailable")
            events = _read_json_lines(item.source_event_log, "cleanup source event log")
            self.initial_event_counts[item.row.name] = _cleanup_event_match_count(
                item, events,
            )

    def _invoke_cli(self, argv: tuple[str, ...], cwd: Path) -> None:
        result = run_argv(
            "group-g-rm",
            argv,
            cwd=cwd,
            timeout_seconds=60,
            env=self.env,
        )
        if result.timed_out:
            raise TimeoutError("Group G rm timed out")
        if result.exit_code != 0:
            raise LiveGateError("Group G provider rm refused")

    def remove(self, target: CleanupObservation) -> None:
        payload = _cleanup_roster_payload(self.cli, env=self.env)
        current = parse_owned_roster(payload, target.row.name)
        if (
            not _same_owned_identity(current, target.row)
            or current.state not in {"stopped", "done", "failed"}
            or current.pid_present
        ):
            raise LiveGateError("Group G row identity drifted before rm")
        consume_side_effect(
            self.authorization,
            "remove",
            {"group": {"short_id": target.row.short_id}},
            self.materialized.paths.consumed_ledger,
            invoke=lambda argv: self._invoke_cli(argv, target.repository_root),
        )

    def _unrelated_worktrees_equal(self) -> bool:
        repositories = {
            _path_key(item.repository_root): item.repository_root
            for item in self.materialized.observations
        }
        current = {
            key: tuple(sorted(
                _path_key(path)
                for path in _cleanup_worktree_inventory(repository)
                if _path_key(path) not in self.approved_paths
            ))
            for key, repository in repositories.items()
        }
        return current == self.unrelated_worktrees

    def observe_removed(self, target: CleanupObservation) -> RemovalObservation:
        if target.source_event_log is None:
            raise LiveGateError("cleanup source event log is unavailable")
        deadline = time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
        latest = RemovalObservation(
            row_identity_equal=True,
            worktree_remove_event_match=False,
            path_absent=False,
            worktree_unregistered=False,
            row_absent=False,
            unrelated_rows_unchanged=False,
            unrelated_worktrees_unchanged=False,
        )
        while True:
            payload = _cleanup_roster_payload(self.cli, env=self.env)
            matching_rows = [
                item for item in payload
                if isinstance(item, dict)
                and (
                    item.get("name") == target.row.name
                    or item.get("id") == target.row.short_id
                )
            ]
            events = _read_json_lines(target.source_event_log, "cleanup source event log")
            event_delta = (
                _cleanup_event_match_count(target, events)
                - self.initial_event_counts[target.row.name]
            )
            unrelated_rows_equal = _cleanup_unrelated_roster_snapshot(
                payload, excluded_names=self.approved_names,
            ) == self.unrelated_roster
            try:
                registered = _cleanup_registered(target.repository_root, target.row.cwd)
                unrelated_worktrees_equal = self._unrelated_worktrees_equal()
            except LiveGateError:
                registered = True
                unrelated_worktrees_equal = False
            latest = RemovalObservation(
                row_identity_equal=True,
                worktree_remove_event_match=event_delta == 1,
                path_absent=not target.row.cwd.exists(),
                worktree_unregistered=not registered,
                row_absent=not matching_rows,
                unrelated_rows_unchanged=unrelated_rows_equal,
                unrelated_worktrees_unchanged=unrelated_worktrees_equal,
            )
            if all((
                latest.worktree_remove_event_match,
                latest.path_absent,
                latest.worktree_unregistered,
                latest.row_absent,
                latest.unrelated_rows_unchanged,
                latest.unrelated_worktrees_unchanged,
            )):
                _release_plan_ownership(
                    self.materialized.paths.root.parent,
                    target,
                )
                return latest
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest
            time.sleep(min(0.25, remaining))


def execute_background(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    approval: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    context_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    execution_env = os.environ if env is None else env
    assert_no_credential_overrides(execution_env)
    target = Path(root).resolve(strict=True)
    circuit_path = _model_circuit_path(target)
    require_model_groups_available(circuit_path)
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_background_capabilities(load_bound_host_capabilities(identity_path))
    prerequisite = load_context_prerequisite(
        _context_root(target) if context_root is None else context_root,
        cli=cli,
        bound_identity=bound_identity,
    )
    materialized = load_background(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
    )
    manifest_sha256, file_manifest, _contract = build_background_execution_manifest(
        materialized,
        prerequisite,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
    )
    git_head, dirty = _git_checkpoint(project_root)
    proof = materialized.paths.worktree_root / materialized.worktree_name / _PROOF_RELATIVE
    scope = build_group_c_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        launch_argv=materialized.launch_argv,
        worktree_hook_argv=materialized.worktree_hook_argv,
        proof_path=str(proof.resolve(strict=False)),
        exact_targets=_background_exact_targets(materialized),
    )
    pending = _read_json(target / "pending-scope.json", "Group C pending scope")
    if pending != _scope_payload(scope):
        raise LiveGateError("Group C preview drifted")
    layout = _read_json(target / "layout.json", "background layout")
    if layout.get("approval_scope_sha256") != approval_digest(scope):
        raise LiveGateError("Group C approval digest binding drifted")
    observations = ExecutionObservations(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
        dirty_tracked=dirty,
    )
    with file_manifest.lease() as lease:
        authorization = claim_execution_authorization(
            scope,
            approval,
            approval_root=target.parent / "approvals",
            observations=observations,
            execution_id=f"group-c-{secrets.token_hex(8)}",
        )

        def run_group_c() -> BackgroundCanaryResult:
            lease.verify_init_ack()
            materialized.paths.event_log.write_bytes(b"")
            adapter = _LiveGroupCAdapter(
                materialized,
                authorization,
                env=execution_env,
            )
            result = run_write_race_canary(adapter)
            lease.verify_init_ack()
            return result

        result = _run_with_model_circuit(
            run_group_c,
            circuit_path=circuit_path,
            source_group="C",
        )
    projection = project_background_result(result, prerequisite)
    projection["cli_content_sha256"] = bound_identity.sha256
    source_sha256 = _combined_source_sha256((
        materialized.paths.event_log,
        materialized.paths.guard_ack,
        materialized.paths.local_state,
        materialized.paths.consumed_ledger,
    ))
    candidate = fixture_envelope(
        kind="live_background_lifecycle",
        observed_cli_version=prerequisite.observed_cli_version,
        source_kind="bounded_background_projection",
        source_sha256=source_sha256,
        payload=projection,
        observed=sorted(projection),
        missing=list(prerequisite.missing_fields),
    )
    write_json_atomic(materialized.paths.candidate, candidate)
    return projection


def _task6_circuit_category(error: LiveGateError) -> str | None:
    message = str(error)
    if "attach requires an inherited visible TTY" in message:
        return None
    if "QUOTA_PAUSED" in message:
        return "quota"
    if any(fragment in message.casefold() for fragment in (
        "worktree", "handoff", "common-dir", "checkout", "repository",
    )):
        return "worktree"
    return "schema"


def _run_with_model_circuit(
    action: Callable[[], Any],
    *,
    circuit_path: str | Path,
    source_group: str,
) -> Any:
    try:
        return action()
    except BaseException as exc:
        category = (
            _task6_circuit_category(exc)
            if isinstance(exc, LiveGateError)
            else "schema"
        )
        if category is not None:
            open_model_group_circuit(
                circuit_path,
                category=category,
                source_group=source_group,
            )
        raise


def _require_inherited_tty() -> None:
    streams = (sys.stdin, sys.stdout, sys.stderr)
    if any(not hasattr(stream, "isatty") or not stream.isatty() for stream in streams):
        raise LiveGateError("attach requires an inherited visible TTY")


def execute_needs_input(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    approval: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    attach: bool,
    context_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    execution_env = os.environ if env is None else env
    assert_no_credential_overrides(execution_env)
    if attach:
        _require_inherited_tty()
    target = Path(root).resolve(strict=True)
    circuit_path = _model_circuit_path(target)
    require_model_groups_available(circuit_path)
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_task6_capabilities(
        load_bound_host_capabilities(identity_path),
        group="D",
        include_attach=attach,
    )
    prerequisite = load_context_prerequisite(
        _context_root(target) if context_root is None else context_root,
        cli=cli,
        bound_identity=bound_identity,
    )
    materialized = load_needs_input(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
    )
    layout = _read_json(target / "layout.json", "Group D layout")
    if layout.get("include_attach") is not attach:
        raise LiveGateError("Group D attach approval mode drifted")
    manifest_sha256, file_manifest, _contract = build_task6_execution_manifest(
        materialized,
        prerequisite,
        group="D",
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
    )
    git_head, dirty = _git_checkpoint(project_root)
    scope = build_group_d_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        launch_argv=materialized.launch_argv,
        worktree_hook_argv=materialized.worktree_hook_argv,
        include_attach=attach,
        exact_targets=_task6_exact_targets(materialized, group="D"),
    )
    pending = _read_json(target / "pending-scope.json", "Group D pending scope")
    if pending != _scope_payload(scope):
        raise LiveGateError("Group D preview drifted")
    if layout.get("approval_scope_sha256") != approval_digest(scope):
        raise LiveGateError("Group D approval digest binding drifted")
    observations = ExecutionObservations(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
        dirty_tracked=dirty,
    )
    with file_manifest.lease() as lease:
        authorization = claim_execution_authorization(
            scope,
            approval,
            approval_root=target.parent / "approvals",
            observations=observations,
            execution_id=f"group-d-{secrets.token_hex(8)}",
        )

        def run_group_d() -> NeedsInputCanaryResult:
            lease.verify_init_ack()
            materialized.paths.event_log.write_bytes(b"")
            adapter = _LiveGroupDAdapter(
                materialized,
                authorization,
                env=execution_env,
            )
            result = run_needs_input_canary(adapter, include_attach=attach)
            lease.verify_init_ack()
            return result

        result = _run_with_model_circuit(
            run_group_d,
            circuit_path=circuit_path,
            source_group="D",
        )
    projection = project_needs_input_result(result, prerequisite)
    projection["cli_content_sha256"] = bound_identity.sha256
    source_sha256 = _combined_source_sha256((
        materialized.paths.event_log,
        materialized.paths.guard_ack,
        materialized.paths.local_state,
        materialized.paths.consumed_ledger,
        materialized.paths.lease_ack,
    ))
    candidate = fixture_envelope(
        kind="live_background_needs_input",
        observed_cli_version=prerequisite.observed_cli_version,
        source_kind="bounded_needs_input_projection",
        source_sha256=source_sha256,
        payload=projection,
        observed=sorted(projection),
        missing=list(prerequisite.missing_fields),
    )
    write_json_atomic(materialized.paths.candidate, candidate)
    return projection


def execute_concurrency(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    approval: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    context_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    execution_env = os.environ if env is None else env
    assert_no_credential_overrides(execution_env)
    target = Path(root).resolve(strict=True)
    circuit_path = _model_circuit_path(target)
    require_model_groups_available(circuit_path)
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_task6_capabilities(
        load_bound_host_capabilities(identity_path), group="F",
    )
    prerequisite = load_context_prerequisite(
        _context_root(target) if context_root is None else context_root,
        cli=cli,
        bound_identity=bound_identity,
    )
    materialized = load_concurrency(
        target,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        bound_identity=bound_identity,
    )
    manifest_sha256, file_manifest, _contract = build_task6_execution_manifest(
        materialized,
        prerequisite,
        group="F",
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
    )
    git_head, dirty = _git_checkpoint(project_root)
    scope = build_group_f_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        launch_argv=materialized.launch_argv,
        group_names=materialized.group_names,
        exact_targets=_task6_exact_targets(materialized, group="F"),
    )
    pending = _read_json(target / "pending-scope.json", "Group F pending scope")
    if pending != _scope_payload(scope):
        raise LiveGateError("Group F preview drifted")
    layout = _read_json(target / "layout.json", "Group F layout")
    if layout.get("approval_scope_sha256") != approval_digest(scope):
        raise LiveGateError("Group F approval digest binding drifted")
    observations = ExecutionObservations(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
        dirty_tracked=dirty,
    )
    with file_manifest.lease() as lease:
        authorization = claim_execution_authorization(
            scope,
            approval,
            approval_root=target.parent / "approvals",
            observations=observations,
            execution_id=f"group-f-{secrets.token_hex(8)}",
        )

        def run_group_f() -> ConcurrencyCanaryResult:
            lease.verify_init_ack()
            materialized.paths.event_log.write_bytes(b"")
            adapter = _LiveGroupFAdapter(
                materialized,
                authorization,
                env=execution_env,
            )
            result = run_concurrency_canary(
                adapter, group_names=materialized.group_names,
            )
            lease.verify_init_ack()
            return result

        result = _run_with_model_circuit(
            run_group_f,
            circuit_path=circuit_path,
            source_group="F",
        )
    projection = project_concurrency_result(result, prerequisite)
    projection["cli_content_sha256"] = bound_identity.sha256
    source_sha256 = _combined_source_sha256((
        materialized.paths.event_log,
        materialized.paths.guard_ack,
        materialized.paths.local_state,
        materialized.paths.consumed_ledger,
    ))
    candidate = fixture_envelope(
        kind="live_background_concurrency",
        observed_cli_version=prerequisite.observed_cli_version,
        source_kind="bounded_concurrency_projection",
        source_sha256=source_sha256,
        payload=projection,
        observed=sorted(projection),
        missing=list(prerequisite.missing_fields),
    )
    write_json_atomic(materialized.paths.candidate, candidate)
    return projection


def _write_cleanup_residual(
    materialized: MaterializedCleanup,
    result: CleanupCanaryResult,
) -> None:
    remaining = materialized.observations[result.removal_success_count:]
    write_json_atomic(materialized.paths.residual, {
        "schema_version": 1,
        "status": result.status,
        "removed_count": result.removal_success_count,
        "residual_count": len(remaining),
        "reason_categories": list(result.residual_reasons),
        "targets": [
            {
                "source_group": item.source_group,
                "short_id": item.row.short_id,
                "row_fingerprint": _cleanup_row_fingerprint(item.row),
                "worktree_path": str(item.row.cwd),
                "creation_scope_sha256": item.creation_scope_sha256,
            }
            for item in remaining
        ],
    })


def execute_cleanup(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    approval: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    worktree_hook: str | Path,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    execution_env = os.environ if env is None else env
    assert_no_credential_overrides(execution_env)
    target = Path(root).resolve(strict=True)
    live_root = target.parent.resolve(strict=True)
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    _require_cleanup_capabilities(load_bound_host_capabilities(identity_path))
    observations = discover_cleanup_observations(
        live_root,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        worktree_hook=worktree_hook,
        bound_identity=bound_identity,
        env=execution_env,
    )
    reasons = _cleanup_audit_reasons(observations)
    retained_f = _retained_group_f_count(live_root)
    materialized = load_cleanup(
        target,
        observations,
        bound_identity=bound_identity,
        retained_group_f_count=retained_f,
    )
    if reasons:
        result = CleanupCanaryResult(
            status="RECOVERY_REQUIRED",
            audited_target_count=len(observations),
            removal_attempt_count=0,
            removal_success_count=0,
            worktree_remove_hook_count=0,
            residual_count=len(observations),
            residual_reasons=reasons,
            all_worktree_remove_events_matched=False,
            all_paths_absent=False,
            all_rows_absent=False,
            unrelated_state_unchanged=True,
        )
        _write_cleanup_residual(materialized, result)
        return project_cleanup_result(result)
    manifest_sha256, file_manifest, _execution_contract = build_cleanup_execution_manifest(
        materialized, cli=cli, python_exe=python_exe,
    )
    git_head, dirty = _git_checkpoint(project_root)
    scope = build_group_g_scope(
        git_head=git_head,
        cli_sha256=bound_identity.sha256,
        executable_manifest_sha256=manifest_sha256,
        cli=cli,
        short_ids=tuple(item.row.short_id for item in observations),
        worktree_targets=tuple(str(item.row.cwd) for item in observations),
        exact_targets=_cleanup_exact_targets(materialized),
        cleanup_contract_sha256=materialized.contract_sha256,
    )
    pending = _read_json(materialized.paths.pending_scope, "Group G pending scope")
    if pending != _scope_payload(scope):
        raise LiveGateError("Group G preview scope drifted")
    execution_observations = ExecutionObservations(
        git_head=git_head,
        cli_sha256=bound_identity.sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
        dirty_tracked=dirty,
    )
    with file_manifest.lease() as lease:
        authorization = claim_execution_authorization(
            scope,
            approval,
            approval_root=live_root / "approvals",
            observations=execution_observations,
            execution_id=f"group-g-{secrets.token_hex(8)}",
        )
        refreshed = discover_cleanup_observations(
            live_root,
            cli=cli,
            python_exe=python_exe,
            hook_sink=hook_sink,
            worktree_hook=worktree_hook,
            bound_identity=bound_identity,
            env=execution_env,
        )
        refreshed_reasons = _cleanup_audit_reasons(refreshed)
        refreshed_contract = _cleanup_contract_payload(
            refreshed, retained_group_f_count=_retained_group_f_count(live_root),
        )
        if refreshed_reasons or refreshed_contract != materialized.contract:
            result = CleanupCanaryResult(
                status="RECOVERY_REQUIRED",
                audited_target_count=len(refreshed),
                removal_attempt_count=0,
                removal_success_count=0,
                worktree_remove_hook_count=0,
                residual_count=len(refreshed),
                residual_reasons=(
                    refreshed_reasons or ("cleanup_audit_digest_drift",)
                ),
                all_worktree_remove_events_matched=False,
                all_paths_absent=False,
                all_rows_absent=False,
                unrelated_state_unchanged=True,
            )
            _write_cleanup_residual(materialized, result)
            return project_cleanup_result(result)
        adapter = _LiveCleanupAdapter(
            materialized,
            authorization,
            cli=cli,
            env=execution_env,
        )
        result = run_cleanup_canary(adapter, materialized.observations)
        lease.verify_init_ack()

    projection = project_cleanup_result(result)
    projection["cli_content_sha256"] = bound_identity.sha256
    projection["retained_group_f_row_only_count"] = retained_f
    if result.status != "PASS":
        _write_cleanup_residual(materialized, result)
        return projection
    source_paths = [
        materialized.paths.contract,
        materialized.paths.pending_scope,
        materialized.paths.consumed_ledger,
        *(
            item.source_event_log
            for item in materialized.observations
            if item.source_event_log is not None
        ),
    ]
    candidate = fixture_envelope(
        kind="live_worktree_remove",
        observed_cli_version=bound_identity.version,
        source_kind="bounded_cleanup_projection",
        source_sha256=_combined_source_sha256(tuple(source_paths)),
        payload=projection,
        observed=sorted(projection),
        missing=(
            ["row_only_group_f_cleanup"] if retained_f else []
        ),
    )
    write_json_atomic(materialized.paths.candidate, candidate)
    return projection


def _run_pretool_guard(args: argparse.Namespace) -> int:
    raw = read_fd_bounded(0, _MAX_LOCAL_BYTES)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveGateError("PreToolUse stdin is malformed") from exc
    if not isinstance(payload, dict):
        raise LiveGateError("PreToolUse stdin must be an object")
    pretool_guard(
        payload,
        lease_ack=args.lease_ack,
        event_log=args.event_log,
        guard_ack=args.guard_ack,
        worktree_root=args.worktree_root,
        execution_id=args.execution_id,
        proof_relative=args.proof_relative,
        policy=args.policy,
    )
    return 0


def _run_concurrency_guard(args: argparse.Namespace) -> int:
    raw = read_fd_bounded(0, _MAX_LOCAL_BYTES)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveGateError("concurrency PreToolUse stdin is malformed") from exc
    if not isinstance(payload, dict):
        raise LiveGateError("concurrency PreToolUse stdin must be an object")
    concurrency_pretool_guard(
        payload,
        guard_ack=args.guard_ack,
        repo=args.repo,
    )
    return 0


def _add_live_group_arguments(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--context-root", type=Path)
    parser.add_argument("--approval", type=Path)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    guard = subparsers.add_parser("pretool-guard")
    guard.add_argument("--lease-ack", type=Path, required=True)
    guard.add_argument("--event-log", type=Path, required=True)
    guard.add_argument("--guard-ack", type=Path, required=True)
    guard.add_argument("--worktree-root", type=Path, required=True)
    guard.add_argument("--execution-id", required=True)
    guard.add_argument("--proof-relative", default=_PROOF_RELATIVE)
    guard.add_argument("--policy", choices=("main", "needs-input"), default="main")

    concurrency_guard = subparsers.add_parser("concurrency-guard")
    concurrency_guard.add_argument("--guard-ack", type=Path, required=True)
    concurrency_guard.add_argument("--repo", type=Path, required=True)

    matrix = subparsers.add_parser("matrix-finalize")
    matrix.add_argument("--group-c-candidate", type=Path, required=True)
    matrix.add_argument("--group-d-candidate", type=Path, required=True)
    matrix.add_argument("--group-f-candidate", type=Path, required=True)
    matrix.add_argument("--stop-failure-contract", type=Path, required=True)
    matrix.add_argument("--output", type=Path, required=True)

    group = subparsers.add_parser("main")
    _add_live_group_arguments(group)

    needs_input = subparsers.add_parser("needs-input")
    _add_live_group_arguments(needs_input)
    needs_input.add_argument("--include-attach", action="store_true")
    needs_input.add_argument("--attach", action="store_true")

    concurrency = subparsers.add_parser("concurrency")
    _add_live_group_arguments(concurrency)

    cleanup = subparsers.add_parser("cleanup")
    cleanup_mode = cleanup.add_mutually_exclusive_group(required=True)
    cleanup_mode.add_argument("--preview", action="store_true")
    cleanup_mode.add_argument("--execute", action="store_true")
    cleanup.add_argument("--root", type=Path, required=True)
    cleanup.add_argument("--cli", type=Path, required=True)
    cleanup.add_argument("--project-root", type=Path, default=Path.cwd())
    cleanup.add_argument("--approval", type=Path)

    args = parser.parse_args()
    if args.command == "pretool-guard":
        try:
            return _run_pretool_guard(args)
        except LiveGateError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "concurrency-guard":
        try:
            return _run_concurrency_guard(args)
        except LiveGateError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "matrix-finalize":
        result = finalize_background_matrix(
            args.group_c_candidate,
            args.group_d_candidate,
            args.group_f_candidate,
            args.stop_failure_contract,
            output=args.output,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "cleanup":
        cleanup_common = {
            "root": args.root,
            "cli": args.cli,
            "project_root": args.project_root,
            "python_exe": Path(sys.executable),
            "hook_sink": Path(__file__).with_name("hook_sink.py"),
            "worktree_hook": Path(__file__).with_name("worktree_hook.py"),
        }
        if args.preview:
            if args.approval is not None:
                parser.error("--approval is only valid with --execute")
            result = preview_cleanup(**cleanup_common)
        else:
            if args.approval is None:
                parser.error("--approval is required with --execute")
            result = execute_cleanup(**cleanup_common, approval=args.approval)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") != "RECOVERY_REQUIRED" else 2

    common = {
        "root": args.root,
        "cli": args.cli,
        "project_root": args.project_root,
        "python_exe": Path(sys.executable),
        "hook_sink": Path(__file__).with_name("hook_sink.py"),
        "worktree_hook": Path(__file__).with_name("worktree_hook.py"),
        "context_root": args.context_root,
    }
    if args.command == "main":
        if args.preview:
            result = preview_background(**common)
        else:
            if args.approval is None:
                parser.error("--approval is required with --execute")
            result = execute_background(**common, approval=args.approval)
    elif args.command == "needs-input":
        if args.preview:
            if args.attach:
                parser.error("--attach is only valid with --execute")
            result = preview_needs_input(
                **common, include_attach=args.include_attach,
            )
        else:
            if args.approval is None:
                parser.error("--approval is required with --execute")
            if args.include_attach:
                parser.error("--include-attach is only valid with --preview")
            result = execute_needs_input(
                **common, approval=args.approval, attach=args.attach,
            )
    else:
        if args.preview:
            result = preview_concurrency(**common)
        else:
            if args.approval is None:
                parser.error("--approval is required with --execute")
            result = execute_concurrency(**common, approval=args.approval)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
