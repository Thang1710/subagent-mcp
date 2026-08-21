from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from spikes.phase0a import live_common

from spikes.phase0a.live_common import (
    ApprovalScope,
    ExecutionObservations,
    BoundCliIdentity,
    BoundExecutableManifest,
    RuntimeBinding,
    SideEffectSpec,
    approval_digest,
    claim_execution_authorization,
    consume_side_effect,
    require_one_shot_approval,
    run_json_command,
    run_stream_command,
    validate_checkpoint_write_set,
)


FIXED_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _approval_root(tmp_path: Path) -> Path:
    return tmp_path / ".phase0a" / "live" / "approvals"


def _make_private_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        if os.name == "nt":
            live_common._set_private_windows_acl(directory)
        else:
            os.chmod(directory, 0o700)
        live_common._verify_private_path(directory, directory=True)


def _security_snapshot(path: Path) -> bytes | int:
    if os.name != "nt":
        return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    return subprocess.run(
        ["icacls", str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows security descriptor contract")
def test_windows_private_descriptor_binds_current_user_as_owner() -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    owner = ctypes.c_void_p()
    owner_defaulted = wintypes.BOOL()
    current_sid = ctypes.c_void_p()
    with live_common._windows_private_descriptor() as descriptor:
        assert advapi32.GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(owner),
            ctypes.byref(owner_defaulted),
        )
        assert owner.value
        assert advapi32.ConvertStringSidToSidW(
            live_common._windows_current_sid(),
            ctypes.byref(current_sid),
        )
        try:
            assert advapi32.EqualSid(owner, current_sid)
        finally:
            kernel32.LocalFree(current_sid)


def _observations(scope: ApprovalScope, *, dirty_tracked: bool = False) -> ExecutionObservations:
    return ExecutionObservations(
        git_head=scope.git_head,
        cli_sha256=scope.cli_sha256,
        executable_manifest_sha256=scope.executable_manifest_sha256,
        trust_revision=scope.trust_revision,
        dirty_tracked=dirty_tracked,
    )


def _write_test_receipt(
    tmp_path: Path,
    scope: ApprovalScope,
    *,
    approved_at: datetime = FIXED_NOW,
    expires_at: datetime | None = None,
    consumed_at: datetime | None = None,
) -> Path:
    receipt = _approval_root(tmp_path) / "test-receipt.json"
    _make_private_directory(receipt.parent)
    live_common._write_private_json(receipt, {
        "scope_sha256": approval_digest(scope),
        "approved_at": approved_at.isoformat(),
        "expires_at": (expires_at or approved_at + timedelta(hours=2)).isoformat(),
        "consumed_at": None if consumed_at is None else consumed_at.isoformat(),
        "claimed_execution_id": None,
    }, exclusive=True)
    return receipt


def _scope(*, attach_uses: int = 1) -> ApprovalScope:
    return ApprovalScope(
        schema_version=1,
        git_head="a" * 40,
        cli_sha256="b" * 64,
        gate_ids=("context_attestation",),
        side_effects=(
            SideEffectSpec(
                kind="attach",
                argv_template=("<bound-cli>", "attach", "{short_id}"),
                bindings=(RuntimeBinding(
                    token="{short_id}",
                    state_key="group.short_id",
                    pattern=r"^[A-Za-z0-9_-]{1,64}$",
                    require_group_owned=True,
                ),),
                max_uses=attach_uses,
                exact_targets=(),
            ),
        ),
        max_provider_session_launches=0,
        max_worktree_creates=0,
        max_stop_respawn_actions=0,
        max_attach_actions=attach_uses,
        max_file_deletes=0,
        max_removals=0,
        background_internal_requests_acknowledged=False,
        executable_manifest_sha256="e" * 64,
        trust_revision=1,
    )


def test_one_shot_receipt_binds_every_side_effect(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)

    assert require_one_shot_approval(
        scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope), now=FIXED_NOW,
    ) == scope

    claim_execution_authorization(
        scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope),
        execution_id="test-execution", now=FIXED_NOW,
    )
    with pytest.raises(PermissionError, match="consumed"):
        require_one_shot_approval(
            scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope), now=FIXED_NOW,
        )


def test_approval_digest_is_canonical_and_binds_all_scope_fields() -> None:
    scope = _scope()
    body = json.dumps(scope.to_dict(), sort_keys=True, separators=(",", ":"))

    assert approval_digest(scope) == __import__("hashlib").sha256(
        body.encode("utf-8")
    ).hexdigest()
    assert approval_digest(scope) != approval_digest(_scope(attach_uses=2))


def test_distinct_provider_arms_share_the_aggregate_launch_counter() -> None:
    scope = replace(
        _scope(),
        side_effects=(
            SideEffectSpec(
                kind="provider_control_launch",
                argv_template=("claude", "control"),
                bindings=(),
                max_uses=1,
                exact_targets=(),
            ),
            SideEffectSpec(
                kind="provider_launch",
                argv_template=("claude", "context"),
                bindings=(),
                max_uses=1,
                exact_targets=(),
            ),
        ),
        max_provider_session_launches=2,
        max_attach_actions=0,
    )

    assert len(approval_digest(scope)) == 64


def test_provider_arms_cannot_exceed_their_aggregate_launch_counter() -> None:
    scope = replace(
        _scope(),
        side_effects=(
            SideEffectSpec(
                kind="provider_control_launch",
                argv_template=("claude", "control"),
                bindings=(),
                max_uses=1,
                exact_targets=(),
            ),
            SideEffectSpec(
                kind="provider_launch",
                argv_template=("claude", "context"),
                bindings=(),
                max_uses=1,
                exact_targets=(),
            ),
        ),
        max_provider_session_launches=1,
        max_attach_actions=0,
    )

    with pytest.raises(ValueError, match="aggregate scope counter"):
        approval_digest(scope)


def test_empty_argv_value_is_allowed_only_for_exact_provider_tools_pair() -> None:
    provider = replace(
        _scope(),
        side_effects=(SideEffectSpec(
            kind="provider_launch",
            argv_template=("claude", "--tools", "", "prompt"),
            bindings=(),
            max_uses=1,
            exact_targets=(),
        ),),
        max_provider_session_launches=1,
        max_attach_actions=0,
    )
    attach = replace(
        _scope(),
        side_effects=(SideEffectSpec(
            kind="attach",
            argv_template=("claude", "attach", ""),
            bindings=(),
            max_uses=1,
            exact_targets=(),
        ),),
    )

    assert len(approval_digest(provider)) == 64
    with pytest.raises(ValueError, match="empty argv"):
        approval_digest(attach)


