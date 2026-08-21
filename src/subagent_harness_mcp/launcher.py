from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_POINTER_BYTES = 64 * 1024
_MAX_JOURNAL_BYTES = 32 * 1024 * 1024
_MAX_JOURNAL_LINE_BYTES = 64 * 1024
_WINDOWS_PYTHON = "Scripts/python.exe"


class LifecycleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    version: str
    root: Path
    manifest_sha256: str

    def validate(self, runtime_parent: Path) -> None:
        _require_version(self.version)
        if not isinstance(self.root, Path):
            raise LifecycleError("RUNTIME_INVALID", "runtime root is invalid")
        if not isinstance(self.manifest_sha256, str) or _SHA256.fullmatch(
            self.manifest_sha256
        ) is None:
            raise LifecycleError("RUNTIME_INVALID", "runtime manifest digest is invalid")
        try:
            parent = runtime_parent.resolve(strict=True)
            root = self.root.resolve(strict=True)
        except OSError as exc:
            raise LifecycleError("RUNTIME_INVALID", "runtime root does not exist") from exc
        if (
            self.root.is_symlink()
            or not root.is_dir()
            or root.parent != parent
            or root.name != self.version
        ):
            raise LifecycleError(
                "RUNTIME_INVALID",
                "runtime root must be an immutable direct child",
            )
        manifest_path = root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise LifecycleError("RUNTIME_INVALID", "runtime manifest is missing")
        if file_sha256(manifest_path) != self.manifest_sha256:
            raise LifecycleError("RUNTIME_INVALID", "runtime manifest digest changed")
        _load_and_validate_manifest(root, expected_version=self.version)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    creation_identity: str
    executable_sha256: str

    def validate(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise LifecycleError("RECOVERY_REQUIRED", "process PID is invalid")
        _require_text(self.creation_identity, "process creation identity", 256)
        if not isinstance(self.executable_sha256, str) or _SHA256.fullmatch(
            self.executable_sha256
        ) is None:
            raise LifecycleError("RECOVERY_REQUIRED", "process executable digest is invalid")


class ProcessBackend(Protocol):
    def observe(self, pid: int) -> ProcessIdentity | None: ...

    def terminate(self, identity: ProcessIdentity) -> None: ...


@dataclass(frozen=True, slots=True)
class OwnedResource:
    resource_id: str
    kind: str
    locator: str
    digest: str


class OwnershipJournal:
    """Append-only ownership evidence; callers serialize mutation with the lifecycle lock."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def active_resources(self) -> dict[str, OwnedResource]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or not self.path.is_file():
            raise LifecycleError(
                "OWNERSHIP_JOURNAL_CORRUPT",
                "ownership journal is not a regular file",
            )
        try:
            if self.path.stat().st_size > _MAX_JOURNAL_BYTES:
                raise LifecycleError(
                    "OWNERSHIP_JOURNAL_CORRUPT",
                    "ownership journal exceeds its size limit",
                )
            lines = self.path.read_bytes().splitlines()
        except OSError as exc:
            raise LifecycleError(
                "OWNERSHIP_JOURNAL_CORRUPT",
                "ownership journal cannot be read",
            ) from exc
        active: dict[str, OwnedResource] = {}
        for line in lines:
            if not line or len(line) > _MAX_JOURNAL_LINE_BYTES:
                raise LifecycleError(
                    "OWNERSHIP_JOURNAL_CORRUPT",
                    "ownership journal contains an invalid record",
                )
            record = _decode_json_object(
                line,
                code="OWNERSHIP_JOURNAL_CORRUPT",
                label="ownership record",
            )
            if record.get("schema_version") != 1:
                raise LifecycleError(
                    "OWNERSHIP_JOURNAL_CORRUPT",
                    "ownership record schema is unsupported",
                )
            resource_id = record.get("resource_id")
            _require_journal_text(resource_id, "resource id", 256)
            event = record.get("event")
            if event == "owned":
                kind = record.get("kind")
                locator = record.get("locator")
                digest = record.get("digest")
                _require_journal_text(kind, "resource kind", 64)
                _require_journal_text(locator, "resource locator", 8192)
                if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                    raise LifecycleError(
                        "OWNERSHIP_JOURNAL_CORRUPT",
                        "ownership digest is invalid",
                    )
                active[resource_id] = OwnedResource(
                    resource_id=resource_id,
                    kind=kind,
                    locator=locator,
                    digest=digest,
                )
            elif event == "removed":
                active.pop(resource_id, None)
            else:
                raise LifecycleError(
                    "OWNERSHIP_JOURNAL_CORRUPT",
                    "ownership event is invalid",
                )
        return active

    def record_owned(
        self,
        *,
        resource_id: str,
        kind: str,
        locator: str,
        digest: str,
    ) -> None:
        _require_text(resource_id, "resource id", 256)
        _require_text(kind, "resource kind", 64)
        _require_text(locator, "resource locator", 8192)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise LifecycleError("OWNERSHIP_INVALID", "resource digest is invalid")
        self._append(
            {
                "digest": digest,
                "event": "owned",
                "kind": kind,
                "locator": locator,
                "recorded_at_utc": _utc_now(),
                "resource_id": resource_id,
                "schema_version": 1,
            }
        )

    def record_removed(self, *, resource_id: str) -> None:
        _require_text(resource_id, "resource id", 256)
        self._append(
            {
                "event": "removed",
                "recorded_at_utc": _utc_now(),
                "resource_id": resource_id,
                "schema_version": 1,
            }
        )

    def _append(self, record: Mapping[str, object]) -> None:
        payload = _encode_json(record)
        if len(payload) > _MAX_JOURNAL_LINE_BYTES:
            raise LifecycleError("OWNERSHIP_INVALID", "ownership record is too large")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            with self.path.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if not existed:
                _fsync_directory(self.path.parent)
        except OSError as exc:
            raise LifecycleError(
                "OWNERSHIP_WRITE_FAILED",
                "ownership journal append failed",
            ) from exc


def build_runtime_manifest(root: Path, *, version: str) -> dict[str, object]:
    """Create manifest data for a runtime source without writing it."""

    _require_version(version)
    if root.is_symlink() or not root.is_dir():
        raise LifecycleError("RUNTIME_INVALID", "runtime source is not a directory")
    files: dict[str, str] = {}
    seen_casefolded: set[str] = set()
    try:
        entries = sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
    except OSError as exc:
        raise LifecycleError("RUNTIME_INVALID", "runtime source cannot be listed") from exc
    for path in entries:
        if path.is_symlink():
            raise LifecycleError("RUNTIME_INVALID", "runtime source contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise LifecycleError("RUNTIME_INVALID", "runtime source has a special file")
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        normalized = _validate_manifest_path(relative)
        folded = normalized.casefold()
        if folded in seen_casefolded:
            raise LifecycleError(
                "RUNTIME_INVALID",
                "runtime contains case-colliding paths",
            )
        seen_casefolded.add(folded)
        files[normalized] = file_sha256(path)
    if _WINDOWS_PYTHON not in files:
        raise LifecycleError(
            "RUNTIME_INVALID",
            "Windows runtime lacks Scripts/python.exe",
        )
    return {"files": files, "schema_version": 1, "version": version}


def stage_runtime(
    source: Path,
    runtime_parent: Path,
    *,
    version: str,
) -> tuple[RuntimeRecord, bool]:
    """Copy one validated candidate into an immutable version directory."""

    _require_version(version)
    if not runtime_parent.is_dir() or runtime_parent.is_symlink():
        raise LifecycleError("RUNTIME_INVALID", "runtime parent must already exist")
    manifest = _load_and_validate_manifest(source, expected_version=version)
    manifest_path = source / "manifest.json"
    manifest_sha256 = file_sha256(manifest_path)
    destination = runtime_parent / version
    candidate = RuntimeRecord(version, destination, manifest_sha256)
    if destination.exists():
        candidate.validate(runtime_parent)
        return candidate, False

    staging = runtime_parent / f".{version}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        files = manifest["files"]
        assert isinstance(files, dict)
        for relative in sorted(files):
            source_file = source / Path(*PurePosixPath(relative).parts)
            destination_file = staging / Path(*PurePosixPath(relative).parts)
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination_file)
            _fsync_file(destination_file)
        shutil.copyfile(manifest_path, staging / "manifest.json")
        _fsync_file(staging / "manifest.json")
        _fsync_tree_directories(staging)
        _load_and_validate_manifest(staging, expected_version=version)
        if file_sha256(staging / "manifest.json") != manifest_sha256:
            raise LifecycleError("RUNTIME_INVALID", "staged manifest digest changed")
        try:
            os.replace(staging, destination)
        except OSError:
            if destination.exists():
                candidate.validate(runtime_parent)
                return candidate, False
            raise
        _fsync_directory(runtime_parent)
        candidate.validate(runtime_parent)
        return candidate, True
    except LifecycleError:
        raise
    except OSError as exc:
        raise LifecycleError("RUNTIME_STAGE_FAILED", "runtime staging failed") from exc
    finally:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                pass


def inspect_runtime_source(source: Path, *, version: str) -> str:
    """Validate a prepared runtime without creating product state."""

    _load_and_validate_manifest(source, expected_version=version)
    return file_sha256(source / "manifest.json")


def read_pointer(pointer: Path, runtime_parent: Path) -> RuntimeRecord:
    try:
        raw = pointer.read_bytes()
    except FileNotFoundError as exc:
        raise LifecycleError("RUNTIME_POINTER_MISSING", "runtime pointer is missing") from exc
    except OSError as exc:
        raise LifecycleError("RUNTIME_POINTER_INVALID", "runtime pointer cannot be read") from exc
    if pointer.is_symlink() or len(raw) > _MAX_POINTER_BYTES:
        raise LifecycleError("RUNTIME_POINTER_INVALID", "runtime pointer is invalid")
    return _decode_pointer(raw, runtime_parent)


def activate_pointer(
    pointer: Path,
    candidate: RuntimeRecord,
    *,
    runtime_parent: Path,
    transaction_path: Path,
    rollback_path: Path,
) -> bool:
    """Atomically activate a runtime and persist a byte-exact one-level rollback."""

    candidate.validate(runtime_parent)
    recover_pointer_transaction(
        pointer,
        runtime_parent=runtime_parent,
        transaction_path=transaction_path,
        rollback_path=rollback_path,
    )
    before = _read_optional_pointer_bytes(pointer, runtime_parent)
    after = _encode_pointer(candidate)
    if before == after:
        return False
    transaction = _pointer_transaction("activate", before=before, after=after)
    _atomic_write_bytes(transaction_path, _encode_json(transaction))
    _atomic_write_bytes(pointer, after)
    _finalize_pointer_transaction(transaction_path, rollback_path)
    return True


def rollback_pointer(
    pointer: Path,
    *,
    runtime_parent: Path,
    transaction_path: Path,
    rollback_path: Path,
) -> bool:
    recover_pointer_transaction(
        pointer,
        runtime_parent=runtime_parent,
        transaction_path=transaction_path,
        rollback_path=rollback_path,
    )
    if not rollback_path.exists():
        return False
    record = _read_rollback_record(rollback_path)
    if record.get("available") is not True:
        return False
    before = _read_optional_pointer_bytes(pointer, runtime_parent)
    if before is None or _sha256_bytes(before) != record.get("current_sha256"):
        raise LifecycleError(
            "RECOVERY_REQUIRED",
            "active pointer no longer matches rollback ownership",
        )
    previous = _decode_base64_bytes(record.get("previous"), "rollback pointer")
    if _sha256_bytes(previous) != record.get("previous_sha256"):
        raise LifecycleError("RECOVERY_REQUIRED", "rollback pointer digest changed")
    _decode_pointer(previous, runtime_parent)
    transaction = _pointer_transaction("rollback", before=before, after=previous)
    _atomic_write_bytes(transaction_path, _encode_json(transaction))
    _atomic_write_bytes(pointer, previous)
    _finalize_pointer_transaction(transaction_path, rollback_path)
    return True


def read_rollback_target(
    pointer: Path,
    *,
    runtime_parent: Path,
    rollback_path: Path,
) -> RuntimeRecord | None:
    if not rollback_path.exists():
        return None
    record = _read_rollback_record(rollback_path)
    if record.get("available") is not True:
        return None
    current = _read_optional_pointer_bytes(pointer, runtime_parent)
    if current is None or _sha256_bytes(current) != record.get("current_sha256"):
        raise LifecycleError(
            "RECOVERY_REQUIRED",
            "active pointer no longer matches rollback ownership",
        )
    previous = _decode_base64_bytes(record.get("previous"), "rollback pointer")
    if _sha256_bytes(previous) != record.get("previous_sha256"):
        raise LifecycleError("RECOVERY_REQUIRED", "rollback pointer digest changed")
    return _decode_pointer(previous, runtime_parent)


def recover_pointer_transaction(
    pointer: Path,
    *,
    runtime_parent: Path,
    transaction_path: Path,
    rollback_path: Path,
) -> bool:
    if not transaction_path.exists():
        return False
    transaction = _read_pointer_transaction(transaction_path)
    before = _decode_optional_base64_bytes(transaction.get("before"), "prior pointer")
    after = _decode_base64_bytes(transaction.get("after"), "candidate pointer")
    _decode_pointer(after, runtime_parent)
    if before is not None:
        _decode_pointer(before, runtime_parent)
    current = _read_optional_pointer_bytes(pointer, runtime_parent)
    if current == after:
        _finalize_pointer_transaction(transaction_path, rollback_path)
        return True
    if current == before:
        transaction_path.unlink()
        _fsync_directory(transaction_path.parent)
        return True
    raise LifecycleError(
        "RECOVERY_REQUIRED",
        "runtime pointer differs from both sides of an interrupted switch",
    )


def render_windows_launcher(launcher_path: Path) -> bytes:
    """Return a PowerShell 5.1 launcher with no dynamic command construction."""

    resolved = launcher_path.resolve(strict=False)
    if resolved.parent.name.casefold() != "bin" or resolved.name.casefold() != (
        "subagent-harness-mcp.ps1"
    ):
        raise LifecycleError("LAUNCHER_INVALID", "launcher path is not the stable path")
    text = """$ErrorActionPreference = 'Stop'
$dataRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runtimesRoot = [IO.Path]::GetFullPath((Join-Path $dataRoot 'runtimes'))
$pointerPath = Join-Path $dataRoot 'current.json'
$pointer = Get-Content -Raw -Encoding UTF8 -LiteralPath $pointerPath | ConvertFrom-Json
if ($pointer.schema_version -ne 1) { throw 'Unsupported Subagent MCP pointer schema.' }
$rootName = [string]$pointer.root
if ([string]::IsNullOrWhiteSpace($rootName) -or $rootName -in @('.', '..') -or [IO.Path]::GetFileName($rootName) -ne $rootName) { throw 'Invalid Subagent MCP runtime root.' }
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $runtimesRoot $rootName))
if ([IO.Directory]::GetParent($runtimeRoot).FullName -ne $runtimesRoot) { throw 'Subagent MCP runtime escapes its owned root.' }
$manifestPath = Join-Path $runtimeRoot 'manifest.json'
$manifestDigest = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifestDigest -ne ([string]$pointer.manifest_sha256).ToLowerInvariant()) { throw 'Subagent MCP runtime manifest digest mismatch.' }
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
$pythonExe = Join-Path $runtimeRoot 'Scripts\\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw 'Subagent MCP staged Python is missing.' }
$pythonDigest = (Get-FileHash -LiteralPath $pythonExe -Algorithm SHA256).Hash.ToLowerInvariant()
$expectedPythonDigest = ([string]$manifest.files.'Scripts/python.exe').ToLowerInvariant()
if ($pythonDigest -ne $expectedPythonDigest) { throw 'Subagent MCP staged Python digest mismatch.' }
$fixedArgs = @('-I', '-B', '-m', 'subagent_harness_mcp.cli', 'serve')
& $pythonExe $fixedArgs
exit [int]$LASTEXITCODE
""".replace("\n", "\r\n")
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def windows_registration_argv(launcher_path: Path) -> tuple[str, ...]:
    resolved = launcher_path.resolve(strict=False)
    return (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(resolved),
    )


def stop_verified_process(expected: ProcessIdentity, backend: ProcessBackend) -> bool:
    if not isinstance(expected, ProcessIdentity):
        raise LifecycleError("RECOVERY_REQUIRED", "expected process identity is missing")
    expected.validate()
    observed = backend.observe(expected.pid)
    if observed is None:
        return False
    if not isinstance(observed, ProcessIdentity):
        raise LifecycleError("RECOVERY_REQUIRED", "observed process identity is invalid")
    observed.validate()
    if observed != expected:
        raise LifecycleError(
            "RECOVERY_REQUIRED",
            "PID was reused or process identity changed; nothing was terminated",
        )
    backend.terminate(expected)
    remaining = backend.observe(expected.pid)
    if remaining is None:
        return True
    if not isinstance(remaining, ProcessIdentity):
        raise LifecycleError("RECOVERY_REQUIRED", "process stop could not be verified")
    remaining.validate()
    raise LifecycleError(
        "RECOVERY_REQUIRED",
        "process remains present or its PID was reused after termination",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LifecycleError("RESOURCE_READ_FAILED", "owned resource cannot be hashed") from exc
    return digest.hexdigest()


def bytes_sha256(payload: bytes) -> str:
    return _sha256_bytes(payload)


def _load_and_validate_manifest(
    root: Path,
    *,
    expected_version: str,
) -> dict[str, object]:
    _require_version(expected_version)
    if root.is_symlink() or not root.is_dir():
        raise LifecycleError("RUNTIME_INVALID", "runtime root is not a directory")
    manifest_path = root / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest cannot be read") from exc
    if manifest_path.is_symlink() or len(raw) > _MAX_MANIFEST_BYTES:
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest is invalid")
    manifest = _decode_json_object(raw, code="RUNTIME_INVALID", label="runtime manifest")
    if manifest.get("schema_version") != 1 or manifest.get("version") != expected_version:
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest identity is invalid")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest files are invalid")
    files: dict[str, str] = {}
    seen_casefolded: set[str] = set()
    for relative, digest in raw_files.items():
        normalized = _validate_manifest_path(relative)
        folded = normalized.casefold()
        if folded in seen_casefolded:
            raise LifecycleError("RUNTIME_INVALID", "runtime paths collide on Windows")
        seen_casefolded.add(folded)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise LifecycleError("RUNTIME_INVALID", "runtime file digest is invalid")
        files[normalized] = digest
    if _WINDOWS_PYTHON not in files:
        raise LifecycleError("RUNTIME_INVALID", "runtime Python is absent from manifest")
    actual: set[str] = set()
    try:
        entries = list(root.rglob("*"))
    except OSError as exc:
        raise LifecycleError("RUNTIME_INVALID", "runtime tree cannot be listed") from exc
    for path in entries:
        if path.is_symlink():
            raise LifecycleError("RUNTIME_INVALID", "runtime contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise LifecycleError("RUNTIME_INVALID", "runtime contains a special file")
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        normalized = _validate_manifest_path(relative)
        actual.add(normalized)
        expected_digest = files.get(normalized)
        if expected_digest is None or file_sha256(path) != expected_digest:
            raise LifecycleError("RUNTIME_INVALID", "runtime file digest changed")
    if actual != set(files):
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest and tree differ")
    result = dict(manifest)
    result["files"] = files
    return result


def _validate_manifest_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest path is invalid")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest path escapes its root")
    normalized = path.as_posix()
    if len(normalized.encode("utf-8")) > 4096:
        raise LifecycleError("RUNTIME_INVALID", "runtime manifest path is too long")
    return normalized


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
    if len(raw) > _MAX_POINTER_BYTES:
        raise LifecycleError("RUNTIME_POINTER_INVALID", "runtime pointer is too large")
    value = _decode_json_object(
        raw,
        code="RUNTIME_POINTER_INVALID",
        label="runtime pointer",
    )
    if value.get("schema_version") != 1:
        raise LifecycleError("RUNTIME_POINTER_INVALID", "pointer schema is unsupported")
    root_name = value.get("root")
    if (
        not isinstance(root_name, str)
        or not root_name
        or root_name in {".", ".."}
        or "/" in root_name
        or "\\" in root_name
        or ":" in root_name
        or Path(root_name).name != root_name
    ):
        raise LifecycleError("RUNTIME_POINTER_INVALID", "pointer root is invalid")
    record = RuntimeRecord(
        version=value.get("version"),
        root=runtime_parent / root_name,
        manifest_sha256=value.get("manifest_sha256"),
    )
    record.validate(runtime_parent)
    return record


def _read_optional_pointer_bytes(pointer: Path, runtime_parent: Path) -> bytes | None:
    try:
        raw = pointer.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LifecycleError("RUNTIME_POINTER_INVALID", "pointer cannot be read") from exc
    if pointer.is_symlink():
        raise LifecycleError("RUNTIME_POINTER_INVALID", "pointer cannot be a symlink")
    _decode_pointer(raw, runtime_parent)
    return raw


def _pointer_transaction(
    operation: str,
    *,
    before: bytes | None,
    after: bytes,
) -> dict[str, object]:
    if operation not in {"activate", "rollback"}:
        raise LifecycleError("RECOVERY_REQUIRED", "pointer operation is invalid")
    return {
        "after": base64.b64encode(after).decode("ascii"),
        "after_sha256": _sha256_bytes(after),
        "before": None if before is None else base64.b64encode(before).decode("ascii"),
        "before_sha256": None if before is None else _sha256_bytes(before),
        "operation": operation,
        "schema_version": 1,
    }


def _read_pointer_transaction(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LifecycleError("RECOVERY_REQUIRED", "pointer transaction cannot be read") from exc
    value = _decode_json_object(raw, code="RECOVERY_REQUIRED", label="pointer transaction")
    if value.get("schema_version") != 1 or value.get("operation") not in {
        "activate",
        "rollback",
    }:
        raise LifecycleError("RECOVERY_REQUIRED", "pointer transaction is invalid")
    before = _decode_optional_base64_bytes(value.get("before"), "prior pointer")
    after = _decode_base64_bytes(value.get("after"), "candidate pointer")
    if value.get("before_sha256") != (
        None if before is None else _sha256_bytes(before)
    ) or value.get("after_sha256") != _sha256_bytes(after):
        raise LifecycleError("RECOVERY_REQUIRED", "pointer transaction digest changed")
    return value


def _finalize_pointer_transaction(transaction_path: Path, rollback_path: Path) -> None:
    transaction = _read_pointer_transaction(transaction_path)
    operation = transaction["operation"]
    after = _decode_base64_bytes(transaction.get("after"), "candidate pointer")
    if operation == "activate" and transaction.get("before") is not None:
        before = _decode_base64_bytes(transaction.get("before"), "prior pointer")
        rollback = {
            "available": True,
            "current_sha256": _sha256_bytes(after),
            "previous": base64.b64encode(before).decode("ascii"),
            "previous_sha256": _sha256_bytes(before),
            "schema_version": 1,
        }
    else:
        rollback = {
            "available": False,
            "current_sha256": _sha256_bytes(after),
            "schema_version": 1,
        }
    _atomic_write_bytes(rollback_path, _encode_json(rollback))
    transaction_path.unlink()
    _fsync_directory(transaction_path.parent)


def _read_rollback_record(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LifecycleError("RECOVERY_REQUIRED", "rollback record cannot be read") from exc
    value = _decode_json_object(raw, code="RECOVERY_REQUIRED", label="rollback record")
    if value.get("schema_version") != 1 or type(value.get("available")) is not bool:
        raise LifecycleError("RECOVERY_REQUIRED", "rollback record is invalid")
    current_digest = value.get("current_sha256")
    if not isinstance(current_digest, str) or _SHA256.fullmatch(current_digest) is None:
        raise LifecycleError("RECOVERY_REQUIRED", "rollback current digest is invalid")
    if value["available"] is True:
        previous_digest = value.get("previous_sha256")
        previous = _decode_base64_bytes(value.get("previous"), "rollback pointer")
        if (
            not isinstance(previous_digest, str)
            or _SHA256.fullmatch(previous_digest) is None
            or _sha256_bytes(previous) != previous_digest
        ):
            raise LifecycleError("RECOVERY_REQUIRED", "rollback record digest changed")
    return value


def _decode_optional_base64_bytes(value: object, label: str) -> bytes | None:
    if value is None:
        return None
    return _decode_base64_bytes(value, label)


def _decode_base64_bytes(value: object, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > _MAX_POINTER_BYTES * 2:
        raise LifecycleError("RECOVERY_REQUIRED", f"{label} is invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise LifecycleError("RECOVERY_REQUIRED", f"{label} is invalid") from exc


def _decode_json_object(raw: bytes, *, code: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_reject_duplicates)
    except (UnicodeError, json.JSONDecodeError, _DuplicateKeyError) as exc:
        raise LifecycleError(code, f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise LifecycleError(code, f"{label} must be an object")
    return value


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _encode_json(value: object) -> bytes:
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
        raise LifecycleError("RESOURCE_INVALID", "value is not canonical JSON") from exc


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        raise LifecycleError("RESOURCE_WRITE_FAILED", "destination parent is missing")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_tree_directories(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


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


def _require_version(value: object) -> None:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise LifecycleError("RUNTIME_INVALID", "runtime version is invalid")


def _require_text(value: object, label: str, max_bytes: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > max_bytes
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise LifecycleError("RESOURCE_INVALID", f"{label} is invalid")


def _require_journal_text(value: object, label: str, max_bytes: int) -> None:
    try:
        _require_text(value, label, max_bytes)
    except LifecycleError as exc:
        raise LifecycleError("OWNERSHIP_JOURNAL_CORRUPT", str(exc)) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )
