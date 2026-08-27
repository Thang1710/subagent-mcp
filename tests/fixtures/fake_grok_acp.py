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
    if operation == "read":
        method = "fs/read_text_file"
        params = {"path": "README.md"}
    elif operation in {"write", "write-eof", "write-hang"}:
        method = "fs/write_text_file"
        params = {"path": "exact.py", "content": "written-through-acp\n"}
    elif operation == "write-denied":
        method = "fs/write_text_file"
        params = {"path": "other.py", "content": "must-not-land\n"}
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


def _trace(path: Path, message: dict[str, Any]) -> None:
    method = message.get("method")
    record: dict[str, object] = {"method": method}
    params = message.get("params")
    if method == "initialize" and isinstance(params, dict):
        record["params"] = params
    elif method == "authenticate" and isinstance(params, dict):
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
        {"id": "cached_token", "name": "Cached native login"}
    ]
    if config.get("mutation") == "missing-auth":
        methods = []
    return {
        "protocolVersion": 1,
        "agentInfo": {"name": "Grok Build", "version": "synthetic"},
        "authMethods": methods,
    }


def _lifecycle_auth(config: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {
        "authenticated": True,
        "methodId": "cached_token",
        "authMethod": "cached-native",
        "apiKeyOverride": False,
        "customPaidRoute": False,
        "noExtraSpend": True,
    }
    mutation = config.get("mutation")
    if mutation == "api-key":
        result.update(
            methodId="xai.api_key",
            authMethod="api-key",
            apiKeyOverride=True,
            noExtraSpend=False,
        )
    elif mutation == "custom-paid":
        result["customPaidRoute"] = True
        result["noExtraSpend"] = False
    return result


def _lifecycle_attestation(config: dict[str, Any]) -> dict[str, object]:
    mode = str(config["mode"])
    tools = ["read_file", "search_files"]
    routes = [["read_file", "repo_read"], ["search_files", "repo_read"]]
    if mode == "writer":
        tools = ["read_file", "write_file"]
        routes = [
            ["read_file", "repo_read"],
            ["write_file", "workspace_write_bridge"],
        ]
    result: dict[str, object] = {
        "pairKey": config["pair_key"],
        "workspaceKey": config["workspace_key"],
        "workspacePath": config["workspace_path"],
        "mode": mode,
        "reasoningEffort": config["reasoning_effort"],
        "builtinToolNames": tools,
        "permissionRoutes": routes,
        "loadedExecutableExtensions": [],
        "disabledExecutableExtensions": config.get("disabled_extensions", []),
        "webSearchEnabled": False,
        "nestedAgentsEnabled": False,
        "terminalEnabled": False,
        "quotaState": config.get("quota_state", "unknown"),
    }
    mutation = config.get("mutation")
    if mutation == "pair-mismatch":
        result["pairKey"] = "f" * 64
    elif mutation == "unsafe-route":
        result["builtinToolNames"] = ["read_file", "shell"]
        result["permissionRoutes"] = [
            ["read_file", "repo_read"],
            ["shell", "terminal"],
        ]
    elif mutation == "missing-isolation":
        result.pop("terminalEnabled")
    elif mutation == "loaded-extension":
        result["loadedExecutableExtensions"] = [["mcp", "unsafe-mcp"]]
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
    _lifecycle_update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "title": "PRIVATE_TOOL_FRAME",
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
    _send(
        {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"stopReason": "end_turn"},
        }
    )


def _serve_grok_lifecycle(config: dict[str, Any], trace_path: Path) -> int:
    session_id = str(config.get("session_id", "native-grok-session-1"))
    pending_prompt: dict[str, Any] | None = None
    prompt_count = 0
    current = _read()
    while current is not None:
        _trace(trace_path, current)
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
        elif method == "session/new" and "id" in current:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": current["id"],
                    "result": {
                        "sessionId": session_id,
                        "models": {"currentModelId": config["model"]},
                        "_meta": {
                            "subagentMcp": _lifecycle_attestation(config),
                        },
                    },
                }
            )
        elif method == "session/prompt" and "id" in current:
            prompt_count += 1
            scenario = config.get("scenario", "happy")
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
