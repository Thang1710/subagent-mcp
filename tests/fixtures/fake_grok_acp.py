"""Deterministic newline JSON-RPC child used by ACP stdio unit tests."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import time
from typing import Any


MAX_LINE_BYTES = 1_048_576
REVERSE_ID_CAP = 64
ACTIVE_CALLBACK_CAP = 16


def _read() -> dict[str, Any] | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("test client sent a non-object")
    return value


def _send(value: object, *, newline: bool = True) -> None:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    sys.stdout.buffer.write(payload + (b"\n" if newline else b""))
    sys.stdout.buffer.flush()


def _result(message: dict[str, Any]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {
            "requestId": message["id"],
            "method": message.get("method"),
            "params": message.get("params", {}),
        },
    }


def _serve_reverse(message: dict[str, Any], *, duplicate: bool) -> None:
    reverse = {
        "jsonrpc": "2.0",
        "id": "reverse-1",
        "method": "fs/read_text_file",
        "params": {"path": "README.md"},
    }
    _send(reverse)
    if duplicate:
        _send(reverse)
    response = _read()
    _send(
        {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"reverseResponse": response},
        }
    )


def _serve_reverse_id_cap(message: dict[str, Any]) -> None:
    for request_id in range(REVERSE_ID_CAP):
        _send(
            {
                "jsonrpc": "2.0",
                "id": f"reverse-{request_id}",
                "method": "fs/read_text_file",
                "params": {"path": f"file-{request_id}.txt"},
            }
        )
        if _read() is None:
            return
    _send(
        {
            "jsonrpc": "2.0",
            "id": "reverse-0",
            "method": "fs/read_text_file",
            "params": {"path": "duplicate.txt"},
        }
    )
    _send(
        {
            "jsonrpc": "2.0",
            "id": f"reverse-{REVERSE_ID_CAP}",
            "method": "fs/read_text_file",
            "params": {"path": "over-cap.txt"},
        }
    )
    time.sleep(60)


def _serve_callback_flood(*, notification: bool) -> None:
    for index in range(ACTIVE_CALLBACK_CAP + 1):
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": "session/update" if notification else "fs/read_text_file",
            "params": {"index": index},
        }
        if not notification:
            message["id"] = f"flood-{index}"
        _send(message)
    time.sleep(60)


def _serve_filesystem_reverse(message: dict[str, Any], *, operation: str) -> None:
    session_id = "native-session-1"
    if operation == "read":
        method = "fs/read_text_file"
        params = {
            "sessionId": session_id,
            "path": str((Path.cwd() / "README.md").resolve()),
        }
    elif operation in {"write", "write-eof", "write-hang"}:
        method = "fs/write_text_file"
        params = {
            "sessionId": session_id,
            "path": str((Path.cwd() / "exact.py").resolve()),
            "content": "written-through-acp\n",
        }
    elif operation == "write-denied":
        method = "fs/write_text_file"
        params = {
            "sessionId": session_id,
            "path": str((Path.cwd() / "other.py").resolve()),
            "content": "must-not-land\n",
        }
    else:
        method = "future/unknown"
        params = {}
    _send(
        {
            "jsonrpc": "2.0",
            "id": "filesystem-1",
            "method": method,
            "params": params,
        }
    )
    if operation == "write-eof":
        return
    response = _read()
    if operation == "write-hang":
        time.sleep(60)
        return
    _send(
        {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"reverseResponse": response},
        }
    )
    current = _read()
    while current is not None:
        if "id" in current:
            _send(_result(current))
        current = _read()


def _trace(
    path: Path,
    message: dict[str, Any],
    *,
    child_role: str | None = None,
) -> None:
    method = message.get("method")
    record: dict[str, object] = {"method": method}
    if child_role is not None:
        record["childRole"] = child_role
    params = message.get("params")
    if method == "initialize" and isinstance(params, dict):
        record["params"] = params
    elif method == "authenticate" and isinstance(params, dict):
        record["params"] = params
    elif method in {
        "_x.ai/billing",
        "_x.ai/auto-topup-rule",
        "_x.ai/models/list",
    } and isinstance(params, dict):
        record["params"] = params
    elif method == "session/new" and isinstance(params, dict):
        record["params"] = params
    elif method == "session/prompt" and isinstance(params, dict):
        prompt = params.get("prompt")
        text = ""
        if isinstance(prompt, list):
            text = "".join(
                str(item.get("text", ""))
                for item in prompt
                if isinstance(item, dict) and item.get("type") == "text"
            )
        record["params"] = {
            "sessionId": params.get("sessionId"),
            "promptBytes": len(text.encode("utf-8")),
            "promptSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    elif method == "session/cancel" and isinstance(params, dict):
        record["params"] = params
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
        stream.write("\n")


def _lifecycle_initialize(config: dict[str, Any]) -> dict[str, object]:
    delay = config.get("handshake_delay", 0)
    if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay > 0:
        time.sleep(float(delay))
    methods: list[dict[str, str]] = [
        {"id": "cached_token", "name": "Cached native login"},
        {"id": "grok.com", "name": "Grok"},
    ]
    default_method: object = "cached_token"
    mutation = config.get("mutation")
    if mutation == "missing-auth":
        methods = [{"id": "grok.com", "name": "Grok"}]
        default_method = "grok.com"
    elif mutation == "api-key":
        methods.append({"id": "xai.api_key", "name": "XAI API key"})
        default_method = "xai.api_key"
    elif mutation == "interactive-auth":
        default_method = "grok.com"
    elif mutation == "malformed-default-auth":
        default_method = None
    elif mutation == "duplicate-auth":
        methods.append({"id": "cached_token", "name": "Duplicate"})
    protocol_version = 2 if mutation == "protocol-mismatch" else 1
    return {
        "protocolVersion": protocol_version,
        "agentInfo": {"name": "Grok Build", "version": "synthetic"},
        "authMethods": methods,
        "_meta": {
            "defaultAuthMethodId": default_method,
            "modelState": {"currentModelId": config["model"]},
        },
    }


def _lifecycle_auth(config: dict[str, Any]) -> dict[str, object]:
    if config.get("mutation") == "malformed-auth-response":
        return {"authenticated": True, "methodId": "cached_token"}
    if config.get("mutation") == "auth-meta":
        return {"_meta": {"methodId": "cached_token"}}
    return {}


def _lifecycle_model_state(
    config: dict[str, Any],
    *,
    session: bool,
) -> dict[str, object]:
    model = str(config["model"])
    mutation = config.get(
        "session_model_state_mutation" if session else "model_state_mutation"
    )
    if not isinstance(mutation, str):
        mutation = config.get("mutation")
    current_model = (
        "different-model"
        if session and mutation == "models-current-mismatch"
        else model
    )
    configured_agent_type = config.get(
        "session_agent_type" if session else "model_agent_type",
        config.get("agent_type", "grok-build"),
    )
    metadata: dict[str, object] = {"agentType": configured_agent_type}
    if mutation == "agent-type-missing":
        metadata = {}
    elif mutation == "agent-type-malformed":
        metadata["agentType"] = True
    elif mutation == "agent-type-control":
        metadata["agentType"] = "unsafe\nagent"
    return {
        "currentModelId": current_model,
        "availableModels": [
            {
                "modelId": model,
                "name": model,
                "_meta": metadata,
            }
        ],
    }


def _lifecycle_agent_profile(mode: object) -> dict[str, object]:
    writer = mode == "writer"
    if not writer and mode != "review":
        raise ValueError("unsupported fake lifecycle mode")
    return {
        "name": "subagent-mcp-writer" if writer else "subagent-mcp-review",
        "description": (
            "Bounded Subagent MCP writer profile."
            if writer
            else "Bounded Subagent MCP review profile."
        ),
        "permissionMode": "bypassPermissions",
        "discoverSkills": False,
        "inheritSkills": False,
        "agentsMd": False,
        "injectDefaultTools": False,
        "tools": ["read_file", "search_replace"] if writer else ["read_file"],
        "disallowedTools": ["search_tool", "use_tool"],
        "skills": [],
        "mcpServers": [],
        "promptMode": "extend",
        "promptBody": "Follow the caller's requested final-output format exactly.",
    }


def _lifecycle_billing(
    config: dict[str, Any], _check_count: int
) -> dict[str, object]:
    mutation = config.get("billing_mutation")
    billing_config: dict[str, object] = {
        "prepaidBalance": {"val": 0},
        "onDemandCap": {"val": 0},
        "isUnifiedBillingUser": True,
    }
    result: dict[str, object] = {
        "config": billing_config,
    }
    if mutation == "billing-missing-config":
        result.pop("config")
    elif mutation == "included-exhausted":
        billing_config["creditUsagePercent"] = 100
    elif mutation == "included-invalid":
        billing_config["creditUsagePercent"] = "unknown"
    elif mutation == "serde-defaults":
        billing_config["prepaidBalance"] = {}
        billing_config["onDemandCap"] = {}
    elif mutation == "prepaid-nonzero":
        billing_config["prepaidBalance"] = {"val": 1}
    elif mutation == "prepaid-unknown":
        billing_config["prepaidBalance"] = {"val": None}
    elif mutation == "on-demand-cap-nonzero":
        billing_config["onDemandCap"] = {"val": 1}
    elif mutation == "on-demand-enabled":
        result["onDemandEnabled"] = True
    elif mutation == "not-unified-billing":
        billing_config["isUnifiedBillingUser"] = False
    return result


def _lifecycle_auto_topup(
    config: dict[str, Any], check_count: int
) -> dict[str, object]:
    mutation = config.get("billing_mutation")
    enabled = mutation == "auto-topup-enabled" or (
        mutation == "second-auto-topup-enabled" and check_count > 1
    )
    if mutation == "auto-topup-malformed":
        return {"rule": {"enabled": "unknown"}}
    if mutation == "serde-defaults":
        return {"rule": {}}
    if enabled or mutation == "auto-topup-disabled":
        return {"rule": {"enabled": enabled}}
    return {}


def _lifecycle_session_meta(config: dict[str, Any]) -> dict[str, object]:
    model = str(config["model"])
    effort = str(config["reasoning_effort"])
    workspace = str(config["workspace_path"])
    mutation = config.get("mutation")
    options: list[dict[str, object]] = [
        {"id": model, "category": "model", "label": model, "selected": True},
        {"id": effort, "category": "mode", "label": effort, "selected": True},
    ]
    if mutation == "model-mismatch":
        options[0]["id"] = "different-model"
    elif mutation == "missing-model":
        options.pop(0)
    elif mutation == "multiple-model":
        options.append(
            {
                "id": "other-model",
                "category": "model",
                "label": "Other",
                "selected": True,
            }
        )
    elif mutation == "reasoning-mismatch":
        options[1]["id"] = "different-effort"
    elif mutation == "missing-reasoning":
        options.pop()
    elif mutation == "multiple-reasoning":
        options.append(
            {
                "id": "other-effort",
                "category": "mode",
                "label": "Other",
                "selected": True,
            }
        )
    elif mutation == "malformed-selected":
        options[1]["selected"] = "yes"
    if mutation == "cwd-mismatch":
        workspace = str(Path(workspace).parent)
    result: dict[str, object] = {
        "currentWorkingDirectory": workspace,
        "x.ai/sessionConfig": {"options": options},
    }
    if mutation == "missing-cwd":
        result.pop("currentWorkingDirectory")
    elif mutation == "missing-session-config":
        result.pop("x.ai/sessionConfig")
    return result


def _lifecycle_update(session_id: str, update: dict[str, object]) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def _lifecycle_finish_prompt(
    message: dict[str, Any], config: dict[str, Any], session_id: str
) -> None:
    _lifecycle_update(
        session_id,
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "PRIVATE_REASONING"},
        },
    )
    if config.get("scenario") == "narrated-tool-answer":
        _lifecycle_update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "I will inspect the file."},
            },
        )
    _lifecycle_update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tool-1",
            "title": "PRIVATE_TOOL_FRAME",
        },
    )
    _lifecycle_update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tool-1",
            "status": "completed",
        },
    )
    _send(
        {
            "jsonrpc": "2.0",
            "method": "future/unknown",
            "params": {"secret": "PRIVATE_UNKNOWN_FRAME"},
        }
    )
    for chunk in config.get("assistant_chunks", ["APP", "ROVED"]):
        _lifecycle_update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": str(chunk)},
            },
        )
    error_code = config.get("error_code")
    if isinstance(error_code, str):
        error: dict[str, object] = {
            "code": error_code,
            "retryable": bool(config.get("error_retryable", False)),
            "message": "PRIVATE_PROVIDER_DETAIL",
        }
        if "error_detail" in config:
            error["detail"] = config["error_detail"]
        _send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "stopReason": "error",
                    "error": error,
                },
            }
        )
        return
    if config.get("scenario") == "malformed-terminal":
        _send({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}})
        return
    stop_reason = config.get("stop_reason", "end_turn")
    _send(
        {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"stopReason": stop_reason},
        }
    )


def _serve_grok_lifecycle(config: dict[str, Any], trace_path: Path) -> int:
    session_id = str(config.get("session_id", "native-grok-session-1"))
    pending_prompt: dict[str, Any] | None = None
    prompt_count = 0
    billing_check_count = 0
    current = _read()
    while current is not None:
        role = config.get("child_role")
        _trace(
            trace_path,
            current,
            child_role=role if isinstance(role, str) else None,
        )
        method = current.get("method")
        if method == config.get("handshake_rpc_method") and "id" in current:
            error: dict[str, object] = {
                "code": config.get("rpc_code", -32603),
                "message": config.get(
                    "rpc_message", "PRIVATE_RPC_PROVIDER_DETAIL"
                ),
            }
            if "rpc_data" in config:
                error["data"] = config["rpc_data"]
            _send({"jsonrpc": "2.0", "id": current["id"], "error": error})
        elif method == "initialize" and "id" in current:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "result": _lifecycle_initialize(config),
                }
            )
        elif method == "authenticate" and "id" in current:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "result": _lifecycle_auth(config),
                }
            )
        elif method in {
            "x.ai/billing",
            "x.ai/auto-topup-rule",
        } and "id" in current:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
        elif method == "_x.ai/billing" and "id" in current:
            billing_mutation = config.get("billing_mutation")
            if billing_mutation == "billing-process-exit":
                return 7
            if billing_mutation == "billing-timeout":
                time.sleep(60)
                return 0
            if billing_mutation == "billing-invalid-result":
                _send({"jsonrpc": "2.0", "id": current["id"], "result": []})
            elif billing_mutation == "billing-method-missing":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": current["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
            elif billing_mutation == "billing-explicit-exhaustion":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": current["id"],
                        "error": {
                            "code": -32603,
                            "message": "PRIVATE_QUOTA_DETAIL",
                            "data": {"providerCode": "quota_exhausted"},
                        },
                    }
                )
            else:
                billing_check_count += 1
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": current["id"],
                        "result": _lifecycle_billing(config, billing_check_count),
                    }
                )
        elif method == "_x.ai/auto-topup-rule" and "id" in current:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "result": _lifecycle_auto_topup(config, billing_check_count),
                }
            )
        elif method == "_x.ai/models/list" and "id" in current:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "result": {
                        "result": _lifecycle_model_state(config, session=False)
                    },
                }
            )
        elif method == "session/new" and "id" in current:
            params = current.get("params")
            if not isinstance(params, dict) or params.get("_meta") != {
                "agentProfile": _lifecycle_agent_profile(config.get("mode"))
            }:
                return 14
            (
                Path(os.environ["GROK_HOME"])
                / "sessions"
                / "E%3A%5Cworkspace"
                / session_id
            ).mkdir(parents=True, exist_ok=True)
            result: dict[str, object] = {
                "sessionId": session_id,
                "models": _lifecycle_model_state(config, session=True),
                "_meta": _lifecycle_session_meta(config),
            }
            if config.get("mutation") == "missing-session-id":
                result.pop("sessionId")
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "result": result,
                }
            )
        elif method == "session/prompt" and "id" in current:
            prompt_count += 1
            scenario = config.get("scenario", "happy")
            if scenario == "plan-exit-writer":
                if config.get("mode") != "writer":
                    return 15
                workspace = Path(str(config["workspace_path"]))
                plan_path = (
                    Path(os.environ["GROK_HOME"])
                    / "sessions"
                    / "E%3A%5Cworkspace"
                    / session_id
                    / "plan.md"
                )
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "internal-plan-write",
                        "method": "fs/write_text_file",
                        "params": {
                            "sessionId": session_id,
                            "path": str(plan_path.resolve()),
                            "content": "PRIVATE_PLAN_CONTENT",
                            "_meta": {},
                        },
                    }
                )
                if _read() != {
                    "jsonrpc": "2.0",
                    "id": "internal-plan-write",
                    "result": {},
                }:
                    _send(
                        {
                            "jsonrpc": "2.0",
                            "id": current["id"],
                            "result": {"stopReason": "cancelled"},
                        }
                    )
                    current = _read()
                    continue
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "internal-plan-read",
                        "method": "fs/read_text_file",
                        "params": {
                            "sessionId": session_id,
                            "path": str(plan_path.resolve()),
                            "_meta": {},
                        },
                    }
                )
                if _read() != {
                    "jsonrpc": "2.0",
                    "id": "internal-plan-read",
                    "result": {"content": "PRIVATE_PLAN_CONTENT"},
                }:
                    return 17
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "plan-exit",
                        "method": "x.ai/exit_plan_mode",
                        "params": {
                            "sessionId": session_id,
                            "toolCallId": "plan-tool-1",
                            "planContent": "PRIVATE_PLAN_CONTENT",
                        },
                    }
                )
                if _read() != {
                    "jsonrpc": "2.0",
                    "id": "plan-exit",
                    "result": {"outcome": "approved"},
                }:
                    _send(
                        {
                            "jsonrpc": "2.0",
                            "id": current["id"],
                            "result": {"stopReason": "cancelled"},
                        }
                    )
                    current = _read()
                    continue
                for request_id, path, content in (
                    (
                        "plan-write-one",
                        workspace / "allowed.txt",
                        "approved-write-one\n",
                    ),
                    (
                        "plan-write-two",
                        workspace / "generated" / "new.txt",
                        "approved-write-two\n",
                    ),
                ):
                    _send(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "fs/write_text_file",
                            "params": {
                                "sessionId": session_id,
                                "path": str(path.resolve()),
                                "content": content,
                                "_meta": {},
                            },
                        }
                    )
                    if _read() != {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {},
                    }:
                        return 16
            elif scenario == "filesystem-wire":
                workspace = Path(str(config["workspace_path"]))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "filesystem-read",
                        "method": "fs/read_text_file",
                        "params": {
                            "sessionId": session_id,
                            "path": str((workspace / "README.md").resolve()),
                            "line": 2,
                            "limit": 1,
                            "_meta": {"source": "fake-grok-acp"},
                        },
                    }
                )
                read_response = _read()
                if read_response != {
                    "jsonrpc": "2.0",
                    "id": "filesystem-read",
                    "result": {"content": "two\n"},
                }:
                    return 8
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "filesystem-write",
                        "method": "fs/write_text_file",
                        "params": {
                            "sessionId": session_id,
                            "path": str((workspace / "allowed.txt").resolve()),
                            "content": "written-through-real-acp-wire\n",
                            "_meta": {},
                        },
                    }
                )
                write_response = _read()
                if write_response != {
                    "jsonrpc": "2.0",
                    "id": "filesystem-write",
                    "result": {},
                }:
                    return 9
            elif scenario == "filesystem-review-boundary":
                if config.get("mode") != "review":
                    return 10
                workspace = Path(str(config["workspace_path"]))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "filesystem-review-read",
                        "method": "fs/read_text_file",
                        "params": {
                            "sessionId": session_id,
                            "path": str((workspace / "README.md").resolve()),
                            "line": 2,
                            "limit": 1,
                            "_meta": {"source": "fake-grok-acp"},
                        },
                    }
                )
                if _read() != {
                    "jsonrpc": "2.0",
                    "id": "filesystem-review-read",
                    "result": {"content": "two\n"},
                }:
                    return 11
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "filesystem-review-write",
                        "method": "fs/write_text_file",
                        "params": {
                            "sessionId": session_id,
                            "path": str((workspace / "denied.txt").resolve()),
                            "content": "must-not-write\n",
                            "_meta": {},
                        },
                    }
                )
                if _read() != {
                    "jsonrpc": "2.0",
                    "id": "filesystem-review-write",
                    "error": {"code": -32603, "message": "Internal error"},
                }:
                    return 12
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "filesystem-review-terminal",
                        "method": "terminal/create",
                        "params": {},
                    }
                )
                if _read() != {
                    "jsonrpc": "2.0",
                    "id": "filesystem-review-terminal",
                    "error": {"code": -32603, "message": "Internal error"},
                }:
                    return 13
            elif scenario in {
                "filesystem-terminal-race",
                "filesystem-dispatch-terminal-race",
            }:
                if config.get("mode") != "writer":
                    return 14
                workspace = Path(str(config["workspace_path"]))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": "filesystem-terminal-race-write",
                        "method": "fs/write_text_file",
                        "params": {
                            "sessionId": session_id,
                            "path": str((workspace / "allowed.txt").resolve()),
                            "content": "terminal-race-write\n",
                            "_meta": {},
                        },
                    }
                )
                if scenario == "filesystem-terminal-race":
                    deadline = time.monotonic() + 2
                    while not tuple(
                        workspace.glob(".allowed.txt.subagent-mcp-*.tmp")
                    ):
                        if time.monotonic() >= deadline:
                            return 15
                        time.sleep(0.005)
            if scenario == "process-exit":
                return 7
            if scenario == "rpc-error":
                error: dict[str, object] = {
                    "code": config.get("rpc_code", -32603),
                    "message": config.get(
                        "rpc_message", "PRIVATE_RPC_PROVIDER_DETAIL"
                    ),
                }
                if "rpc_data" in config:
                    error["data"] = config["rpc_data"]
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": current["id"],
                        "error": error,
                    }
                )
                continue
            if scenario in {
                "long",
                "cancelled",
                "cancel-late-success",
                "cancel-timeout",
            } or (scenario == "second-long" and prompt_count > 1):
                pending_prompt = current
            else:
                _lifecycle_finish_prompt(current, config, session_id)
                if scenario in {
                    "filesystem-terminal-race",
                    "filesystem-dispatch-terminal-race",
                }:
                    _trace(
                        trace_path,
                        {"method": "test/terminal-response-sent"},
                        child_role=str(config.get("child_role", "session")),
                    )
        elif method == "session/cancel" and pending_prompt is not None:
            if config.get("scenario") == "cancel-timeout":
                continue
            if config.get("scenario") == "cancel-late-success":
                late = dict(config)
                late["assistant_chunks"] = ["LATE SUCCESS"]
                _lifecycle_finish_prompt(pending_prompt, late, session_id)
            else:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": pending_prompt["id"],
                        "result": {"stopReason": "cancelled"},
                    }
                )
            pending_prompt = None
        current = _read()
    return 0


def main() -> int:
    scenario = sys.argv[1]

    if scenario == "grok-lifecycle":
        config = json.loads(sys.argv[2])
        if not isinstance(config, dict):
            raise TypeError("lifecycle config must be an object")
        return _serve_grok_lifecycle(config, Path(sys.argv[3]))

    if scenario == "ignore-eof":
        while _read() is not None:
            pass
        time.sleep(60)
        return 0

    first = _read()
    if first is None:
        return 0

    if scenario == "malformed":
        sys.stdout.buffer.write(b"{not-json}\n")
        sys.stdout.buffer.flush()
        return 0
    if scenario == "invalid-envelope":
        _send([])
        return 0
    if scenario == "invalid-utf8":
        sys.stdout.buffer.write(b"\xff\n")
        sys.stdout.buffer.flush()
        return 0
    if scenario == "unterminated":
        _send(_result(first), newline=False)
        return 0
    if scenario == "oversized":
        sys.stdout.buffer.write(b"{\"blob\":\"" + (b"x" * MAX_LINE_BYTES) + b"\"}\n")
        sys.stdout.buffer.flush()
        return 0
    if scenario == "eof":
        return 0
    if scenario == "stderr":
        sys.stderr.buffer.write(b"e" * (MAX_LINE_BYTES // 2))
        sys.stderr.buffer.flush()
        _send(_result(first))
        return 0
    if scenario == "rpc-error":
        _send(
            {
                "jsonrpc": "2.0",
                "id": first["id"],
                "error": {
                    "code": -32603,
                    "message": "bounded provider detail",
                    "data": {"providerCode": "TEST_ONLY"},
                },
            }
        )
        return 0
    if scenario in {"reverse", "reverse-duplicate"}:
        _serve_reverse(first, duplicate=scenario == "reverse-duplicate")
        return 0
    if scenario == "reverse-id-cap":
        _serve_reverse_id_cap(first)
        return 0
    if scenario == "reverse-active-flood":
        _serve_callback_flood(notification=False)
        return 0
    if scenario == "notification-active-flood":
        _serve_callback_flood(notification=True)
        return 0
    if scenario in {
        "filesystem-read",
        "filesystem-unknown",
        "filesystem-write",
        "filesystem-write-eof",
        "filesystem-write-hang",
        "filesystem-write-denied",
    }:
        _serve_filesystem_reverse(first, operation=scenario.removeprefix("filesystem-"))
        return 0
    if scenario == "slow-notification-response":
        _send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"kind": "slow"},
            }
        )
        _send(_result(first))
        return 0
    if scenario == "ordered-notifications-response":
        for index in (1, 2):
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"index": index},
                }
            )
        _send(_result(first))
        return 0
    if scenario == "correlate":
        second = _read()
        if second is None:
            return 1
        _send(_result(second))
        _send(_result(first))
        return 0
    if scenario == "unknown-and-duplicate":
        _send(
            {
                "jsonrpc": "2.0",
                "id": 999_999,
                "result": {"ignored": True},
            }
        )
        _send(_result(first))
        _send(_result(first))
        second = _read()
        if second is not None:
            _send(_result(second))
        return 0
    if scenario == "hang-once":
        _send(
            {
                "jsonrpc": "2.0",
                "method": "request/seen",
                "params": {"requestId": first["id"]},
            }
        )
        second = _read()
        if second is not None:
            _send(_result(second))
        return 0
    if scenario == "graceful-hang":
        _send(
            {
                "jsonrpc": "2.0",
                "method": "request/seen",
                "params": {"requestId": first["id"]},
            }
        )
        while _read() is not None:
            pass
        return 0

    current: dict[str, Any] | None = first
    while current is not None:
        if "id" not in current:
            if current.get("method") == "initialized":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"kind": "ready"},
                    }
                )
        elif current.get("method") == "identity":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "result": {
                        "argv": sys.argv[2:],
                        "cwd": os.getcwd(),
                        "env": os.environ.get("ACP_TEST_ENV"),
                    },
                }
            )
        else:
            _send(_result(current))
        current = _read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
