from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .contracts import normalize_agents, normalize_auth
from .core import run_argv, write_json_atomic


_CREDENTIAL_OVERRIDE_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_WRAPPER_REJECTION_REASONS = (
    "wrapper_ownership_is_not_the_standalone_cli",
    "wrapper_swap_and_cleanup_lifecycle_risk",
    "observer_visibility_is_not_wrapper_acceptance",
)
_SAFE_CATEGORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_CONTRACT = re.compile(r"^\d+\.\d+\.\d+ \(Claude Code\)$")
_MAX_VERSION_BYTES = 128


def path_record(path: str | Path) -> dict[str, Any]:
    """Return non-sensitive filesystem metadata without retaining the path."""
    target = Path(path)
    try:
        stat = target.stat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size if target.is_file() else None,
        "mtime_ns": stat.st_mtime_ns,
    }


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while chunk := stream.read(64 * 1024):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _same_file_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
    )


def _bound_file_identity(target: Path, stream: BinaryIO) -> dict[str, Any] | None:
    handle_stat = os.fstat(stream.fileno())
    before_path_stat = target.stat()
    digest = _hash_stream(stream)
    after_path_stat = target.stat()
    if not (
        _same_file_stat(handle_stat, before_path_stat)
        and _same_file_stat(handle_stat, after_path_stat)
    ):
        return None
    return {
        "canonical_path": str(target),
        "device": handle_stat.st_dev,
        "inode": handle_stat.st_ino,
        "size": handle_stat.st_size,
        "sha256": digest,
    }


def executable_identity(path: str | Path, *, observed_version: str) -> dict[str, Any]:
    """Record a stable canonical executable identity, never command output."""
    target = Path(path).resolve(strict=True)
    with target.open("rb") as stream:
        identity = _bound_file_identity(target, stream)
    if identity is None:
        raise ValueError("executable identity changed while hashing")
    return {**identity, "observed_version": observed_version}


def credential_precedence_ok(env: Mapping[str, str]) -> bool:
    return not any(env.get(name) for name in _CREDENTIAL_OVERRIDE_NAMES)


def _probe_status(result: Any) -> dict[str, bool]:
    return {"exit_ok": result.exit_code == 0, "timed_out": bool(result.timed_out)}


def _probe_failure(result: Any, reason: str = "nonzero_or_timeout") -> dict[str, Any]:
    return {"status": "probe_failed", "reason": reason, "probe": _probe_status(result)}


def _safe_category(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_CATEGORY.fullmatch(value):
        raise ValueError("value must be a bounded category string")
    return value


def _safe_optional_category(value: Any) -> str | None:
    return None if value is None else _safe_category(value)


def _normalize_auth_contract(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_auth(payload)
    return {
        "logged_in": normalized["logged_in"],
        "auth_method": _safe_category(normalized["auth_method"]),
        "api_provider": _safe_category(normalized["api_provider"]),
    }


def _normalize_agents_contract(payload: list[Any]) -> list[dict[str, Any]]:
    normalized = normalize_agents(payload)
    contract: list[dict[str, Any]] = []
    for raw, agent in zip(payload, normalized):
        if isinstance(raw.get("waitingFor"), (dict, list)):
            raise ValueError("waitingFor must not be nested")
        contract.append({
            "id_present": agent["id_present"],
            "session_id_present": agent["session_id_present"],
            "name_present": agent["name_present"],
            "cwd_present": agent["cwd_present"],
            "kind": _safe_optional_category(agent["kind"]),
            "state": _safe_optional_category(agent["state"]),
            "pid_present": agent["pid_present"],
            "status": _safe_optional_category(agent["status"]),
            "waiting_for_present": "waitingFor" in raw,
            "started_at_present": agent["started_at_present"],
        })
    return contract


def _parse_normalized(result: Any, expected_type: type, normalize) -> Any:
    if result.exit_code != 0 or result.timed_out:
        return _probe_failure(result)
    try:
        decoded = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _probe_failure(result, "malformed_output")
    if not isinstance(decoded, expected_type):
        return _probe_failure(result, "malformed_output")
    try:
        return normalize(decoded)
    except ValueError:
        return _probe_failure(result, "malformed_output")


def parse_cli_version(result: Any) -> str | None:
    if result.exit_code != 0 or result.timed_out:
        return None
    output = result.stdout
    if not isinstance(output, str) or len(output.encode("utf-8")) > _MAX_VERSION_BYTES:
        return None
    if output.endswith("\r\n"):
        output = output[:-2]
    elif output.endswith("\n") or output.endswith("\r"):
        output = output[:-1]
    if "\r" in output or "\n" in output or not _VERSION_CONTRACT.fullmatch(output):
        return None
    return output


def _not_run_payload() -> dict[str, str]:
    return {"status": "probe_not_run"}


def compare_observers(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    mismatches: dict[str, Any] = {}
    observed_present: list[str] = []
    left_paths = left.get("paths", {})
    right_paths = right.get("paths", {})
    for name in sorted(set(left_paths) | set(right_paths)):
        left_record = left_paths.get(name, {})
        right_record = right_paths.get(name, {})
        left_exists = bool(left_record.get("exists"))
        right_exists = bool(right_record.get("exists"))
        if left_exists or right_exists:
            observed_present.append(name)
        if left_exists != right_exists:
            mismatches[name] = {
                "left_exists": left_exists,
                "right_exists": right_exists,
            }
            continue
        if not left_exists:
            continue
        left_identity = (left_record.get("device"), left_record.get("inode"))
        right_identity = (right_record.get("device"), right_record.get("inode"))
        if None in left_identity or None in right_identity:
            mismatches[name] = {"identity": "missing"}
        elif left_identity != right_identity:
            mismatches[name] = {"identity": "different"}
    status = "mismatch" if mismatches else (
        "matched_present" if observed_present else "not_observed"
    )
    return {
        "status": status,
        "mismatches": mismatches,
        "observed_present": observed_present,
    }


def build_snapshot(
    observer: str,
    claude_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    runner=run_argv,
) -> dict[str, Any]:
    probe_env = os.environ if env is None else env
    appdata = probe_env.get("APPDATA")
    cache_root = Path(appdata) / "Claude" / "claude-code" if appdata else None
    wrapper = Path.home() / "bin" / "claude.cmd"
    credential_env_present = {
        name: bool(probe_env.get(name)) for name in _CREDENTIAL_OVERRIDE_NAMES
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "observer": observer,
        "paths": {
            "desktop_cache_root": path_record(cache_root) if cache_root else {"exists": False},
            "wrapper": path_record(wrapper),
        },
        "wrapper_evaluation": {
            "accepted": False,
            "rejection_reasons": list(_WRAPPER_REJECTION_REASONS),
        },
        "credential_env_present": credential_env_present,
        "credential_overrides_absent": credential_precedence_ok(probe_env),
        "credential_precedence_evidence": "not_observed",
    }
    try:
        canonical_cli = Path(claude_path).resolve(strict=True)
    except FileNotFoundError:
        payload.update({
            "standalone_cli": {"status": "not_found"},
            "auth": _not_run_payload(),
            "agents": _not_run_payload(),
            "probes": {},
        })
        return payload

    with canonical_cli.open("rb") as stream:
        before_identity = _bound_file_identity(canonical_cli, stream)
        if before_identity is None:
            payload.update({
                "standalone_cli": {"status": "probe_failed", "reason": "identity_changed"},
                "auth": _not_run_payload(),
                "agents": _not_run_payload(),
                "probes": {},
            })
            return payload
        version_result = runner(
            "version", [str(canonical_cli), "--version"], timeout_seconds=30, env=probe_env
        )
        after_identity = _bound_file_identity(canonical_cli, stream)

    payload["probes"] = {"version": _probe_status(version_result)}
    if after_identity is None or before_identity != after_identity:
        payload.update({
            "standalone_cli": _probe_failure(version_result, "identity_changed"),
            "auth": _not_run_payload(),
            "agents": _not_run_payload(),
        })
        return payload
    version = parse_cli_version(version_result)
    if version is None:
        payload.update({
            "standalone_cli": _probe_failure(
                version_result,
                "malformed_output" if version_result.exit_code == 0 and not version_result.timed_out else "nonzero_or_timeout",
            ),
            "auth": _not_run_payload(),
            "agents": _not_run_payload(),
        })
        return payload

    payload["standalone_cli"] = {**before_identity, "observed_version": version}
    auth_result = runner(
        "auth_status", [str(canonical_cli), "auth", "status"], timeout_seconds=30, env=probe_env
    )
    agents_result = runner(
        "agents_json", [str(canonical_cli), "agents", "--json", "--all"], timeout_seconds=30, env=probe_env
    )
    payload["probes"].update({
        "auth_status": _probe_status(auth_result),
        "agents_json": _probe_status(agents_result),
    })
    payload["auth"] = _parse_normalized(auth_result, dict, _normalize_auth_contract)
    payload["agents"] = _parse_normalized(agents_result, list, _normalize_agents_contract)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", required=True)
    parser.add_argument("--claude-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_json_atomic(args.output, build_snapshot(args.observer, args.claude_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