def test_expired_receipt_is_rejected_before_side_effect(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(
        tmp_path,
        scope,
        approved_at=FIXED_NOW - timedelta(hours=1),
        expires_at=FIXED_NOW - timedelta(seconds=1),
    )

    with pytest.raises(PermissionError, match="expired"):
        require_one_shot_approval(
            scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope), now=FIXED_NOW,
        )


def test_mismatched_digest_never_invokes_side_effect_runner(tmp_path: Path) -> None:
    original = _scope()
    receipt = _write_test_receipt(tmp_path, original)
    calls: list[tuple[str, ...]] = []

    with pytest.raises(PermissionError, match="digest mismatch"):
        claim_execution_authorization(
            _scope(attach_uses=2),
            receipt,
            approval_root=_approval_root(tmp_path), observations=_observations(original),
            execution_id="test-execution",
            now=FIXED_NOW,
        )

    assert calls == []


def test_consumed_side_effect_appends_concrete_argv_before_invocation(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    ledger = tmp_path / ".phase0a/live/consumed.json"
    observed: list[tuple[str, ...]] = []
    authorization = claim_execution_authorization(
        scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope),
        execution_id="test-execution", now=FIXED_NOW,
    )

    result = consume_side_effect(
        authorization,
        "attach",
        {"group": {"short_id": "abc_123"}},
        ledger,
        now=FIXED_NOW,
        invoke=lambda argv: observed.append(argv),
    )

    assert result is None
    assert observed == [("<bound-cli>", "attach", "abc_123")]
    assert json.loads(ledger.read_text(encoding="utf-8")) == [
        {"argv": ["<bound-cli>", "attach", "abc_123"], "kind": "attach", "targets": []}
    ]
    with pytest.raises(PermissionError, match="consumed"):
        require_one_shot_approval(
            scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope), now=FIXED_NOW,
        )


def test_consumed_ledger_cannot_escape_authorized_live_root_or_mutate_external_directory(
    tmp_path: Path,
) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    authorization = claim_execution_authorization(
        scope,
        receipt,
        approval_root=_approval_root(tmp_path),
        observations=_observations(scope),
        execution_id="contained-execution",
        now=FIXED_NOW,
    )
    external = tmp_path / "external-repo"
    external.mkdir()
    sentinel = external / "owned.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before_security = _security_snapshot(external)
    before_content = sentinel.read_bytes()
    calls: list[tuple[str, ...]] = []

    with pytest.raises(PermissionError, match="live root|ledger"):
        consume_side_effect(
            authorization,
            "attach",
            {"group": {"short_id": "abc_123"}},
            external / "consumed.json",
            now=FIXED_NOW,
            invoke=lambda argv: calls.append(argv),
        )

    assert calls == []
    assert not (external / "consumed.json").exists()
    assert sentinel.read_bytes() == before_content
    assert _security_snapshot(external) == before_security


def test_consumed_ledger_requires_precreated_private_parent(tmp_path: Path) -> None:
    scope = _scope(attach_uses=2)
    receipt = _write_test_receipt(tmp_path, scope)
    authorization = claim_execution_authorization(
        scope,
        receipt,
        approval_root=_approval_root(tmp_path),
        observations=_observations(scope),
        execution_id="precreated-execution",
        now=FIXED_NOW,
    )
    live_root = _approval_root(tmp_path).parent
    missing_parent = live_root / "missing"
    calls: list[tuple[str, ...]] = []

    with pytest.raises(PermissionError, match="pre-created|private"):
        consume_side_effect(
            authorization,
            "attach",
            {"group": {"short_id": "abc_123"}},
            missing_parent / "consumed.json",
            now=FIXED_NOW,
            invoke=lambda argv: calls.append(argv),
        )
    assert calls == []
    assert not missing_parent.exists()

    insecure_parent = live_root / "insecure"
    insecure_parent.mkdir(mode=0o755)
    if os.name != "nt":
        os.chmod(insecure_parent, 0o755)
    before_security = _security_snapshot(insecure_parent)
    with pytest.raises(PermissionError, match="private|owner-only"):
        consume_side_effect(
            authorization,
            "attach",
            {"group": {"short_id": "abc_123"}},
            insecure_parent / "consumed.json",
            now=FIXED_NOW,
            invoke=lambda argv: calls.append(argv),
        )
    assert calls == []
    assert not (insecure_parent / "consumed.json").exists()
    assert _security_snapshot(insecure_parent) == before_security


def test_consumed_ledger_rejects_traversal_and_live_identity_drift(
    tmp_path: Path,
) -> None:
    scope = _scope(attach_uses=2)
    receipt = _write_test_receipt(tmp_path, scope)
    authorization = claim_execution_authorization(
        scope,
        receipt,
        approval_root=_approval_root(tmp_path),
        observations=_observations(scope),
        execution_id="stable-live-root",
        now=FIXED_NOW,
    )
    live_root = _approval_root(tmp_path).parent
    calls: list[tuple[str, ...]] = []

    with pytest.raises(PermissionError, match="traversal"):
        consume_side_effect(
            authorization,
            "attach",
            {"group": {"short_id": "abc_123"}},
            live_root / "nested" / ".." / "consumed.json",
            invoke=lambda argv: calls.append(argv),
        )

    drifted = replace(
        authorization,
        live_root_identity=tuple(value + 1 for value in authorization.live_root_identity),
    )
    with pytest.raises(PermissionError, match="identity (changed|drifted)"):
        consume_side_effect(
            drifted,
            "attach",
            {"group": {"short_id": "abc_123"}},
            live_root / "consumed.json",
            invoke=lambda argv: calls.append(argv),
        )

    assert calls == []


def test_consumed_ledger_rejects_symlink_or_reparse_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    authorization = claim_execution_authorization(
        scope,
        receipt,
        approval_root=_approval_root(tmp_path),
        observations=_observations(scope),
        execution_id="reparse-parent",
        now=FIXED_NOW,
    )
    live_root = _approval_root(tmp_path).parent
    outside = tmp_path / "outside-private"
    _make_private_directory(outside)
    link = live_root / "linked"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (NotImplementedError, OSError):
        if os.name != "nt":
            pytest.skip("directory symlinks are unavailable")
        _make_private_directory(link)
        original_is_reparse = live_common._windows_handle_is_reparse

        def simulated_reparse(handle: int) -> bool:
            return (
                Path(live_common._canonical_windows_path_from_handle(handle)) == link
                or original_is_reparse(handle)
            )

        monkeypatch.setattr(live_common, "_windows_handle_is_reparse", simulated_reparse)

    with pytest.raises(PermissionError, match="reparse|private|pre-created"):
        consume_side_effect(
            authorization,
            "attach",
            {"group": {"short_id": "abc_123"}},
            link / "consumed.json",
        )
    assert not (outside / "consumed.json").exists()
    assert not (link / "consumed.json").exists()


