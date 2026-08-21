from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import selectors
import subprocess
import stat
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator, Mapping, Sequence

from .contracts import peek_top_level_type
from .core import redact_text
from .manifest import TrustKey, blocked_items, scan_project


_APPROVAL_ROOT = Path(".phase0a/live/approvals")
_MAX_APPROVAL_LIFETIME = timedelta(hours=2)
_MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_STREAM_BYTES = 64 * 1024 * 1024
_MAX_FINAL_TEXT_BYTES = 256 * 1024
_MAX_STREAM_OBJECTS = 128
_MAX_STREAM_NAME_BYTES = 256
_MAX_PUMP_EVENTS = 64
_MAX_POST_INIT_TIMEOUT_SECONDS = 600
_SIDE_EFFECT_LIMITS = {
    "provider_launch": "max_provider_session_launches",
    "provider_control_launch": "max_provider_session_launches",
    "worktree_create": "max_worktree_creates",
    "stop": "max_stop_respawn_actions",
    "respawn": "max_stop_respawn_actions",
    "attach": "max_attach_actions",
    "file_delete": "max_file_deletes",
    "remove": "max_removals",
}


@dataclass(frozen=True)
class RuntimeBinding:
    token: str
    state_key: str
    pattern: str
    require_group_owned: bool


@dataclass(frozen=True)
class SideEffectSpec:
    kind: str
    argv_template: tuple[str, ...]
    bindings: tuple[RuntimeBinding, ...]
    max_uses: int
    exact_targets: tuple[str, ...]


@dataclass(frozen=True)
class ApprovalScope:
    schema_version: int
    git_head: str
    cli_sha256: str
    gate_ids: tuple[str, ...]
    side_effects: tuple[SideEffectSpec, ...]
    max_provider_session_launches: int
    max_worktree_creates: int
    max_stop_respawn_actions: int
    max_attach_actions: int
    max_file_deletes: int
    max_removals: int
    background_internal_requests_acknowledged: bool
    executable_manifest_sha256: str
    trust_revision: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionObservations:
    git_head: str
    cli_sha256: str
    executable_manifest_sha256: str
    trust_revision: int
    dirty_tracked: bool


@dataclass(frozen=True)
class ExecutionAuthorization:
    scope: ApprovalScope
    receipt_path: Path
    approval_root: Path
    live_root: Path
    execution_id: str
    observations: ExecutionObservations
    live_root_identity: tuple[int, ...]
    approval_root_identity: tuple[int, ...]
    receipt_identity: tuple[int, ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _validate_scope(scope: ApprovalScope) -> None:
    if scope.schema_version != 1:
        raise ValueError("unsupported approval scope schema_version")
    if not isinstance(scope.trust_revision, int) or isinstance(scope.trust_revision, bool) or scope.trust_revision < 0:
        raise ValueError("trust revision must be a non-negative integer")
    if len(scope.git_head) != 40 or any(char not in "0123456789abcdef" for char in scope.git_head):
        raise ValueError("git_head must be a lowercase SHA-1")
    for name in ("cli_sha256", "executable_manifest_sha256"):
        value = getattr(scope, name)
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"{name} must be a lowercase SHA-256")
    if not scope.gate_ids or any(not gate.strip() for gate in scope.gate_ids):
        raise ValueError("gate_ids must be nonempty")
    if len(set(scope.gate_ids)) != len(scope.gate_ids):
        raise ValueError("gate_ids must be unique")
    counters = {name: getattr(scope, name) for name in _SIDE_EFFECT_LIMITS.values()}
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counters.values()):
        raise ValueError("side-effect counters must be non-negative")
    if len({effect.kind for effect in scope.side_effects}) != len(scope.side_effects):
        raise ValueError("approval scope permits one side-effect spec per kind")
    for effect in scope.side_effects:
        if effect.kind not in _SIDE_EFFECT_LIMITS:
            raise ValueError(f"unknown side-effect kind: {effect.kind}")
        if effect.max_uses < 1:
            raise ValueError("side-effect max_uses must be positive")
        if effect.max_uses > getattr(scope, _SIDE_EFFECT_LIMITS[effect.kind]):
            raise ValueError("side-effect max_uses exceeds its scope counter")
        if not effect.argv_template or any(not isinstance(part, str) for part in effect.argv_template):
            raise ValueError("side-effect argv_template must contain strings")
        empty_positions = [
            index for index, part in enumerate(effect.argv_template) if part == ""
        ]
        if empty_positions and not (
            effect.kind in {"provider_launch", "provider_control_launch"}
            and len(empty_positions) == 1
            and empty_positions[0] > 0
            and effect.argv_template[empty_positions[0] - 1] == "--tools"
        ):
            raise ValueError("empty argv is allowed only for a provider --tools pair")
        tokens = [binding.token for binding in effect.bindings]
        if len(set(tokens)) != len(tokens):
            raise ValueError("side-effect bindings must have unique placeholders")
        for binding in effect.bindings:
            if not binding.token.startswith("{") or not binding.token.endswith("}"):
                raise ValueError("runtime binding token must be a placeholder")
            if not binding.state_key.startswith("group."):
                raise ValueError("runtime binding must use this group's state")
            if not binding.require_group_owned:
                raise ValueError("runtime binding must require group ownership")
            if not binding.pattern.startswith("^") or not binding.pattern.endswith("$"):
                raise ValueError("runtime binding regex must be anchored")
            re.compile(binding.pattern)
            if sum(part.count(binding.token) for part in effect.argv_template) != 1:
                raise ValueError("runtime binding placeholder must appear exactly once")
    for counter in set(_SIDE_EFFECT_LIMITS.values()):
        if sum(
            effect.max_uses
            for effect in scope.side_effects
            if _SIDE_EFFECT_LIMITS[effect.kind] == counter
        ) > getattr(scope, counter):
            raise ValueError("side-effect uses exceed their aggregate scope counter")


def approval_digest(scope: ApprovalScope) -> str:
    _validate_scope(scope)
    return hashlib.sha256(_canonical_json(scope.to_dict()).encode("utf-8")).hexdigest()


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(handle):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def _windows_current_sid() -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise OSError(ctypes.get_last_error(), "GetTokenInformation size failed")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        user = ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents
        value = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(user.Sid, ctypes.byref(value)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return value.value
        finally:
            kernel32.LocalFree(value)
    finally:
        _windows_close_handle(token)


@contextmanager
def _windows_private_descriptor() -> Iterator[Any]:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    descriptor = ctypes.c_void_p()
    current_sid = _windows_current_sid()
    sddl = f"O:{current_sid}D:P(A;;FA;;;{current_sid})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None,
    ):
        raise OSError(ctypes.get_last_error(), "security descriptor conversion failed")
    try:
        yield descriptor
    finally:
        kernel32.LocalFree(descriptor)


