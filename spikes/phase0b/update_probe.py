from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ACTIVE_ROSTER_STATES = {"working", "waiting"}
_TERMINAL_ROSTER_STATES = {"done", "failed", "stopped"}


class UpdateProbeError(RuntimeError):
    pass


class HealthCheckFailed(UpdateProbeError):
    pass


class SimulatedCrash(UpdateProbeError):
    pass


class StateQuarantined(UpdateProbeError):
    pass


@dataclass(frozen=True)
class RuntimeRecord:
    version: str
    root: Path
    manifest_sha256: str

    def validate(self, runtime_parent: Path) -> None:
        if not isinstance(self.version, str) or _VERSION.fullmatch(self.version) is None:
            raise ValueError("invalid runtime version")
        if not isinstance(self.root, Path):
            raise ValueError("invalid runtime root")
        if (
            not isinstance(self.manifest_sha256, str)
            or _SHA256.fullmatch(self.manifest_sha256) is None
        ):
            raise ValueError("invalid runtime manifest SHA-256")
        try:
            parent = runtime_parent.resolve(strict=True)
            root = self.root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("runtime root does not exist") from exc
        if self.root.is_symlink() or not root.is_dir() or root.parent != parent:
            raise ValueError("runtime root must be a direct child of the runtime parent")
        manifest = root / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("runtime manifest is missing")
        if _sha256_file(manifest) != self.manifest_sha256:
            raise ValueError("runtime manifest SHA-256 mismatch")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_identity: str
    executable_sha256: str

    def validate(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("invalid process identity")
        _validate_opaque(self.creation_identity, "process identity", limit=128)
        if (
            not isinstance(self.executable_sha256, str)
            or _SHA256.fullmatch(self.executable_sha256) is None
        ):
            raise ValueError("invalid process identity")


@dataclass(frozen=True)
class PointerSnapshot:
    record: RuntimeRecord
    raw: bytes


@dataclass(frozen=True)
class ManagedRestartDecision:
    action: str
    execution_key: str
    native_resume_key: str


@dataclass(frozen=True)
class PublicRosterRow:
    ownership_digest: str
    status: str
    native_session_present: bool
    workspace_equal: bool

    def validate(self) -> None:
        if (
            not isinstance(self.ownership_digest, str)
            or _SHA256.fullmatch(self.ownership_digest) is None
        ):
            raise ValueError("invalid public roster ownership digest")
        if self.status not in _ACTIVE_ROSTER_STATES | _TERMINAL_ROSTER_STATES:
            raise ValueError("invalid public roster status")
        if type(self.native_session_present) is not bool or type(self.workspace_equal) is not bool:
            raise ValueError("invalid public roster aggregate")


@dataclass(frozen=True)
class PublicRosterAggregate:
    source: str
    rows: tuple[PublicRosterRow, ...]

    def validate(self) -> None:
        if self.source != "claude_agents_json_all" or not isinstance(self.rows, tuple):
            raise ValueError("visible restart requires the supplied public roster aggregate")
        for row in self.rows:
            if not isinstance(row, PublicRosterRow):
                raise ValueError("invalid public roster aggregate")
            row.validate()


def switch_pointer(
    pointer: Path,
    candidate: RuntimeRecord,
    *,
    runtime_parent: Path,
    health_ok: bool,
    crash_after_stage: bool = False,
) -> PointerSnapshot | None:
    """Durably stage and atomically select one immutable temporary runtime."""
    candidate.validate(runtime_parent)
    if type(health_ok) is not bool or type(crash_after_stage) is not bool:
        raise ValueError("switch flags must be booleans")
    if not pointer.parent.is_dir():
        raise ValueError("pointer parent must already exist")

    pending = _pending_path(pointer)
    if pending.exists() or _temporary_path(pending).exists():
        raise UpdateProbeError("pending pointer requires recovery")

    previous: PointerSnapshot | None = None
    if pointer.exists():
        raw = pointer.read_bytes()
        previous = PointerSnapshot(
            record=_decode_pointer(raw, runtime_parent),
            raw=raw,
        )

    _write_atomic_bytes(pending, _encode_pointer(candidate))
    if not health_ok:
        pending.unlink()
        _fsync_directory(pointer.parent)
        raise HealthCheckFailed("staged runtime health failed")
    if crash_after_stage:
        raise SimulatedCrash("staged pointer persisted before activation")

    os.replace(pending, pointer)
    _fsync_directory(pointer.parent)
    try:
        selected = read_pointer(pointer, runtime_parent)
    except Exception:
        if previous is not None:
            _write_atomic_bytes(pointer, previous.raw)
        raise
    if selected != candidate:
        if previous is not None:
            _write_atomic_bytes(pointer, previous.raw)
        raise UpdateProbeError("selected runtime differs from staged runtime")
    return previous


def rollback_pointer(
    pointer: Path,
    previous: PointerSnapshot,
    *,
    runtime_parent: Path,
) -> None:
    """Restore the exact prior pointer bytes after revalidating its manifest."""
    if not isinstance(previous, PointerSnapshot):
        raise ValueError("invalid rollback snapshot")
    decoded = _decode_pointer(previous.raw, runtime_parent)
    if decoded != previous.record:
        raise ValueError("rollback snapshot identity mismatch")
    previous.record.validate(runtime_parent)
    _write_atomic_bytes(pointer, previous.raw)
    if pointer.read_bytes() != previous.raw or read_pointer(pointer, runtime_parent) != previous.record:
        raise UpdateProbeError("exact pointer rollback failed")


def read_pointer(pointer: Path, runtime_parent: Path) -> RuntimeRecord:
    if not pointer.is_file() or pointer.is_symlink():
        raise ValueError("runtime pointer is missing")
    return _decode_pointer(pointer.read_bytes(), runtime_parent)


def recover_pointer(
    pointer: Path,
    *,
    runtime_parent: Path,
    quarantine_dir: Path,
) -> RuntimeRecord:
    """Keep the valid current runtime and quarantine incomplete pointer stages."""
    try:
        current = read_pointer(pointer, runtime_parent)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if pointer.exists():
            destination = _quarantine(pointer, quarantine_dir)
            raise StateQuarantined(f"runtime pointer quarantined as {destination.name}") from exc
        raise

    for staged in (_pending_path(pointer), _temporary_path(_pending_path(pointer))):
        if staged.exists():
            _quarantine(staged, quarantine_dir)
    return current


def load_restart_state(path: Path, *, quarantine_dir: Path) -> dict[str, object]:
    """Load additive restart state, preserving unknown fields byte-for-byte when idle."""
    if not path.exists():
        return {"schema_version": 1, "executions": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        _validate_restart_state(value)
        return value
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if path.exists():
            destination = _quarantine(path, quarantine_dir)
            raise StateQuarantined(f"restart state quarantined as {destination.name}") from exc
        raise


def managed_restart(
    state_path: Path,
    *,
    request_key: str,
    new_execution_key: str,
    new_native_resume_key: str,
    quarantine_dir: Path,
) -> ManagedRestartDecision:
    """Claim a new request once or resume the one durable execution after restart."""
    _validate_opaque(request_key, "request key")
    _validate_opaque(new_execution_key, "execution key")
    _validate_opaque(new_native_resume_key, "native resume key", limit=256)
    with _exclusive_file_lock(state_path):
        state = load_restart_state(state_path, quarantine_dir=quarantine_dir)
        executions = state["executions"]
        assert isinstance(executions, list)
        for item in executions:
            assert isinstance(item, dict)
            if item["request_key"] == request_key:
                return ManagedRestartDecision(
                    action="return_recorded" if item["terminal"] else "resume",
                    execution_key=item["execution_key"],
                    native_resume_key=item["native_resume_key"],
                )

        executions.append(
            {
                "execution_key": new_execution_key,
                "native_resume_key": new_native_resume_key,
                "request_key": request_key,
                "terminal": False,
            }
        )
        _write_atomic_bytes(state_path, _encode_json(state))
        return ManagedRestartDecision(
            action="start",
            execution_key=new_execution_key,
            native_resume_key=new_native_resume_key,
        )


def terminate_if_same_process(
    expected: ProcessIdentity,
    observed: ProcessIdentity,
    terminate: Callable[[int], object],
) -> bool:
    """Invoke the supplied test double only for an exact process identity match."""
    if not isinstance(expected, ProcessIdentity) or not isinstance(observed, ProcessIdentity):
        raise ValueError("invalid process identity")
    expected.validate()
    observed.validate()
    if observed != expected:
        return False
    terminate(expected.pid)
    return True


def visible_restart(
    ownership_digest: str,
    roster: PublicRosterAggregate,
) -> str:
    """Decide reattachment solely from an already sanitized public roster aggregate."""
    if not isinstance(ownership_digest, str) or _SHA256.fullmatch(ownership_digest) is None:
        raise ValueError("invalid ownership digest")
    if not isinstance(roster, PublicRosterAggregate):
        raise ValueError("visible restart requires the supplied public roster aggregate")
    roster.validate()
    matches = [row for row in roster.rows if row.ownership_digest == ownership_digest]
    if len(matches) != 1:
        return "recovery_required"
    match = matches[0]
    if not match.native_session_present or not match.workspace_equal:
        return "recovery_required"
    if match.status in _ACTIVE_ROSTER_STATES:
        return "reattach"
    if match.status in _TERMINAL_ROSTER_STATES:
        return "return_recorded"
    return "recovery_required"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_opaque(value: object, label: str, *, limit: int = 128) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > limit
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ValueError(f"invalid {label}")


def _encode_pointer(record: RuntimeRecord) -> bytes:
    return _encode_json(
        {
            "manifest_sha256": record.manifest_sha256,
            "root": record.root.name,
            "schema_version": 1,
            "version": record.version,
        }
    )


def _decode_pointer(raw: bytes, runtime_parent: Path) -> RuntimeRecord:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid runtime pointer") from exc
    if not isinstance(value, Mapping):
        raise ValueError("invalid runtime pointer")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("invalid runtime pointer schema")
    root_name = value.get("root")
    if (
        not isinstance(root_name, str)
        or not root_name
        or root_name in {".", ".."}
        or "/" in root_name
        or "\\" in root_name
    ):
        raise ValueError("invalid runtime pointer root")
    record = RuntimeRecord(
        version=value.get("version"),
        root=runtime_parent / root_name,
        manifest_sha256=value.get("manifest_sha256"),
    )
    record.validate(runtime_parent)
    return record


def _validate_restart_state(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("restart state must be an object")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported restart state schema")
    executions = value.get("executions")
    if not isinstance(executions, list):
        raise ValueError("restart state executions must be an array")
    seen: set[str] = set()
    required = {"request_key", "execution_key", "native_resume_key", "terminal"}
    for item in executions:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("invalid restart execution")
        _validate_opaque(item["request_key"], "request key")
        _validate_opaque(item["execution_key"], "execution key")
        _validate_opaque(item["native_resume_key"], "native resume key", limit=256)
        if type(item["terminal"]) is not bool:
            raise ValueError("invalid restart execution terminal")
        request_key = item["request_key"]
        if request_key in seen:
            raise ValueError("duplicate idempotency request")
        seen.add(request_key)


def _encode_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _pending_path(pointer: Path) -> Path:
    return pointer.with_name(pointer.name + ".pending")


def _temporary_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def _write_atomic_bytes(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        raise ValueError("destination parent must already exist")
    temporary = _temporary_path(path)
    if temporary.exists():
        raise UpdateProbeError("stale temporary file requires recovery")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


@contextmanager
def _exclusive_file_lock(state_path: Path):
    """Serialize a restart claim across threads and processes without deleting the lock."""
    lock_path = state_path.with_name(state_path.name + ".lock")
    if not lock_path.parent.is_dir():
        raise ValueError("state parent must already exist")
    with lock_path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


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


def _quarantine(path: Path, quarantine_dir: Path) -> Path:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    base = quarantine_dir / f"{path.name}.quarantine"
    destination = base
    suffix = 1
    while destination.exists():
        destination = quarantine_dir / f"{base.name}.{suffix}"
        suffix += 1
    os.replace(path, destination)
    _fsync_directory(path.parent)
    _fsync_directory(quarantine_dir)
    return destination