def test_missing_or_dirty_execute_observations_fail_before_receipt_claim(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    with pytest.raises(PermissionError, match="observations"):
        require_one_shot_approval(scope, receipt, approval_root=_approval_root(tmp_path), now=FIXED_NOW)
    with pytest.raises(PermissionError, match="dirty"):
        require_one_shot_approval(
            scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope, dirty_tracked=True), now=FIXED_NOW,
        )


def test_future_receipt_and_root_substitution_are_rejected(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope, approved_at=FIXED_NOW + timedelta(seconds=1))
    with pytest.raises(PermissionError, match="future"):
        require_one_shot_approval(
            scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope), now=FIXED_NOW,
        )
    with pytest.raises(PermissionError, match="bound approval root"):
        require_one_shot_approval(
            scope, receipt, approval_root=tmp_path / "other" / "approvals", observations=_observations(scope), now=FIXED_NOW,
        )
    assert not (tmp_path / "other").exists()


def test_group_claim_allows_each_approved_use_once_under_race(tmp_path: Path) -> None:
    scope = _scope(attach_uses=2)
    receipt = _write_test_receipt(tmp_path, scope)
    authorization = claim_execution_authorization(
        scope, receipt, approval_root=_approval_root(tmp_path), observations=_observations(scope),
        execution_id="test-execution", now=FIXED_NOW,
    )
    calls: list[tuple[str, ...]] = []
    errors: list[BaseException] = []
    start = threading.Barrier(3)

    def consume() -> None:
        try:
            start.wait()
            consume_side_effect(
                authorization, "attach", {"group": {"short_id": "abc_123"}},
                tmp_path / ".phase0a/live/consumed.json", now=FIXED_NOW,
                invoke=lambda argv: calls.append(argv),
            )
        except BaseException as exc:  # tests record competing outcome
            errors.append(exc)

    first = threading.Thread(target=consume)
    second = threading.Thread(target=consume)
    first.start(); second.start(); start.wait(); first.join(); second.join()

    assert errors == []
    assert calls == [("<bound-cli>", "attach", "abc_123")] * 2


def test_competing_receipt_claim_race_has_exactly_one_winner(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    start = threading.Barrier(3)
    winners: list[str] = []
    errors: list[BaseException] = []

    def claim(execution_id: str) -> None:
        try:
            start.wait()
            claim_execution_authorization(
                scope,
                receipt,
                approval_root=_approval_root(tmp_path),
                observations=_observations(scope),
                execution_id=execution_id,
                now=FIXED_NOW,
            )
            winners.append(execution_id)
        except BaseException as exc:  # tests record the losing claim
            errors.append(exc)

    first = threading.Thread(target=claim, args=("execution-one",))
    second = threading.Thread(target=claim, args=("execution-two",))
    first.start(); second.start(); start.wait(); first.join(); second.join()

    assert len(winners) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], PermissionError)


def test_claim_reads_and_updates_the_pinned_receipt_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    original_read_text = Path.read_text
    original_private_write = live_common._write_private_json

    def reject_receipt_reopen(path: Path, *args: object, **kwargs: object) -> str:
        if path == receipt:
            raise AssertionError("receipt was reopened by path")
        return original_read_text(path, *args, **kwargs)

    def reject_receipt_replace(path: Path, *args: object, **kwargs: object) -> None:
        if path == receipt:
            raise AssertionError("receipt was replaced by path")
        original_private_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_receipt_reopen)
    monkeypatch.setattr(live_common, "_write_private_json", reject_receipt_replace)

    authorization = claim_execution_authorization(
        scope,
        receipt,
        approval_root=_approval_root(tmp_path),
        observations=_observations(scope),
        execution_id="pinned-execution",
        now=FIXED_NOW,
    )

    assert authorization.execution_id == "pinned-execution"
    marker = receipt.with_name(receipt.name + ".claim")
    live_common._verify_private_path(marker, directory=False)
    assert not receipt.with_name(receipt.name + ".claim.lock").exists()


def test_claim_marker_durability_precedes_receipt_update_and_crash_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    ordering: list[str] = []
    original_barrier = live_common._durable_claim_barrier
    original_replace = live_common._replace_json_fd

    def record_barrier(opened: object) -> None:
        ordering.append("marker-durable")
        original_barrier(opened)

    def record_replace(fd: int, payload: object) -> None:
        ordering.append("receipt-update")
        original_replace(fd, payload)

    monkeypatch.setattr(live_common, "_durable_claim_barrier", record_barrier)
    monkeypatch.setattr(live_common, "_replace_json_fd", record_replace)
    claim_execution_authorization(
        scope,
        receipt,
        approval_root=_approval_root(tmp_path),
        observations=_observations(scope),
        execution_id="ordered-execution",
        now=FIXED_NOW,
    )
    assert ordering == ["marker-durable", "receipt-update"]

    second_scope = _scope(attach_uses=2)
    second_receipt = _write_test_receipt(tmp_path / "second", second_scope)

    def crash_after_marker(_opened: object) -> None:
        raise OSError("injected marker durability failure")

    monkeypatch.setattr(live_common, "_durable_claim_barrier", crash_after_marker)
    with pytest.raises(OSError, match="durability failure"):
        claim_execution_authorization(
            second_scope,
            second_receipt,
            approval_root=_approval_root(tmp_path / "second"),
            observations=_observations(second_scope),
            execution_id="crashed-execution",
            now=FIXED_NOW,
        )

    payload = json.loads(second_receipt.read_text(encoding="utf-8"))
    assert payload["consumed_at"] is None
    assert payload["claimed_execution_id"] is None
    assert second_receipt.with_name(second_receipt.name + ".claim").is_file()
    with pytest.raises(PermissionError, match="consumed"):
        require_one_shot_approval(
            second_scope,
            second_receipt,
            approval_root=_approval_root(tmp_path / "second"),
            observations=_observations(second_scope),
            now=FIXED_NOW,
        )


