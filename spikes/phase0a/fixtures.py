from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import redact_data, write_json_atomic


FIXTURE_SCHEMA_VERSION = 1
_MAX_RETAINED_SOURCE_BYTES = 64 * 1024 * 1024
_REQUIRED_TOP_LEVEL = {
    "fixture_schema_version",
    "kind",
    "observed_cli_version",
    "source",
    "coverage",
    "payload",
}
_FORBIDDEN_KEYS = {
    "authorization",
    "api_key",
    "api-key",
    "apikey",
    "auth_token",
    "auth-token",
    "authtoken",
    "oauth_token",
    "oauth-token",
    "oauthtoken",
    "password",
    "secret",
    "cookie",
    "email",
    "orgid",
    "org_id",
    "request_id",
    "requestid",
    "session_id",
    "sessionid",
    "id",
    "pid",
    "cwd",
    "transcript_path",
    "stdout",
    "stderr",
    "raw_output",
    "source_path",
    "run_id",
}
_SAFE_NORMALIZED_KEYS = {
    "api_provider",
    "auth",
    "auth_method",
    "final_result",
    "forbidden_surface_presence",
    "requested_model",
    "plugin_disable_effective",
    "relative_plugin_delta",
    "result_is_error",
    "result_subtype",
    "sha256",
    "source_hashes",
    "usage_credits_disabled_inferred",
}
_SAFE_TOKEN_COUNT_KEYS = {
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "input_tokens",
    "output_tokens",
}
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(sk-ant-[a-z0-9_-]+|bearer\s+\S+|"
    r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|"
    r"[a-z]:[\\/]+users[\\/]+[^\\/]+|/(?:home|users)/[^/]+|/root(?:/|$))"
)


@dataclass(frozen=True)
class RetainedSource:
    data: bytes
    sha256: str


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
    )


def read_retained_source(retained_root: str | Path, source_path: str | Path) -> RetainedSource:
    try:
        root = Path(retained_root).resolve(strict=True)
        candidate = Path(source_path)
        pre_open = candidate.lstat()
    except OSError:
        raise ValueError("retained source unavailable") from None
    if stat.S_ISLNK(pre_open.st_mode):
        raise ValueError("retained source must not be a symlink")
    if pre_open.st_size > _MAX_RETAINED_SOURCE_BYTES:
        raise ValueError("retained source exceeds 64 MiB limit")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ValueError("retained source unavailable") from None
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("source is outside retained root") from exc
    try:
        with resolved.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("retained source must be a regular file")
            if _file_signature(pre_open) != _file_signature(before):
                raise ValueError("retained source identity changed before read")
            data = stream.read(_MAX_RETAINED_SOURCE_BYTES + 1)
            if len(data) > _MAX_RETAINED_SOURCE_BYTES:
                raise ValueError("retained source exceeds 64 MiB limit")
            after = os.fstat(stream.fileno())
    except OSError:
        raise ValueError("retained source unavailable") from None
    if _file_signature(before) != _file_signature(after) or len(data) != before.st_size:
        raise ValueError("retained source changed during read")
    try:
        post_read = resolved.stat()
    except OSError:
        raise ValueError("retained source unavailable") from None
    if _file_signature(after) != _file_signature(post_read):
        raise ValueError("retained source identity changed during read")
    return RetainedSource(data=data, sha256=hashlib.sha256(data).hexdigest())


def _normalize_key(value: str) -> str:
    separated = _ACRONYM_BOUNDARY.sub(r"\1_\2", value)
    separated = _CAMEL_BOUNDARY.sub("_", separated)
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def _forbidden_key_class(raw_key: str) -> bool:
    key = _normalize_key(raw_key)
    if (
        key in _SAFE_NORMALIZED_KEYS
        or key.endswith("_present")
        or key.endswith("_count")
        or key in _SAFE_TOKEN_COUNT_KEYS
    ):
        return False
    tokens = set(key.split("_"))
    if key in _FORBIDDEN_KEYS or key in {"id", "pid", "cwd", "result"}:
        return True
    if key.endswith("_id") or key.endswith("_identifier"):
        return True
    if key.endswith("_at") or "timestamp" in tokens:
        return True
    if tokens & {
        "account",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "email",
        "oauth",
        "password",
        "plugin",
        "plugins",
        "prompt",
        "raw",
        "secret",
        "stderr",
        "stdout",
        "transcript",
    }:
        return True
    if key.endswith(("_text", "_content", "_body", "_message", "_response")):
        return True
    if ("auth" in tokens or "api" in tokens) and tokens & {"key", "token", "secret", "credential"}:
        return True
    if tokens & {"org", "organization"}:
        return True
    if "request" in tokens and tokens & {"id", "identifier"}:
        return True
    if "native" in tokens and tokens & {"id", "identifier", "name", "session"}:
        return True
    return False


