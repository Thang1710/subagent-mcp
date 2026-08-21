from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .core import ProbeResult, read_fd_bounded, write_json_atomic
from .hook_sink import build_hook_settings
from .live_common import (
    ApprovalScope,
    BoundCliIdentity,
    BoundExecutableFile,
    BoundExecutableManifest,
    ExecutionObservations,
    approval_digest,
    claim_execution_authorization,
)
from .live_host import _bounded_text_probe, load_bound_host_identity
from .strict_probe import prepare_probe


_DENIED_TOOLS = (
    "mcp__codex__*",
    "mcp__agent_bridge__*",
    "mcp__subagent_harness_mcp__*",
)
_OBSERVER_HOOKS = ("Setup", "SessionStart", "InstructionsLoaded")
_REQUIRED_INIT_HOOKS = ("Setup", "InstructionsLoaded")
_CREDENTIAL_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


@dataclass(frozen=True)
class GroupAPaths:
    cwd: Path
    settings: Path
    empty_mcp: Path
    event_log: Path


@dataclass(frozen=True)
class MaterializedGroupA:
    paths: GroupAPaths
    strict_argv: tuple[str, ...]
    control_argv: tuple[str, ...]
    cli_sha256: str
    cli_version: str
    marker_path: Path
    marker_exit_path: Path
    marker_token: str


def build_init_argv(
    cli: str | Path,
    paths: GroupAPaths,
    *,
    strict: bool,
) -> tuple[str, ...]:
    argv = [
        str(Path(cli).resolve()),
        "--init-only",
        "--no-session-persistence",
        "--setting-sources",
        "user,project,local",
        "--settings",
        str(paths.settings.resolve()),
    ]
    if strict:
        argv.append("--strict-mcp-config")
    argv.extend([
        "--mcp-config",
        str(paths.empty_mcp.resolve()),
        "--tools",
        "",
        "--prompt-suggestions",
        "false",
        "--disallowedTools",
        *_DENIED_TOOLS,
    ])
    return tuple(argv)


def build_group_a_settings(
    python_exe: str | Path,
    hook_sink: str | Path,
    event_log: str | Path,
    *,
    observer_cli: str | Path,
    observer_cli_sha256: str,
) -> dict[str, Any]:
    if len(observer_cli_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in observer_cli_sha256
    ):
        raise ValueError("observer CLI digest must be a lowercase SHA-256")
    return build_hook_settings(
        Path(python_exe),
        Path(hook_sink),
        Path(event_log),
        events=_OBSERVER_HOOKS,
        extra_args=(
            "--observer-cli", str(Path(observer_cli).resolve(strict=True)),
            "--observer-cli-sha256", observer_cli_sha256,
        ),
    )


def build_group_a_scope(
    *,
    git_head: str,
    cli_sha256: str,
    executable_manifest_sha256: str,
    trust_revision: int,
) -> ApprovalScope:
    return ApprovalScope(
        schema_version=1,
        git_head=git_head,
        cli_sha256=cli_sha256,
        gate_ids=(
            "init_only_capability",
            "observer_visibility",
            "strict_mcp_pre_spawn",
        ),
        side_effects=(),
        max_provider_session_launches=0,
        max_worktree_creates=0,
        max_stop_respawn_actions=0,
        max_attach_actions=0,
        max_file_deletes=0,
        max_removals=0,
        background_internal_requests_acknowledged=False,
        executable_manifest_sha256=executable_manifest_sha256,
        trust_revision=trust_revision,
    )