def test_partial_claim_marker_and_torn_receipt_fail_closed(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    marker = receipt.with_name(receipt.name + ".claim")
    live_common._write_private_json(
        marker,
        {"execution_id": "crashed", "scope_sha256": approval_digest(scope)},
        exclusive=True,
    )

    with pytest.raises(PermissionError, match="claim|consumed"):
        claim_execution_authorization(
            scope,
            receipt,
            approval_root=_approval_root(tmp_path),
            observations=_observations(scope),
            execution_id="second-execution",
            now=FIXED_NOW,
        )

    receipt.write_bytes(b'{"scope_sha256":')
    with pytest.raises(PermissionError, match="receipt"):
        require_one_shot_approval(
            scope,
            receipt,
            approval_root=_approval_root(tmp_path),
            observations=_observations(scope),
            now=FIXED_NOW,
        )


def test_approval_storage_is_owner_only(tmp_path: Path) -> None:
    root = _approval_root(tmp_path)
    nested = root / "nested"
    _make_private_directory(nested)
    receipt = nested / "receipt.json"
    ledger = nested / "ledger.json"
    live_common._write_private_json(receipt, {"ok": True}, exclusive=True)
    live_common._write_private_json(ledger, [], exclusive=True)
    with pytest.raises(FileExistsError):
        live_common._write_private_json(receipt, {"overwritten": True}, exclusive=True)
    assert json.loads(receipt.read_text(encoding="utf-8")) == {"ok": True}

    for directory in (root, nested):
        live_common._verify_private_path(directory, directory=True)
    for private_file in (receipt, ledger):
        live_common._verify_private_path(private_file, directory=False)
    if os.name != "nt":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(nested.stat().st_mode) == 0o700
        assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


def test_private_runtime_group_root_is_owner_only_and_reuses_only_empty_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "group-b"

    created = live_common.prepare_private_runtime_group_root(root)

    assert created == root.resolve(strict=True)
    live_common._verify_private_path(created, directory=True)
    assert live_common.prepare_private_runtime_group_root(root) == created

    (root / "state.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="fresh empty root"):
        live_common.prepare_private_runtime_group_root(root)


def test_private_runtime_group_root_rejects_insecure_existing_without_repair(
    tmp_path: Path,
) -> None:
    root = tmp_path / "group-c"
    root.mkdir(mode=0o700)
    _make_insecure_directory(root)
    before_security = _security_snapshot(root)

    with pytest.raises(PermissionError, match="private|another principal|owner-only"):
        live_common.prepare_private_runtime_group_root(root)

    assert list(root.iterdir()) == []
    assert _security_snapshot(root) == before_security


def test_private_runtime_group_root_rejects_indirect_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    _make_private_directory(target)
    link = tmp_path / "group-d"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("test host cannot create a directory reparse point")

    with pytest.raises(PermissionError, match="direct"):
        live_common.prepare_private_runtime_group_root(link)

    assert link.is_symlink()
    assert list(target.iterdir()) == []


def test_private_json_requires_precreated_private_parent_without_tightening(
    tmp_path: Path,
) -> None:
    missing_parent = tmp_path / "missing" / "security"
    with pytest.raises(PermissionError, match="pre-created|private"):
        live_common._write_private_json(missing_parent / "receipt.json", {"ok": True}, exclusive=True)
    assert not missing_parent.exists()

    insecure_parent = tmp_path / "insecure-security"
    insecure_parent.mkdir(mode=0o755)
    if os.name != "nt":
        os.chmod(insecure_parent, 0o755)
    before_security = _security_snapshot(insecure_parent)
    with pytest.raises(PermissionError, match="private|owner-only"):
        live_common._write_private_json(insecure_parent / "receipt.json", {"ok": True}, exclusive=True)
    assert not (insecure_parent / "receipt.json").exists()
    assert _security_snapshot(insecure_parent) == before_security


def test_insecure_live_root_fails_closed_without_acl_or_mode_repair(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    live_root = _approval_root(tmp_path).parent
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(live_root), "/grant", "*S-1-1-0:(R)"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        os.chmod(live_root, 0o755)
    before_security = _security_snapshot(live_root)
    before_receipt = receipt.read_bytes()

    with pytest.raises(PermissionError, match="private|another principal|owner-only"):
        require_one_shot_approval(
            scope,
            receipt,
            approval_root=_approval_root(tmp_path),
            observations=_observations(scope),
            now=FIXED_NOW,
        )

    assert receipt.read_bytes() == before_receipt
    assert _security_snapshot(live_root) == before_security


def _make_insecure_directory(path: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        os.chmod(path, 0o755)


def test_prepare_approval_storage_creates_only_fixed_private_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = live_common.prepare_approval_storage(Path(".phase0a/live"))

    assert result == {
        "live_root_created": True,
        "approvals_created": True,
        "repaired_existing": False,
    }
    live_root = tmp_path / ".phase0a" / "live"
    approval_root = live_root / "approvals"
    live_common._verify_private_path(live_root, directory=True)
    live_common._verify_private_path(approval_root, directory=True)


def test_prepare_approval_storage_private_creator_owns_every_new_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    created: list[Path] = []
    original_create = live_common._create_private_directory

    def record_private_create(path: Path) -> None:
        created.append(path)
        original_create(path)

    monkeypatch.setattr(live_common, "_create_private_directory", record_private_create)

    live_common.prepare_approval_storage(".phase0a/live")

    phase_root = tmp_path / ".phase0a"
    assert created == [
        phase_root,
        phase_root / "live",
        phase_root / "live" / "approvals",
    ]


@pytest.mark.parametrize("root", ["alternate", ".phase0a/other"])
def test_prepare_approval_storage_rejects_arbitrary_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PermissionError, match="fixed"):
        live_common.prepare_approval_storage(root)

    assert not (tmp_path / ".phase0a").exists()


def test_prepare_approval_storage_rejects_outside_and_reparse_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionError, match="fixed"):
        live_common.prepare_approval_storage(tmp_path / ".phase0a" / "live")

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / ".phase0a"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("test host cannot create a directory reparse point")
    with pytest.raises(PermissionError, match="direct|indirect"):
        live_common.prepare_approval_storage(".phase0a/live")


def test_prepare_approval_storage_rejects_insecure_existing_root_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    live_root = tmp_path / ".phase0a" / "live"
    _make_private_directory(live_root)
    _make_insecure_directory(live_root)
    before_security = _security_snapshot(live_root)

    with pytest.raises(PermissionError, match="private|owner-only|another principal"):
        live_common.prepare_approval_storage(".phase0a/live")

    assert _security_snapshot(live_root) == before_security
    assert not (live_root / "approvals").exists()


def test_prepare_existing_live_does_not_require_phase_root_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    live_root = tmp_path / ".phase0a" / "live"
    _make_private_directory(live_root)
    _make_insecure_directory(live_root)
    phase_root = live_root.parent
    owner_checks: list[Path] = []
    original_owner_check = live_common._verify_current_owner_path

    def reject_phase_owner(path: Path) -> None:
        owner_checks.append(path)
        if path == phase_root:
            raise PermissionError("phase root belongs to another principal")
        original_owner_check(path)

    monkeypatch.setattr(live_common, "_verify_current_owner_path", reject_phase_owner)

    result = live_common.prepare_approval_storage(".phase0a/live", repair_existing=True)

    assert result == {
        "live_root_created": False,
        "approvals_created": True,
        "repaired_existing": True,
    }
    assert phase_root not in owner_checks
    assert live_root in owner_checks
    live_common._verify_private_path(live_root, directory=True)


def test_prepare_missing_live_requires_phase_root_owner_before_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    phase_root = tmp_path / ".phase0a"
    phase_root.mkdir()
    original_owner_check = live_common._verify_current_owner_path

    def reject_phase_owner(path: Path) -> None:
        if path == phase_root:
            raise PermissionError("phase root belongs to another principal")
        original_owner_check(path)

    monkeypatch.setattr(live_common, "_verify_current_owner_path", reject_phase_owner)

    with pytest.raises(PermissionError, match="another principal"):
        live_common.prepare_approval_storage(".phase0a/live")

    assert not (phase_root / "live").exists()


def test_prepare_approval_storage_repairs_only_direct_roots_and_preserves_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    live_root = tmp_path / ".phase0a" / "live"
    approval_root = live_root / "approvals"
    _make_private_directory(approval_root)
    sentinel = live_root / "existing-sentinel.bin"
    sentinel.write_bytes(b"preserve these bytes")
    before_identity = (sentinel.stat().st_dev, sentinel.stat().st_ino)
    before_security = _security_snapshot(sentinel)
    _make_insecure_directory(live_root)

    result = live_common.prepare_approval_storage(".phase0a/live", repair_existing=True)

    assert result == {
        "live_root_created": False,
        "approvals_created": False,
        "repaired_existing": True,
    }
    assert sentinel.read_bytes() == b"preserve these bytes"
    assert (sentinel.stat().st_dev, sentinel.stat().st_ino) == before_identity
    assert _security_snapshot(sentinel) == before_security
    live_common._verify_private_path(live_root, directory=True)
    live_common._verify_private_path(approval_root, directory=True)


def test_prepare_approval_storage_refuses_nonempty_insecure_approvals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    approval_root = tmp_path / ".phase0a" / "live" / "approvals"
    _make_private_directory(approval_root)
    sentinel = approval_root / "existing-receipt.json"
    sentinel.write_bytes(b"must not be changed")
    before_identity = (sentinel.stat().st_dev, sentinel.stat().st_ino)
    _make_insecure_directory(approval_root)
    before_security = _security_snapshot(approval_root)

    with pytest.raises(PermissionError, match="nonempty"):
        live_common.prepare_approval_storage(".phase0a/live", repair_existing=True)

    assert sentinel.read_bytes() == b"must not be changed"
    assert (sentinel.stat().st_dev, sentinel.stat().st_ino) == before_identity
    assert _security_snapshot(approval_root) == before_security


def test_prepare_approval_storage_is_idempotent_for_private_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    first = live_common.prepare_approval_storage(".phase0a/live")
    live_root = tmp_path / ".phase0a" / "live"
    approval_root = live_root / "approvals"
    before_live = _security_snapshot(live_root)
    before_approvals = _security_snapshot(approval_root)

    second = live_common.prepare_approval_storage(".phase0a/live")

    assert first["live_root_created"] is True
    assert second == {
        "live_root_created": False,
        "approvals_created": False,
        "repaired_existing": False,
    }
    assert _security_snapshot(live_root) == before_live
    assert _security_snapshot(approval_root) == before_approvals


def test_prepare_approval_storage_allows_approve_scope_to_write_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    scope_path = tmp_path / "pending-scope.json"
    scope_path.write_text(json.dumps(_scope().to_dict()), encoding="utf-8")
    live_common.prepare_approval_storage(".phase0a/live")
    receipt = Path(".phase0a/live/approvals/approved-A.json")

    live_common._approve_scope(scope_path, receipt, 120)

    live_common._verify_private_path(tmp_path / receipt, directory=False)


def test_main_dispatches_prepare_approval_storage_without_external_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Path, bool]] = []

    def fake_prepare(root: Path, *, repair_existing: bool) -> dict[str, bool]:
        calls.append((root, repair_existing))
        return {
            "live_root_created": False,
            "approvals_created": False,
            "repaired_existing": repair_existing,
        }

    monkeypatch.setattr(live_common, "prepare_approval_storage", fake_prepare)

    assert live_common.main([
        "prepare-approval-storage", "--root", ".phase0a/live", "--repair-existing",
    ]) == 0

    assert calls == [(Path(".phase0a/live"), True)]
    assert json.loads(capsys.readouterr().out) == {
        "live_root_created": False,
        "approvals_created": False,
        "repaired_existing": True,
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL verification")
def test_windows_private_storage_rejects_everyone_access(tmp_path: Path) -> None:
    root = _approval_root(tmp_path)
    _make_private_directory(root)
    subprocess.run(
        ["icacls", str(root), "/grant", "*S-1-1-0:(R)"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    with pytest.raises(PermissionError, match="another principal"):
        live_common._verify_private_path(root, directory=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-safe handle contract")
def test_windows_approval_root_and_receipt_handles_block_path_substitution(tmp_path: Path) -> None:
    scope = _scope()
    receipt = _write_test_receipt(tmp_path, scope)
    root = _approval_root(tmp_path)

    with live_common._open_approval_receipt(receipt, root, exclusive=True) as opened:
        with pytest.raises(PermissionError):
            receipt.unlink()
        with pytest.raises(PermissionError):
            root.rename(tmp_path / "swapped-approvals")
        assert opened.read_payload()["scope_sha256"] == approval_digest(scope)


def _child(lines: list[str], *, linger: bool = False) -> list[str]:
    script = "import sys, time\n" + "\n".join(
        f"print({line!r}, flush=True)" for line in lines
    )
    if linger:
        script += "\ntime.sleep(60)"
    return [sys.executable, "-u", "-c", script]


def _init() -> str:
    return json.dumps({
        "type": "system", "subtype": "init", "model": "claude-sonnet-5",
        "effort": "low", "tools": [], "mcp_servers": [], "plugins": [],
        "requestedAutoCompactionWindow": 274000,
        "requestedAutoCompactionTriggerPercent": 85.0,
        "requestedAutoCompactionTriggerTokens": 274000,
        "effectiveAutoCompactionWindow": 250000,
        "effectiveAutoCompactionTriggerPercent": 80.0,
        "effectiveAutoCompactionTriggerTokens": 200000,
    })


def test_allowed_warning_waits_for_final_result() -> None:
    result = run_stream_command(_child([
        _init(),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "isUsingOverage": False,
        }}),
        json.dumps({"type": "result", "is_error": False, "result": "OK"}),
    ]))

    assert result.classification == "success"
    assert result.is_using_overage is False
    assert result.requested_auto_compaction_window == 274000
    assert result.effective_auto_compaction_window == 250000


def test_stream_records_stderr_presence_without_retaining_its_text() -> None:
    script = (
        "import json, sys; "
        f"print({_init()!r}, flush=True); "
        "print(json.dumps({'type':'result','is_error':False,'result':'OK'}), flush=True); "
        "sys.stderr.write('hook failed at private path'); sys.stderr.flush()"
    )

    result = run_stream_command([sys.executable, "-u", "-c", script])

    assert result.classification == "success"
    assert result.stderr_bytes == len(b"hook failed at private path")
    assert not hasattr(result, "stderr")


def test_compaction_fields_reject_bool_and_preserve_requested_effective_drift() -> None:
    invalid = json.loads(_init())
    invalid["effectiveAutoCompactionTriggerTokens"] = True
    result = run_stream_command(_child([
        json.dumps(invalid), json.dumps({"type": "result", "is_error": False}),
    ]))

    assert result.classification == "protocol_error"


def test_requested_compaction_policy_allows_absent_effective_init_fields() -> None:
    init = json.loads(_init())
    for field in list(init):
        if "Compaction" in field:
            init.pop(field)
    result = run_stream_command(
        _child([json.dumps(init), json.dumps({"type": "result", "is_error": False})]),
        requested_auto_compaction_window=274000,
        requested_auto_compaction_trigger_percent=85.0,
        requested_auto_compaction_trigger_tokens=274000,
    )

    assert result.classification == "success"
    assert result.requested_auto_compaction_window == 274000
    assert result.effective_auto_compaction_window is None


def test_requested_compaction_policy_rejects_invalid_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_common.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")))
    with pytest.raises(ValueError, match="requested_auto_compaction_window"):
        run_stream_command([sys.executable], requested_auto_compaction_window=True)


def test_init_requested_compaction_drift_is_protocol_error() -> None:
    init = json.loads(_init())
    init["requestedAutoCompactionWindow"] = 123
    result = run_stream_command(
        _child([json.dumps(init), json.dumps({"type": "result", "is_error": False})]),
        requested_auto_compaction_window=274000,
        requested_auto_compaction_trigger_percent=85.0,
        requested_auto_compaction_trigger_tokens=274000,
    )
    assert result.classification == "protocol_error"


def test_overage_true_terminates_owned_child() -> None:
    result = run_stream_command(_child([
        _init(),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "isUsingOverage": True,
        }}),
    ], linger=True), timeout_seconds=5)

    assert result.classification == "usage_credits_forbidden"
    assert result.exit_code is not None