def _require_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings")
    return value


def _scan_sensitive(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("fixture object keys must be strings")
            normalized_key = _normalize_key(raw_key)
            if normalized_key in {
                "hook_error_observed", "usage_credits_off_confirmed",
            } and type(item) is not bool:
                raise ValueError("fixture confirmation aggregate must be a boolean")
            if normalized_key == "plugin_disable_effective" and item not in {
                "PASS", "BLOCKED", "CAPABILITY_MISSING",
            }:
                raise ValueError("plugin aggregate must be a bounded status")
            if normalized_key == "relative_plugin_delta" and item is not None and (
                isinstance(item, bool) or not isinstance(item, int) or item < 0
            ):
                raise ValueError("plugin aggregate must be a non-negative integer or null")
            if normalized_key.endswith("_present") and type(item) is not bool:
                raise ValueError("fixture presence aggregate must be a boolean")
            if normalized_key.endswith("_count") or normalized_key in _SAFE_TOKEN_COUNT_KEYS:
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise ValueError("fixture count aggregate must be a non-negative integer")
            if normalized_key == "forbidden_surface_presence":
                if not isinstance(item, dict) or any(
                    not isinstance(name, str) or type(present) is not bool
                    for name, present in item.items()
                ):
                    raise ValueError("fixture presence aggregate must contain booleans")
            if _forbidden_key_class(raw_key):
                raise ValueError("forbidden fixture key class")
            _scan_sensitive(item)
    elif isinstance(value, list):
        for item in value:
            _scan_sensitive(item)
    elif isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        raise ValueError("fixture contains a credential, PII value, or absolute home path")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_usage_credits_disabled(rate_limit: dict[str, Any]) -> bool:
    return (
        rate_limit.get("overage_status") == "rejected"
        and rate_limit.get("overage_disabled_reason") == "out_of_credits"
        and rate_limit.get("is_using_overage") is False
    )


def validate_fixture(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _REQUIRED_TOP_LEVEL:
        raise ValueError("fixture must contain exactly the required top-level keys")
    if type(value["fixture_schema_version"]) is not int or value["fixture_schema_version"] != 1:
        raise ValueError("unsupported fixture_schema_version")
    for key in ("kind", "observed_cli_version"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"{key} must be a nonempty string")

    source = value["source"]
    if not isinstance(source, dict) or set(source) != {"kind", "sha256"}:
        raise ValueError("source must contain exactly kind and sha256")
    if not isinstance(source["kind"], str) or not source["kind"].strip():
        raise ValueError("source.kind must be a nonempty string")
    if not isinstance(source["sha256"], str) or not _LOWER_HEX_64.fullmatch(source["sha256"]):
        raise ValueError("source.sha256 must be a lowercase SHA-256 digest")

    coverage = value["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {"observed", "missing"}:
        raise ValueError("coverage must contain exactly observed and missing")
    observed = _require_string_list(coverage["observed"], "coverage.observed")
    missing = _require_string_list(coverage["missing"], "coverage.missing")
    if len(observed) != len(set(observed)) or len(missing) != len(set(missing)):
        raise ValueError("coverage arrays must not contain duplicates")
    if set(observed) & set(missing):
        raise ValueError("coverage observed and missing must not overlap")
    if observed != sorted(observed) or missing != sorted(missing):
        raise ValueError("coverage arrays must be sorted")
    if not isinstance(value["payload"], dict):
        raise ValueError("payload must be an object")
    _scan_sensitive(value)


def fixture_envelope(
    *,
    kind: str,
    observed_cli_version: str,
    source_kind: str,
    source_sha256: str,
    payload: dict[str, Any],
    observed: list[str],
    missing: list[str],
) -> dict[str, Any]:
    _scan_sensitive(payload)
    envelope = {
        "fixture_schema_version": FIXTURE_SCHEMA_VERSION,
        "kind": kind,
        "observed_cli_version": observed_cli_version,
        "source": {"kind": source_kind, "sha256": source_sha256},
        "coverage": {"observed": sorted(set(observed)), "missing": sorted(set(missing))},
        "payload": redact_data(payload),
    }
    validate_fixture(envelope)
    return envelope


def _combined_digest(digests: list[str]) -> str:
    return hashlib.sha256(("\n".join(sorted(digests)) + "\n").encode("ascii")).hexdigest()


def write_model_outcomes_fixture(
    requested_sources: list[tuple[str, str | Path]],
    output_path: str | Path,
    observed_cli_version: str,
    *,
    retained_root: str | Path,
) -> dict[str, Any]:
    from .contracts import classify_turn, normalize_stream_bytes

    if not requested_sources:
        raise ValueError("model outcomes require at least one retained source")
    outcome_records: list[tuple[str, dict[str, Any]]] = []
    source_hashes: list[str] = []
    for requested_model, source_path in requested_sources:
        source = read_retained_source(retained_root, source_path)
        normalized = normalize_stream_bytes(source.data)
        result = normalized["result"]
        if result is None:
            raise ValueError("model outcome source has no final result")
        source_digest = source.sha256
        source_hashes.append(source_digest)
        outcome_records.append(
            (source_digest, {
                "requested_model": requested_model,
                "observed_model": normalized["init"]["model"],
                "classification": classify_turn(normalized),
                "result_is_error": result["is_error"],
                "result_subtype": result["subtype"],
                "rate_limits": [
                    {
                        "status": item["status"],
                        "error_code": item["error_code"],
                        "overage_status": item["overage_status"],
                        "overage_disabled_reason": item["overage_disabled_reason"],
                        "is_using_overage": item["is_using_overage"],
                        "usage_credits_disabled_inferred": infer_usage_credits_disabled(item),
                    }
                    for item in normalized["rate_limits"]
                ],
                "cost_metadata_present": (
                    result["total_cost_usd"] is not None or result["usage"] is not None
                ),
            })
        )
    outcome_records.sort(
        key=lambda record: (
            str(record[1]["requested_model"]),
            str(record[1]["observed_model"]),
            record[0],
        )
    )
    outcomes = [outcome for _, outcome in outcome_records]
    fixture = fixture_envelope(
        kind="model_outcomes",
        observed_cli_version=observed_cli_version,
        source_kind="managed_model_stream_set",
        source_sha256=_combined_digest(source_hashes),
        payload={"outcomes": outcomes},
        observed=[
            "classification",
            "cost_metadata_present",
            "observed_model",
            "rate_limits",
            "requested_model",
            "result_is_error",
            "result_subtype",
            "usage_credits_disabled_inferred",
        ],
        missing=[],
    )
    write_json_atomic(output_path, fixture)
    return fixture


def write_strict_mcp_fixture(
    *,
    output_path: str | Path,
    observed_cli_version: str,
    retained_root: str | Path,
    role_sources: list[tuple[str, list[str | Path]]],
) -> dict[str, Any]:
    roles = [role for role, _ in role_sources]
    if len(roles) != len(set(roles)):
        raise ValueError("duplicate strict MCP role")
    if any(role not in {"strict", "control"} for role in roles):
        raise ValueError("ambiguous strict MCP role")
    if set(roles) != {"strict", "control"} or len(role_sources) != 2:
        raise ValueError("strict MCP evidence requires exactly strict and control roles")

    observations: dict[str, dict[str, Any]] = {}
    source_hashes: list[str] = []
    declared_server_count: int | None = None
    for role, paths in role_sources:
        if not paths:
            raise ValueError("strict MCP role has no retained sources")
        json_objects: list[dict[str, Any]] = []
        marker_count = 0
        for path in paths:
            source = read_retained_source(retained_root, path)
            source_hashes.append(source.sha256)
            if source.data == b"spawned":
                marker_count += 1
                continue
            try:
                value = json.loads(source.data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("ambiguous strict MCP retained artifact") from exc
            if not isinstance(value, dict):
                raise ValueError("ambiguous strict MCP retained artifact")
            json_objects.append(value)

        classified_objects: list[tuple[str, dict[str, Any]]] = []
        for value in json_objects:
            classes = []
            if {"exit_code", "marker_spawned"} <= set(value):
                classes.append("result")
            if "mcpServers" in value:
                classes.append("config")
            if len(classes) > 1:
                raise ValueError("overlapping strict MCP artifact classes")
            if not classes:
                raise ValueError(f"ambiguous strict MCP {role}-role artifacts")
            classified_objects.append((classes[0], value))

        result_candidates = [
            value for artifact_class, value in classified_objects
            if artifact_class == "result"
        ]
        if len(result_candidates) != 1:
            raise ValueError("ambiguous strict MCP role observation")
        observation = result_candidates[0]
        exit_code = observation["exit_code"]
        marker_spawned = observation["marker_spawned"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError("strict MCP exit_code must be an integer")
        if type(marker_spawned) is not bool:
            raise ValueError("strict MCP marker_spawned must be a boolean")

        if role == "strict":
            if type(observation.get("init_only_rejected")) is not bool or "interpretation" in observation:
                raise ValueError("ambiguous strict MCP role observation")
            configs = [
                value for artifact_class, value in classified_objects
                if artifact_class == "config"
            ]
            if len(json_objects) != 2 or len(configs) != 1 or marker_count:
                raise ValueError("ambiguous strict MCP strict-role artifacts")
            servers = configs[0]["mcpServers"]
            if not isinstance(servers, dict):
                raise ValueError("strict MCP declared servers must be an object")
            declared_server_count = len(servers)
        else:
            if observation.get("interpretation") != "CONTROL_PROVED_STARTUP" or "init_only_rejected" in observation:
                raise ValueError("ambiguous strict MCP role observation")
            if (
                len(classified_objects) != 1
                or classified_objects[0][0] != "result"
                or marker_count != 1
            ):
                raise ValueError("ambiguous strict MCP control-role artifacts")
        observations[role] = {
            "exit_success": exit_code == 0,
            "marker_spawned": marker_spawned,
        }

    strict = observations["strict"]
    control = observations["control"]
    if not (
        strict["exit_success"]
        and control["exit_success"]
        and strict["marker_spawned"] is False
        and control["marker_spawned"] is True
    ):
        raise ValueError("strict MCP differential did not pass")
    if declared_server_count is None:
        raise ValueError("strict MCP declared server count is missing")
    source_hashes.sort()
    fixture = fixture_envelope(
        kind="strict_mcp_control",
        observed_cli_version=observed_cli_version,
        source_kind="subagent-harness-mcp_marker_artifact_set",
        source_sha256=_combined_digest(source_hashes),
        payload={
            "declared_server_count": declared_server_count,
            "strict_marker_spawned": strict["marker_spawned"],
            "control_marker_spawned": control["marker_spawned"],
            "strict_exit_success": strict["exit_success"],
            "control_exit_success": control["exit_success"],
            "source_hashes": source_hashes,
        },
        observed=[
            "control_exit_success",
            "control_marker_spawned",
            "declared_server_count",
            "source_hashes",
            "strict_exit_success",
            "strict_marker_spawned",
        ],
        missing=[],
    )
    write_json_atomic(output_path, fixture)
    return fixture


def write_evidence_index(
    fixture_root: str | Path,
    observed_cli_version: str,
) -> dict[str, Any]:
    root = Path(fixture_root)
    output_path = root / "evidence-index.json"
    entries: dict[str, dict[str, str]] = {}
    for path in sorted(root.glob("*.json")):
        if path == output_path:
            continue
        fixture = json.loads(path.read_text(encoding="utf-8"))
        validate_fixture(fixture)
        entries[path.name] = {"sha256": sha256_file(path), "kind": fixture["kind"]}
    if not entries:
        raise ValueError("evidence index requires at least one fixture")
    canonical = json.dumps(entries, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    fixture = fixture_envelope(
        kind="evidence_index",
        observed_cli_version=observed_cli_version,
        source_kind="committed_fixture_set",
        source_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        payload={"fixtures": entries},
        observed=["fixture_hashes", "fixture_kinds"],
        missing=[],
    )
    write_json_atomic(output_path, fixture)
    return fixture
