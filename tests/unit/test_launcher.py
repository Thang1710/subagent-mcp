from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from subagent_harness_mcp.launcher import (
    LifecycleError,
    OwnershipJournal,
    ProcessIdentity,
    RuntimeRecord,
    activate_pointer,
    build_runtime_manifest,
    read_pointer,
    recover_pointer_transaction,
    render_windows_launcher,
    rollback_pointer,
    stage_runtime,
    stop_verified_process,
    windows_registration_argv,
)


def _runtime_source(tmp_path: Path, version: str, payload: bytes) -> Path:
    source = tmp_path / f"source-{version}"
    executable = source / "Scripts" / "python.exe"
    package = source / "Lib" / "site-packages" / "subagent_harness_mcp" / "cli.py"
    executable.parent.mkdir(parents=True)
    package.parent.mkdir(parents=True)
    executable.write_bytes(payload)
    package.write_text("# staged runtime\n", encoding="utf-8")
    manifest = build_runtime_manifest(source, version=version)
    (source / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return source


def test_runtime_stage_is_immutable_and_rejects_manifest_drift(tmp_path: Path) -> None:
    source = _runtime_source(tmp_path, "1.0.0", b"python-one")
    runtime_parent = tmp_path / "data" / "runtimes"
    runtime_parent.mkdir(parents=True)

    record, changed = stage_runtime(source, runtime_parent, version="1.0.0")

    assert changed is True
    record.validate(runtime_parent)
    assert record.root == runtime_parent / "1.0.0"
    repeated, repeated_changed = stage_runtime(
        source,
        runtime_parent,
        version="1.0.0",
    )
    assert repeated == record
    assert repeated_changed is False

    (record.root / "Scripts" / "python.exe").write_bytes(b"tampered")
    with pytest.raises(LifecycleError, match="digest") as error:
        record.validate(runtime_parent)
    assert error.value.code == "RUNTIME_INVALID"


def test_runtime_manifest_rejects_path_traversal_and_symlinks(tmp_path: Path) -> None:
    source = _runtime_source(tmp_path, "1.0.0", b"python")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["../escape"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime_parent = tmp_path / "runtimes"
    runtime_parent.mkdir()

    with pytest.raises(LifecycleError) as error:
        stage_runtime(source, runtime_parent, version="1.0.0")
    assert error.value.code == "RUNTIME_INVALID"


def test_pointer_rollback_restores_byte_exact_prior_document(tmp_path: Path) -> None:
    runtime_parent = tmp_path / "runtimes"
    runtime_parent.mkdir()
    first, _ = stage_runtime(
        _runtime_source(tmp_path, "1.0.0", b"one"),
        runtime_parent,
        version="1.0.0",
    )
    second, _ = stage_runtime(
        _runtime_source(tmp_path, "2.0.0", b"two"),
        runtime_parent,
        version="2.0.0",
    )
    pointer = tmp_path / "current.json"
    transaction = tmp_path / "current.transaction.json"
    rollback = tmp_path / "rollback.json"
    activate_pointer(
        pointer,
        first,
        runtime_parent=runtime_parent,
        transaction_path=transaction,
        rollback_path=rollback,
    )
    initial = json.loads(pointer.read_text(encoding="utf-8"))
    initial["future"] = {"preserved": True}
    pointer.write_text(
        json.dumps(initial, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    prior_bytes = pointer.read_bytes()

    activate_pointer(
        pointer,
        second,
        runtime_parent=runtime_parent,
        transaction_path=transaction,
        rollback_path=rollback,
    )
    assert read_pointer(pointer, runtime_parent) == second

    assert rollback_pointer(
        pointer,
        runtime_parent=runtime_parent,
        transaction_path=transaction,
        rollback_path=rollback,
    ) is True
    assert pointer.read_bytes() == prior_bytes
    assert read_pointer(pointer, runtime_parent) == first
    assert rollback_pointer(
        pointer,
        runtime_parent=runtime_parent,
        transaction_path=transaction,
        rollback_path=rollback,
    ) is False


def test_pointer_recovery_finalizes_an_activated_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subagent_harness_mcp.launcher as launcher_module

    runtime_parent = tmp_path / "runtimes"
    runtime_parent.mkdir()
    first, _ = stage_runtime(
        _runtime_source(tmp_path, "1.0.0", b"one"),
        runtime_parent,
        version="1.0.0",
    )
    second, _ = stage_runtime(
        _runtime_source(tmp_path, "2.0.0", b"two"),
        runtime_parent,
        version="2.0.0",
    )
    pointer = tmp_path / "current.json"
    transaction = tmp_path / "current.transaction.json"
    rollback = tmp_path / "rollback.json"
    activate_pointer(
        pointer,
        first,
        runtime_parent=runtime_parent,
        transaction_path=transaction,
        rollback_path=rollback,
    )
    original_finalize = launcher_module._finalize_pointer_transaction

    def crash_before_finalize(*args, **kwargs):
        raise OSError("simulated crash after activation")

    monkeypatch.setattr(
        launcher_module,
        "_finalize_pointer_transaction",
        crash_before_finalize,
    )
    with pytest.raises(OSError, match="simulated crash"):
        activate_pointer(
            pointer,
            second,
            runtime_parent=runtime_parent,
            transaction_path=transaction,
            rollback_path=rollback,
        )
    assert transaction.is_file()
    assert read_pointer(pointer, runtime_parent) == second

    monkeypatch.setattr(
        launcher_module,
        "_finalize_pointer_transaction",
        original_finalize,
    )
    assert recover_pointer_transaction(
        pointer,
        runtime_parent=runtime_parent,
        transaction_path=transaction,
        rollback_path=rollback,
    ) is True
    assert not transaction.exists()
    assert rollback_pointer(
        pointer,
        runtime_parent=runtime_parent,
        transaction_path=transaction,
        rollback_path=rollback,
    ) is True
    assert read_pointer(pointer, runtime_parent) == first


def test_windows_launcher_has_bom_and_only_fixed_execution_argv(tmp_path: Path) -> None:
    launcher = tmp_path / "data" / "bin" / "subagent-harness-mcp.ps1"
    payload = render_windows_launcher(launcher)
    text = payload.decode("utf-8-sig")

    assert payload.startswith(b"\xef\xbb\xbf")
    assert "Invoke-Expression" not in text
    assert "Start-Process" not in text
    assert "@args" not in text
    assert "'-I', '-B', '-m', 'subagent_harness_mcp.cli', 'serve'" in text
    assert "Get-FileHash" not in text
    assert "[Security.Cryptography.SHA256]::Create()" in text
    assert ".ComputeHash(" in text
    assert windows_registration_argv(launcher) == (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher.resolve(strict=False)),
    )


def test_lifecycle_cli_registers_the_public_mcp_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subagent_harness_mcp.cli as cli_module
    import subagent_harness_mcp.install as install_module

    registered: list[str] = []

    class _Manager:
        def __init__(self, paths: object) -> None:
            assert paths is not None

        def register(
            self,
            client_id: str,
            *,
            backend: object,
            dry_run: bool,
        ) -> install_module.LifecycleResult:
            assert backend is not None
            registered.append(client_id)
            return install_module.LifecycleResult(
                operation="register",
                changed=True,
                dry_run=dry_run,
                recovery_required=False,
                actions=("register_official_command",),
            )

    monkeypatch.setattr(install_module, "WindowsLifecycleManager", _Manager)

    assert cli_module._run_lifecycle(
        "register",
        ("--client", "codex", "--dry-run"),
    ) == 0
    assert registered == ["subagent-mcp"]
    output = capsys.readouterr()
    assert output.err == ""
    assert "register: planned" in output.out


class _ProcessBackend:
    def __init__(self, observed: ProcessIdentity | None) -> None:
        self.observed = observed
        self.terminated: list[ProcessIdentity] = []

    def observe(self, pid: int) -> ProcessIdentity | None:
        assert isinstance(pid, int)
        return self.observed

    def terminate(self, identity: ProcessIdentity) -> None:
        self.terminated.append(identity)
        self.observed = None


def test_process_target_requires_pid_creation_and_executable_digest() -> None:
    expected = ProcessIdentity(17, "created-original", "a" * 64)
    reused = ProcessIdentity(17, "created-reused", "a" * 64)
    backend = _ProcessBackend(reused)

    with pytest.raises(LifecycleError) as error:
        stop_verified_process(expected, backend)
    assert error.value.code == "RECOVERY_REQUIRED"
    assert backend.terminated == []

    backend.observed = expected
    assert stop_verified_process(expected, backend) is True
    assert backend.terminated == [expected]
    assert stop_verified_process(expected, backend) is False


def test_ownership_journal_is_append_only_and_fails_closed_on_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ownership.jsonl"
    journal = OwnershipJournal(path)
    locator = str((tmp_path / "owned.bin").resolve(strict=False))
    digest = hashlib.sha256(b"owned").hexdigest()

    journal.record_owned(
        resource_id="launcher:windows",
        kind="file",
        locator=locator,
        digest=digest,
    )
    first_size = path.stat().st_size
    journal.record_removed(resource_id="launcher:windows")
    assert path.stat().st_size > first_size
    assert journal.active_resources() == {}

    with path.open("ab") as stream:
        stream.write(b"not-json\n")
    with pytest.raises(LifecycleError) as error:
        journal.active_resources()
    assert error.value.code == "OWNERSHIP_JOURNAL_CORRUPT"


def test_lifecycle_cli_dry_runs_never_create_product_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from subagent_harness_mcp.cli import main

    home = tmp_path / "portable-home"
    source = _runtime_source(tmp_path, "1.0.0", b"python")
    monkeypatch.setenv("SUBAGENT_MCP_HOME", str(home))
    commands = (
        (
            "install",
            "--runtime",
            str(source),
            "--runtime-version",
            "1.0.0",
            "--dry-run",
        ),
        (
            "update",
            "--runtime",
            str(source),
            "--runtime-version",
            "1.0.0",
            "--dry-run",
        ),
        ("rollback", "--dry-run"),
        ("register", "--client", "codex", "--dry-run"),
        ("uninstall", "--client", "codex", "--dry-run"),
    )

    for command in commands:
        assert main(command) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.count(":") >= len(commands)
    assert not home.exists()