def test_terminal_quota_pauses_without_retry() -> None:
    result = run_stream_command(_child([
        _init(),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}),
        json.dumps({"type": "result", "is_error": True, "subtype": "rate_limit", "result": "quota"}),
    ]))

    assert result.classification == "quota_paused"


def test_terminal_credits_required_pauses_only_after_error_result() -> None:
    result = run_stream_command(_child([
        _init(),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"errorCode": "credits_required"}}),
        json.dumps({"type": "result", "is_error": True, "subtype": "credits_required"}),
    ]))

    assert result.classification == "quota_paused"


def test_informational_rejected_advisory_does_not_pause_an_unrelated_error() -> None:
    result = run_stream_command(_child([
        _init(),
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}),
        json.dumps({"type": "result", "is_error": True, "subtype": "internal_error"}),
    ]))

    assert result.classification == "terminal_error"


@pytest.mark.parametrize("lines", [
    [_init(), "{not-json"],
    [_init(), _init(), json.dumps({"type": "result", "is_error": False})],
    [_init(), json.dumps({"type": "result", "is_error": False}), json.dumps({"type": "result", "is_error": False})],
    [json.dumps({"type": "result", "is_error": False})],
])
def test_stream_protocol_failures_fail_closed(lines: list[str]) -> None:
    assert run_stream_command(_child(lines)).classification == "protocol_error"


