from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent

from subagent_harness_mcp import __version__
from subagent_harness_mcp.contracts import (
    ADAPTER_API_VERSION,
    AdapterManifest,
    AgentDescriptor,
    AgentEvent,
    AgentStatus,
    ServiceError,
)
from subagent_harness_mcp.server import create_server


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOL_NAMES = {
    "runtime_list",
    "runtime_check",
    "runtime_configure",
    "runtime_canary",
    "project_scan",
    "project_trust",
    "agent_spawn",
    "agent_status",
    "agent_send",
    "agent_wait",
    "agent_interrupt",
    "agent_close",
    "workspace_release",
}
SIDE_EFFECTING_TOOL_NAMES = {
    "runtime_configure",
    "runtime_canary",
    "project_trust",
    "agent_spawn",
    "agent_send",
    "agent_interrupt",
    "agent_close",
    "workspace_release",
}


def _run(awaitable):
    return asyncio.run(awaitable)


def _metadata(result: CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    marker = "\n```subagent-mcp-meta\n"
    markdown, separator, encoded = content.text.partition(marker)
    assert separator == marker
    assert markdown.strip()
    assert encoded.endswith("\n```")
    return json.loads(encoded[:-4])


def _terminal_status() -> AgentStatus:
    manifest = AdapterManifest(
        adapter_api_version=ADAPTER_API_VERSION,
        runtime_id="future-runtime",
        provider_id="future-provider",
        harness_id="future-harness",
        display_name="Future sub-agent",
        adapter_version="1.0.0",
        supported_platforms=("win32",),
        supported_transports=("managed-sdk",),
        capabilities=frozenset({"session"}),
        semantic_permissions=frozenset({"repo_read"}),
        reasoning_schema={"type": "object"},
    )
    descriptor = AgentDescriptor.from_manifest(
        manifest,
        model="vendor/model",
        transport="managed-sdk",
    )
    result = {"text": "one provider result", "model": "vendor/model"}
    return AgentStatus(
        conversation_id="conversation-1",
        execution_id="execution-1",
        external_session_id="native-session-1",
        workspace_path=r"C:\private\workspace",
        conversation_state="idle",
        execution_state="succeeded",
        state_revision=3,
        descriptor=descriptor,
        result=result,
        needs_input=(),
        events=(AgentEvent(5, "completed", {"result": result}),),
        next_event_cursor=5,
    )


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def runtime_list(self):
        self.calls.append(("runtime_list", None))
        return ({"runtime_id": "future-runtime", "state": "ready"},)

    async def runtime_check(self, runtime_id: str, refresh_quota: bool = False):
        self.calls.append(("runtime_check", (runtime_id, refresh_quota)))
        if runtime_id == "explode":
            raise RuntimeError(
                r"secret-token at C:\Users\private\state.db should not escape"
            )
        return {"runtime_id": runtime_id, "state": "ready"}

    async def runtime_configure(self, payload):
        self.calls.append(("runtime_configure", payload))
        raise ServiceError("CAPABILITY_MISSING", "configuration is not available")

    async def runtime_canary(self, payload):
        self.calls.append(("runtime_canary", payload))
        raise ServiceError("CAPABILITY_MISSING", "live canary is not available")

    async def project_scan(self, payload):
        self.calls.append(("project_scan", payload))
        raise ServiceError("CAPABILITY_MISSING", "project scan is not available")

    async def project_trust(self, payload):
        self.calls.append(("project_trust", payload))
        raise ServiceError("CAPABILITY_MISSING", "project trust is not available")

    async def workspace_release(self, payload):
        self.calls.append(("workspace_release", payload))
        raise ServiceError("CAPABILITY_MISSING", "workspace release is not available")

    async def agent_spawn(self, request):
        self.calls.append(("agent_spawn", request))
        return {
            "conversation_id": "conversation-1",
            "execution_id": "execution-1",
            "status": "succeeded",
        }

    async def agent_status(self, request):
        self.calls.append(("agent_status", request))
        return {"conversation_id": request.conversation_id, "status": "succeeded"}

    async def agent_send(self, request):
        self.calls.append(("agent_send", request))
        return {"conversation_id": request.conversation_id, "status": "succeeded"}

    async def agent_wait(self, request):
        self.calls.append(("agent_wait", request))
        return tuple(
            {
                "conversation_id": target.conversation_id,
                "status": "succeeded",
            }
            for target in request.targets
        )

    async def agent_interrupt(self, request):
        self.calls.append(("agent_interrupt", request))
        return {"conversation_id": request.conversation_id, "status": "interrupted"}

    async def agent_close(self, request):
        self.calls.append(("agent_close", request))
        return {"conversation_id": request.conversation_id, "status": "closed"}


def test_server_identity_discovery_and_public_schema_match() -> None:
    service = _RecordingService()
    server = create_server(service)

    tools = _run(server.list_tools())
    schemas = {tool.name: tool.input_schema for tool in tools}
    public_schema = json.loads(
        (ROOT / "schemas" / "tools-v1.json").read_text(encoding="utf-8")
    )

    assert server.name == "Subagent MCP"
    assert server.title == "Subagent MCP"
    assert server.version == __version__
    assert set(schemas) == EXPECTED_TOOL_NAMES == set(public_schema["tools"])
    assert all(tool.output_schema is None for tool in tools)
    for name in SIDE_EFFECTING_TOOL_NAMES:
        assert "request_id" in schemas[name]["required"]
        assert public_schema["tools"][name]["request_id_required"] is True


def test_runtime_check_refresh_quota_is_explicit_and_defaults_local() -> None:
    service = _RecordingService()
    server = create_server(service)
    schemas = {tool.name: tool.input_schema for tool in _run(server.list_tools())}
    public_schema = json.loads(
        (ROOT / "schemas" / "tools-v1.json").read_text(encoding="utf-8")
    )

    default = _metadata(
        _run(server.call_tool("runtime_check", {"runtime_id": "future-runtime"}))
    )
    refreshed = _metadata(
        _run(
            server.call_tool(
                "runtime_check",
                {"runtime_id": "future-runtime", "refresh_quota": True},
            )
        )
    )

    assert schemas["runtime_check"]["properties"]["refresh_quota"] == {
        "default": False,
        "title": "Refresh Quota",
        "type": "boolean",
    }
    assert public_schema["tools"]["runtime_check"]["refresh_quota_default"] is False
    assert default["result"]["state"] == refreshed["result"]["state"] == "ready"
    assert service.calls == [
        ("runtime_check", ("future-runtime", False)),
        ("runtime_check", ("future-runtime", True)),
    ]


def test_success_and_service_error_are_text_only_with_final_metadata() -> None:
    service = _RecordingService()
    server = create_server(service)

    success = _run(server.call_tool("runtime_list", {}))
    unsupported = _run(
        server.call_tool(
            "runtime_canary",
            {
                "request_id": "canary-1",
                "runtime_id": "future-runtime",
                "variant_id": "future-variant",
            },
        )
    )

    success_meta = _metadata(success)
    error_meta = _metadata(unsupported)
    assert success.is_error is False
    assert success.structured_content is None
    assert success_meta == {
        "ok": True,
        "result": [{"runtime_id": "future-runtime", "state": "ready"}],
        "tool": "runtime_list",
    }
    assert unsupported.is_error is True
    assert unsupported.structured_content is None
    assert error_meta["error"]["code"] == "CAPABILITY_MISSING"
    assert service.calls == [
        ("runtime_list", None),
        (
            "runtime_canary",
            {
                "api_version": 1,
                "request_id": "canary-1",
                "runtime_id": "future-runtime",
                "transport": "managed-sdk",
                "variant_id": "future-variant",
            },
        ),
    ]


def test_spawn_maps_public_packet_without_changing_provider_native_values(
    tmp_path: Path,
) -> None:
    service = _RecordingService()
    server = create_server(service)
    cwd = str(tmp_path.resolve())

    result = _run(
        server.call_tool(
            "agent_spawn",
            {
                "request_id": "spawn-1",
                "runtime_id": "future-runtime",
                "variant_id": "vendor/model-policy:v1",
                "task": {
                    "title": "Bounded implementation",
                    "prompt": "Implement the requested slice.",
                    "acceptance_criteria": ["Return one normalized result."],
                    "role": "sub-agent",
                    "authority": ["AGENTS.md"],
                    "repository_base": "base-commit",
                    "repository_head": "head-commit",
                },
                "cwd": cwd,
                "mode": "implement",
                "transport": "managed-sdk",
                "required_capabilities": ["repo_read", "workspace_write"],
                "context_policy_id": "declared-native",
                "permission_policy_id": "bounded-writer",
                "workspace": "current",
            },
        )
    )

    assert result.is_error is False
    name, request = service.calls[-1]
    assert name == "agent_spawn"
    assert request.request_id == "spawn-1"
    assert request.runtime_id == "future-runtime"
    assert request.variant_id == "vendor/model-policy:v1"
    assert request.task.prompt == "Implement the requested slice."
    assert request.task.authority == ("AGENTS.md",)
    assert request.task.repository_base == "base-commit"
    assert request.task.repository_head == "head-commit"
    assert request.cwd == cwd
    assert request.transport == "managed-sdk"
    assert request.permissions == ("repo_read", "workspace_write")


def test_invalid_version_request_id_and_workspace_fail_before_service_call(
    tmp_path: Path,
) -> None:
    service = _RecordingService()
    server = create_server(service)
    common_spawn = {
        "request_id": "spawn-1",
        "runtime_id": "future-runtime",
        "variant_id": "future-variant",
        "task": {
            "title": "Review",
            "prompt": "Review.",
            "acceptance_criteria": ["Report."],
            "role": "sub-agent",
        },
        "cwd": str(tmp_path.resolve()),
        "mode": "review",
    }

    invalid_version = _run(
        server.call_tool("runtime_list", {"api_version": 2})
    )
    oversized_id = _run(
        server.call_tool(
            "runtime_canary",
            {
                "request_id": "🚀" * 65,
                "runtime_id": "future-runtime",
                "variant_id": "future-variant",
            },
        )
    )
    unsupported_workspace = _run(
        server.call_tool(
            "agent_spawn",
            common_spawn | {"workspace": {"strategy": "create"}},
        )
    )

    assert _metadata(invalid_version)["error"]["code"] == "REQUEST_INVALID"
    assert _metadata(oversized_id)["error"]["code"] == "REQUEST_INVALID"
    assert _metadata(unsupported_workspace)["error"]["code"] == "CAPABILITY_MISSING"
    assert service.calls == []


def test_unexpected_exception_is_sanitized_without_path_secret_or_traceback() -> None:
    service = _RecordingService()
    server = create_server(service)

    result = _run(
        server.call_tool("runtime_check", {"runtime_id": "explode"})
    )
    content = result.content[0]
    assert isinstance(content, TextContent)
    meta = _metadata(result)

    assert result.is_error is True
    assert meta["error"]["code"] == "INTERNAL_ERROR"
    assert "secret-token" not in content.text
    assert "Users\\private" not in content.text
    assert "Traceback" not in content.text


def test_non_json_service_result_is_sanitized_instead_of_breaking_protocol() -> None:
    class _InvalidResultService(_RecordingService):
        async def runtime_list(self):
            return object()

    server = create_server(_InvalidResultService())

    result = _run(server.call_tool("runtime_list", {}))

    assert result.is_error is True
    assert _metadata(result)["error"]["code"] == "INTERNAL_ERROR"


def test_lifecycle_tools_publish_compact_response_mode_and_long_local_wait() -> None:
    server = create_server(_RecordingService())
    schemas = {tool.name: tool.input_schema for tool in _run(server.list_tools())}

    for name in {
        "agent_spawn",
        "agent_status",
        "agent_send",
        "agent_wait",
        "agent_interrupt",
        "agent_close",
    }:
        response_mode = schemas[name]["properties"]["response_mode"]
        assert response_mode["default"] == "compact"
    assert schemas["agent_wait"]["properties"]["timeout_seconds"]["default"] == 300.0


def test_status_is_compact_by_default_and_full_only_when_requested() -> None:
    class _StatusService(_RecordingService):
        async def agent_status(self, request):
            self.calls.append(("agent_status", request))
            return _terminal_status()

    service = _StatusService()
    server = create_server(service)

    compact = _metadata(
        _run(server.call_tool("agent_status", {"conversation_id": "conversation-1"}))
    )["result"]
    full = _metadata(
        _run(
            server.call_tool(
                "agent_status",
                {"conversation_id": "conversation-1", "response_mode": "full"},
            )
        )
    )["result"]

    assert compact == _terminal_status().to_compact_dict()
    assert "descriptor" not in compact
    assert "workspace_path" not in compact
    assert "events" not in compact
    assert compact["conversation_state"] == "idle"
    assert json.dumps(compact).count("one provider result") == 1
    assert full == _terminal_status().to_dict()


def test_unknown_response_mode_fails_before_service_call() -> None:
    service = _RecordingService()
    server = create_server(service)

    result = _run(
        server.call_tool(
            "agent_status",
            {"conversation_id": "conversation-1", "response_mode": "verbose"},
        )
    )

    assert _metadata(result)["error"]["code"] == "REQUEST_INVALID"
    assert service.calls == []


def test_wait_uses_five_minute_default_without_model_polling() -> None:
    service = _RecordingService()
    server = create_server(service)

    result = _run(
        server.call_tool(
            "agent_wait",
            {"targets": [{"conversation_id": "conversation-1"}]},
        )
    )

    assert result.is_error is False
    name, request = service.calls[-1]
    assert name == "agent_wait"
    assert request.timeout_seconds == 300.0
