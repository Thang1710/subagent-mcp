from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from .launcher import (
    LifecycleError,
    OwnedResource,
    OwnershipJournal,
    ProcessBackend,
    ProcessIdentity,
    RuntimeRecord,
    activate_pointer,
    bytes_sha256,
    file_sha256,
    inspect_runtime_source,
    read_pointer,
    read_rollback_target,
    recover_pointer_transaction,
    render_windows_launcher,
    rollback_pointer,
    stage_runtime,
    stop_verified_process,
    windows_registration_argv,
)
from .paths import ProductPaths


_LOCK_TIMEOUT_SECONDS = 5.0
_PROCESS_LIFECYCLE_LOCK = threading.Lock()


class HealthBackend(Protocol):
    def check(self, runtime: RuntimeRecord) -> bool: ...


class RegistrationBackend(Protocol):
    def readback(self, client_id: str) -> tuple[str, ...] | None: ...

    def register(self, client_id: str, argv: tuple[str, ...]) -> None: ...

    def unregister(self, client_id: str) -> None: ...


class SubprocessHealthBackend:
    """Run only the staged package version command with a fixed argv array."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds

    def check(self, runtime: RuntimeRecord) -> bool:
        python = runtime.root / "Scripts" / "python.exe"
        try:
            completed = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-B",
                    "-m",
                    "subagent_harness_mcp.cli",
                    "--version",
                ],
                cwd=runtime.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0


class CodexRegistrationBackend:
    """Use only Codex's public MCP lifecycle commands and JSON readback."""

    def __init__(self, executable: str = "codex") -> None:
        _require_text(executable, "Codex executable", 4096)
        self.executable = executable

    def readback(self, client_id: str) -> tuple[str, ...] | None:
        completed = self._run(("mcp", "get", client_id, "--json"))
        if completed.returncode != 0:
            diagnostic = (completed.stderr + completed.stdout).decode(
                "utf-8",
                errors="replace",
            )[:4096]
            folded = diagnostic.strip().casefold()
            current_missing = (
                f"error: no mcp server named '{client_id}' found.".casefold()
            )
            if (
                folded == current_missing
                or "not found" in folded
                or "does not exist" in folded
            ):
                return None
            raise LifecycleError(
                "REGISTRATION_READBACK_FAILED",
                "Codex MCP registration readback failed",
            )
        try:
            document = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LifecycleError(
                "REGISTRATION_READBACK_FAILED",
                "Codex MCP registration readback is malformed",
            ) from exc
        if not isinstance(document, dict):
            raise LifecycleError(
                "REGISTRATION_READBACK_FAILED",
                "Codex MCP registration readback is invalid",
            )
        if "name" in document and document["name"] != client_id:
            raise LifecycleError(
                "REGISTRATION_READBACK_FAILED",
                "Codex MCP registration identity changed",
            )
        transport = document.get("transport")
        source = transport if isinstance(transport, dict) else document
        if isinstance(source.get("stdio"), dict):
            source = source["stdio"]
        command = source.get("command")
        arguments = source.get("args", [])
        if (
            not isinstance(command, str)
            or not command
            or not isinstance(arguments, list)
            or not all(isinstance(value, str) for value in arguments)
        ):
            raise LifecycleError(
                "REGISTRATION_READBACK_FAILED",
                "Codex MCP stdio registration is invalid",
            )
        return (command, *arguments)

    def register(self, client_id: str, argv: tuple[str, ...]) -> None:
        completed = self._run(("mcp", "add", client_id, "--", *argv))
        if completed.returncode != 0:
            raise LifecycleError("REGISTRATION_FAILED", "Codex MCP add failed")

    def unregister(self, client_id: str) -> None:
        completed = self._run(("mcp", "remove", client_id))
        if completed.returncode != 0:
            raise LifecycleError("REGISTRATION_FAILED", "Codex MCP remove failed")

    def _run(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30.0,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LifecycleError(
                "REGISTRATION_FAILED",
                "Codex MCP lifecycle command could not run",
            ) from exc


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    operation: str
    changed: bool
    dry_run: bool
    recovery_required: bool
    actions: tuple[str, ...]


class WindowsLifecycleManager:
    """Own the Windows preview's immutable runtime and conservative lifecycle."""

    def __init__(self, paths: ProductPaths) -> None:
        self.paths = paths
        self.runtimes_dir = paths.data_dir / "runtimes"
        self.bin_dir = paths.data_dir / "bin"
        self.launcher_path = self.bin_dir / "subagent-harness-mcp.ps1"
        self.pointer_path = paths.data_dir / "current.json"
        self.pointer_transaction_path = paths.data_dir / "current.transaction.json"
        self.rollback_path = paths.data_dir / "rollback.json"
        self.lock_path = paths.data_dir / "lifecycle.lock"
        self.ownership = OwnershipJournal(paths.data_dir / "ownership.jsonl")

    def current_runtime(self) -> RuntimeRecord:
        if self.pointer_transaction_path.exists():
            raise LifecycleError(
                "RECOVERY_REQUIRED",
                "an interrupted pointer switch must be recovered first",
            )
        return read_pointer(self.pointer_path, self.runtimes_dir)

    def install(
        self,
        source: Path,
        *,
        version: str,
        health: HealthBackend,
        dry_run: bool = False,
    ) -> LifecycleResult:
        inspect_runtime_source(source, version=version)
        actions = ("stage_runtime", "write_launcher", "activate_runtime")
        if dry_run:
            return _result("install", changed=True, dry_run=True, actions=actions)
        self._ensure_layout()
        with self._locked():
            self._recover_locked()
            active = self.ownership.active_resources()
            if self.pointer_path.exists():
                current = read_pointer(self.pointer_path, self.runtimes_dir)
                if current.version != version:
                    raise LifecycleError(
                        "ALREADY_INSTALLED",
                        "another runtime is active; use update instead of install",
                    )
                source_digest = inspect_runtime_source(source, version=version)
                if current.manifest_sha256 != source_digest:
                    raise LifecycleError(
                        "RUNTIME_CONFLICT",
                        "active version has different immutable content",
                    )
                self._require_owned_runtime(current, active)
                self._require_owned_launcher(active)
                return _result("install", changed=False)

            runtime, staged = stage_runtime(source, self.runtimes_dir, version=version)
            runtime_id = _runtime_resource_id(version)
            existing_runtime = active.get(runtime_id)
            if staged:
                self.ownership.record_owned(
                    resource_id=runtime_id,
                    kind="runtime",
                    locator=str(runtime.root.resolve(strict=True)),
                    digest=runtime.manifest_sha256,
                )
            elif existing_runtime is None:
                raise LifecycleError(
                    "OWNERSHIP_CONFLICT",
                    "an unowned runtime already occupies the version path",
                )
            else:
                self._require_owned_runtime(runtime, active)
            self._check_health(runtime, health)
            launcher_changed = self._ensure_launcher(self.ownership.active_resources())
            pointer_changed = self._activate_runtime(runtime)
            return _result(
                "install",
                changed=staged or launcher_changed or pointer_changed,
                actions=actions,
            )

    def update(
        self,
        source: Path,
        *,
        version: str,
        health: HealthBackend,
        dry_run: bool = False,
    ) -> LifecycleResult:
        source_digest = inspect_runtime_source(source, version=version)
        actions = ("stage_runtime", "health_check", "switch_pointer")
        if dry_run:
            return _result("update", changed=True, dry_run=True, actions=actions)
        self._require_existing_data_root()
        with self._locked():
            self._recover_locked()
            active = self.ownership.active_resources()
            self._require_owned_launcher(active)
            current = read_pointer(self.pointer_path, self.runtimes_dir)
            if current.version == version:
                if current.manifest_sha256 != source_digest:
                    raise LifecycleError(
                        "RUNTIME_CONFLICT",
                        "active version has different immutable content",
                    )
                self._require_owned_runtime(current, active)
                return _result("update", changed=False)

            runtime, staged = stage_runtime(source, self.runtimes_dir, version=version)
            runtime_id = _runtime_resource_id(version)
            existing_runtime = active.get(runtime_id)
            if staged:
                self.ownership.record_owned(
                    resource_id=runtime_id,
                    kind="runtime",
                    locator=str(runtime.root.resolve(strict=True)),
                    digest=runtime.manifest_sha256,
                )
            elif existing_runtime is None:
                raise LifecycleError(
                    "OWNERSHIP_CONFLICT",
                    "an unowned runtime already occupies the version path",
                )
            else:
                self._require_owned_runtime(runtime, active)
            self._check_health(runtime, health)
            try:
                changed = self._activate_runtime(runtime)
            except OSError as exc:
                raise LifecycleError(
                    "RECOVERY_REQUIRED",
                    "runtime pointer activation was interrupted",
                ) from exc
            return _result("update", changed=changed, actions=actions)

    def rollback(
        self,
        *,
        health: HealthBackend,
        dry_run: bool = False,
    ) -> LifecycleResult:
        actions = ("health_check_previous", "restore_pointer")
        if dry_run:
            if not self.paths.data_dir.exists():
                return _result("rollback", changed=False, dry_run=True)
            target = read_rollback_target(
                self.pointer_path,
                runtime_parent=self.runtimes_dir,
                rollback_path=self.rollback_path,
            )
            return _result(
                "rollback",
                changed=target is not None,
                dry_run=True,
                actions=actions if target is not None else (),
            )
        self._require_existing_data_root()
        with self._locked():
            self._recover_locked()
            target = read_rollback_target(
                self.pointer_path,
                runtime_parent=self.runtimes_dir,
                rollback_path=self.rollback_path,
            )
            if target is None:
                return _result("rollback", changed=False)
            self._require_owned_runtime(target, self.ownership.active_resources())
            self._check_health(target, health)
            changed = rollback_pointer(
                self.pointer_path,
                runtime_parent=self.runtimes_dir,
                transaction_path=self.pointer_transaction_path,
                rollback_path=self.rollback_path,
            )
            if changed:
                self._record_pointer_resources()
            return _result("rollback", changed=changed, actions=actions)

    def recover(self, *, dry_run: bool = False) -> LifecycleResult:
        if not self.paths.data_dir.exists():
            return _result("recover", changed=False, dry_run=dry_run)
        if dry_run:
            return _result(
                "recover",
                changed=self.pointer_transaction_path.exists(),
                dry_run=True,
                actions=("recover_pointer",)
                if self.pointer_transaction_path.exists()
                else (),
            )
        with self._locked():
            changed = self._recover_locked()
            return _result(
                "recover",
                changed=changed,
                actions=("recover_pointer",) if changed else (),
            )

    def register(
        self,
        client_id: str,
        *,
        backend: RegistrationBackend,
        dry_run: bool = False,
    ) -> LifecycleResult:
        _require_text(client_id, "client id", 128)
        actions = ("register_official_command", "readback_registration")
        if dry_run:
            return _result("register", changed=True, dry_run=True, actions=actions)
        self._require_existing_data_root()
        with self._locked():
            self._recover_locked()
            active = self.ownership.active_resources()
            self._require_owned_launcher(active)
            expected = windows_registration_argv(self.launcher_path)
            digest = _argv_digest(expected)
            resource_id = _registration_resource_id(client_id)
            owned = active.get(resource_id)
            before = _read_registration(backend, client_id)
            if owned is not None:
                if (
                    owned.kind != "registration"
                    or owned.locator != client_id
                    or owned.digest != digest
                    or before != expected
                ):
                    raise LifecycleError(
                        "REGISTRATION_READBACK_MISMATCH",
                        "owned client registration no longer matches",
                    )
                return _result("register", changed=False)
            if before is not None:
                raise LifecycleError(
                    "OWNERSHIP_CONFLICT",
                    "client registration already exists and is not owned",
                )
            try:
                backend.register(client_id, expected)
                observed = _read_registration(backend, client_id)
            except Exception as exc:
                raise LifecycleError(
                    "REGISTRATION_FAILED",
                    "official client registration failed",
                ) from exc
            if observed != expected:
                try:
                    backend.unregister(client_id)
                    cleaned = _read_registration(backend, client_id)
                except Exception as exc:
                    raise LifecycleError(
                        "RECOVERY_REQUIRED",
                        "registration readback failed and cleanup was not verified",
                    ) from exc
                if cleaned is not None:
                    raise LifecycleError(
                        "RECOVERY_REQUIRED",
                        "registration readback failed and cleanup was not verified",
                    )
                raise LifecycleError(
                    "REGISTRATION_READBACK_MISMATCH",
                    "official registration did not read back exactly",
                )
            self.ownership.record_owned(
                resource_id=resource_id,
                kind="registration",
                locator=client_id,
                digest=digest,
            )
            return _result("register", changed=True, actions=actions)

    def uninstall(
        self,
        *,
        registration_backend: RegistrationBackend | None = None,
        process_identity: ProcessIdentity | None = None,
        process_backend: ProcessBackend | None = None,
        dry_run: bool = False,
    ) -> LifecycleResult:
        if not self.paths.data_dir.exists():
            return _result("uninstall", changed=False, dry_run=dry_run)
        with self._locked():
            try:
                self._recover_locked()
                active = self.ownership.active_resources()
                if not active:
                    return _result("uninstall", changed=False, dry_run=dry_run)
                self._preflight_uninstall(
                    active,
                    registration_backend=registration_backend,
                    process_identity=process_identity,
                    process_backend=process_backend,
                )
            except LifecycleError:
                return _result(
                    "uninstall",
                    changed=False,
                    dry_run=dry_run,
                    recovery_required=True,
                    actions=("manual_recovery_required",),
                )
            actions = tuple(
                ["stop_verified_process"]
                if process_identity is not None
                else []
            ) + ("remove_exact_owned_resources", "preserve_config_state_sessions")
            if dry_run:
                return _result(
                    "uninstall",
                    changed=True,
                    dry_run=True,
                    actions=actions,
                )
            changed = False
            if process_identity is not None:
                assert process_backend is not None
                try:
                    changed = stop_verified_process(process_identity, process_backend) or changed
                except LifecycleError:
                    return _result(
                        "uninstall",
                        changed=changed,
                        recovery_required=True,
                        actions=("manual_recovery_required",),
                    )

            registrations = sorted(
                (resource for resource in active.values() if resource.kind == "registration"),
                key=lambda resource: resource.resource_id,
            )
            for resource in registrations:
                assert registration_backend is not None
                try:
                    registration_backend.unregister(resource.locator)
                    if _read_registration(registration_backend, resource.locator) is not None:
                        raise LifecycleError(
                            "RECOVERY_REQUIRED",
                            "client registration removal did not read back",
                        )
                    self.ownership.record_removed(resource_id=resource.resource_id)
                    changed = True
                except Exception:
                    return _result(
                        "uninstall",
                        changed=changed,
                        recovery_required=True,
                        actions=("manual_recovery_required",),
                    )

            runtimes = sorted(
                (resource for resource in active.values() if resource.kind == "runtime"),
                key=lambda resource: resource.resource_id,
            )
            for resource in runtimes:
                try:
                    shutil.rmtree(Path(resource.locator))
                    self.ownership.record_removed(resource_id=resource.resource_id)
                    changed = True
                except OSError:
                    return _result(
                        "uninstall",
                        changed=changed,
                        recovery_required=True,
                        actions=("manual_recovery_required",),
                    )

            files = sorted(
                (resource for resource in active.values() if resource.kind == "file"),
                key=_file_removal_order,
            )
            for resource in files:
                try:
                    Path(resource.locator).unlink()
                    self.ownership.record_removed(resource_id=resource.resource_id)
                    changed = True
                except OSError:
                    return _result(
                        "uninstall",
                        changed=changed,
                        recovery_required=True,
                        actions=("manual_recovery_required",),
                    )
            return _result("uninstall", changed=changed, actions=actions)

    def _ensure_layout(self) -> None:
        try:
            self.runtimes_dir.mkdir(parents=True, exist_ok=True)
            self.bin_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LifecycleError("INSTALL_FAILED", "product data root cannot be created") from exc

    def _require_existing_data_root(self) -> None:
        if not self.paths.data_dir.is_dir() or not self.runtimes_dir.is_dir():
            raise LifecycleError("INSTALL_REQUIRED", "Subagent MCP is not installed")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with _PROCESS_LIFECYCLE_LOCK:
            try:
                with _exclusive_file_lock(
                    self.lock_path,
                    timeout_seconds=_LOCK_TIMEOUT_SECONDS,
                ):
                    yield
            except TimeoutError as exc:
                raise LifecycleError("LIFECYCLE_BUSY", "lifecycle command is already running") from exc

    def _recover_locked(self) -> bool:
        changed = recover_pointer_transaction(
            self.pointer_path,
            runtime_parent=self.runtimes_dir,
            transaction_path=self.pointer_transaction_path,
            rollback_path=self.rollback_path,
        )
        if changed:
            self._record_pointer_resources()
        return changed

    def _ensure_launcher(self, active: Mapping[str, OwnedResource]) -> bool:
        expected = render_windows_launcher(self.launcher_path)
        expected_digest = bytes_sha256(expected)
        owned = active.get("launcher:windows")
        if self.launcher_path.exists():
            if (
                owned is None
                or owned.kind != "file"
                or Path(owned.locator).resolve(strict=False)
                != self.launcher_path.resolve(strict=False)
                or owned.digest != expected_digest
                or file_sha256(self.launcher_path) != expected_digest
            ):
                raise LifecycleError(
                    "OWNERSHIP_CONFLICT",
                    "stable launcher exists but exact ownership does not match",
                )
            return False
        if owned is not None:
            raise LifecycleError(
                "RECOVERY_REQUIRED",
                "owned stable launcher is missing",
            )
        _atomic_write_bytes(self.launcher_path, expected)
        self.ownership.record_owned(
            resource_id="launcher:windows",
            kind="file",
            locator=str(self.launcher_path.resolve(strict=True)),
            digest=expected_digest,
        )
        return True

    def _activate_runtime(self, runtime: RuntimeRecord) -> bool:
        changed = activate_pointer(
            self.pointer_path,
            runtime,
            runtime_parent=self.runtimes_dir,
            transaction_path=self.pointer_transaction_path,
            rollback_path=self.rollback_path,
        )
        if changed:
            self._record_pointer_resources()
        return changed

    def _record_pointer_resources(self) -> None:
        if self.pointer_path.is_file():
            self.ownership.record_owned(
                resource_id="runtime:pointer",
                kind="file",
                locator=str(self.pointer_path.resolve(strict=True)),
                digest=file_sha256(self.pointer_path),
            )
        if self.rollback_path.is_file():
            self.ownership.record_owned(
                resource_id="runtime:rollback",
                kind="file",
                locator=str(self.rollback_path.resolve(strict=True)),
                digest=file_sha256(self.rollback_path),
            )

    def _require_owned_launcher(self, active: Mapping[str, OwnedResource]) -> None:
        owned = active.get("launcher:windows")
        expected = render_windows_launcher(self.launcher_path)
        expected_digest = bytes_sha256(expected)
        if (
            owned is None
            or owned.kind != "file"
            or Path(owned.locator).resolve(strict=False)
            != self.launcher_path.resolve(strict=False)
            or owned.digest != expected_digest
            or not self.launcher_path.is_file()
            or file_sha256(self.launcher_path) != expected_digest
        ):
            raise LifecycleError(
                "RECOVERY_REQUIRED",
                "stable launcher ownership or content changed",
            )

    def _require_owned_runtime(
        self,
        runtime: RuntimeRecord,
        active: Mapping[str, OwnedResource],
    ) -> None:
        owned = active.get(_runtime_resource_id(runtime.version))
        if (
            owned is None
            or owned.kind != "runtime"
            or Path(owned.locator).resolve(strict=False)
            != runtime.root.resolve(strict=False)
            or owned.digest != runtime.manifest_sha256
        ):
            raise LifecycleError("RECOVERY_REQUIRED", "runtime ownership changed")
        runtime.validate(self.runtimes_dir)

    def _check_health(self, runtime: RuntimeRecord, health: HealthBackend) -> None:
        try:
            healthy = health.check(runtime)
        except Exception as exc:
            raise LifecycleError("HEALTH_CHECK_FAILED", "runtime health check failed") from exc
        if healthy is not True:
            raise LifecycleError("HEALTH_CHECK_FAILED", "runtime health check failed")

    def _preflight_uninstall(
        self,
        active: Mapping[str, OwnedResource],
        *,
        registration_backend: RegistrationBackend | None,
        process_identity: ProcessIdentity | None,
        process_backend: ProcessBackend | None,
    ) -> None:
        if process_identity is not None:
            if process_backend is None:
                raise LifecycleError("RECOVERY_REQUIRED", "process backend is missing")
            process_identity.validate()
            observed = process_backend.observe(process_identity.pid)
            if observed is not None:
                if not isinstance(observed, ProcessIdentity):
                    raise LifecycleError("RECOVERY_REQUIRED", "process identity is invalid")
                observed.validate()
                if observed != process_identity:
                    raise LifecycleError("RECOVERY_REQUIRED", "process identity changed")
        elif process_backend is not None:
            raise LifecycleError("RECOVERY_REQUIRED", "expected process identity is missing")

        data_root = self.paths.data_dir.resolve(strict=True)
        for resource in active.values():
            if resource.kind == "registration":
                if registration_backend is None:
                    raise LifecycleError(
                        "RECOVERY_REQUIRED",
                        "owned registration needs its official backend",
                    )
                observed = _read_registration(registration_backend, resource.locator)
                if observed is None or _argv_digest(observed) != resource.digest:
                    raise LifecycleError(
                        "RECOVERY_REQUIRED",
                        "owned registration readback changed",
                    )
                continue
            path = Path(resource.locator).resolve(strict=False)
            try:
                path.relative_to(data_root)
            except ValueError as exc:
                raise LifecycleError(
                    "RECOVERY_REQUIRED",
                    "owned resource escapes the product data root",
                ) from exc
            if resource.kind == "runtime":
                if not resource.resource_id.startswith("runtime:version:"):
                    raise LifecycleError("RECOVERY_REQUIRED", "runtime resource id is invalid")
                version = resource.resource_id.removeprefix("runtime:version:")
                record = RuntimeRecord(version, path, resource.digest)
                record.validate(self.runtimes_dir)
                continue
            if resource.kind == "file":
                expected_paths = {
                    "launcher:windows": self.launcher_path,
                    "runtime:pointer": self.pointer_path,
                    "runtime:rollback": self.rollback_path,
                }
                expected = expected_paths.get(resource.resource_id)
                if (
                    expected is None
                    or path != expected.resolve(strict=False)
                    or not path.is_file()
                    or path.is_symlink()
                    or file_sha256(path) != resource.digest
                ):
                    raise LifecycleError(
                        "RECOVERY_REQUIRED",
                        "owned file identity or digest changed",
                    )
                continue
            raise LifecycleError("RECOVERY_REQUIRED", "owned resource kind is unknown")


def _result(
    operation: str,
    *,
    changed: bool,
    dry_run: bool = False,
    recovery_required: bool = False,
    actions: tuple[str, ...] = (),
) -> LifecycleResult:
    return LifecycleResult(
        operation=operation,
        changed=changed,
        dry_run=dry_run,
        recovery_required=recovery_required,
        actions=actions,
    )


def _runtime_resource_id(version: str) -> str:
    return f"runtime:version:{version}"


def _registration_resource_id(client_id: str) -> str:
    return f"registration:{client_id}"


def _argv_digest(argv: tuple[str, ...]) -> str:
    payload = (
        json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_registration(
    backend: RegistrationBackend,
    client_id: str,
) -> tuple[str, ...] | None:
    try:
        value = backend.readback(client_id)
    except Exception as exc:
        raise LifecycleError(
            "REGISTRATION_READBACK_FAILED",
            "official registration readback failed",
        ) from exc
    if value is None:
        return None
    if not isinstance(value, tuple) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise LifecycleError(
            "REGISTRATION_READBACK_FAILED",
            "official registration readback is invalid",
        )
    return value


def _file_removal_order(resource: OwnedResource) -> tuple[int, str]:
    order = {
        "runtime:rollback": 0,
        "runtime:pointer": 1,
        "launcher:windows": 2,
    }
    return order.get(resource.resource_id, 99), resource.resource_id


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
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
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _is_busy_lock_error(error: OSError, *, windows: bool) -> bool:
    if windows:
        return error.errno == errno.EACCES or getattr(error, "winerror", None) == 33
    return error.errno in {errno.EACCES, errno.EAGAIN}


def _try_lock(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _is_busy_lock_error(exc, windows=True):
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if _is_busy_lock_error(exc, windows=False):
            return False
        raise
    return True


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.path.getsize(path) == 0:
            os.write(descriptor, b"\0")
        deadline = time.monotonic() + timeout_seconds
        while not _try_lock(descriptor):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("lifecycle lock timeout")
            time.sleep(min(poll_seconds, remaining))
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock(descriptor)
        finally:
            os.close(descriptor)


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


def _require_text(value: object, label: str, max_bytes: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > max_bytes
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise LifecycleError("REQUEST_INVALID", f"{label} is invalid")