def test_stream_model_mismatch_terminates_before_result() -> None:
    result = run_stream_command(
        _child([_init()], linger=True), expected_model="claude-opus-5", timeout_seconds=5
    )

    assert result.classification == "model_mismatch"


def test_stream_rejects_unstructured_init_mcp_and_plugin_items() -> None:
    malformed_init = json.dumps({
        "type": "system", "subtype": "init", "model": "claude-sonnet-5",
        "tools": [], "mcp_servers": ["not-an-object"], "plugins": ["not-an-object"],
    })
    result = run_stream_command(_child([
        malformed_init, json.dumps({"type": "result", "is_error": False}),
    ]))

    assert result.classification == "protocol_error"


def test_stream_allows_an_exact_8mib_line_before_protocol_validation() -> None:
    script = (
        "import sys; "
        f"sys.stdout.buffer.write(b'x' * {8 * 1024 * 1024} + b'\\n'); "
        "sys.stdout.buffer.flush()"
    )
    result = run_stream_command([sys.executable, "-c", script])

    assert result.classification == "protocol_error"


def test_stream_timeout_fires_while_child_emits_no_bytes() -> None:
    result = run_stream_command(_child([], linger=True), timeout_seconds=0.05)

    assert result.classification == "timeout"
    assert result.init_envelope_observed is False
    assert result.result_envelope_observed is False
    assert result.timeout_phase == "pre_init"