def _set_private_windows_acl(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    security_information = 0x00000004 | 0x80000000
    try:
        _verify_current_owner_windows_path(path)
    except PermissionError:
        security_information |= 0x00000001
    with _windows_private_descriptor() as descriptor:
        if not advapi32.SetFileSecurityW(
            str(path), security_information, descriptor,
        ):
            raise OSError(ctypes.get_last_error(), "SetFileSecurityW failed", str(path))


def _verify_current_owner_windows_path(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x00000001,
        ctypes.byref(owner), None, None, None, ctypes.byref(descriptor),
    )
    if status:
        raise OSError(status, "GetNamedSecurityInfoW failed", str(path))
    current_sid = ctypes.c_void_p()
    try:
        if not advapi32.ConvertStringSidToSidW(_windows_current_sid(), ctypes.byref(current_sid)):
            raise OSError(ctypes.get_last_error(), "ConvertStringSidToSidW failed")
        if not owner.value or not advapi32.EqualSid(owner, current_sid):
            raise PermissionError("private storage owner is not the current user")
    finally:
        if current_sid.value:
            kernel32.LocalFree(current_sid)
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _verify_private_windows_path(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class ACL(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD), ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x00000001 | 0x00000004,
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if status:
        raise OSError(status, "GetNamedSecurityInfoW failed", str(path))
    current_sid = ctypes.c_void_p()
    try:
        if not advapi32.ConvertStringSidToSidW(_windows_current_sid(), ctypes.byref(current_sid)):
            raise OSError(ctypes.get_last_error(), "ConvertStringSidToSidW failed")
        if not owner.value or not advapi32.EqualSid(owner, current_sid):
            raise PermissionError("private storage owner is not the current user")
        if not dacl.value:
            raise PermissionError("private storage has a null DACL")
        acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        if acl.AceCount != 1:
            raise PermissionError("private storage grants access to another principal")
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
            raise OSError(ctypes.get_last_error(), "GetAce failed")
        header = ctypes.cast(ace_pointer, ctypes.POINTER(ACE_HEADER)).contents
        if header.AceType != 0 or header.AceSize < 12:
            raise PermissionError("private storage ACL is not owner-only")
        mask = ctypes.c_uint32.from_address(ace_pointer.value + 4).value
        ace_sid = ctypes.c_void_p(ace_pointer.value + 8)
        if not advapi32.EqualSid(ace_sid, current_sid) or mask & 0x001F01FF != 0x001F01FF:
            raise PermissionError("private storage ACL is not owner-only")
    finally:
        if current_sid.value:
            kernel32.LocalFree(current_sid)
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _verify_current_owner_path(path: Path) -> None:
    if os.name == "nt":
        _verify_current_owner_windows_path(path)
        return
    if path.stat(follow_symlinks=False).st_uid != os.getuid():
        raise PermissionError("private storage is not owned by the current user")


def _verify_private_path(path: Path, *, directory: bool) -> None:
    if not path.exists() or _is_reparse(path) or path.is_dir() != directory:
        raise PermissionError("private storage path is unavailable or indirect")
    if os.name == "nt":
        _verify_private_windows_path(path)
        return
    metadata = path.stat(follow_symlinks=False)
    _verify_current_owner_path(path)
    expected = 0o700 if directory else 0o600
    if stat.S_IMODE(metadata.st_mode) != expected:
        raise PermissionError("private storage permissions are not owner-only")


def _verify_private_posix_fd(fd: int, *, directory: bool) -> None:
    metadata = os.fstat(fd)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise PermissionError("private storage handle is not current-user owned")
    expected_mode = 0o700 if directory else 0o600
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PermissionError("private storage handle permissions are not owner-only")


def _require_precreated_private_directory(path: Path) -> Path:
    target = path.absolute()
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise PermissionError("security directory must be pre-created and private") from exc
    if resolved != target or _is_reparse(target):
        raise PermissionError("security directory must be direct and private")
    _verify_private_path(target, directory=True)
    return target


def _require_direct_directory(path: Path) -> Path:
    target = path.absolute()
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise PermissionError("security directory must be direct") from exc
    if resolved != target or _is_reparse(target) or not target.is_dir():
        raise PermissionError("security directory must be direct")
    return target


def _require_direct_current_owner_directory(path: Path) -> Path:
    target = _require_direct_directory(path)
    _verify_current_owner_path(target)
    return target


def _path_exists_or_is_indirect(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _create_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    if os.name == "nt":
        _require_direct_directory(path)
        _set_private_windows_acl(path)
    else:
        _require_direct_current_owner_directory(path)
        os.chmod(path, 0o700)
    _verify_private_path(path, directory=True)


def prepare_private_runtime_group_root(root: str | Path) -> Path:
    """Create or validate one fresh owner-only runtime group root."""
    target = Path(root).absolute()
    if _path_exists_or_is_indirect(target):
        target = _require_direct_directory(target)
        if any(target.iterdir()):
            raise FileExistsError(
                "runtime group materialization requires a fresh empty root",
            )
        _verify_private_path(target, directory=True)
        return target

    _require_direct_directory(target.parent)
    _create_private_directory(target)
    return target.resolve(strict=True)


def _restrict_private_directory(path: Path) -> None:
    _require_direct_current_owner_directory(path)
    if os.name == "nt":
        _set_private_windows_acl(path)
    else:
        os.chmod(path, 0o700)
    _verify_private_path(path, directory=True)


def _fixed_live_root(root: str | Path) -> Path:
    raw = os.fspath(root)
    if not isinstance(raw, str):
        raise PermissionError("approval storage root must be the fixed relative .phase0a/live path")
    windows = PureWindowsPath(raw)
    if Path(raw).is_absolute() or windows.is_absolute() or windows.drive:
        raise PermissionError("approval storage root must be the fixed relative .phase0a/live path")
    if tuple(raw.replace("\\", "/").split("/")) != (".phase0a", "live"):
        raise PermissionError("approval storage root must be the fixed relative .phase0a/live path")
    return (Path.cwd() / ".phase0a" / "live").absolute()


def prepare_approval_storage(
    root: str | Path,
    *,
    repair_existing: bool = False,
) -> dict[str, bool]:
    """Create or explicitly repair the fixed controller-owned approval directories."""
    if not isinstance(repair_existing, bool):
        raise ValueError("repair_existing must be a boolean")
    live_root = _fixed_live_root(root)
    phase_root = live_root.parent
    approval_root = live_root / "approvals"

    live_exists = _path_exists_or_is_indirect(live_root)
    if _path_exists_or_is_indirect(phase_root):
        if live_exists:
            _require_direct_directory(phase_root)
        else:
            _require_direct_current_owner_directory(phase_root)
    else:
        phase_root.mkdir(mode=0o700)
        _require_direct_current_owner_directory(phase_root)

    approval_exists = _path_exists_or_is_indirect(approval_root)
    live_private = False
    approvals_private = False
    if live_exists:
        _require_direct_current_owner_directory(live_root)
        try:
            _verify_private_path(live_root, directory=True)
            live_private = True
        except PermissionError:
            pass
    if approval_exists:
        _require_direct_current_owner_directory(approval_root)
        try:
            _verify_private_path(approval_root, directory=True)
            approvals_private = True
        except PermissionError:
            pass

    insecure_existing = (live_exists and not live_private) or (
        approval_exists and not approvals_private
    )
    if insecure_existing and not repair_existing:
        raise PermissionError("existing approval storage is not private; explicit repair is required")
    if approval_exists and not approvals_private and any(approval_root.iterdir()):
        raise PermissionError("cannot repair nonempty insecure approvals directory")

    if not live_exists:
        _create_private_directory(live_root)
    elif not live_private:
        _restrict_private_directory(live_root)
    if not approval_exists:
        _create_private_directory(approval_root)
    elif not approvals_private:
        _restrict_private_directory(approval_root)

    _verify_private_path(live_root, directory=True)
    _verify_private_path(approval_root, directory=True)
    return {
        "live_root_created": not live_exists,
        "approvals_created": not approval_exists,
        "repaired_existing": insecure_existing,
    }


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short private-storage write")
        view = view[written:]
    os.fsync(fd)


def _windows_create_private_file(path: Path, *, disposition: int) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
        wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    with _windows_private_descriptor() as descriptor:
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
        handle = create_file(
            str(path), 0x80000000 | 0x40000000, 0, ctypes.byref(attributes),
            disposition, 0x00000080 | 0x00200000, None,
        )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, "private file already exists", str(path))
        raise OSError(error, "CreateFileW failed", str(path))
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except BaseException:
        _windows_close_handle(handle)
        raise


def _write_private_json(path: Path, payload: Any, *, exclusive: bool = False) -> None:
    path = path.absolute()
    parent = _require_precreated_private_directory(path.parent)
    data = _json_bytes(payload)
    if os.name == "nt":
        parent_handle = _windows_open_handle(
            parent,
            desired_access=0x80000000 | 0x40000000,
            share_mode=0x00000001 | 0x00000002,
            directory=True,
        )
        try:
            if (
                _windows_handle_is_reparse(parent_handle)
                or Path(_canonical_windows_path_from_handle(parent_handle)) != parent
            ):
                raise PermissionError("private JSON parent was substituted")
            _verify_private_path(parent, directory=True)
            if exclusive:
                fd = _windows_create_private_file(path, disposition=1)
                try:
                    if _canonical_path_from_fd(fd) != str(path):
                        raise PermissionError("private JSON file was substituted")
                    _write_all(fd, data)
                    if _canonical_path_from_fd(fd) != str(path):
                        raise PermissionError("private JSON file was substituted")
                finally:
                    os.close(fd)
                _verify_private_path(path, directory=False)
                _flush_windows_handle(parent_handle)
                return

            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
            temporary = Path(temporary_name)
            try:
                _set_private_windows_acl(temporary)
                _write_all(fd, data)
                os.close(fd)
                fd = -1
                _verify_private_path(temporary, directory=False)
                os.replace(temporary, path)
                _verify_private_path(path, directory=False)
                _flush_windows_handle(parent_handle)
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        finally:
            _windows_close_handle(parent_handle)
        return

    parent_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _verify_private_posix_fd(parent_fd, directory=True)
        if _canonical_posix_path_from_fd(parent_fd) != str(parent):
            raise PermissionError("private JSON parent was substituted")
        if exclusive:
            fd = os.open(
                path.name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, data)
                if _canonical_posix_path_from_fd(fd) != str(path):
                    raise PermissionError("private JSON file was substituted")
            finally:
                os.close(fd)
            if _canonical_posix_path_from_fd(parent_fd) != str(parent):
                raise PermissionError("private JSON parent was substituted")
            os.fsync(parent_fd)
            return

        temporary_name = f".{path.name}.{os.urandom(16).hex()}"
        fd = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            if _canonical_posix_path_from_fd(fd) != str(parent / temporary_name):
                raise PermissionError("private JSON temporary was substituted")
        finally:
            os.close(fd)
        try:
            if _canonical_posix_path_from_fd(parent_fd) != str(parent):
                raise PermissionError("private JSON parent was substituted")
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            if _canonical_posix_path_from_fd(parent_fd) != str(parent):
                raise PermissionError("private JSON parent was substituted")
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent_fd)


def _read_json_value_fd(fd: int, label: str) -> Any:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > 1024 * 1024:
            raise PermissionError(f"{label} exceeds its size bound")
        chunks.append(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermissionError(f"{label} is invalid or torn") from exc
    return payload


def _read_json_fd(fd: int, label: str) -> dict[str, Any]:
    payload = _read_json_value_fd(fd, label)
    if not isinstance(payload, dict):
        raise PermissionError(f"{label} must be an object")
    return payload


def _replace_json_fd(fd: int, payload: Any) -> None:
    data = _json_bytes(payload)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    _write_all(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)


def _stable_fd_identity(fd: int) -> tuple[int, ...]:
    metadata = os.fstat(fd)
    return (metadata.st_dev, metadata.st_ino)


def _lock_posix_receipt(fd: int, *, exclusive: bool, timeout_seconds: float) -> None:
    import fcntl

    operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(fd, operation)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("approval receipt lock timeout") from exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _windows_open_handle(
    path: Path, *, desired_access: int, share_mode: int, directory: bool,
    timeout_seconds: float = 5,
) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flags = 0x00200000 | (0x02000000 if directory else 0x00000080)
    deadline = time.monotonic() + timeout_seconds
    while True:
        handle = create_file(str(path), desired_access, share_mode, None, 3, flags, None)
        if handle != wintypes.HANDLE(-1).value:
            return int(handle)
        error = ctypes.get_last_error()
        if error not in {32, 33} or time.monotonic() >= deadline:
            raise PermissionError(error, "approval path cannot be opened safely", str(path))
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _windows_handle_is_reparse(handle: int) -> bool:
    return bool(_windows_handle_information(handle).dwFileAttributes & 0x00000400)


def _windows_handle_information(handle: int) -> Any:
    import ctypes
    from ctypes import wintypes

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD),
        ]

    info = BY_HANDLE_FILE_INFORMATION()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
    return info


def _canonical_windows_path_from_handle(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final.restype = wintypes.DWORD
    size = get_final(handle, None, 0, 0)
    if not size:
        raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
    buffer = ctypes.create_unicode_buffer(size + 1)
    if not get_final(handle, buffer, len(buffer), 0):
        raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return str(Path(value).resolve(strict=True))


def _windows_handle_identity(handle: int) -> tuple[int, ...]:
    info = _windows_handle_information(handle)
    return (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow)


@dataclass
class _OpenedApprovalReceipt:
    live_root_path: Path
    root_path: Path
    receipt_path: Path
    live_root_identity: tuple[int, ...]
    root_identity: tuple[int, ...]
    receipt_identity: tuple[int, ...]
    receipt_fd: int
    live_root_fd: int | None = None
    root_fd: int | None = None
    live_root_handle: int | None = None
    root_handle: int | None = None

    @property
    def claim_name(self) -> str:
        return self.receipt_path.name + ".claim"

    def read_payload(self) -> dict[str, Any]:
        self._verify_paths()
        payload = _read_json_fd(self.receipt_fd, "approval receipt")
        self._verify_paths()
        return payload

    def write_payload(self, payload: Any) -> None:
        self._verify_paths()
        _replace_json_fd(self.receipt_fd, payload)
        self._verify_paths()

    def _verify_paths(self) -> None:
        if os.name == "nt":
            import msvcrt

            receipt_handle = msvcrt.get_osfhandle(self.receipt_fd)
            if (
                _windows_handle_is_reparse(self.live_root_handle or 0)
                or Path(_canonical_windows_path_from_handle(self.live_root_handle or 0)) != self.live_root_path
                or _windows_handle_identity(self.live_root_handle or 0) != self.live_root_identity
                or Path(_canonical_windows_path_from_handle(self.root_handle or 0)).parent != self.live_root_path
                or _windows_handle_is_reparse(self.root_handle or 0)
                or _windows_handle_is_reparse(receipt_handle)
                or Path(_canonical_windows_path_from_handle(receipt_handle)).parent != self.root_path
                or _windows_handle_identity(self.root_handle or 0) != self.root_identity
                or _windows_handle_identity(receipt_handle) != self.receipt_identity
            ):
                raise PermissionError("approval root or receipt was substituted")
            return
        if self.live_root_fd is None or self.root_fd is None:
            raise PermissionError("approval root handle is unavailable")
        if (
            _canonical_posix_path_from_fd(self.live_root_fd) != str(self.live_root_path)
            or _stable_fd_identity(self.live_root_fd) != self.live_root_identity
            or Path(_canonical_posix_path_from_fd(self.root_fd)).parent != self.live_root_path
            or _canonical_posix_path_from_fd(self.root_fd) != str(self.root_path)
            or Path(_canonical_posix_path_from_fd(self.receipt_fd)).parent != self.root_path
            or _stable_fd_identity(self.root_fd) != self.root_identity
            or _stable_fd_identity(self.receipt_fd) != self.receipt_identity
        ):
            raise PermissionError("approval root or receipt was substituted")

    def _open_claim(self) -> int | None:
        if os.name == "nt":
            claim_path = self.root_path / self.claim_name
            try:
                handle = _windows_open_handle(
                    claim_path, desired_access=0x80000000, share_mode=0x00000001,
                    directory=False,
                )
            except PermissionError as exc:
                if exc.errno in {2, 3}:
                    return None
                raise
            import msvcrt

            try:
                if _windows_handle_is_reparse(handle) or Path(_canonical_windows_path_from_handle(handle)).parent != self.root_path:
                    raise PermissionError("approval claim marker was substituted")
                _verify_private_path(claim_path, directory=False)
                return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            except BaseException:
                _windows_close_handle(handle)
                raise
        assert self.root_fd is not None
        try:
            fd = os.open(
                self.claim_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self.root_fd,
            )
        except FileNotFoundError:
            return None
        try:
            _verify_private_posix_fd(fd, directory=False)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read_claim(self) -> dict[str, Any] | None:
        self._verify_paths()
        fd = self._open_claim()
        if fd is None:
            return None
        try:
            payload = _read_json_fd(fd, "approval claim marker")
            self._verify_paths()
            return payload
        finally:
            os.close(fd)

    def create_claim(self, payload: dict[str, Any]) -> None:
        data = _json_bytes(payload)
        claim_path = self.root_path / self.claim_name
        try:
            if os.name == "nt":
                fd = _windows_create_private_file(claim_path, disposition=1)
            else:
                assert self.root_fd is not None
                fd = os.open(
                    self.claim_name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=self.root_fd,
                )
        except FileExistsError as exc:
            raise PermissionError("approval receipt already has a claim marker") from exc
        try:
            if os.name == "nt":
                if _canonical_path_from_fd(fd) != str(claim_path):
                    raise PermissionError("approval claim marker was substituted")
                _verify_private_path(claim_path, directory=False)
            else:
                os.fchmod(fd, 0o600)
                _verify_private_posix_fd(fd, directory=False)
            _write_all(fd, data)
            if _canonical_path_from_fd(fd) != str(claim_path):
                raise PermissionError("approval claim marker was substituted")
            self._verify_paths()
            _durable_claim_barrier(self)
        finally:
            os.close(fd)


def _flush_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    if not kernel32.FlushFileBuffers(handle):
        raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")


def _durable_claim_barrier(opened: _OpenedApprovalReceipt) -> None:
    if os.name == "nt":
        if opened.root_handle is None:
            raise OSError("approval root handle is unavailable")
        _flush_windows_handle(opened.root_handle)
        return
    if opened.root_fd is None:
        raise OSError("approval root descriptor is unavailable")
    os.fsync(opened.root_fd)


@contextmanager
def _open_approval_receipt(
    receipt_path: str | Path,
    approval_root: str | Path,
    *,
    exclusive: bool,
) -> Iterator[_OpenedApprovalReceipt]:
    receipt = Path(receipt_path).absolute()
    root_argument = Path(approval_root).absolute()
    if (
        root_argument.name != "approvals"
        or receipt.parent != root_argument
        or not receipt.name
        or receipt.name in {".", ".."}
    ):
        raise PermissionError("receipt must be a direct child of the bound approval root")
    try:
        live_root = root_argument.parent.resolve(strict=True)
        root = root_argument.resolve(strict=True)
    except OSError as exc:
        raise PermissionError("bound approval root is unavailable") from exc
    if live_root != root_argument.parent or root != root_argument or root.parent != live_root:
        raise PermissionError("bound live or approval root is indirect")
    _verify_private_path(live_root, directory=True)
    _verify_private_path(root, directory=True)

    if os.name == "nt":
        import msvcrt

        live_root_handle = _windows_open_handle(
            live_root,
            desired_access=0x80000000,
            share_mode=0x00000001 | 0x00000002,
            directory=True,
        )
        root_handle: int | None = None
        receipt_fd = -1
        try:
            if (
                _windows_handle_is_reparse(live_root_handle)
                or Path(_canonical_windows_path_from_handle(live_root_handle)) != live_root
            ):
                raise PermissionError("bound live root was substituted")
            _verify_private_path(live_root, directory=True)
            root_handle = _windows_open_handle(
                root,
                desired_access=0x80000000 | (0x40000000 if exclusive else 0),
                share_mode=0x00000001 | 0x00000002,
                directory=True,
            )
            if _windows_handle_is_reparse(root_handle):
                raise PermissionError("bound approval root is a reparse point")
            root_from_handle = Path(_canonical_windows_path_from_handle(root_handle))
            if root_from_handle != root or root_from_handle.parent != live_root:
                raise PermissionError("bound approval root was substituted")
            _verify_private_path(root, directory=True)
            receipt_handle = _windows_open_handle(
                root / receipt.name,
                desired_access=0x80000000 | (0x40000000 if exclusive else 0),
                share_mode=0 if exclusive else 0x00000001,
                directory=False,
            )
            try:
                if _windows_handle_is_reparse(receipt_handle):
                    raise PermissionError("approval receipt is a reparse point")
                if Path(_canonical_windows_path_from_handle(receipt_handle)).parent != root:
                    raise PermissionError("approval receipt escaped the bound root")
                receipt_fd = msvcrt.open_osfhandle(
                    receipt_handle,
                    (os.O_RDWR if exclusive else os.O_RDONLY) | getattr(os, "O_BINARY", 0),
                )
            except BaseException:
                _windows_close_handle(receipt_handle)
                raise
            _verify_private_path(root / receipt.name, directory=False)
            opened = _OpenedApprovalReceipt(
                live_root_path=live_root,
                root_path=root,
                receipt_path=root / receipt.name,
                live_root_identity=_windows_handle_identity(live_root_handle),
                root_identity=_windows_handle_identity(root_handle),
                receipt_identity=_windows_handle_identity(receipt_handle),
                receipt_fd=receipt_fd,
                live_root_handle=live_root_handle,
                root_handle=root_handle,
            )
            opened._verify_paths()
            yield opened
        finally:
            if receipt_fd >= 0:
                os.close(receipt_fd)
            if root_handle is not None:
                _windows_close_handle(root_handle)
            _windows_close_handle(live_root_handle)
        return

    live_root_fd = os.open(
        live_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    root_fd = -1
    receipt_fd = -1
    try:
        _verify_private_posix_fd(live_root_fd, directory=True)
        if _canonical_posix_path_from_fd(live_root_fd) != str(live_root):
            raise PermissionError("bound live root was substituted")
        root_fd = os.open(
            root.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=live_root_fd,
        )
        _verify_private_posix_fd(root_fd, directory=True)
        if _canonical_posix_path_from_fd(root_fd) != str(root):
            raise PermissionError("bound approval root was substituted")
        receipt_fd = os.open(
            receipt.name,
            (os.O_RDWR if exclusive else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        _lock_posix_receipt(receipt_fd, exclusive=exclusive, timeout_seconds=5)
        if Path(_canonical_posix_path_from_fd(receipt_fd)).parent != root:
            raise PermissionError("approval receipt escaped the bound root")
        _verify_private_path(root / receipt.name, directory=False)
        opened = _OpenedApprovalReceipt(
            live_root_path=live_root,
            root_path=root,
            receipt_path=root / receipt.name,
            live_root_identity=_stable_fd_identity(live_root_fd),
            root_identity=_stable_fd_identity(root_fd),
            receipt_identity=_stable_fd_identity(receipt_fd),
            receipt_fd=receipt_fd,
            live_root_fd=live_root_fd,
            root_fd=root_fd,
        )
        opened._verify_paths()
        yield opened
    finally:
        if receipt_fd >= 0:
            os.close(receipt_fd)
        if root_fd >= 0:
            os.close(root_fd)
        os.close(live_root_fd)


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PermissionError(f"receipt {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PermissionError(f"receipt {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise PermissionError(f"receipt {label} lacks timezone")
    return parsed.astimezone(timezone.utc)


def require_one_shot_approval(
    scope: ApprovalScope,
    receipt_path: str | Path,
    *,
    approval_root: str | Path,
    observations: ExecutionObservations | None = None,
    now: datetime | None = None,
) -> ApprovalScope:
    expected_digest = approval_digest(scope)
    if observations is None:
        raise PermissionError("execute observations are required")
    if not isinstance(observations.dirty_tracked, bool):
        raise PermissionError("dirty-tree observation is required")
    with _open_approval_receipt(receipt_path, approval_root, exclusive=False) as opened:
        payload = opened.read_payload()
        claimed = opened.read_claim()
        _validate_receipt_payload(
            payload, expected_digest=expected_digest, observations=observations,
            scope=scope, now=now, claimed=claimed,
        )
    return scope


def _validate_receipt_payload(
    payload: dict[str, Any], *, expected_digest: str,
    observations: ExecutionObservations, scope: ApprovalScope,
    now: datetime | None, claimed: dict[str, Any] | None,
) -> None:
    allowed = {"scope_sha256", "approved_at", "expires_at", "consumed_at", "claimed_execution_id"}
    if set(payload) != allowed:
        raise PermissionError("approval receipt contains unsupported identity or fields")
    if payload.get("scope_sha256") != expected_digest:
        raise PermissionError("approval receipt digest mismatch")
    if claimed is not None or payload.get("consumed_at") is not None or payload.get("claimed_execution_id") is not None:
        raise PermissionError("approval receipt is consumed")
    approved_at = _parse_time(payload.get("approved_at"), "approved_at")
    expires_at = _parse_time(payload.get("expires_at"), "expires_at")
    if expires_at < approved_at or expires_at - approved_at > _MAX_APPROVAL_LIFETIME:
        raise PermissionError("approval receipt expiry is invalid")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if approved_at > current:
        raise PermissionError("approval receipt approved_at is in the future")
    if current > expires_at:
        raise PermissionError("approval receipt is expired")
    if observations.dirty_tracked:
        raise PermissionError("tracked tree is dirty")
    for actual, expected, label in (
        (observations.git_head, scope.git_head, "HEAD"),
        (observations.cli_sha256, scope.cli_sha256, "CLI identity"),
        (observations.executable_manifest_sha256, scope.executable_manifest_sha256, "executable manifest"),
        (observations.trust_revision, scope.trust_revision, "trust revision"),
    ):
        if actual != expected:
            raise PermissionError(f"approval receipt {label} mismatch")


def _receipt_payload(scope: ApprovalScope, approved_at: datetime, expires_at: datetime, consumed_at: datetime | None, claimed_execution_id: str | None = None) -> dict[str, object]:
    return {
        "scope_sha256": approval_digest(scope),
        "approved_at": approved_at.astimezone(timezone.utc).isoformat(),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "consumed_at": None if consumed_at is None else consumed_at.astimezone(timezone.utc).isoformat(),
        "claimed_execution_id": claimed_execution_id,
    }


def claim_execution_authorization(
    scope: ApprovalScope,
    receipt_path: str | Path,
    *,
    approval_root: str | Path,
    observations: ExecutionObservations | None,
    execution_id: str,
    now: datetime | None = None,
) -> ExecutionAuthorization:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id):
        raise PermissionError("execution_id is invalid")
    if observations is None:
        raise PermissionError("execute observations are required")
    expected_digest = approval_digest(scope)
    with _open_approval_receipt(receipt_path, approval_root, exclusive=True) as opened:
        payload = opened.read_payload()
        claimed = opened.read_claim()
        _validate_receipt_payload(
            payload, expected_digest=expected_digest, observations=observations,
            scope=scope, now=now, claimed=claimed,
        )
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        opened.create_claim({
            "execution_id": execution_id,
            "scope_sha256": expected_digest,
            "claimed_at": current.isoformat(),
        })
        payload["claimed_execution_id"] = execution_id
        payload["consumed_at"] = current.isoformat()
        opened.write_payload(payload)
        return ExecutionAuthorization(
            scope=scope,
            receipt_path=opened.receipt_path,
            approval_root=opened.root_path,
            live_root=opened.live_root_path,
            execution_id=execution_id,
            observations=observations,
            live_root_identity=opened.live_root_identity,
            approval_root_identity=opened.root_identity,
            receipt_identity=opened.receipt_identity,
        )


def _state_value(state: Mapping[str, Any], state_key: str) -> str:
    current: Any = state
    for part in state_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise PermissionError(f"missing approved runtime state: {state_key}")
        current = current[part]
    if not isinstance(current, str):
        raise PermissionError(f"runtime state must be a string: {state_key}")
    return current


@dataclass
class _PinnedPrivateDirectory:
    path: Path
    identity: tuple[int, ...]
    fd: int | None = None
    handle: int | None = None

    def flush_entry(self) -> None:
        if os.name == "nt":
            if self.handle is None:
                raise OSError("private directory handle is unavailable")
            _flush_windows_handle(self.handle)
            return
        if self.fd is None:
            raise OSError("private directory descriptor is unavailable")
        os.fsync(self.fd)


def _ledger_location(
    authorization: ExecutionAuthorization,
    ledger_path: str | Path,
) -> tuple[Path, tuple[str, ...]]:
    raw = Path(ledger_path)
    raw_parts = raw.parts + PureWindowsPath(str(ledger_path)).parts
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise PermissionError("ledger path must not contain traversal")
    candidate = (raw if raw.is_absolute() else Path.cwd() / raw).absolute()
    try:
        relative = candidate.relative_to(authorization.live_root)
    except (ValueError, OSError) as exc:
        raise PermissionError("ledger path escaped the authorized live root") from exc
    if not relative.parts or relative.name in {"", ".", ".."}:
        raise PermissionError("ledger path is invalid")
    return candidate, tuple(relative.parts[:-1])


@contextmanager
def _open_private_ledger_parent(
    authorization: ExecutionAuthorization,
    relative_parts: Sequence[str],
) -> Iterator[_PinnedPrivateDirectory]:
    if any(not part or part in {".", ".."} for part in relative_parts):
        raise PermissionError("ledger parent contains traversal")
    expected_path = authorization.live_root
    if os.name == "nt":
        handles: list[int] = []
        try:
            for index, part in enumerate((None, *relative_parts)):
                if part is not None:
                    expected_path = expected_path / part
                handle = _windows_open_handle(
                    expected_path,
                    desired_access=0x80000000 | (0x40000000 if index == len(relative_parts) else 0),
                    share_mode=0x00000001 | 0x00000002,
                    directory=True,
                )
                handles.append(handle)
                if (
                    _windows_handle_is_reparse(handle)
                    or Path(_canonical_windows_path_from_handle(handle)) != expected_path
                ):
                    raise PermissionError("ledger parent was substituted or is a reparse point")
                _verify_private_path(expected_path, directory=True)
                identity = _windows_handle_identity(handle)
                if index == 0 and identity != authorization.live_root_identity:
                    raise PermissionError("authorized live-root identity drifted")
            yield _PinnedPrivateDirectory(expected_path, identity, handle=handles[-1])
        except (OSError, PermissionError) as exc:
            if isinstance(exc, PermissionError) and exc.errno not in {2, 3}:
                raise
            raise PermissionError("ledger parent must be pre-created and private") from exc
        finally:
            while handles:
                _windows_close_handle(handles.pop())
        return

    descriptors: list[int] = []
    try:
        fd = os.open(
            expected_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(fd)
        _verify_private_posix_fd(fd, directory=True)
        if (
            _canonical_posix_path_from_fd(fd) != str(expected_path)
            or _stable_fd_identity(fd) != authorization.live_root_identity
        ):
            raise PermissionError("authorized live-root identity drifted")
        for part in relative_parts:
            expected_path = expected_path / part
            fd = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptors[-1],
            )
            descriptors.append(fd)
            _verify_private_posix_fd(fd, directory=True)
            if _canonical_posix_path_from_fd(fd) != str(expected_path):
                raise PermissionError("ledger parent was substituted")
        yield _PinnedPrivateDirectory(expected_path, _stable_fd_identity(descriptors[-1]), fd=descriptors[-1])
    except (OSError, PermissionError) as exc:
        if isinstance(exc, PermissionError):
            raise
        raise PermissionError("ledger parent must be pre-created and private") from exc
    finally:
        while descriptors:
            os.close(descriptors.pop())


@contextmanager
def _open_consumed_ledger(
    parent: _PinnedPrivateDirectory,
    ledger_path: Path,
) -> Iterator[tuple[int, bool, tuple[int, ...]]]:
    created = False
    fd = -1
    if os.name == "nt":
        import msvcrt

        try:
            handle = _windows_open_handle(
                ledger_path,
                desired_access=0x80000000 | 0x40000000,
                share_mode=0,
                directory=False,
            )
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
            except BaseException:
                _windows_close_handle(handle)
                raise
        except PermissionError as exc:
            if exc.errno not in {2, 3}:
                raise
            fd = _windows_create_private_file(ledger_path, disposition=1)
            created = True
    else:
        if parent.fd is None:
            raise PermissionError("ledger parent descriptor is unavailable")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(ledger_path.name, flags, dir_fd=parent.fd)
        except FileNotFoundError:
            fd = os.open(
                ledger_path.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent.fd,
            )
            os.fchmod(fd, 0o600)
            created = True
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        if os.name == "nt":
            import msvcrt

            handle = msvcrt.get_osfhandle(fd)
            if (
                _windows_handle_is_reparse(handle)
                or Path(_canonical_windows_path_from_handle(handle)) != ledger_path
            ):
                raise PermissionError("ledger file was substituted")
            _verify_private_path(ledger_path, directory=False)
        else:
            _verify_private_posix_fd(fd, directory=False)
            if _canonical_posix_path_from_fd(fd) != str(ledger_path):
                raise PermissionError("ledger file was substituted")
        identity = _stable_fd_identity(fd)
        yield fd, created, identity
    finally:
        if fd >= 0:
            os.close(fd)


def consume_side_effect(
    authorization: ExecutionAuthorization,
    kind: str,
    state: Mapping[str, Any],
    ledger_path: str | Path,
    *,
    now: datetime | None = None,
    invoke: Callable[[tuple[str, ...]], Any] | None = None,
) -> Any:
    scope = authorization.scope
    effects = [effect for effect in scope.side_effects if effect.kind == kind]
    if len(effects) != 1:
        raise PermissionError("approval scope does not declare exactly one requested side effect")
    effect = effects[0]
    ledger, parent_parts = _ledger_location(authorization, ledger_path)
    with _open_approval_receipt(
        authorization.receipt_path, authorization.approval_root, exclusive=True,
    ) as opened:
        if (
            opened.live_root_identity != authorization.live_root_identity
            or opened.root_identity != authorization.approval_root_identity
            or opened.receipt_identity != authorization.receipt_identity
        ):
            raise PermissionError("execution authorization storage identity changed")
        payload = opened.read_payload()
        claim = opened.read_claim()
        if (
            payload.get("scope_sha256") != approval_digest(scope)
            or payload.get("claimed_execution_id") != authorization.execution_id
            or payload.get("consumed_at") is None
            or claim is None
            or claim.get("execution_id") != authorization.execution_id
            or claim.get("scope_sha256") != approval_digest(scope)
        ):
            raise PermissionError("execution authorization is no longer valid")
        with _open_private_ledger_parent(authorization, parent_parts) as parent:
            if parent.path != ledger.parent:
                raise PermissionError("ledger parent escaped the authorized live root")
            with _open_consumed_ledger(parent, ledger) as (ledger_fd, created, ledger_identity):
                loaded = [] if created else _read_json_value_fd(ledger_fd, "consumed-side-effect ledger")
                if not isinstance(loaded, list) or any(not isinstance(item, dict) for item in loaded):
                    raise PermissionError("consumed-side-effect ledger is invalid")
                if sum(item.get("kind") == kind for item in loaded) >= effect.max_uses:
                    raise PermissionError("approved side-effect uses are exhausted")
                argv = list(effect.argv_template)
                for binding in effect.bindings:
                    value = _state_value(state, binding.state_key)
                    if not binding.require_group_owned or re.fullmatch(binding.pattern, value) is None:
                        raise PermissionError("runtime substitution is not approved")
                    argv = [part.replace(binding.token, value) for part in argv]
                if any("{" in part or "}" in part for part in argv):
                    raise PermissionError("side-effect argv has unresolved placeholders")
                loaded.append({"kind": kind, "argv": argv, "targets": list(effect.exact_targets)})
                _replace_json_fd(ledger_fd, loaded)
                if _stable_fd_identity(ledger_fd) != ledger_identity:
                    raise PermissionError("consumed-side-effect ledger identity drifted")
                if os.name == "nt":
                    import msvcrt

                    ledger_handle = msvcrt.get_osfhandle(ledger_fd)
                    current_ledger_path = Path(_canonical_windows_path_from_handle(ledger_handle))
                else:
                    current_ledger_path = Path(_canonical_posix_path_from_fd(ledger_fd))
                if current_ledger_path != ledger:
                    raise PermissionError("consumed-side-effect ledger was substituted")
                if created:
                    parent.flush_entry()
                concrete_argv = tuple(argv)
                return None if invoke is None else invoke(concrete_argv)

@dataclass(frozen=True)
class BoundCliIdentity:
    canonical_path: str
    sha256: str
    file_identity: tuple[int, ...]
    version: str

    @classmethod
    def capture(cls, path: str | Path, *, version: str) -> "BoundCliIdentity":
        target = Path(path).resolve(strict=True)
        return cls(str(target), _sha256_file(target), _file_identity(target), version)

    def matches(self, path: str | Path) -> bool:
        try:
            target = Path(path).resolve(strict=True)
        except OSError:
            return False
        return (
            str(target) == self.canonical_path
            and _file_identity(target) == self.file_identity
            and _sha256_file(target) == self.sha256
        )


@dataclass(frozen=True)
class BoundExecutableFile:
    canonical_path: str
    sha256: str
    file_identity: tuple[int, ...]


@dataclass(frozen=True)
class BoundExecutableManifest:
    repository_id: str
    trust_revision: int
    entries: tuple[BoundExecutableFile, ...]

    @classmethod
    def capture_project(
        cls,
        project_root: str | Path,
        *,
        trust_revision: int,
        trusted_items: set[TrustKey],
        expected_generated: Mapping[str, str | Path] = {},
        generated_roots: Sequence[str | Path] = (),
    ) -> "BoundExecutableManifest":
        root = Path(project_root).resolve(strict=True)
        manifest = scan_project(root)
        if blocked_items(manifest, trusted_items=trusted_items, trust_revision=trust_revision):
            raise PermissionError("project executable manifest contains untrusted items")
        expected = {key: str(Path(value).resolve(strict=True)) for key, value in expected_generated.items()}
        observed: set[str] = set()
        for raw_root in generated_roots:
            supplied_root = Path(raw_root)
            if _is_reparse(supplied_root):
                raise PermissionError("generated root is a symlink or reparse point")
            generated_root = supplied_root.resolve(strict=True)
            if not generated_root.is_dir() or _is_reparse(generated_root):
                raise PermissionError("generated root is not a regular directory")
            for candidate in generated_root.rglob("*"):
                if _is_reparse(candidate):
                    raise PermissionError("generated inventory contains a symlink or reparse point")
                if candidate.is_file():
                    observed.add(str(candidate.resolve(strict=True)))
        if set(expected.values()) != observed:
            raise PermissionError("generated executable inventory drifted")
        paths: dict[str, Path] = {}
        for category in ("settings", "hook_targets", "instruction_files", "external_imports"):
            for item in manifest.get(category, []):
                if not item.get("exists"):
                    raise PermissionError("project executable manifest has a missing item")
                paths[str(item["canonical_path"])] = Path(str(item["canonical_path"]))
        for path in expected.values():
            paths[path] = Path(path)
        entries = tuple(sorted((
            BoundExecutableFile(
                canonical_path=str(path.resolve(strict=True)),
                sha256=_sha256_file(path),
                file_identity=_file_identity(path),
            )
            for path in paths.values()
        ), key=lambda entry: entry.canonical_path))
        if not entries or len({entry.canonical_path for entry in entries}) != len(entries):
            raise ValueError("executable manifest paths must be unique and nonempty")
        return cls(manifest["repository_id"], trust_revision, entries)

    def matches(self) -> bool:
        for entry in self.entries:
            try:
                path = Path(entry.canonical_path).resolve(strict=True)
            except OSError:
                return False
            if (
                str(path) != entry.canonical_path
                or _file_identity(path) != entry.file_identity
                or _sha256_file(path) != entry.sha256
            ):
                return False
        return True

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json(asdict(self)).encode("utf-8")).hexdigest()

    def lease(self) -> "ManifestLease":
        return ManifestLease(self)


class ManifestLease:
    def __init__(self, manifest: BoundExecutableManifest) -> None:
        self._manifest = manifest
        self._handles: list[tuple[BoundExecutableFile, int]] = []

    def __enter__(self) -> "ManifestLease":
        try:
            for entry in self._manifest.entries:
                fd = _open_held_read_fd(Path(entry.canonical_path))
                self._handles.append((entry, fd))
            self.verify_init_ack()
            return self
        except BaseException:
            self.release()
            raise

    def verify_init_ack(self) -> None:
        for entry, fd in self._handles:
            identity = _file_identity_fd(fd)
            digest = _sha256_fd(fd)
            path = Path(entry.canonical_path)
            if identity != entry.file_identity or digest != entry.sha256 or not path.exists() or _canonical_path_from_fd(fd) != str(path.resolve(strict=True)):
                raise PermissionError("executable manifest drifted before initialization acknowledgement")

    def release(self) -> None:
        while self._handles:
            _, fd = self._handles.pop()
            os.close(fd)

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def _file_identity(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _file_identity_fd(fd: int) -> tuple[int, ...]:
    stat_result = os.fstat(fd)
    return (stat_result.st_dev, stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)


def _open_held_read_fd(path: Path) -> int:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_SH)
            return fd
        except BaseException:
            os.close(fd)
            raise
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path), 0x80000000, 0x00000001, None, 3,
        0x00000080 | 0x00200000, None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed", str(path))
    fd = -1
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        if _is_reparse(path) or _canonical_path_from_fd(fd) != str(path.resolve(strict=True)):
            raise PermissionError("held executable is a reparse point or path substituted")
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        else:
            _windows_close_handle(handle)
        raise


def _linux_path_from_fd(fd: int) -> str:
    return str(Path(os.readlink(f"/proc/self/fd/{fd}")).resolve(strict=True))


def _macos_path_from_fd(fd: int) -> str:
    import ctypes

    buffer = ctypes.create_string_buffer(1024)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fcntl(fd, 50, buffer) != 0:  # F_GETPATH
        error = ctypes.get_errno()
        raise OSError(error, "F_GETPATH failed")
    return str(Path(os.fsdecode(buffer.value)).resolve(strict=True))


def _canonical_posix_path_from_fd(fd: int, *, platform: str | None = None) -> str:
    selected = sys.platform if platform is None else platform
    if selected.startswith("linux"):
        return _linux_path_from_fd(fd)
    if selected == "darwin":
        return _macos_path_from_fd(fd)
    raise OSError(errno.ENOTSUP, f"POSIX fd path resolution is unsupported on {selected}")


def _canonical_path_from_fd(fd: int) -> str:
    if os.name != "nt":
        return _canonical_posix_path_from_fd(fd)
    import msvcrt

    return _canonical_windows_path_from_handle(msvcrt.get_osfhandle(fd))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def validate_checkpoint_write_set(paths: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute() or PureWindowsPath(raw).drive:
            raise ValueError("checkpoint paths must be nonempty relative paths")
        value = raw.replace("\\", "/")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("checkpoint paths must not contain traversal or dot segments")
        if parts[0].casefold() == ".phase0a":
            raise ValueError(".phase0a files are always excluded from checkpoints")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("checkpoint paths must be unique")
    return tuple(normalized)


@dataclass(frozen=True)
class LiveCircuitResult:
    classification: str
    exit_code: int | None
    model: str | None
    effort: str | None
    requested_auto_compaction_window: int | None
    requested_auto_compaction_trigger_percent: float | None
    requested_auto_compaction_trigger_tokens: int | None
    effective_auto_compaction_window: int | None
    effective_auto_compaction_trigger_percent: float | None
    effective_auto_compaction_trigger_tokens: int | None
    tools: tuple[str, ...]
    mcp_server_count: int
    plugin_count: int
    is_using_overage: bool | None
    rate_statuses: tuple[str, ...]
    source_sha256: str
    stream_bytes: int
    final_marker_matched: bool
    sanitized_final_text: str | None
    provider_error_code: str | None
    unknown_top_level_fields: tuple[str, ...]
    stderr_bytes: int = 0
    init_envelope_observed: bool = False
    result_envelope_observed: bool = False
    timeout_phase: str | None = None


_PIPE_CHUNK_BYTES = 64 * 1024


def _windows_pipe_available(fd: int) -> int | None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    available = wintypes.DWORD()
    if kernel32.PeekNamedPipe(
        msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None,
    ):
        return int(available.value)
    error = ctypes.get_last_error()
    if error in {6, 109, 232}:
        return None
    raise OSError(error, "PeekNamedPipe failed")


class _PipePump:
    def __init__(self, process: subprocess.Popen[bytes], streams: Mapping[str, Any]) -> None:
        self._process = process
        self._streams = dict(streams)
        self._closed: set[str] = set()
        self._selector: selectors.BaseSelector | None = None
        if os.name != "nt":
            self._selector = selectors.DefaultSelector()
            for label, stream in self._streams.items():
                fd = stream.fileno()
                os.set_blocking(fd, False)
                self._selector.register(fd, selectors.EVENT_READ, label)

    @property
    def closed_count(self) -> int:
        return len(self._closed)

    def _mark_closed(self, label: str) -> tuple[str, None] | None:
        if label in self._closed:
            return None
        self._closed.add(label)
        if self._selector is not None:
            try:
                self._selector.unregister(self._streams[label].fileno())
            except (KeyError, OSError, ValueError):
                pass
        return label, None

    def _read(self, label: str, maximum: int = _PIPE_CHUNK_BYTES) -> tuple[str, bytes | None] | None:
        try:
            chunk = os.read(self._streams[label].fileno(), maximum)
        except BlockingIOError:
            return None
        except OSError as exc:
            if exc.errno not in {errno.EBADF, errno.EPIPE}:
                raise
            return self._mark_closed(label)
        if not chunk:
            return self._mark_closed(label)
        return label, chunk

    def poll(self, timeout: float) -> list[tuple[str, bytes | None]]:
        timeout = max(0.0, timeout)
        if self._selector is not None:
            ready = self._selector.select(timeout)
            events = [
                event
                for key, _mask in ready
                if (event := self._read(str(key.data))) is not None
            ]
            if not events and self._process.poll() is not None:
                for label in self._streams:
                    if label not in self._closed:
                        event = self._read(label)
                        if event is not None:
                            events.append(event)
            return events

        deadline = time.monotonic() + timeout
        while True:
            events: list[tuple[str, bytes | None]] = []
            process_exited = self._process.poll() is not None
            for label, stream in self._streams.items():
                if label in self._closed:
                    continue
                available = _windows_pipe_available(stream.fileno())
                if available is None or (process_exited and available == 0):
                    event = self._mark_closed(label)
                elif available:
                    event = self._read(label, min(available, _PIPE_CHUNK_BYTES))
                else:
                    event = None
                if event is not None:
                    events.append(event)
            if events or time.monotonic() >= deadline:
                return events
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        if self._selector is not None:
            self._selector.close()
        for stream in self._streams.values():
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class _LinePipePump:
    def __init__(self, process: subprocess.Popen[bytes], streams: Mapping[str, Any]) -> None:
        self._pump = _PipePump(process, streams)
        self._pending: deque[tuple[str, bytes | None]] = deque()
        self._stdout = bytearray()

    def next_event(self, timeout: float) -> tuple[str, bytes | None] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._pending:
                return self._pending.popleft()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            for label, chunk in self._pump.poll(remaining):
                if label != "stdout":
                    self._pending.append((label, chunk))
                    continue
                if chunk is None:
                    if self._stdout:
                        self._pending.append((label, bytes(self._stdout)))
                        self._stdout.clear()
                    self._pending.append((label, None))
                    continue
                self._stdout.extend(chunk)
                while True:
                    newline = self._stdout.find(b"\n")
                    if newline < 0:
                        break
                    self._pending.append((label, bytes(self._stdout[:newline + 1])))
                    del self._stdout[:newline + 1]
                if len(self._stdout) > _MAX_COMMAND_OUTPUT_BYTES + 1:
                    self._pending.append((label, bytes(self._stdout)))
                    self._stdout.clear()

    def close(self) -> None:
        self._pump.close()


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def run_json_command(
    argv: Sequence[str],
    expected_type: type[Any] = dict,
    *,
    timeout_seconds: float = 30,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    if not argv or any(not isinstance(value, str) for value in argv):
        raise ValueError("argv must be a non-empty sequence of strings")
    process = subprocess.Popen(
        list(argv), cwd=None if cwd is None else str(cwd), env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        pump = _PipePump(process, {"stdout": process.stdout, "stderr": process.stderr})
    except BaseException:
        _terminate(process)
        process.stdout.close()
        process.stderr.close()
        raise
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    counts = {"stdout": 0, "stderr": 0}
    deadline = time.monotonic() + timeout_seconds
    try:
        while pump.closed_count < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("JSON command timed out")
            for label, chunk in pump.poll(min(remaining, 0.05)):
                if chunk is None:
                    continue
                counts[label] += len(chunk)
                if counts[label] > _MAX_COMMAND_OUTPUT_BYTES:
                    raise ValueError(f"{label} exceeds 8 MiB")
                (stdout if label == "stdout" else stderr).append(chunk)
    except BaseException:
        _terminate(process)
        raise
    finally:
        pump.close()
    try:
        exit_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _terminate(process)
        raise TimeoutError("JSON command timed out while waiting for exit") from exc
    if exit_code != 0:
        raise RuntimeError("JSON command failed")
    if stderr:
        raise RuntimeError("JSON command wrote stderr")
    try:
        payload = json.loads(b"".join(stdout).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON command output is invalid") from exc
    if not isinstance(payload, expected_type):
        raise ValueError("JSON command output has unexpected type")
    return payload


def _cap_utf8(value: str, limit: int) -> str:
    data = value.encode("utf-8")[:limit]
    return data.decode("utf-8", "ignore")


def _result(
    classification: str, process: subprocess.Popen[bytes], *, model: str | None,
    effort: str | None, requested_window: int | None, requested_trigger_percent: float | None,
    requested_trigger_tokens: int | None, window: int | None, trigger_percent: float | None,
    trigger_tokens: int | None, tools: tuple[str, ...], mcp_server_count: int,
    plugin_count: int, is_using_overage: bool | None, rate_statuses: list[str],
    digest: hashlib._Hash, stream_bytes: int, final_marker_matched: bool,
    sanitized_final_text: str | None, provider_error_code: str | None,
    unknown_top_level_fields: set[str], stderr_bytes: int,
    init_envelope_observed: bool, result_envelope_observed: bool,
    timeout_phase: str | None,
) -> LiveCircuitResult:
    return LiveCircuitResult(
        classification=classification,
        exit_code=process.poll(),
        model=model,
        effort=effort,
        requested_auto_compaction_window=requested_window,
        requested_auto_compaction_trigger_percent=requested_trigger_percent,
        requested_auto_compaction_trigger_tokens=requested_trigger_tokens,
        effective_auto_compaction_window=window,
        effective_auto_compaction_trigger_percent=trigger_percent,
        effective_auto_compaction_trigger_tokens=trigger_tokens,
        tools=tools,
        mcp_server_count=mcp_server_count,
        plugin_count=plugin_count,
        is_using_overage=is_using_overage,
        rate_statuses=tuple(rate_statuses),
        source_sha256=digest.hexdigest(),
        stream_bytes=stream_bytes,
        final_marker_matched=final_marker_matched,
        sanitized_final_text=sanitized_final_text,
        provider_error_code=provider_error_code,
        unknown_top_level_fields=tuple(sorted(unknown_top_level_fields)),
        stderr_bytes=stderr_bytes,
        init_envelope_observed=init_envelope_observed,
        result_envelope_observed=result_envelope_observed,
        timeout_phase=timeout_phase,
    )


def run_stream_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 30,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    expected_model: str | None = None,
    requested_auto_compaction_window: int | None = None,
    requested_auto_compaction_trigger_percent: float | None = None,
    requested_auto_compaction_trigger_tokens: int | None = None,
    final_policy: str = "discard",
    final_marker: str | None = None,
    post_init_timeout_seconds: float | None = None,
) -> LiveCircuitResult:
    if not argv or any(not isinstance(value, str) for value in argv):
        raise ValueError("argv must be a non-empty sequence of strings")
    if final_policy not in {"exact_marker", "sanitized_text", "discard"}:
        raise ValueError("final_policy must be exact_marker, sanitized_text, or discard")
    if final_policy == "exact_marker" and not isinstance(final_marker, str):
        raise ValueError("exact_marker policy requires final_marker")
    def validate_requested(value: Any, name: str, *, percent: bool = False) -> None:
        if value is None:
            return
        if not isinstance(value, (int, float)) or isinstance(value, bool) or (percent and not 0 < float(value) <= 100) or (not percent and (not isinstance(value, int) or value <= 0)):
            raise ValueError(f"{name} is invalid")

    validate_requested(requested_auto_compaction_window, "requested_auto_compaction_window")
    validate_requested(requested_auto_compaction_trigger_percent, "requested_auto_compaction_trigger_percent", percent=True)
    validate_requested(requested_auto_compaction_trigger_tokens, "requested_auto_compaction_trigger_tokens")
    if (
        post_init_timeout_seconds is not None
        and (
            not isinstance(post_init_timeout_seconds, (int, float))
            or isinstance(post_init_timeout_seconds, bool)
            or not 0 < float(post_init_timeout_seconds) <= _MAX_POST_INIT_TIMEOUT_SECONDS
        )
    ):
        raise ValueError(
            "post_init_timeout_seconds must be between zero and 600 seconds",
        )
    try:
        process = subprocess.Popen(
            list(argv), cwd=None if cwd is None else str(cwd), env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        )
    except OSError:
        return LiveCircuitResult(
            classification="process_start_failed",
            exit_code=None,
            model=None,
            effort=None,
            requested_auto_compaction_window=None,
            requested_auto_compaction_trigger_percent=None,
            requested_auto_compaction_trigger_tokens=None,
            effective_auto_compaction_window=None,
            effective_auto_compaction_trigger_percent=None,
            effective_auto_compaction_trigger_tokens=None,
            tools=(),
            mcp_server_count=0,
            plugin_count=0,
            is_using_overage=None,
            rate_statuses=(),
            source_sha256=hashlib.sha256(b"").hexdigest(),
            stream_bytes=0,
            final_marker_matched=False,
            sanitized_final_text=None,
            provider_error_code=None,
            unknown_top_level_fields=(),
        )
    assert process.stdout is not None and process.stderr is not None
    try:
        pump = _LinePipePump(process, {"stdout": process.stdout, "stderr": process.stderr})
    except BaseException:
        _terminate(process)
        process.stdout.close()
        process.stderr.close()
        raise
    digest = hashlib.sha256()
    stream_bytes = 0
    stderr_bytes = 0
    closed: set[str] = set()
    model = effort = None
    requested_window = requested_auto_compaction_window
    requested_trigger_percent = requested_auto_compaction_trigger_percent
    requested_trigger_tokens = requested_auto_compaction_trigger_tokens
    window = trigger_percent = trigger_tokens = None
    tools: tuple[str, ...] = ()
    mcp_server_count = plugin_count = 0
    is_using_overage: bool | None = None
    rate_statuses: list[str] = []
    quota_signal_seen = False
    init_seen = result_seen = False
    classification: str | None = None
    final_text: str | None = None
    provider_error_code: str | None = None
    unknown_top_level_fields: set[str] = set()
    marker_matched = False
    deadline = time.monotonic() + timeout_seconds
    active_timeout_phase = "pre_init"
    timeout_phase: str | None = None
    try:
        while len(closed) < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                classification = "timeout"
                timeout_phase = active_timeout_phase
                _terminate(process)
                break
            event = pump.next_event(min(remaining, 0.05))
            if event is None:
                continue
            label, raw = event
            if raw is None:
                closed.add(label)
                continue
            if label == "stderr":
                stderr_bytes += len(raw)
                if stderr_bytes > _MAX_COMMAND_OUTPUT_BYTES:
                    classification = "stderr_limit"
                    _terminate(process)
                    break
                continue
            stream_bytes += len(raw)
            digest.update(raw)
            line_bytes = raw[:-1] if raw.endswith(b"\n") else raw
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            if stream_bytes > _MAX_STREAM_BYTES or len(line_bytes) > _MAX_COMMAND_OUTPUT_BYTES:
                classification = "stream_limit"
                _terminate(process)
                break
            try:
                line = line_bytes.decode("utf-8")
                item_type = peek_top_level_type(line)
                if item_type not in {"system", "rate_limit_event", "result"}:
                    continue
                item = json.loads(line)
                if item.get("type") != item_type:
                    raise ValueError("stream type changed after decode")
                known_fields = {
                    "system": {"type", "subtype", "model", "effort", "tools", "mcp_servers", "plugins", "requestedAutoCompactionWindow", "requestedAutoCompactionTriggerPercent", "requestedAutoCompactionTriggerTokens", "effectiveAutoCompactionWindow", "effectiveAutoCompactionTriggerPercent", "effectiveAutoCompactionTriggerTokens"},
                    "rate_limit_event": {"type", "rate_limit_info"},
                    "result": {"type", "subtype", "error_code", "is_error", "result", "stop_reason", "usage", "total_cost_usd"},
                }
                for unknown_name in set(item) - known_fields[item_type]:
                    if (
                        not isinstance(unknown_name, str)
                        or len(unknown_name.encode("utf-8")) > _MAX_STREAM_NAME_BYTES
                        or redact_text(unknown_name) != unknown_name
                        or re.search(r"(?i)(authorization|token|secret|password|cookie|email)", unknown_name)
                    ):
                        raise ValueError("unsafe unknown top-level field")
                    unknown_top_level_fields.add(unknown_name)
                if len(unknown_top_level_fields) > _MAX_STREAM_OBJECTS:
                    raise ValueError("too many unknown top-level fields")
                if item_type == "system" and item.get("subtype") == "init":
                    if init_seen:
                        raise ValueError("duplicate system/init")
                    candidate_model = item.get("model")
                    candidate_tools = item.get("tools")
                    servers = item.get("mcp_servers")
                    plugins = item.get("plugins")
                    if (
                        not isinstance(candidate_model, str)
                        or not isinstance(candidate_tools, list)
                        or any(not isinstance(tool, str) for tool in candidate_tools)
                        or not isinstance(servers, list)
                        or any(
                            not isinstance(server, dict)
                            or not isinstance(server.get("name"), str)
                            or not isinstance(server.get("status"), str)
                            for server in servers
                        )
                        or not isinstance(plugins, list)
                        or any(
                            not isinstance(plugin, dict)
                            or not isinstance(plugin.get("name"), str)
                            for plugin in plugins
                        )
                    ):
                        raise ValueError("invalid system/init")
                    if (
                        len(candidate_tools) > _MAX_STREAM_OBJECTS
                        or len(servers) > _MAX_STREAM_OBJECTS
                        or len(plugins) > _MAX_STREAM_OBJECTS
                        or any(len(tool.encode("utf-8")) > _MAX_STREAM_NAME_BYTES for tool in candidate_tools)
                        or any(len(server["name"].encode("utf-8")) > _MAX_STREAM_NAME_BYTES for server in servers)
                        or any(len(plugin["name"].encode("utf-8")) > _MAX_STREAM_NAME_BYTES for plugin in plugins)
                    ):
                        raise ValueError("system/init fields exceed stream caps")
                    def integer(name: str) -> int | None:
                        value = item.get(name)
                        if value is None:
                            return None
                        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                            raise ValueError(f"{name} must be a positive integer")
                        return value

                    def percent(name: str) -> float | None:
                        value = item.get(name)
                        if value is None:
                            return None
                        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < float(value) <= 100:
                            raise ValueError(f"{name} must be a percentage")
                        return float(value)

                    observed_requested_window = integer("requestedAutoCompactionWindow")
                    observed_requested_trigger_percent = percent("requestedAutoCompactionTriggerPercent")
                    observed_requested_trigger_tokens = integer("requestedAutoCompactionTriggerTokens")
                    requested_window = requested_window if requested_window is not None else observed_requested_window
                    requested_trigger_percent = requested_trigger_percent if requested_trigger_percent is not None else observed_requested_trigger_percent
                    requested_trigger_tokens = requested_trigger_tokens if requested_trigger_tokens is not None else observed_requested_trigger_tokens
                    if (
                        (requested_auto_compaction_window is not None and observed_requested_window is not None and requested_auto_compaction_window != observed_requested_window)
                        or (requested_auto_compaction_trigger_percent is not None and observed_requested_trigger_percent is not None and float(requested_auto_compaction_trigger_percent) != observed_requested_trigger_percent)
                        or (requested_auto_compaction_trigger_tokens is not None and observed_requested_trigger_tokens is not None and requested_auto_compaction_trigger_tokens != observed_requested_trigger_tokens)
                    ):
                        raise ValueError("requested compaction policy drift")
                    window = integer("effectiveAutoCompactionWindow")
                    trigger_percent = percent("effectiveAutoCompactionTriggerPercent")
                    trigger_tokens = integer("effectiveAutoCompactionTriggerTokens")
                    init_seen = True
                    if post_init_timeout_seconds is not None:
                        deadline = time.monotonic() + float(post_init_timeout_seconds)
                        active_timeout_phase = "post_init"
                    if expected_model is not None and candidate_model != expected_model:
                        classification = "model_mismatch"
                        _terminate(process)
                        break
                    model = candidate_model
                    effort = item.get("effort") if isinstance(item.get("effort"), str) else None
                    tools = tuple(sorted(candidate_tools))
                    mcp_server_count = len(servers)
                    plugin_count = len(plugins)
                elif item_type == "rate_limit_event":
                    info = item.get("rate_limit_info")
                    if not isinstance(info, dict):
                        raise ValueError("invalid rate_limit_event")
                    status = info.get("status")
                    if status is not None and not isinstance(status, str):
                        raise ValueError("invalid rate limit status")
                    if status is not None:
                        if len(status.encode("utf-8")) > _MAX_STREAM_NAME_BYTES or len(rate_statuses) >= _MAX_STREAM_OBJECTS:
                            raise ValueError("rate-limit fields exceed stream caps")
                        rate_statuses.append(status)
                    if status == "rejected" or info.get("errorCode") == "credits_required":
                        quota_signal_seen = True
                    overage = info.get("isUsingOverage")
                    if overage is not None and not isinstance(overage, bool):
                        raise ValueError("invalid isUsingOverage")
                    if overage is not None:
                        is_using_overage = overage
                    if overage is True:
                        classification = "usage_credits_forbidden"
                        _terminate(process)
                        break
                elif item_type == "result":
                    if result_seen:
                        raise ValueError("duplicate result")
                    result_seen = True
                    if not isinstance(item.get("is_error"), bool):
                        raise ValueError("invalid result")
                    raw_final = item.get("result")
                    if isinstance(raw_final, str):
                        if final_policy == "sanitized_text":
                            final_text = _cap_utf8(redact_text(raw_final), _MAX_FINAL_TEXT_BYTES)
                        elif final_policy == "exact_marker":
                            marker_matched = raw_final == final_marker
                    if item["is_error"]:
                        terminal_code = item.get("error_code", item.get("subtype"))
                        provider_error_code = terminal_code if isinstance(terminal_code, str) and len(terminal_code.encode("utf-8")) <= _MAX_STREAM_NAME_BYTES else None
                        classification = (
                            "quota_paused"
                            if quota_signal_seen and terminal_code in {"rate_limit", "billing_error", "credits_required"}
                            else "terminal_error"
                        )
                    else:
                        classification = "success"
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                classification = "protocol_error"
                _terminate(process)
                break
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                classification = "timeout"
                timeout_phase = active_timeout_phase
                _terminate(process)
        if (not init_seen or not result_seen) and classification not in {"timeout", "process_start_failed", "model_mismatch", "usage_credits_forbidden", "stream_limit", "stderr_limit"}:
            classification = "protocol_error"
        elif classification is None:
            classification = "process_error"
        if classification == "success" and process.poll() not in {0, None}:
            classification = "process_error"
        return _result(
            classification, process, model=model, effort=effort, requested_window=requested_window,
            requested_trigger_percent=requested_trigger_percent, requested_trigger_tokens=requested_trigger_tokens, window=window,
            trigger_percent=trigger_percent, trigger_tokens=trigger_tokens, tools=tools,
            mcp_server_count=mcp_server_count, plugin_count=plugin_count,
            is_using_overage=is_using_overage, rate_statuses=rate_statuses, digest=digest,
            stream_bytes=stream_bytes, final_marker_matched=marker_matched,
            sanitized_final_text=final_text, provider_error_code=provider_error_code,
            unknown_top_level_fields=unknown_top_level_fields,
            stderr_bytes=stderr_bytes,
            init_envelope_observed=init_seen,
            result_envelope_observed=result_seen,
            timeout_phase=timeout_phase,
        )
    finally:
        try:
            if process.poll() is None:
                _terminate(process)
        finally:
            pump.close()


def _scope_from_dict(payload: dict[str, Any]) -> ApprovalScope:
    effects = tuple(
        SideEffectSpec(
            kind=item["kind"],
            argv_template=tuple(item["argv_template"]),
            bindings=tuple(RuntimeBinding(**binding) for binding in item["bindings"]),
            max_uses=item["max_uses"],
            exact_targets=tuple(item["exact_targets"]),
        )
        for item in payload["side_effects"]
    )
    return ApprovalScope(
        **{**payload, "gate_ids": tuple(payload["gate_ids"]), "side_effects": effects}
    )


def _approve_scope(scope_path: Path, output_path: Path, expires_minutes: int) -> None:
    if expires_minutes < 1 or expires_minutes > 120:
        raise ValueError("expires-minutes must be between 1 and 120")
    approval_root = (Path.cwd() / _APPROVAL_ROOT).absolute()
    _require_precreated_private_directory(approval_root.parent)
    _require_precreated_private_directory(approval_root)
    if output_path.absolute().parent != approval_root:
        raise PermissionError("receipt must be a direct child of the bound approval root")
    scope = _scope_from_dict(json.loads(scope_path.read_text(encoding="utf-8")))
    approved_at = datetime.now(timezone.utc)
    _write_private_json(
        output_path,
        _receipt_payload(scope, approved_at, approved_at + timedelta(minutes=expires_minutes), None),
        exclusive=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    approve = subcommands.add_parser("approve-scope")
    approve.add_argument("--scope", type=Path, required=True)
    approve.add_argument("--output", type=Path, required=True)
    approve.add_argument("--expires-minutes", type=int, required=True)
    prepare = subcommands.add_parser("prepare-approval-storage")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--repair-existing", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "approve-scope":
        _approve_scope(args.scope, args.output, args.expires_minutes)
    else:
        print(_canonical_json(prepare_approval_storage(
            args.root,
            repair_existing=args.repair_existing,
        )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
