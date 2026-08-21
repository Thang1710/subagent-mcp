from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import normalize_agents, normalize_auth
from .core import ProbeResult, read_fd_bounded, write_json_atomic
from .host_probe import parse_cli_version
from . import live_common as _live_common
from .live_common import (
    BoundCliIdentity,
    BoundExecutableFile,
    BoundExecutableManifest,
    run_json_command,
)
from .manifest import TrustKey, blocked_items, scan_project


_CREDENTIAL_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_SAFE_CATEGORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HANDLE_BRANCHES = (
    "success",
    "timeout",
    "cancelled",
    "child_failure",
    "start_failure",
)
_MAX_HELP_BYTES = 8 * 1024 * 1024
_MAX_WRAPPER_BYTES = 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOOLS_SYNTAX = re.compile(r"(?im)^\s*--tools\s+<tools\.\.\.>(?:\s|$)")
_PROMPT_SUGGESTIONS_SYNTAX = re.compile(
    r"(?im)^\s*--prompt-suggestions\s+(?:\[value\]|<boolean>)(?:\s|$)"
)
_BOUND_CAPABILITY_KEYS = (
    "tools_empty_documented",
    "prompt_suggestions_false_documented",
    "stop_help_recognized",
    "respawn_help_recognized",
    "attach_help_recognized",
    "rm_help_recognized",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_repository_id(root: str | Path) -> str:
    project = Path(root).resolve(strict=True)
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={project.as_posix()}",
            "-C",
            str(project),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
        shell=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return f"path:{project}"
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = project / common
    return f"git:{common.resolve(strict=True)}"


def _probe_json(result: ProbeResult, expected: type) -> Any:
    if result.timed_out or result.exit_code != 0:
        raise RuntimeError("no-model CLI probe failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("no-model CLI probe returned malformed JSON") from exc
    if not isinstance(payload, expected):
        raise ValueError("no-model CLI probe returned an unexpected JSON type")
    return payload


def _bounded_text_probe(
    name: str,
    argv: list[str],
    *,
    timeout_seconds: float,
    env: Mapping[str, str],
    cwd: str | Path | None = None,
) -> ProbeResult:
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=None if cwd is None else str(cwd),
        shell=False,
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        pump = _live_common._PipePump(
            process, {"stdout": process.stdout, "stderr": process.stderr},
        )
    except BaseException:
        _live_common._terminate(process)
        process.stdout.close()
        process.stderr.close()
        raise
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    counts = {"stdout": 0, "stderr": 0}
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        while pump.closed_count < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _live_common._terminate(process)
                break
            for label, chunk in pump.poll(min(remaining, 0.05)):
                if chunk is None:
                    continue
                counts[label] += len(chunk)
                if counts[label] > _MAX_HELP_BYTES:
                    _live_common._terminate(process)
                    raise ValueError(f"{label} exceeds 8 MiB")
                chunks[label].append(chunk)
    finally:
        pump.close()
    if process.poll() is None:
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _live_common._terminate(process)
    return ProbeResult(
        name=name,
        argv=tuple(argv),
        cwd=None if cwd is None else str(Path(cwd).resolve()),
        started_at=started_at,
        duration_ms=round((time.monotonic() - started) * 1000),
        exit_code=process.returncode,
        stdout=b"".join(chunks["stdout"]).decode("utf-8", "replace"),
        stderr=b"".join(chunks["stderr"]).decode("utf-8", "replace"),
        timed_out=timed_out,
    )


def _invoke_text(
    runner: Callable[..., ProbeResult] | None,
    name: str,
    argv: list[str],
    *,
    env: Mapping[str, str],
) -> ProbeResult:
    if runner is not None:
        return runner(name, argv, timeout_seconds=30, env=env)
    return _bounded_text_probe(name, argv, timeout_seconds=30, env=env)


def _invoke_json(
    runner: Callable[..., ProbeResult] | None,
    name: str,
    argv: list[str],
    expected: type,
    *,
    env: Mapping[str, str],
) -> Any:
    if runner is not None:
        return _probe_json(runner(name, argv, timeout_seconds=30, env=env), expected)
    return run_json_command(argv, expected_type=expected, timeout_seconds=30, env=env)


def _category(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_CATEGORY.fullmatch(value):
        raise ValueError(f"{label} must be a bounded category")
    return value


def _override_presence(env: Mapping[str, str]) -> dict[str, bool]:
    return {
        "anthropic_api_key_present": bool(env.get("ANTHROPIC_API_KEY")),
        "anthropic_auth_token_present": bool(env.get("ANTHROPIC_AUTH_TOKEN")),
        "claude_code_oauth_token_present": bool(env.get("CLAUDE_CODE_OAUTH_TOKEN")),
    }


def write_bound_host_identity(
    root: str | Path,
    cli: str | Path,
    evidence: Mapping[str, Any],
    *,
    capabilities: Mapping[str, Any] | None = None,
) -> Path:
    version = evidence.get("observed_cli_version")
    expected_sha256 = evidence.get("cli_content_sha256")
    if (
        evidence.get("status") != "ready"
        or evidence.get("identity_stable") is not True
        or not isinstance(version, str)
        or not version.strip()
        or not isinstance(expected_sha256, str)
        or _HEX64.fullmatch(expected_sha256) is None
    ):
        raise PermissionError("host evidence is not ready for identity binding")
    identity = BoundCliIdentity.capture(cli, version=version)
    if identity.sha256 != expected_sha256 or not identity.matches(cli):
        raise PermissionError("host CLI identity drifted before binding")
    target = Path(root).absolute()
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or getattr(
        target.stat(follow_symlinks=False), "st_file_attributes", 0
    ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise PermissionError("bound host identity root must be direct")
    output = target / "bound-identity.json"
    capability_record = {key: False for key in _BOUND_CAPABILITY_KEYS}
    if capabilities is not None:
        for key in capability_record:
            if key in capabilities:
                if type(capabilities[key]) is not bool:
                    raise ValueError("bound host capability must be a boolean")
                capability_record[key] = capabilities[key]
    write_json_atomic(output, {
        "schema_version": 1,
        "canonical_path": identity.canonical_path,
        "sha256": identity.sha256,
        "version": identity.version,
        "file_identity": list(identity.file_identity),
        "context_flags": capability_record,
    })
    return output


def _load_bound_host_payload(path: str | Path) -> dict[str, Any]:
    target = Path(path).absolute()
    try:
        metadata = target.stat(follow_symlinks=False)
        attributes = getattr(metadata, "st_file_attributes", 0)
        if target.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise PermissionError("bound host identity must be a direct file")
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            payload = json.loads(read_fd_bounded(fd, 64 * 1024).decode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PermissionError("bound host identity is unavailable") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "canonical_path", "sha256", "version", "file_identity",
        "context_flags",
    }:
        raise PermissionError("bound host identity schema is invalid")
    file_identity = payload["file_identity"]
    context_flags = payload["context_flags"]
    if (
        payload["schema_version"] != 1
        or not isinstance(payload["canonical_path"], str)
        or not isinstance(payload["sha256"], str)
        or _HEX64.fullmatch(payload["sha256"]) is None
        or not isinstance(payload["version"], str)
        or not payload["version"].strip()
        or not isinstance(file_identity, list)
        or len(file_identity) != 4
        or any(isinstance(value, bool) or not isinstance(value, int) for value in file_identity)
        or not isinstance(context_flags, dict)
        or set(context_flags) != set(_BOUND_CAPABILITY_KEYS)
        or any(type(value) is not bool for value in context_flags.values())
    ):
        raise PermissionError("bound host identity schema is invalid")
    return payload


def load_bound_host_identity(
    path: str | Path,
    cli: str | Path,
) -> BoundCliIdentity:
    payload = _load_bound_host_payload(path)
    file_identity = payload["file_identity"]
    identity = BoundCliIdentity(
        canonical_path=payload["canonical_path"],
        sha256=payload["sha256"],
        version=payload["version"],
        file_identity=tuple(file_identity),
    )
    if not identity.matches(cli):
        raise PermissionError("bound host identity drifted")
    return identity


def load_bound_host_capabilities(path: str | Path) -> dict[str, bool]:
    payload = _load_bound_host_payload(path)
    return dict(payload["context_flags"])


def project_manifest_evidence(
    manifest: dict[str, Any],
    *,
    trusted_items: set[TrustKey] | None = None,
    trust_revision: int = 1,
) -> dict[str, Any]:
    """Project a path-free manifest summary suitable for committed evidence."""
    blocked = blocked_items(
        manifest,
        trusted_items=set() if trusted_items is None else trusted_items,
        trust_revision=trust_revision,
    )
    digest = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    repository_id = manifest.get("repository_id")
    return {
        "repository_kind": "git" if isinstance(repository_id, str) and repository_id.startswith("git:") else "path",
        "instruction_count": len(manifest.get("instruction_files", [])),
        "hook_target_count": len(manifest.get("hook_targets", [])),
        "external_count": len(manifest.get("external_imports", [])),
        "blocked_count": len(blocked),
        "manifest_digest": digest,
    }


def inspect_runtime_ownership(
    standalone: str | Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Inspect Desktop wrapper ownership/lifecycle clues without executing it."""
    standalone_path = Path(standalone).resolve(strict=True)
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    wrapper = home / "bin" / "claude.cmd"
    appdata = env.get("APPDATA")
    cache_root = Path(appdata) / "Claude" / "claude-code" if appdata else None
    wrapper_present = wrapper.is_file()
    wrapper_digest: str | None = None
    versioned_dependency = False
    selected_cache_digest: str | None = None
    selected_cache_observed = False
    selected_cache_below_root = False
    selected_cache_distinct = False
    if wrapper_present:
        with wrapper.open("rb") as stream:
            data = stream.read(_MAX_WRAPPER_BYTES + 1)
        if len(data) > _MAX_WRAPPER_BYTES:
            raise ValueError("Desktop wrapper exceeds the bounded read limit")
        wrapper_digest = hashlib.sha256(data).hexdigest()
        decoded = data.decode("utf-8-sig", "replace")
        text = decoded.casefold()
        versioned_dependency = (
            "claude-code" in text
            and re.search(r"(?:^|[\\/])\d+\.\d+\.\d+(?:[\\/]|$)", text) is not None
        )
        expanded = decoded
        for name, value in env.items():
            expanded = re.sub(
                rf"%{re.escape(name)}%",
                lambda _match, replacement=value: replacement,
                expanded,
                flags=re.IGNORECASE,
            )
        target_match = re.search(
            r"(?i)@?([A-Za-z]:\\[^\r\n%*]+?claude\.exe)", expanded,
        )
        if target_match is not None and cache_root is not None:
            candidate = Path(target_match.group(1).strip().strip('"'))
            try:
                candidate = candidate.resolve(strict=True)
                cache = cache_root.resolve(strict=True)
                selected_cache_below_root = _is_below(candidate, cache)
                if selected_cache_below_root and candidate.is_file():
                    selected = BoundCliIdentity.capture(candidate, version="unverified")
                    selected_cache_digest = selected.sha256
                    selected_cache_observed = True
                    selected_cache_distinct = candidate != standalone_path
            except OSError:
                pass
    try:
        wrapper_distinct = not wrapper_present or wrapper.resolve(strict=True) != standalone_path
    except OSError:
        wrapper_distinct = True
    rejection_complete = (
        wrapper_present
        and wrapper_distinct
        and bool(cache_root and cache_root.is_dir())
        and versioned_dependency
        and selected_cache_observed
        and selected_cache_below_root
        and selected_cache_distinct
    )
    return {
        "wrapper_present": wrapper_present,
        "wrapper_distinct_from_standalone": wrapper_distinct,
        "wrapper_content_sha256": wrapper_digest,
        "desktop_cache_root_present": bool(cache_root and cache_root.is_dir()),
        "versioned_cache_dependency_present": versioned_dependency,
        "selected_cache_target_observed": selected_cache_observed,
        "selected_cache_below_desktop_root": selected_cache_below_root,
        "selected_cache_distinct_from_standalone": selected_cache_distinct,
        "selected_cache_content_sha256": selected_cache_digest,
        "rejection_evidence_complete": rejection_complete,
        "desktop_runtime_accepted": False,
    }


def _roster_projection(payload: list[Any]) -> dict[str, Any]:
    normalized = normalize_agents(payload)
    states: dict[str, int] = {}
    for row in normalized:
        raw_state = row.get("state") or row.get("status") or "unknown"
        state = _category(raw_state, "roster state")
        states[state] = states.get(state, 0) + 1
    return {"row_count": len(normalized), "state_counts": dict(sorted(states.items()))}


def collect_host_evidence(
    root: str | Path,
    cli: str | Path,
    env: Mapping[str, str],
    *,
    project_root: str | Path,
    runner: Callable[..., ProbeResult] | None = None,
    trusted_items: set[TrustKey] | None = None,
    trust_revision: int = 1,
) -> dict[str, Any]:
    """Collect a sanitized no-model host projection without persisting raw CLI output."""
    del root  # Local opaque persistence is owned by the later approved evidence lane.
    try:
        initial = BoundCliIdentity.capture(cli, version="unverified")
    except (FileNotFoundError, OSError):
        return {"status": "INSTALL_REQUIRED", "next_action": "recheck"}
    executable_manifest = BoundExecutableManifest(
        repository_id="standalone-cli",
        trust_revision=trust_revision,
        entries=(BoundExecutableFile(
            canonical_path=initial.canonical_path,
            sha256=initial.sha256,
            file_identity=initial.file_identity,
        ),),
    )
    with executable_manifest.lease() as lease:
        return _collect_bound_host_evidence(
            initial,
            lease,
            cli,
            env,
            project_root=project_root,
            runner=runner,
            trusted_items=trusted_items,
            trust_revision=trust_revision,
        )


def _collect_bound_host_evidence(
    initial: BoundCliIdentity,
    lease: Any,
    cli: str | Path,
    env: Mapping[str, str],
    *,
    project_root: str | Path,
    runner: Callable[..., ProbeResult] | None,
    trusted_items: set[TrustKey] | None,
    trust_revision: int,
) -> dict[str, Any]:
    cli_argv = initial.canonical_path
    lease.verify_init_ack()
    try:
        version_result = _invoke_text(runner, "version", [cli_argv, "--version"], env=env)
    except (OSError, RuntimeError, TimeoutError, ValueError):
        return {"status": "incompatible", "reason": "version_probe_failed"}
    lease.verify_init_ack()
    version = parse_cli_version(version_result)
    if version is None or not initial.matches(cli):
        return {"status": "incompatible", "reason": "identity_or_version"}
    bound = BoundCliIdentity(
        canonical_path=initial.canonical_path,
        sha256=initial.sha256,
        file_identity=initial.file_identity,
        version=version,
    )

    overrides = _override_presence(env)
    if any(overrides.values()):
        return {
            "status": "credential_override",
            "observed_cli_version": version,
            "cli_content_sha256": bound.sha256,
            "identity_stable": bound.matches(cli),
            "credential_override_presence": overrides,
        }

    if not bound.matches(cli):
        return {"status": "incompatible", "reason": "identity_drift"}
    try:
        auth = normalize_auth(_invoke_json(
            runner, "auth_status", [cli_argv, "auth", "status"], dict, env=env,
        ))
        lease.verify_init_ack()
        auth_method = _category(auth.get("auth_method"), "auth method")
        api_provider = _category(auth.get("api_provider"), "api provider")
    except (RuntimeError, ValueError, PermissionError):
        return {"status": "incompatible", "reason": "auth_contract"}
    if auth["logged_in"] is not True:
        return {
            "status": "AUTH_REQUIRED",
            "observed_cli_version": version,
            "cli_content_sha256": bound.sha256,
            "identity_stable": True,
            "auth": {"logged_in": False, "auth_method": auth_method, "api_provider": api_provider},
            "next_action": "claude auth login",
        }
    if auth_method != "claude.ai" or api_provider != "firstParty":
        return {"status": "incompatible", "reason": "unsupported_auth_source"}

    if any(_override_presence(env).values()) or not bound.matches(cli):
        return {"status": "incompatible", "reason": "pre_agents_preflight"}
    try:
        roster = _roster_projection(_invoke_json(
            runner, "agents_json", [cli_argv, "agents", "--json", "--all"], list, env=env,
        ))
        lease.verify_init_ack()
    except (RuntimeError, ValueError, PermissionError):
        return {"status": "incompatible", "reason": "agents_contract"}

    manifest = scan_project(project_root)
    manifest["repository_id"] = _safe_repository_id(project_root)
    manifest_projection = project_manifest_evidence(
        manifest, trusted_items=trusted_items, trust_revision=trust_revision,
    )
    ownership = inspect_runtime_ownership(cli, env)
    return {
        "status": "ready" if manifest_projection["blocked_count"] == 0 else "project_content_untrusted",
        "observed_cli_version": version,
        "cli_content_sha256": bound.sha256,
        "cli_size": Path(bound.canonical_path).stat().st_size,
        "identity_stable": True,
        "auth": {
            "logged_in": True,
            "auth_method": auth_method,
            "api_provider": api_provider,
        },
        "credential_override_presence": overrides,
        "roster": roster,
        "wrapper_rejection": ownership,
        "project_manifest": manifest_projection,
    }


def collect_cli_capabilities(
    cli: str | Path,
    env: Mapping[str, str],
    *,
    runner: Callable[..., ProbeResult] | None = None,
) -> dict[str, bool]:
    """Recognize only the plan's non-mutating help surfaces; retain no help text."""
    bound = BoundCliIdentity.capture(cli, version="unverified")
    manifest = BoundExecutableManifest(
        repository_id="standalone-cli",
        trust_revision=0,
        entries=(BoundExecutableFile(
            canonical_path=bound.canonical_path,
            sha256=bound.sha256,
            file_identity=bound.file_identity,
        ),),
    )
    specs = (
        ("top_level", ("--help",), re.compile(r"(?im)^\s*usage:\s*claude(?:\s|\[|$)")),
        ("stop", ("stop", "--help"), re.compile(r"(?im)^\s*usage:\s*claude\s+stop\b")),
        ("respawn", ("respawn", "--help"), re.compile(r"(?im)^\s*usage:\s*claude\s+respawn\b")),
        ("attach", ("attach", "--help"), re.compile(r"(?im)^\s*usage:\s*claude\s+attach\b")),
        ("rm", ("rm", "--help"), re.compile(r"(?im)^\s*usage:\s*claude\s+rm\b")),
    )
    result: dict[str, bool] = {}
    with manifest.lease() as lease:
        for name, suffix, usage_pattern in specs:
            if any(_override_presence(env).values()) or not bound.matches(cli):
                raise PermissionError("CLI capability preflight drifted")
            probe = _invoke_text(
                runner, f"{name}_help", [bound.canonical_path, *suffix], env=env,
            )
            lease.verify_init_ack()
            if not bound.matches(cli):
                raise PermissionError("CLI capability identity drifted")
            combined = probe.stdout + probe.stderr
            size = len(combined.encode("utf-8"))
            result[f"{name}_help_recognized"] = (
                probe.exit_code == 0
                and not probe.timed_out
                and 0 < size <= _MAX_HELP_BYTES
                and usage_pattern.search(combined) is not None
            )
            if name == "top_level":
                result["tools_empty_documented"] = bool(
                    result["top_level_help_recognized"]
                    and _TOOLS_SYNTAX.search(combined)
                )
                result["prompt_suggestions_false_documented"] = bool(
                    result["top_level_help_recognized"]
                    and _PROMPT_SUGGESTIONS_SYNTAX.search(combined)
                )
    return result


def classify_inventory(
    *,
    residual_count: int = 0,
    plan_owned_count: int = 0,
    unknown_count: int = 0,
) -> str:
    """Classify sanitized residual counts without adopting unknown host state."""
    values = (residual_count, plan_owned_count, unknown_count)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("inventory counts must be non-negative integers")
    if unknown_count:
        return "user_or_unknown_residual"
    if residual_count == 0 and plan_owned_count == 0:
        return "expected_clean"
    if residual_count > 0 and plan_owned_count == residual_count:
        return "plan_owned_residual"
    return "recovery_required"


def build_inventory_projection(
    host_evidence: Mapping[str, Any],
    *,
    matching_process_count: int,
    live_worktree_count: int,
    plan_owned_count: int,
) -> dict[str, Any]:
    roster = host_evidence.get("roster", {})
    roster_count = roster.get("row_count", 0) if isinstance(roster, dict) else 0
    counts = (roster_count, matching_process_count, live_worktree_count, plan_owned_count)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError("inventory projection counts must be non-negative integers")
    residual_count = roster_count + matching_process_count + live_worktree_count
    unknown_count = max(0, residual_count - plan_owned_count)
    classification = (
        classify_inventory(
            residual_count=residual_count,
            plan_owned_count=plan_owned_count,
            unknown_count=unknown_count,
        )
        if host_evidence.get("status") == "ready"
        else "recovery_required"
    )
    return {
        "classification": classification,
        "roster_row_count": roster_count,
        "matching_process_count": matching_process_count,
        "live_worktree_count": live_worktree_count,
        "plan_owned_count": plan_owned_count,
    }


def load_plan_owned_count(path: str | Path) -> int:
    """Count only records carrying exact approval and target provenance."""
    target = Path(path)
    if not target.exists():
        return 0
    attributes = getattr(target.stat(follow_symlinks=False), "st_file_attributes", 0)
    if target.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError("ownership state must be a direct file")
    with target.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("ownership state exceeds 1 MiB")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("ownership state is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "records"}:
        raise ValueError("ownership state has an unexpected schema")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise ValueError("ownership state schema version is unsupported")
    records = payload["records"]
    if not isinstance(records, list) or len(records) > 128:
        raise ValueError("ownership records must be a bounded array")
    fingerprints: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "kind", "approval_digest", "target_fingerprint",
        }:
            raise ValueError("ownership record has an unexpected schema")
        if record["kind"] not in {"row", "process", "worktree"}:
            raise ValueError("ownership record kind is unsupported")
        if not isinstance(record["approval_digest"], str) or not _HEX64.fullmatch(record["approval_digest"]):
            raise ValueError("ownership approval digest is invalid")
        fingerprint = record["target_fingerprint"]
        if not isinstance(fingerprint, str) or not _HEX64.fullmatch(fingerprint):
            raise ValueError("ownership target fingerprint is invalid")
        if fingerprint in fingerprints:
            raise ValueError("ownership target fingerprint is duplicated")
        fingerprints.add(fingerprint)
    return len(records)


def _matching_cli_process_count(cli: str | Path) -> int:
    target = Path(cli).resolve(strict=True)
    if os.name == "nt":
        script = (
            "$target=[IO.Path]::GetFullPath($env:SUBAGENT_PHASE0A_CLI_TARGET);"
            "$count=@(Get-CimInstance Win32_Process | Where-Object {"
            "$_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $target"
            "}).Count;[Console]::Out.Write($count)"
        )
        process_env = dict(os.environ)
        process_env["SUBAGENT_PHASE0A_CLI_TARGET"] = str(target)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            shell=False,
            env=process_env,
        )
        output = result.stdout.strip()
        if result.returncode != 0 or len(output) > 32 or not output.isdecimal():
            raise RuntimeError("process inventory unavailable")
        return int(output)
    proc = Path("/proc")
    if not proc.is_dir():
        raise RuntimeError("process inventory unavailable")
    count = 0
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            executable = (entry / "exe").resolve(strict=True)
        except OSError:
            continue
        if executable == target:
            count += 1
    return count


def _is_below(path: Path, root: Path) -> bool:
    normalized_path = os.path.normcase(str(path.resolve(strict=False)))
    normalized_root = os.path.normcase(str(root.resolve(strict=False)))
    try:
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


def _live_worktree_count(project_root: str | Path, live_root: str | Path) -> int:
    project = Path(project_root).resolve(strict=True)
    owned_root = Path(live_root).resolve(strict=False)
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={project.as_posix()}",
            "-C",
            str(project),
            "worktree",
            "list",
            "--porcelain",
        ],
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
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 1024 * 1024:
        raise RuntimeError("Git worktree inventory unavailable")
    count = 0
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line[len("worktree "):])
        if _is_below(candidate, owned_root):
            count += 1
    return count


def _wait_for_file(path: Path, process: subprocess.Popen[bytes], deadline: float) -> None:
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            raise RuntimeError("handle helper exited before the ready event")
        time.sleep(0.01)
    raise TimeoutError("handle helper did not become ready")


def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _run_windows_handle_branch(root: Path, branch: str) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Windows handle branch is not available on this platform")
    if branch not in _HANDLE_BRANCHES:
        raise ValueError("unknown handle-matrix branch")
    root.mkdir(parents=True, exist_ok=True)
    settings = root / f"{branch}-settings.json"
    ready = root / f"{branch}-ready.txt"
    atomic_candidate = root / f"{branch}-atomic.json"
    settings.write_text('{"safe":true}\n', encoding="utf-8")
    ready.unlink(missing_ok=True)
    atomic_candidate.unlink(missing_ok=True)
    helper = Path(__file__).with_name("hold_file.ps1").resolve(strict=True)
    hold_ms = 1000 if branch == "success" else 30000
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-Path",
        str(settings),
        "-ReadyPath",
        str(ready),
        "-HoldMilliseconds",
        str(hold_ms),
    ]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    in_place_denied = False
    atomic_denied = False
    branch_observed = False
    try:
        _wait_for_file(ready, process, time.monotonic() + 5)
        try:
            settings.write_text('{"held":false}\n', encoding="utf-8")
        except PermissionError:
            in_place_denied = True
        atomic_candidate.write_text('{"replacement":true}\n', encoding="utf-8")
        try:
            os.replace(atomic_candidate, settings)
        except PermissionError:
            atomic_denied = True

        if branch == "success":
            process.wait(timeout=5)
            branch_observed = process.returncode == 0
        elif branch == "timeout":
            try:
                process.wait(timeout=0.01)
            except subprocess.TimeoutExpired:
                branch_observed = True
        elif branch == "cancelled":
            branch_observed = process.poll() is None
        elif branch == "child_failure":
            failure = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "exit 7"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
                shell=False,
            )
            branch_observed = failure.returncode == 7
        else:
            missing_child = root / f"missing-phase0a-{os.getpid()}-{time.monotonic_ns()}.exe"
            if missing_child.exists():
                raise RuntimeError("start-failure sentinel unexpectedly exists")
            try:
                unexpected = subprocess.Popen(
                    [str(missing_child)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                )
            except OSError:
                branch_observed = True
            else:
                _stop_owned_process(unexpected)
                if unexpected.stdout is not None:
                    unexpected.stdout.close()
                if unexpected.stderr is not None:
                    unexpected.stderr.close()
    finally:
        _stop_owned_process(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    started = time.monotonic()
    deadline = started + 5
    released_atomic = False
    while time.monotonic() < deadline:
        try:
            atomic_candidate.write_text('{"released":true}\n', encoding="utf-8")
            os.replace(atomic_candidate, settings)
            released_atomic = True
            break
        except PermissionError:
            time.sleep(0.01)
    in_place_after_release = False
    try:
        settings.write_text('{"released":true}\n', encoding="utf-8")
        in_place_after_release = True
    except PermissionError:
        pass
    ready.unlink(missing_ok=True)
    atomic_candidate.unlink(missing_ok=True)
    return {
        "held_save_denied": in_place_denied and atomic_denied,
        "branch_observed": branch_observed,
        "save_after_release": released_atomic and in_place_after_release,
        "release_latency_ms": round((time.monotonic() - started) * 1000),
    }


def run_windows_handle_matrix(
    root: str | Path,
    *,
    branch_runner: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run or deterministically exercise the five cleanup branches."""
    target = Path(root)
    if branch_runner is None:
        if os.name != "nt":
            return {
                "status": "not_applicable",
                "editor_application_canary": "not_run",
                "branches": {},
            }
        branch_runner = lambda branch: _run_windows_handle_branch(target, branch)

    results: dict[str, dict[str, bool]] = {}
    passed = True
    release_latencies: list[int] = []
    for branch in _HANDLE_BRANCHES:
        raw = branch_runner(branch)
        held = raw.get("held_save_denied") is True
        released = raw.get("save_after_release") is True
        branch_observed = raw.get("branch_observed") is True
        results[branch] = {
            "held_save_denied": held,
            "save_after_release": released,
        }
        latency = raw.get("release_latency_ms")
        if isinstance(latency, int) and not isinstance(latency, bool) and latency >= 0:
            release_latencies.append(latency)
        passed = passed and held and released and branch_observed
    return {
        "status": "pass" if passed else "blocked",
        "editor_application_canary": "not_run",
        "max_release_latency_ms": max(release_latencies, default=0),
        "branches": dict(sorted(results.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("--root", type=Path, required=True)
    inventory.add_argument("--cli", type=Path, required=True)
    inventory.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args()
    if args.command == "inventory":
        evidence = collect_host_evidence(
            args.root, args.cli, os.environ, project_root=args.project_root,
        )
        try:
            inventory_result = build_inventory_projection(
                evidence,
                matching_process_count=_matching_cli_process_count(args.cli),
                live_worktree_count=_live_worktree_count(args.project_root, args.root.parent),
                plan_owned_count=load_plan_owned_count(args.root.parent / "ownership.json"),
            )
        except (OSError, RuntimeError, ValueError):
            inventory_result = {
                "classification": "recovery_required",
                "roster_row_count": 0,
                "matching_process_count": 0,
                "live_worktree_count": 0,
                "plan_owned_count": 0,
            }
        print(json.dumps(inventory_result, sort_keys=True))
        return 0 if inventory_result["classification"] == "expected_clean" else 2
    if args.root is None or args.cli is None or args.project_root is None:
        parser.error("--root, --cli, and --project-root are required")
    evidence = collect_host_evidence(
        args.root, args.cli, os.environ, project_root=args.project_root,
    )
    capabilities = (
        collect_cli_capabilities(args.cli, os.environ)
        if evidence.get("status") == "ready"
        else {}
    )
    handles = run_windows_handle_matrix(args.root / "windows-handles")
    succeeded = (
        evidence.get("status") == "ready"
        and capabilities
        and all(capabilities.values())
        and handles.get("status") in {"pass", "not_applicable"}
    )
    if succeeded:
        write_bound_host_identity(
            args.root, args.cli, evidence, capabilities=capabilities,
        )
    print(json.dumps({"host": evidence, "capabilities": capabilities, "windows_handles": handles}, sort_keys=True))
    return 0 if succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