def test_stream_resets_deadline_once_after_valid_init() -> None:
    final = json.dumps({"type": "result", "is_error": False, "result": "OK"})
    script = (
        "import time; "
        f"print({_init()!r}, flush=True); "
        "time.sleep(0.20); "
        f"print({final!r}, flush=True)"
    )

    result = run_stream_command(
        [sys.executable, "-u", "-c", script],
        timeout_seconds=0.15,
        post_init_timeout_seconds=0.50,
    )

    assert result.classification == "success"
    assert result.init_envelope_observed is True
    assert result.result_envelope_observed is True
    assert result.timeout_phase is None


def test_stream_reports_post_init_timeout_for_slow_completion() -> None:
    result = run_stream_command(
        _child([_init()], linger=True),
        timeout_seconds=0.50,
        post_init_timeout_seconds=0.05,
    )

    assert result.classification == "timeout"
    assert result.init_envelope_observed is True
    assert result.result_envelope_observed is False
    assert result.timeout_phase == "post_init"


@pytest.mark.parametrize("value", [True, 0, -1, 601])
def test_stream_rejects_unbounded_post_init_timeout_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    monkeypatch.setattr(
        live_common.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not spawn"),
        ),
    )

    with pytest.raises(ValueError, match="post_init_timeout_seconds"):
        run_stream_command(
            [sys.executable],
            post_init_timeout_seconds=value,  # type: ignore[arg-type]
        )


def test_pump_reader_threads_are_gone_after_normal_timeout_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_stream_command(_child([_init(), json.dumps({"type": "result", "is_error": False})])).classification == "success"
    assert run_stream_command(_child([], linger=True), timeout_seconds=0.05).classification == "timeout"
    monkeypatch.setattr(live_common, "_MAX_STREAM_BYTES", 1)
    assert run_stream_command(_child([json.dumps({"type": "assistant", "text": "x"})])).classification == "stream_limit"
    assert not [thread for thread in threading.enumerate() if thread.name.startswith("subagent-live-pump-")]


def test_json_reader_threads_are_gone_after_normal_timeout_and_stderr_limit() -> None:
    assert run_json_command(_child([json.dumps({"ok": True})])) == {"ok": True}
    with pytest.raises(TimeoutError):
        run_json_command(_child([], linger=True), timeout_seconds=0.05)
    script = "import sys; sys.stderr.buffer.write(b'x' * (8 * 1024 * 1024 + 1)); sys.stderr.flush()"
    with pytest.raises(ValueError, match="stderr"):
        run_json_command([sys.executable, "-c", script])
    assert not [thread for thread in threading.enumerate() if thread.name.startswith("subagent-live-pump-")]


def test_permanently_blocking_readline_is_never_started_for_json_or_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    readline_called = threading.Event()
    real_popen = live_common.subprocess.Popen
    original_output_limit = live_common._MAX_COMMAND_OUTPUT_BYTES
    original_stream_limit = live_common._MAX_STREAM_BYTES

    class BlockingReadline:
        def __init__(self, stream: object) -> None:
            self._stream = stream

        def fileno(self) -> int:
            return self._stream.fileno()  # type: ignore[attr-defined]

        def close(self) -> None:
            self._stream.close()  # type: ignore[attr-defined]

        def readline(self, *_args: object, **_kwargs: object) -> bytes:
            readline_called.set()
            release.wait()
            return b""

    class WrappedProcess:
        def __init__(self, process: object) -> None:
            self._process = process
            self.stdout = BlockingReadline(process.stdout)  # type: ignore[attr-defined]
            self.stderr = BlockingReadline(process.stderr)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._process, name)

    def wrapped_popen(*args: object, **kwargs: object) -> WrappedProcess:
        return WrappedProcess(real_popen(*args, **kwargs))

    monkeypatch.setattr(live_common.subprocess, "Popen", wrapped_popen)
    try:
        assert run_json_command(
            _child([json.dumps({"ok": True})]), timeout_seconds=2,
        ) == {"ok": True}
        with pytest.raises(TimeoutError):
            run_json_command(_child([], linger=True), timeout_seconds=0.05)
        monkeypatch.setattr(live_common, "_MAX_COMMAND_OUTPUT_BYTES", 128)
        stderr_script = "import sys; sys.stderr.buffer.write(b'x' * 129); sys.stderr.flush()"
        with pytest.raises(ValueError, match="stderr"):
            run_json_command([sys.executable, "-c", stderr_script])

        monkeypatch.setattr(live_common, "_MAX_COMMAND_OUTPUT_BYTES", original_output_limit)
        assert run_stream_command(
            _child([_init(), json.dumps({"type": "result", "is_error": False})]),
            timeout_seconds=2,
        ).classification == "success"
        assert run_stream_command(_child([], linger=True), timeout_seconds=0.05).classification == "timeout"
        monkeypatch.setattr(live_common, "_MAX_STREAM_BYTES", 1)
        assert run_stream_command(
            _child([json.dumps({"type": "assistant", "text": "x"})])
        ).classification == "stream_limit"
        assert not readline_called.is_set()
    finally:
        release.set()
        monkeypatch.setattr(live_common, "_MAX_COMMAND_OUTPUT_BYTES", original_output_limit)
        monkeypatch.setattr(live_common, "_MAX_STREAM_BYTES", original_stream_limit)
        for thread in threading.enumerate():
            if thread.name.startswith("subagent-live-pump-"):
                thread.join(timeout=1)
    assert not [thread for thread in threading.enumerate() if thread.name.startswith("subagent-live-pump-")]


def test_stream_process_start_failure_is_classified_without_retry() -> None:
    result = run_stream_command(["not-a-real-subagent-harness-mcp-executable"])

    assert result.classification == "process_start_failed"
    assert result.exit_code is None


def test_stream_rejects_missing_result_after_terminal_error_and_nonzero_success_exit() -> None:
    missing_init = run_stream_command(_child([json.dumps({"type": "result", "is_error": True, "subtype": "internal_error"})]))
    assert missing_init.classification == "protocol_error"
    script = f"import sys; print({_init()!r}); print('{{\"type\":\"result\",\"is_error\":false}}'); sys.exit(7)"
    nonzero = run_stream_command([sys.executable, "-c", script])
    assert nonzero.classification == "process_error"


def test_stream_sanitizes_only_final_text_and_tracks_utf8_provenance() -> None:
    assistant_noise = json.dumps({"type": "assistant", "thinking": "ANTHROPIC_API_KEY=secret"})
    result = run_stream_command(
        _child([
            _init(), assistant_noise,
            json.dumps({"type": "result", "is_error": False, "result": "café ANTHROPIC_API_KEY=secret"}),
        ]),
        final_policy="sanitized_text",
    )

    assert result.classification == "success"
    assert result.sanitized_final_text == "café ANTHROPIC_API_KEY=[REDACTED]"
    assert "secret" not in json.dumps(result.__dict__)
    assert len(result.source_sha256) == 64
    assert result.stream_bytes > 0


