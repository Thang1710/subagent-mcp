from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path
from typing import Any

from .core import write_json_atomic
from .fixtures import (
    fixture_envelope,
    infer_usage_credits_disabled,
    read_retained_source,
    sha256_file,
)


STOP_FAILURE_CATEGORIES = (
    "rate_limit",
    "authentication_failed",
    "oauth_org_not_allowed",
    "billing_error",
    "invalid_request",
    "server_error",
    "max_output_tokens",
    "unknown",
)
TRANSPORT_CIRCUIT_CONDITIONS = STOP_FAILURE_CATEGORIES + (
    "model_not_found",
    "overloaded",
)
_STOP_FAILURE_PROVENANCE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

_MAX_STREAM_LINE_BYTES = 8 * 1024 * 1024
_JSON_DECODER = json.JSONDecoder()
_RATE_LIMIT_KEYS = {
    "status", "rateLimitType", "resetsAt", "utilization", "overageStatus",
    "overageDisabledReason", "isUsingOverage", "errorCode",
}


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _skip_string(text: str, index: int) -> int:
    if index >= len(text) or text[index] != '"':
        raise ValueError("expected JSON string")
    index += 1
    escaped = False
    while index < len(text):
        char = text[index]
        index += 1
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index
    raise ValueError("unterminated JSON string")


def _skip_value(text: str, index: int) -> int:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise ValueError("missing JSON value")
    if text[index] == '"':
        return _skip_string(text, index)
    if text[index] not in "[{":
        while index < len(text) and text[index] not in ",}":
            index += 1
        return index
    stack = ["]" if text[index] == "[" else "}"]
    index += 1
    while index < len(text) and stack:
        char = text[index]
        if char == '"':
            index = _skip_string(text, index)
            continue
        if char == "[":
            stack.append("]")
        elif char == "{":
            stack.append("}")
        elif char in "]}":
            if char != stack.pop():
                raise ValueError("mismatched JSON container")
        index += 1
    if stack:
        raise ValueError("unterminated JSON container")
    return index


def peek_top_level_type(line: str) -> str | None:
    if len(line.encode("utf-8")) > _MAX_STREAM_LINE_BYTES:
        raise ValueError("stream line exceeds 8 MiB")
    index = _skip_ws(line, 0)
    if index >= len(line) or line[index] != "{":
        return None
    index += 1
    item_type: str | None = None
    while True:
        index = _skip_ws(line, index)
        if index >= len(line) or line[index] == "}":
            return item_type
        key, end = _JSON_DECODER.raw_decode(line, index)
        if not isinstance(key, str):
            raise ValueError("top-level JSON key must be a string")
        index = _skip_ws(line, end)
        if index >= len(line) or line[index] != ":":
            raise ValueError("missing JSON colon")
        index = _skip_ws(line, index + 1)
        if key == "type":
            if item_type is not None:
                raise ValueError("multiple top-level type keys")
            value, end = _JSON_DECODER.raw_decode(line, index)
            if not isinstance(value, str):
                raise ValueError("top-level type must be a string")
            item_type = value
            index = _skip_ws(line, end)
        else:
            index = _skip_ws(line, _skip_value(line, index))
        if index < len(line) and line[index] == ",":
            index += 1
            continue
        if index < len(line) and line[index] == "}":
            return item_type
        raise ValueError("malformed top-level JSON object")


def _iter_stream_handle(stream):
    bom = b"\xef\xbb\xbf"
    read_limit = _MAX_STREAM_LINE_BYTES + len(bom) + 2
    first_line = True
    while raw_line := stream.readline(read_limit + 1):
        if first_line and raw_line.startswith(bom):
            raw_line = raw_line[len(bom):]
        first_line = False
        if raw_line.endswith(b"\n"):
            raw_line = raw_line[:-1]
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
        elif raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        if len(raw_line) > _MAX_STREAM_LINE_BYTES:
            raise ValueError("stream line exceeds 8 MiB")
        yield raw_line.decode("utf-8")


