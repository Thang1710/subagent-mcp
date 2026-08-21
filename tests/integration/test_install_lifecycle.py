from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from subagent_harness_mcp.install import (
    CodexRegistrationBackend,
    SubprocessHealthBackend,
    WindowsLifecycleManager,
)
from subagent_harness_mcp.launcher import (
    LifecycleError,
    ProcessIdentity,
    RuntimeRecord,
    build_runtime_manifest,
    read_pointer,
    windows_registration_argv,
)
from subagent_harness_mcp.paths import ProductPaths


class _HealthBackend:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.checked: list[str] = []

    def check(self, runtime: RuntimeRecord) -> bool:
        self.checked.append(runtime.version)
        return self.healthy


class _RegistrationBackend:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, ...]] = {}
        self.readback_override: tuple[str, ...] | None | object = _UNSET
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def readback(self, client_id: str) -> tuple[str, ...] | None:
        if self.readback_override is not _UNSET and client_id in self.rows:
            assert self.readback_override is None or isinstance(
                self.readback_override,
                tuple,
            )
            return self.readback_override
        return self.rows.get(client_id)

    def register(self, client_id: str, argv: tuple[str, ...]) -> None:
        self.registered.append(client_id)
        self.rows[client_id] = argv

    def unregister(self, client_id: str) -> None:
        self.unregistered.append(client_id)
        self.rows.pop(client_id, None)


class _ProcessBackend:
    def __init__(self, observed: ProcessIdentity | None) -> None:
        self.observed = observed
        self.terminated: list[ProcessIdentity] = []

    def observe(self, pid: int) -> ProcessIdentity | None:
        return self.observed

    def terminate(self, identity: ProcessIdentity) -> None:
        self.terminated.append(identity)
        self.observed = None


_UNSET = object()


def _paths(tmp_path: Path) -> ProductPaths:
    home = tmp_path / "home"
    return ProductPaths(home / "config", home / "state", home / "data")