def materialize_group_a(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    marker_script: str | Path,
    hook_sink: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedGroupA:
    target = Path(root).resolve()
    if bound_identity.version == "unverified" or not bound_identity.matches(cli):
        raise PermissionError("Task 2 bound CLI identity is required")
    identity = bound_identity
    layout = prepare_probe(target, Path(python_exe), Path(marker_script))
    paths = GroupAPaths(
        cwd=Path(layout["repo"]),
        settings=Path(layout["settings"]),
        empty_mcp=Path(layout["declared_config"]),
        event_log=target / "events.jsonl",
    )
    settings = build_group_a_settings(
        python_exe,
        hook_sink,
        paths.event_log,
        observer_cli=identity.canonical_path,
        observer_cli_sha256=identity.sha256,
    )
    write_json_atomic(paths.settings, settings)
    return MaterializedGroupA(
        paths=paths,
        strict_argv=build_init_argv(cli, paths, strict=True),
        control_argv=build_init_argv(cli, paths, strict=False),
        cli_sha256=identity.sha256,
        cli_version=identity.version,
        marker_path=Path(layout["marker"]),
        marker_exit_path=Path(layout["marker_exit"]),
        marker_token=layout["marker_token"],
    )


def _expected_python_process_image(python_exe: Path) -> Path:
    launcher = python_exe.resolve(strict=True)
    controller_launcher = Path(sys.executable).resolve(strict=True)
    if os.path.normcase(str(launcher)) == os.path.normcase(str(controller_launcher)):
        return Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
    return launcher


def build_group_a_execution_manifest(
    materialized: MaterializedGroupA,
    *,
    python_exe: str | Path,
    marker_script: str | Path,
    hook_sink: str | Path,
) -> tuple[str, BoundExecutableManifest, dict[str, Any]]:
    paths = {
        Path(materialized.strict_argv[0]),
        Path(python_exe),
        Path(marker_script),
        Path(hook_sink),
        materialized.paths.settings,
        materialized.paths.empty_mcp,
        materialized.paths.cwd / ".mcp.json",
        Path(__file__),
        Path(prepare_probe.__code__.co_filename),
        Path(build_hook_settings.__code__.co_filename),
        Path(write_json_atomic.__code__.co_filename),
        Path(claim_execution_authorization.__code__.co_filename),
        Path(_bounded_text_probe.__code__.co_filename),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("locking.py"),
        _expected_python_process_image(Path(python_exe)),
    }
    entries = []
    for path in sorted((path.resolve(strict=True) for path in paths), key=str):
        identity = BoundCliIdentity.capture(path, version="unverified")
        entries.append(BoundExecutableFile(
            canonical_path=identity.canonical_path,
            sha256=identity.sha256,
            file_identity=identity.file_identity,
        ))
    file_manifest = BoundExecutableManifest(
        repository_id="group-a-generated",
        trust_revision=1,
        entries=tuple(entries),
    )
    contract = {
        "schema_version": 1,
        "file_manifest_sha256": file_manifest.sha256,
        "strict_argv": list(materialized.strict_argv),
        "control_argv": list(materialized.control_argv),
        "cwd": str(materialized.paths.cwd.resolve(strict=True)),
        "observed_cli_version": materialized.cli_version,
        "owned_marker_process": {
            "argv": [
                str(Path(python_exe).resolve(strict=True)),
                str(Path(marker_script).resolve(strict=True)),
            ],
            "environment": {
                "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER": str(
                    materialized.marker_path.resolve()
                ),
                "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT": str(
                    materialized.marker_exit_path.resolve()
                ),
                "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN": materialized.marker_token,
            },
            "deadline_seconds": 30,
            "max_processes": 1,
        },
    }
    return execution_contract_digest(contract), file_manifest, contract


def execution_contract_digest(contract: Mapping[str, Any]) -> str:
    body = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _scope_payload(scope: ApprovalScope) -> dict[str, Any]:
    return json.loads(json.dumps(scope.to_dict()))


def assert_no_credential_overrides(env: Mapping[str, str]) -> None:
    if any(bool(env.get(name)) for name in _CREDENTIAL_OVERRIDES):
        raise PermissionError("credential override blocks Group A")


def _marker_token_from_config(config_path: Path, target: Path) -> str:
    fd = os.open(config_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        payload = json.loads(read_fd_bounded(fd, 1024 * 1024).decode("utf-8"))
    finally:
        os.close(fd)
    try:
        server = payload["mcpServers"]["subagent_harness_mcp_phase0a_repo_marker"]
        env = server["env"]
        token = env["SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN"]
    except (KeyError, TypeError) as exc:
        raise PermissionError("Group A marker configuration is invalid") from exc
    if (
        not isinstance(token, str)
        or re.fullmatch(r"[0-9a-f]{32}", token) is None
        or env.get("SUBAGENT_HARNESS_MCP_PHASE0A_MARKER")
        != str((target / "repo-mcp-spawned.txt").resolve())
        or env.get("SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT")
        != str((target / "repo-mcp-exited.txt").resolve())
    ):
        raise PermissionError("Group A marker configuration is invalid")
    return token


def load_group_a(
    root: str | Path,
    *,
    cli: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedGroupA:
    target = Path(root).resolve(strict=True)
    if bound_identity.version == "unverified" or not bound_identity.matches(cli):
        raise PermissionError("Task 2 bound CLI identity is required")
    identity = bound_identity
    paths = GroupAPaths(
        cwd=(target / "repo").resolve(strict=True),
        settings=(target / "settings.json").resolve(strict=True),
        empty_mcp=(target / "declared-empty.json").resolve(strict=True),
        event_log=target / "events.jsonl",
    )
    repo_mcp = (paths.cwd / ".mcp.json").resolve(strict=True)
    marker_token = _marker_token_from_config(repo_mcp, target)
    return MaterializedGroupA(
        paths=paths,
        strict_argv=build_init_argv(cli, paths, strict=True),
        control_argv=build_init_argv(cli, paths, strict=False),
        cli_sha256=identity.sha256,
        cli_version=identity.version,
        marker_path=target / "repo-mcp-spawned.txt",
        marker_exit_path=target / "repo-mcp-exited.txt",
        marker_token=marker_token,
    )


def _git_checkpoint(project_root: str | Path) -> tuple[str, bool]:
    project = Path(project_root).resolve(strict=True)
    base = ["git", "-c", f"safe.directory={project.as_posix()}", "-C", str(project)]
    head = subprocess.run(
        [*base, "rev-parse", "HEAD"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        shell=False,
    )
    status = subprocess.run(
        [*base, "status", "--porcelain=v1", "--untracked-files=all"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        shell=False,
    )
    value = head.stdout.strip()
    if (
        head.returncode != 0
        or status.returncode != 0
        or len(value) != 40
        or any(char not in "0123456789abcdef" for char in value)
        or len(status.stdout.encode("utf-8")) > 1024 * 1024
    ):
        raise RuntimeError("Git checkpoint observation failed")
    return value, bool(status.stdout)


def _task2_identity_path(root: str | Path) -> Path:
    return Path(root).absolute().parent / "host" / "bound-identity.json"


def preview_group_a(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    python_exe: str | Path,
    marker_script: str | Path,
    hook_sink: str | Path,
) -> dict[str, Any]:
    git_head, dirty = _git_checkpoint(project_root)
    if dirty:
        raise PermissionError("tracked checkout must be clean before preview")
    bound_identity = load_bound_host_identity(_task2_identity_path(root), cli)
    materialized = materialize_group_a(
        root,
        cli=cli,
        python_exe=python_exe,
        marker_script=marker_script,
        hook_sink=hook_sink,
        bound_identity=bound_identity,
    )
    manifest_sha256, _manifest, execution_contract = build_group_a_execution_manifest(
        materialized,
        python_exe=python_exe,
        marker_script=marker_script,
        hook_sink=hook_sink,
    )
    scope = build_group_a_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
    )
    scope_payload = _scope_payload(scope)
    display = {
        "scope": scope_payload,
        "scope_sha256": approval_digest(scope),
        "execution_contract": execution_contract,
    }
    write_json_atomic(Path(root) / "pending-scope.json", scope_payload)
    return display


def _read_events(path: Path) -> bytes:
    if not path.exists():
        return b""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return read_fd_bounded(fd, 1024 * 1024)
    finally:
        os.close(fd)


def _read_marker_record(path: Path, ownership_token: str) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        payload = json.loads(read_fd_bounded(fd, 4096).decode("utf-8"))
    finally:
        os.close(fd)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "pid", "ownership_token", "creation_identity", "executable_sha256",
    }:
        raise PermissionError("marker ownership record schema is invalid")
    if (
        payload["schema_version"] != 1
        or isinstance(payload["pid"], bool)
        or not isinstance(payload["pid"], int)
        or payload["pid"] <= 0
        or payload["ownership_token"] != ownership_token
        or not isinstance(payload["creation_identity"], str)
        or re.fullmatch(r"(?:windows|linux):[0-9]+|unsupported", payload["creation_identity"])
        is None
        or not isinstance(payload["executable_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["executable_sha256"]) is None
    ):
        raise PermissionError("marker ownership record is invalid")
    return payload


def _windows_process_creation_identity(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
    ):
        raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
    return f"windows:{(created.high << 32) | created.low}"


def _stop_windows_marker_process(
    record: Mapping[str, Any],
    expected_python: Path,
) -> tuple[bool, bool]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0001 | 0x1000 | 0x00100000, False, record["pid"])
    if not handle:
        return (True, False) if ctypes.get_last_error() == 87 else (False, False)
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False, False
        if (
            os.path.normcase(str(Path(buffer.value).resolve(strict=True)))
            != os.path.normcase(str(expected_python.resolve(strict=True)))
            or _windows_process_creation_identity(handle) != record["creation_identity"]
        ):
            return False, False
        wait = kernel32.WaitForSingleObject(handle, 0)
        if wait == 0:
            return True, False
        if wait != 258 or not kernel32.TerminateProcess(handle, 124):
            return False, False
        return kernel32.WaitForSingleObject(handle, 5000) == 0, True
    finally:
        kernel32.CloseHandle(handle)


def _stop_linux_marker_process(
    record: Mapping[str, Any],
    expected_python: Path,
) -> tuple[bool, bool]:
    import select
    import signal

    pid = record["pid"]
    try:
        pid_fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True, False
    except (AttributeError, OSError):
        return False, False
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rpartition(") ")[2].split()
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        if (
            record["creation_identity"] != f"linux:{stat_fields[19]}"
            or executable != expected_python.resolve(strict=True)
            or not hasattr(signal, "pidfd_send_signal")
        ):
            return False, False
        signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
        if not select.select([pid_fd], [], [], 5)[0]:
            signal.pidfd_send_signal(pid_fd, signal.SIGKILL)
        return bool(select.select([pid_fd], [], [], 5)[0]), True
    except (OSError, IndexError):
        return False, False
    finally:
        os.close(pid_fd)


def _stop_owned_marker_process(
    record: Mapping[str, Any],
    *,
    expected_python: Path,
) -> tuple[bool, bool]:
    expected_image = _expected_python_process_image(expected_python)
    expected = BoundCliIdentity.capture(expected_image, version="unverified")
    if record.get("executable_sha256") != expected.sha256:
        return False, False
    if os.name == "nt":
        return _stop_windows_marker_process(record, expected_image)
    if sys.platform.startswith("linux"):
        return _stop_linux_marker_process(record, expected_image)
    return False, False


def _marker_state(materialized: MaterializedGroupA, python_exe: Path) -> tuple[bool, bool]:
    started_present = materialized.marker_path.is_file()
    exit_present = materialized.marker_exit_path.is_file()
    if not started_present and not exit_present:
        return False, True
    if not started_present:
        return False, False
    try:
        record = _read_marker_record(materialized.marker_path, materialized.marker_token)
        if exit_present and _read_marker_record(
            materialized.marker_exit_path, materialized.marker_token
        ) != record:
            return True, False
        stopped, _forced = _stop_owned_marker_process(record, expected_python=python_exe)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PermissionError, ValueError):
        return True, False
    return True, stopped


def _invoke_group_a_arm(
    materialized: MaterializedGroupA,
    argv: tuple[str, ...],
    *,
    env: Mapping[str, str],
    python_exe: str | Path,
) -> dict[str, Any]:
    assert_no_credential_overrides(env)
    materialized.paths.event_log.parent.mkdir(parents=True, exist_ok=True)
    materialized.paths.event_log.write_bytes(b"")
    probe = _bounded_text_probe(
        "group_a_init",
        list(argv),
        timeout_seconds=30,
        env=env,
        cwd=materialized.paths.cwd,
    )
    marker_spawned, marker_cleanup_confirmed = _marker_state(
        materialized, Path(python_exe)
    )
    return observe_init_arm(
        probe,
        _read_events(materialized.paths.event_log),
        marker_spawned=marker_spawned,
        marker_cleanup_confirmed=marker_cleanup_confirmed,
        expected_cli_sha256=materialized.cli_sha256,
    )


def execute_group_a(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    approval: str | Path,
    python_exe: str | Path,
    marker_script: str | Path,
    hook_sink: str | Path,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    execution_env = os.environ if env is None else env
    assert_no_credential_overrides(execution_env)
    target = Path(root).resolve(strict=True)
    bound_identity = load_bound_host_identity(_task2_identity_path(target), cli)
    materialized = load_group_a(target, cli=cli, bound_identity=bound_identity)
    manifest_sha256, file_manifest, _execution_contract = build_group_a_execution_manifest(
        materialized,
        python_exe=python_exe,
        marker_script=marker_script,
        hook_sink=hook_sink,
    )
    git_head, dirty = _git_checkpoint(project_root)
    scope = build_group_a_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
    )
    pending = json.loads((target / "pending-scope.json").read_text(encoding="utf-8"))
    if not isinstance(pending, dict) or pending != _scope_payload(scope):
        raise PermissionError("Group A preview drifted")
    observations = ExecutionObservations(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
        dirty_tracked=dirty,
    )
    with file_manifest.lease() as lease:
        claim_execution_authorization(
            scope,
            approval,
            approval_root=target.parent / "approvals",
            observations=observations,
            execution_id=f"group-a-{secrets.token_hex(8)}",
        )
        lease.verify_init_ack()
        def invoke(argv: tuple[str, ...]) -> dict[str, Any]:
            result = _invoke_group_a_arm(
                materialized, argv, env=execution_env, python_exe=python_exe,
            )
            lease.verify_init_ack()
            return result

        return run_group_a(
            materialized.strict_argv,
            materialized.control_argv,
            invoke=invoke,
        )


def _arm_passes(value: dict[str, Any], *, marker_spawned: bool) -> bool:
    hooks = value.get("hooks")
    return (
        _arm_safe_to_run_control(value, marker_spawned=marker_spawned)
        and isinstance(hooks, list)
        and set(_REQUIRED_INIT_HOOKS).issubset(hooks)
        and set(hooks).issubset(_OBSERVER_HOOKS)
    )


def _arm_safe_to_run_control(value: dict[str, Any], *, marker_spawned: bool) -> bool:
    return (
        value.get("exit_success") is True
        and value.get("marker_spawned") is marker_spawned
        and value.get("marker_cleanup_confirmed") is True
        and value.get("model_event_count") == 0
        and value.get("rate_event_count") == 0
        and value.get("hook_error_count") == 0
        and value.get("observer_identity_match") is True
    )


def adjudicate_group_a(
    strict: dict[str, Any],
    control: dict[str, Any],
) -> dict[str, Any]:
    strict_pass = _arm_passes(strict, marker_spawned=False)
    control_pass = _arm_passes(control, marker_spawned=True)
    cleanup_values = [
        arm["marker_cleanup_confirmed"]
        for arm in (strict, control)
        if "marker_cleanup_confirmed" in arm
    ]
    cleanup_confirmed = bool(cleanup_values) and all(value is True for value in cleanup_values)
    recovery_required = any(value is False for value in cleanup_values)
    observed_hooks = sorted(set(strict.get("hooks", []))) if isinstance(strict.get("hooks"), list) else []
    passed = strict_pass and control_pass
    return {
        "status": "pass" if passed else "recovery_required" if recovery_required else "blocked",
        "init_only_capability": passed,
        "observer_visibility": passed,
        "strict_mcp_pre_spawn": passed,
        "observed_hooks": observed_hooks,
        "marker_cleanup_confirmed": cleanup_confirmed,
    }


def run_group_a(
    strict_argv: tuple[str, ...],
    control_argv: tuple[str, ...],
    *,
    invoke: Callable[[tuple[str, ...]], dict[str, Any]],
) -> dict[str, Any]:
    strict = invoke(strict_argv)
    if not _arm_safe_to_run_control(strict, marker_spawned=False):
        return adjudicate_group_a(strict, {})
    control = invoke(control_argv)
    return adjudicate_group_a(strict, control)


def observe_init_arm(
    probe: ProbeResult,
    event_bytes: bytes,
    *,
    marker_spawned: bool,
    marker_cleanup_confirmed: bool,
    expected_cli_sha256: str,
) -> dict[str, Any]:
    model_events = 0
    rate_events = 0
    for line in probe.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"assistant", "result"}:
            model_events += 1
        elif item_type == "rate_limit_event":
            rate_events += 1

    hooks: list[str] = []
    observer_matches: list[bool] = []
    hook_errors = 1 if probe.stderr.strip() else 0
    try:
        event_text = event_bytes.decode("utf-8")
    except UnicodeDecodeError:
        event_text = ""
        hook_errors += 1
    for line in event_text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            hook_errors += 1
            continue
        if not isinstance(item, dict):
            hook_errors += 1
            continue
        hook_name = item.get("hook_event_name")
        if isinstance(hook_name, str):
            hooks.append(hook_name)
        observer_matches.append(item.get("observer_cli_sha256") == expected_cli_sha256)
    return {
        "exit_success": probe.exit_code == 0 and not probe.timed_out,
        "marker_spawned": marker_spawned,
        "marker_cleanup_confirmed": marker_cleanup_confirmed,
        "model_event_count": model_events,
        "rate_event_count": rate_events,
        "hook_error_count": hook_errors,
        "hooks": sorted(set(hooks)),
        "observer_identity_match": bool(observer_matches) and all(observer_matches),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args(argv)

    python_exe = Path(sys.executable)
    marker_script = Path(__file__).with_name("marker_mcp.py")
    hook_sink = Path(__file__).with_name("hook_sink.py")
    if args.preview:
        if args.approval is not None:
            parser.error("--approval is valid only with --execute")
        result = preview_group_a(
            args.root,
            cli=args.cli,
            project_root=args.project_root,
            python_exe=python_exe,
            marker_script=marker_script,
            hook_sink=hook_sink,
        )
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.approval is None:
        parser.error("--approval is required with --execute")
    result = execute_group_a(
        args.root,
        cli=args.cli,
        project_root=args.project_root,
        approval=args.approval,
        python_exe=python_exe,
        marker_script=marker_script,
        hook_sink=hook_sink,
        env=os.environ,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