def _iter_stream_lines(path: str | Path):
    with Path(path).open("rb") as stream:
        yield from _iter_stream_handle(stream)


def _iter_stream_bytes(data: bytes):
    if not isinstance(data, bytes):
        raise ValueError("stream snapshot must be bytes")
    yield from _iter_stream_handle(io.BytesIO(data))


def _require_bool(payload: dict[str, Any], key: str, label: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _optional_bool(payload: dict[str, Any], key: str, label: str) -> bool | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _require_string_list(payload: dict[str, Any], key: str, label: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings")
    return value


def _require_nonempty_string(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _optional_string(payload: dict[str, Any], key: str, label: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    return value


def normalize_auth(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "logged_in": _require_bool(payload, "loggedIn", "loggedIn"),
        "auth_method": payload.get("authMethod"),
        "api_provider": payload.get("apiProvider"),
    }


def normalize_agents(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("agents payload must be an array")
    normalized: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("agent entry must be an object")
        agent_id = _optional_string(item, "id", "id")
        session_id = _optional_string(item, "sessionId", "sessionId")
        name = _optional_string(item, "name", "name")
        cwd = _optional_string(item, "cwd", "cwd")
        kind = _optional_string(item, "kind", "kind")
        state = _optional_string(item, "state", "state")
        status = _optional_string(item, "status", "status")
        normalized.append({
            "id_present": agent_id is not None,
            "session_id_present": session_id is not None,
            "name_present": name is not None,
            "cwd_present": cwd is not None,
            "kind": kind,
            "state": state,
            "pid_present": item.get("pid") is not None,
            "status": status,
            "waiting_for": item.get("waitingFor"),
            "started_at_present": item.get("startedAt") is not None,
        })
    return normalized


def normalize_stop_failure(payload: dict[str, Any]) -> dict[str, Any]:
    raw_category = payload.get("error")
    safe_raw = (
        raw_category
        if isinstance(raw_category, str)
        and _STOP_FAILURE_PROVENANCE.fullmatch(raw_category) is not None
        else "invalid"
    )
    category = safe_raw if safe_raw in STOP_FAILURE_CATEGORIES else "unknown"
    return {
        "category": category,
        "raw_category": safe_raw,
        "retry_after": payload.get("retry_after"),
    }


def normalize_transport_circuit_condition(value: Any) -> str:
    if not isinstance(value, str) or _STOP_FAILURE_PROVENANCE.fullmatch(value) is None:
        return "unknown"
    return value if value in TRANSPORT_CIRCUIT_CONDITIONS else "unknown"


def _normalize_rate_limit(item: dict[str, Any]) -> dict[str, Any]:
    info = item.get("rate_limit_info")
    if not isinstance(info, dict):
        info = {}
    return {
        "status": _optional_string(info, "status", "rate_limit_info.status"),
        "rate_limit_type": info.get("rateLimitType"),
        "resets_at": info.get("resetsAt"),
        "utilization": info.get("utilization"),
        "overage_status": info.get("overageStatus"),
        "overage_disabled_reason": info.get("overageDisabledReason"),
        "is_using_overage": _optional_bool(info, "isUsingOverage", "rate_limit_info.isUsingOverage"),
        "error_code": _optional_string(info, "errorCode", "rate_limit_info.errorCode"),
        "unknown_keys": sorted(key for key in info if key not in _RATE_LIMIT_KEYS),
    }


def classify_turn(normalized: dict[str, Any]) -> str:
    result = normalized.get("result")
    if not isinstance(result, dict):
        return "incomplete"
    is_error = result.get("is_error")
    if is_error is False:
        return "success"
    if is_error is not True:
        return "incomplete"
    rate_limits = normalized.get("rate_limits") or []
    if any(item.get("error_code") == "credits_required" for item in rate_limits):
        return "terminal_credits_required"
    if any(item.get("status") == "rejected" for item in rate_limits):
        return "terminal_quota"
    return "terminal_error"


def _normalize_stream_lines(lines) -> dict[str, Any]:
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    rate_limits: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        item_type = peek_top_level_type(line)
        if item_type not in {"system", "result", "rate_limit_event"}:
            continue
        item = json.loads(line)
        if item.get("type") != item_type:
            raise ValueError("top-level type changed after decode")
        if item.get("type") == "system" and item.get("subtype") == "init":
            if init is not None:
                raise ValueError("stream has multiple system/init events")
            mcp_servers = item.get("mcp_servers")
            plugins = item.get("plugins")
            if not isinstance(mcp_servers, list):
                raise ValueError("system.mcp_servers must be an array")
            if not isinstance(plugins, list):
                raise ValueError("system.plugins must be an array")
            if any(not isinstance(server, dict) for server in mcp_servers):
                raise ValueError("system.mcp_servers member must be an object")
            if any(not isinstance(plugin, dict) for plugin in plugins):
                raise ValueError("system.plugins member must be an object")
            init = {
                "model": _require_nonempty_string(item, "model", "system.model"),
                "tools": sorted(_require_string_list(item, "tools", "system.tools")),
                "mcp_servers": sorted(
                    [
                        {
                            "name": _require_nonempty_string(
                                server, "name", "system.mcp_servers.name"
                            ),
                            "status": _require_nonempty_string(
                                server, "status", "system.mcp_servers.status"
                            ),
                        }
                        for server in mcp_servers
                    ],
                    key=lambda server: str(server["name"]),
                ),
                "plugins": sorted(
                    [
                        _require_nonempty_string(
                            plugin, "name", "system.plugins.name"
                        )
                        for plugin in plugins
                    ]
                ),
                "capabilities": sorted(
                    _require_string_list(
                        item, "capabilities", "system.capabilities"
                    )
                ),
                "permission_mode": _require_nonempty_string(
                    item, "permissionMode", "system.permissionMode"
                ),
                "cwd_present": bool(
                    _require_nonempty_string(item, "cwd", "system.cwd")
                ),
            }
        elif item.get("type") == "rate_limit_event":
            rate_limits.append(_normalize_rate_limit(item))
        elif item.get("type") == "result":
            if result is not None:
                raise ValueError("stream has multiple result events")
            result = {
                "subtype": item.get("subtype"),
                "is_error": _require_bool(item, "is_error", "result.is_error"),
                "stop_reason": item.get("stop_reason"),
                "total_cost_usd": item.get("total_cost_usd"),
                "usage": item.get("usage"),
            }
    if init is None:
        raise ValueError("stream has no system/init event")
    return {"init": init, "rate_limits": rate_limits, "result": result}


def normalize_stream_bytes(data: bytes) -> dict[str, Any]:
    return _normalize_stream_lines(_iter_stream_bytes(data))


def normalize_stream_json(path: str | Path) -> dict[str, Any]:
    return _normalize_stream_lines(_iter_stream_lines(path))


_CONTEXT_OBSERVED = [
    "capabilities",
    "cost_metadata_present",
    "cwd_present",
    "final_result",
    "forbidden_surface_presence",
    "mcp_servers",
    "model",
    "permission_mode",
    "plugin_disable_effective",
    "rate_limit_advisory",
    "relative_plugin_delta",
    "tool_count",
    "usage_credits_disabled_inferred",
]
_CONTEXT_MISSING = [
    "additional_directories",
    "agents",
    "auto_compaction_window",
    "auto_memory_mode",
    "background_environment_equivalence",
    "bridge_hooks",
    "claude_rule_sources",
    "cleanup_period",
    "content_hashes",
    "inherited_hooks",
    "nested_agent_cap",
    "nested_agent_depth",
    "setting_sources",
    "skills",
    "system_prompt_append",
    "system_prompt_preset",
]


def _forbidden_surface_presence(names: list[str]) -> dict[str, bool]:
    lowered = [name.casefold() for name in names]
    return {
        "agent_bridge": any("agent-bridge" in name or "agent_bridge" in name or "agentbridge" in name for name in lowered),
        "codex": any("codex" in name for name in lowered),
        "subagent_mcp": any(
            "subagent-harness-mcp" in name or "subagent_mcp" in name or "subagent_harness_mcp" in name
            for name in lowered
        ),
    }


def _cost_metadata_present(result: dict[str, Any]) -> bool:
    total_cost = result["total_cost_usd"]
    if total_cost is not None and (isinstance(total_cost, bool) or not isinstance(total_cost, (int, float))):
        raise ValueError("result.total_cost_usd must be numeric or null")
    usage = result["usage"]
    if usage is None:
        pass
    elif isinstance(usage, dict):
        for key in (
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "input_tokens",
            "output_tokens",
        ):
            if key not in usage:
                continue
            value = usage[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"result.usage.{key} must be an integer")
    else:
        raise ValueError("result.usage must be an object or null")
    return total_cost is not None or usage is not None


def write_context_fixture(
    source_path: str | Path,
    output_path: str | Path,
    observed_cli_version: str,
    *,
    retained_root: str | Path,
) -> dict[str, Any]:
    source = read_retained_source(retained_root, source_path)
    normalized = normalize_stream_bytes(source.data)
    init = dict(normalized["init"])
    tool_names = init.pop("tools")
    plugin_names = init.pop("plugins")
    init["tool_count"] = len(tool_names)
    init["forbidden_surface_presence"] = _forbidden_surface_presence(
        tool_names + plugin_names
    )
    rate_limits = [
        {
            **{key: value for key, value in item.items() if key != "resets_at"},
            "usage_credits_disabled_inferred": infer_usage_credits_disabled(item),
        }
        for item in normalized["rate_limits"]
    ]
    result = normalized["result"]
    payload = {
        "init": init,
        "rate_limit_advisory": rate_limits,
        "final_result": None if result is None else {
            "subtype": result["subtype"],
            "is_error": result["is_error"],
            "stop_reason": result["stop_reason"],
        },
        "cost_metadata_present": False if result is None else _cost_metadata_present(result),
        "plugin_disable_effective": "BLOCKED",
        "relative_plugin_delta": None,
    }
    fixture = fixture_envelope(
        kind="context_attestation",
        observed_cli_version=observed_cli_version,
        source_kind="managed_proxy",
        source_sha256=source.sha256,
        payload=payload,
        observed=_CONTEXT_OBSERVED,
        missing=_CONTEXT_MISSING,
    )
    write_json_atomic(output_path, fixture)
    return fixture


_LIVE_CONTEXT_PUBLIC_KEYS = (
    "cli_content_sha256",
    "status",
    "init_subset_status",
    "terminal_classification",
    "process_exit_code",
    "init_envelope_observed",
    "result_envelope_observed",
    "timeout_phase",
    "requested_model",
    "requested_effort",
    "requested_setting_sources",
    "requested_auto_compaction_window_tokens",
    "requested_auto_compaction_trigger_percent",
    "requested_auto_compaction_trigger_tokens",
    "effective_model",
    "effective_effort",
    "effective_setting_sources",
    "effective_auto_compaction_window_tokens",
    "effective_auto_compaction_trigger_percent",
    "effective_auto_compaction_trigger_formula",
    "effective_auto_compaction_trigger_tokens",
    "auto_memory_mode",
    "effective_cleanup_period",
    "claude_md_sources",
    "rule_sources",
    "skill_sources",
    "agent_sources",
    "extension_sources_attested",
    "inherited_hook_sources",
    "subagent_mcp_hook_sources",
    "declared_mcp_servers",
    "tool_allow_rules",
    "tool_deny_rules",
    "nested_agent_cap",
    "nested_agent_depth",
    "additional_directories",
    "system_preset_attested",
    "system_append_attested",
    "content_hashes",
    "attestation_sources",
    "tool_count",
    "mcp_server_count",
    "plugin_disable_effective",
    "relative_plugin_delta",
    "is_using_overage",
    "rate_statuses",
    "final_marker_matched",
    "checkout_clean",
    "instructions_loaded",
    "attested_configuration",
    "production_equivalent_attestation",
    "declared_native_attestation",
    "background_eligible",
    "usage_credits_off_confirmed",
    "hook_error_observed",
    "missing_fields",
)


def write_live_context_fixture(
    projection: dict[str, Any],
    output_path: str | Path,
    observed_cli_version: str,
    *,
    source_sha256: str,
) -> dict[str, Any]:
    missing_keys = [key for key in _LIVE_CONTEXT_PUBLIC_KEYS if key not in projection]
    if missing_keys:
        raise ValueError("live context projection is incomplete")
    if re.fullmatch(r"[0-9a-f]{64}", str(projection["cli_content_sha256"])) is None:
        raise ValueError("live context CLI digest is invalid")
    missing = projection["missing_fields"]
    if (
        not isinstance(missing, list)
        or any(not isinstance(item, str) or not item for item in missing)
        or missing != sorted(set(missing))
    ):
        raise ValueError("live context missing_fields must be sorted and unique")
    payload = {key: projection[key] for key in _LIVE_CONTEXT_PUBLIC_KEYS}
    observed = [
        key for key, value in payload.items()
        if value is not None and key not in set(missing)
    ]
    fixture = fixture_envelope(
        kind="live_context_attestation",
        observed_cli_version=observed_cli_version,
        source_kind="live_context_projection",
        source_sha256=source_sha256,
        payload=payload,
        observed=observed,
        missing=missing,
    )
    write_json_atomic(output_path, fixture)
    return fixture


def _fixture_agents(payload: Any) -> list[dict[str, Any]]:
    normalized = normalize_agents(payload)
    for raw, item in zip(payload, normalized):
        waiting_for = raw.get("waitingFor")
        if waiting_for is not None and not isinstance(waiting_for, str):
            raise ValueError("waitingFor must be a string or null")
        item.pop("waiting_for")
        item["waiting_for_present"] = "waitingFor" in raw
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--agents", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retained-root", type=Path, required=True)
    args = parser.parse_args()
    version_source = read_retained_source(args.retained_root, args.version_file)
    auth_source = read_retained_source(args.retained_root, args.auth)
    agents_source = read_retained_source(args.retained_root, args.agents)
    version = version_source.data.decode("utf-8-sig").strip()
    auth = normalize_auth(json.loads(auth_source.data.decode("utf-8-sig")))
    agents = _fixture_agents(json.loads(agents_source.data.decode("utf-8-sig")))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        args.output_dir / "auth-status.json",
        fixture_envelope(
            kind="auth_status",
            observed_cli_version=version,
            source_kind="auth_status_json",
            source_sha256=auth_source.sha256,
            payload={"auth": auth},
            observed=["auth.api_provider", "auth.auth_method", "auth.logged_in"],
            missing=[],
        ),
    )
    write_json_atomic(
        args.output_dir / "agents-normalized.json",
        fixture_envelope(
            kind="agents_normalized",
            observed_cli_version=version,
            source_kind="agents_json",
            source_sha256=agents_source.sha256,
            payload={"agents": agents},
            observed=["agents"],
            missing=[],
        ),
    )
    write_json_atomic(
        args.output_dir / "stop-failure-contract.json",
        fixture_envelope(
            kind="stop_failure_contract",
            observed_cli_version=version,
            source_kind="subagent-harness-mcp_contract_source",
            source_sha256=sha256_file(__file__),
            payload={
                "documented_categories": list(STOP_FAILURE_CATEGORIES),
                "non_hook_transport_conditions": [
                    "model_not_found", "overloaded",
                ],
                "unknown_value_policy": (
                    "map_to_unknown_with_bounded_redacted_safe_raw_category"
                ),
            },
            observed=[
                "documented_categories",
                "non_hook_transport_conditions",
                "unknown_value_policy",
            ],
            missing=["live_stop_failure_observation"],
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