def _runtime_source(tmp_path: Path, version: str, marker: str) -> Path:
    source = tmp_path / f"candidate-{version}-{marker}"
    executable = source / "Scripts" / "python.exe"
    package = source / "Lib" / "site-packages" / "subagent_harness_mcp" / "cli.py"
    executable.parent.mkdir(parents=True)
    package.parent.mkdir(parents=True)
    executable.write_bytes(f"python-{marker}".encode("utf-8"))
    package.write_text(f"# runtime {marker}\n", encoding="utf-8")
    manifest = build_runtime_manifest(source, version=version)
    (source / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return source


def test_subprocess_health_check_disables_bytecode_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data" / "runtimes" / "1.0.0"
    python = root / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    observed: list[tuple[list[str], dict[str, object]]] = []

    def capture(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", capture)
    runtime = RuntimeRecord("1.0.0", root, "a" * 64)

    assert SubprocessHealthBackend().check(runtime) is True
    assert observed[0][0] == [
        str(python),
        "-I",
        "-B",
        "-m",
        "subagent_harness_mcp.cli",
        "--version",
    ]
    assert observed[0][1]["shell"] is False


def test_codex_registration_accepts_current_missing_server_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(
        argv: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            argv,
            1,
            b"",
            b"Error: No MCP server named 'subagent-harness-mcp' found.\r\n",
        )

    monkeypatch.setattr(subprocess, "run", missing)

    assert CodexRegistrationBackend().readback("subagent-harness-mcp") is None


def test_dry_run_plans_install_without_creating_product_roots(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    source = _runtime_source(tmp_path, "1.0.0", "one")

    result = manager.install(
        source,
        version="1.0.0",
        health=_HealthBackend(),
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.changed is True
    assert "stage_runtime" in result.actions
    assert not paths.data_dir.exists()
    assert not paths.config_dir.exists()
    assert not paths.state_dir.exists()


def test_install_update_and_rollback_are_idempotent_and_keep_old_runtime(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    first_source = _runtime_source(tmp_path, "1.0.0", "one")
    second_source = _runtime_source(tmp_path, "2.0.0", "two")
    health = _HealthBackend()

    installed = manager.install(
        first_source,
        version="1.0.0",
        health=health,
    )
    repeated = manager.install(
        first_source,
        version="1.0.0",
        health=health,
    )
    first_pointer = manager.pointer_path.read_bytes()
    pointer_document = json.loads(first_pointer.decode("utf-8"))
    pointer_document["future"] = {"keep": True}
    manager.pointer_path.write_text(
        json.dumps(pointer_document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    prior_bytes = manager.pointer_path.read_bytes()

    with (paths.data_dir / "runtimes" / "1.0.0" / "manifest.json").open("rb") as old:
        updated = manager.update(
            second_source,
            version="2.0.0",
            health=health,
        )
        assert old.read()
    repeated_update = manager.update(
        second_source,
        version="2.0.0",
        health=health,
    )

    assert installed.changed is True
    assert repeated.changed is False
    assert updated.changed is True
    assert repeated_update.changed is False
    assert manager.current_runtime().version == "2.0.0"
    assert (paths.data_dir / "runtimes" / "1.0.0").is_dir()

    rolled_back = manager.rollback(health=health)
    repeated_rollback = manager.rollback(health=health)
    assert rolled_back.changed is True
    assert repeated_rollback.changed is False
    assert manager.pointer_path.read_bytes() == prior_bytes
    assert manager.current_runtime().version == "1.0.0"


def test_failed_health_never_switches_the_active_pointer(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    manager.install(
        _runtime_source(tmp_path, "1.0.0", "one"),
        version="1.0.0",
        health=_HealthBackend(),
    )
    before = manager.pointer_path.read_bytes()

    with pytest.raises(LifecycleError) as error:
        manager.update(
            _runtime_source(tmp_path, "2.0.0", "two"),
            version="2.0.0",
            health=_HealthBackend(healthy=False),
        )
    assert error.value.code == "HEALTH_CHECK_FAILED"
    assert manager.pointer_path.read_bytes() == before
    assert manager.current_runtime().version == "1.0.0"
    assert (paths.data_dir / "runtimes" / "2.0.0").is_dir()


def test_concurrent_same_update_has_one_activation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    manager.install(
        _runtime_source(tmp_path, "1.0.0", "one"),
        version="1.0.0",
        health=_HealthBackend(),
    )
    second = _runtime_source(tmp_path, "2.0.0", "two")
    health = _HealthBackend()

    def update_once(_index: int):
        return manager.update(second, version="2.0.0", health=health)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update_once, (1, 2)))

    assert sorted(result.changed for result in results) == [False, True]
    assert manager.current_runtime().version == "2.0.0"
    assert health.checked.count("2.0.0") == 1


def test_registration_requires_exact_readback_and_is_conservatively_owned(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    manager.install(
        _runtime_source(tmp_path, "1.0.0", "one"),
        version="1.0.0",
        health=_HealthBackend(),
    )
    backend = _RegistrationBackend()
    backend.readback_override = ("wrong",)

    with pytest.raises(LifecycleError) as error:
        manager.register("codex", backend=backend)
    assert error.value.code == "REGISTRATION_READBACK_MISMATCH"
    assert backend.registered == ["codex"]
    assert backend.unregistered == ["codex"]
    assert not any(
        resource.kind == "registration"
        for resource in manager.ownership.active_resources().values()
    )

    backend.readback_override = _UNSET
    registered = manager.register("codex", backend=backend)
    repeated = manager.register("codex", backend=backend)
    assert registered.changed is True
    assert repeated.changed is False
    assert backend.rows["codex"] == windows_registration_argv(manager.launcher_path)


def test_pid_reuse_stops_uninstall_before_any_owned_resource_is_removed(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    manager.install(
        _runtime_source(tmp_path, "1.0.0", "one"),
        version="1.0.0",
        health=_HealthBackend(),
    )
    expected = ProcessIdentity(42, "created-original", "a" * 64)
    backend = _ProcessBackend(ProcessIdentity(42, "created-reused", "a" * 64))

    result = manager.uninstall(
        process_identity=expected,
        process_backend=backend,
    )

    assert result.recovery_required is True
    assert backend.terminated == []
    assert manager.launcher_path.is_file()
    assert manager.pointer_path.is_file()
    assert manager.current_runtime().root.is_dir()


def test_uninstall_preserves_user_state_and_refuses_registration_drift(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    manager.install(
        _runtime_source(tmp_path, "1.0.0", "one"),
        version="1.0.0",
        health=_HealthBackend(),
    )
    backend = _RegistrationBackend()
    manager.register("codex", backend=backend)
    paths.config_dir.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)
    config = paths.config_dir / "config.json"
    state = paths.state_dir / "state.db"
    config.write_bytes(b"user-config")
    state.write_bytes(b"user-state")
    backend.rows["codex"] = ("user-modified",)

    refused = manager.uninstall(registration_backend=backend)
    assert refused.recovery_required is True
    assert manager.launcher_path.exists()
    assert manager.pointer_path.exists()
    assert config.read_bytes() == b"user-config"
    assert state.read_bytes() == b"user-state"

    backend.rows["codex"] = windows_registration_argv(manager.launcher_path)
    removed = manager.uninstall(registration_backend=backend)
    repeated = manager.uninstall(registration_backend=backend)
    assert removed.changed is True
    assert removed.recovery_required is False
    assert repeated.changed is False
    assert not manager.launcher_path.exists()
    assert not manager.pointer_path.exists()
    assert not (paths.data_dir / "runtimes" / "1.0.0").exists()
    assert config.read_bytes() == b"user-config"
    assert state.read_bytes() == b"user-state"


def test_uninstall_reports_locked_resource_for_idempotent_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subagent_harness_mcp.install as install_module

    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    manager.install(
        _runtime_source(tmp_path, "1.0.0", "one"),
        version="1.0.0",
        health=_HealthBackend(),
    )
    original_rmtree = install_module.shutil.rmtree
    failed = False

    def locked_once(path: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise PermissionError("locked runtime")
        original_rmtree(path)

    monkeypatch.setattr(install_module.shutil, "rmtree", locked_once)
    first = manager.uninstall()
    second = manager.uninstall()
    assert first.recovery_required is True
    assert manager.ownership.active_resources() == {}
    assert second.changed is True
    assert second.recovery_required is False


def test_pointer_ownership_drift_prevents_destructive_uninstall(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager = WindowsLifecycleManager(paths)
    manager.install(
        _runtime_source(tmp_path, "1.0.0", "one"),
        version="1.0.0",
        health=_HealthBackend(),
    )
    manager.pointer_path.write_bytes(b'{"schema_version":1,"root":"other"}\n')

    result = manager.uninstall()

    assert result.recovery_required is True
    assert manager.pointer_path.exists()
    assert manager.launcher_path.exists()
    assert (paths.data_dir / "runtimes" / "1.0.0").exists()