def test_stream_enforces_cumulative_and_stderr_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_common, "_MAX_STREAM_BYTES", 128)
    oversized_stream = _child([json.dumps({"type": "assistant", "text": "x" * 80})] * 2)
    assert run_stream_command(oversized_stream).classification == "stream_limit"
    script = "import sys; sys.stderr.buffer.write(b'x' * (8 * 1024 * 1024 + 1)); sys.stderr.flush()"
    assert run_stream_command([sys.executable, "-c", script]).classification == "stderr_limit"


def test_sanitized_final_text_is_capped_at_256kib_utf8() -> None:
    script = (
        "import json; "
        f"print({_init()!r}); "
        "print(json.dumps({'type':'result','is_error':False,'result':'é' * 200000}), flush=True)"
    )
    result = run_stream_command([sys.executable, "-u", "-c", script], final_policy="sanitized_text")

    assert result.classification == "success"
    assert result.sanitized_final_text is not None
    assert len(result.sanitized_final_text.encode("utf-8")) <= 256 * 1024


def test_exact_final_marker_is_retained_as_boolean_only() -> None:
    result = run_stream_command(
        _child([_init(), json.dumps({"type": "result", "is_error": False, "result": "MATCH"})]),
        final_policy="exact_marker",
        final_marker="MATCH",
    )

    assert result.final_marker_matched is True
    assert result.sanitized_final_text is None


def test_json_command_validates_type() -> None:
    assert run_json_command(_child([json.dumps({"ok": True})])) == {"ok": True}
    with pytest.raises(ValueError, match="unexpected type"):
        run_json_command(_child([json.dumps(["not", "an", "object"])]))


def test_bound_identity_and_manifest_reject_replaced_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = repo / "runner.py"
    executable.write_text("first", encoding="utf-8")
    cli = BoundCliIdentity.capture(executable, version="1.0")
    manifest = BoundExecutableManifest.capture_project(
        repo, trust_revision=1, trusted_items=set(),
        expected_generated={"runner": executable}, generated_roots=(repo,),
    )

    assert cli.matches(executable) is True
    assert manifest.matches() is True
    executable.write_text("replaced", encoding="utf-8")
    assert cli.matches(executable) is False
    assert manifest.matches() is False


def test_manifest_closed_inventory_rejects_missing_new_and_untrusted_transitive_items(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    generated = repo / "runner.py"
    generated.write_text("safe", encoding="utf-8")
    with pytest.raises(PermissionError, match="inventory"):
        BoundExecutableManifest.capture_project(
            repo, trust_revision=1, trusted_items=set(),
            expected_generated={"runner": generated}, generated_roots=(),
        )
    extra = repo / "extra.py"
    extra.write_text("extra", encoding="utf-8")
    with pytest.raises(PermissionError, match="inventory"):
        BoundExecutableManifest.capture_project(
            repo, trust_revision=1, trusted_items=set(),
            expected_generated={"runner": generated}, generated_roots=(repo,),
        )
    extra.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("unsafe", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(f"@{outside}\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="untrusted"):
        BoundExecutableManifest.capture_project(
            repo, trust_revision=1, trusted_items=set(),
            expected_generated={"runner": generated}, generated_roots=(repo,),
        )


def test_manifest_lease_blocks_windows_write_replace_delete_until_release(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    generated = repo / "runner.py"
    generated.write_text("safe", encoding="utf-8")
    manifest = BoundExecutableManifest.capture_project(
        repo, trust_revision=1, trusted_items=set(),
        expected_generated={"runner": generated}, generated_roots=(repo,),
    )
    lease = manifest.lease()
    with lease:
        lease.verify_init_ack()
        if os.name == "nt":
            with pytest.raises(PermissionError):
                generated.write_text("changed", encoding="utf-8")
            replacement = repo / "replacement.py"
            replacement.write_text("replacement", encoding="utf-8")
            with pytest.raises(PermissionError):
                os.replace(replacement, generated)
            with pytest.raises(PermissionError):
                generated.unlink()
    assert lease._handles == []
    if os.name == "nt":
        generated.write_text("changed", encoding="utf-8")


def test_manifest_partial_open_failure_closes_every_prior_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = repo / "first.py"
    second = repo / "second.py"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    manifest = BoundExecutableManifest.capture_project(
        repo,
        trust_revision=1,
        trusted_items=set(),
        expected_generated={"first": first, "second": second},
        generated_roots=(repo,),
    )
    real_open = live_common._open_held_read_fd
    opened: list[int] = []

    def fail_second(path: Path) -> int:
        if opened:
            raise OSError("injected second manifest open failure")
        fd = real_open(path)
        opened.append(fd)
        return fd

    monkeypatch.setattr(live_common, "_open_held_read_fd", fail_second)
    with pytest.raises(OSError, match="injected"):
        with manifest.lease():
            pass

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_posix_fd_path_dispatch_is_linux_macos_or_explicitly_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_common, "_linux_path_from_fd", lambda fd: f"/linux/{fd}")
    monkeypatch.setattr(live_common, "_macos_path_from_fd", lambda fd: f"/macos/{fd}")

    assert live_common._canonical_posix_path_from_fd(7, platform="linux") == "/linux/7"
    assert live_common._canonical_posix_path_from_fd(8, platform="darwin") == "/macos/8"
    with pytest.raises(OSError, match="unsupported"):
        live_common._canonical_posix_path_from_fd(9, platform="freebsd13")


def test_live_root_is_ignored_and_checkpoint_write_set_rejects_it() -> None:
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo.as_posix()}", "check-ignore", "-q", ".phase0a/live/sentinel"],
        cwd=repo,
        check=False,
    )

    assert result.returncode == 0
    with pytest.raises(ValueError, match=".phase0a"):
        validate_checkpoint_write_set(["spikes/phase0a/live_common.py", ".phase0a/live/sentinel"])


@pytest.mark.parametrize("path", ["x/../.phase0a/a", "./.phase0a/a", ".PHASE0A/a", "../outside", "C:/rooted"])
def test_checkpoint_write_set_rejects_normalized_or_rooted_escapes(path: str) -> None:
    with pytest.raises(ValueError):
        validate_checkpoint_write_set([path])
