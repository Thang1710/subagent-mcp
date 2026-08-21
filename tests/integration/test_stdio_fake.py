from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent

from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.server import create_server
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore


ROOT = Path(__file__).resolve().parents[2]


class _Ids:
    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def __call__(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter)}"


def _write_fake_config(home: Path) -> None:
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(home)},
        os_name="nt",
    )
    ConfigStore(paths).save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "fake": {
                    "enabled": True,
                    "selection_mode": "fixed",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "configured",
                            "model": "future/model-v9",
                            "reasoning": {"mode": "provider-native"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )


def _service(home: Path, harness: FakeHarness) -> SubagentMcpService:
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(home)},
        os_name="nt",
    )
    return SubagentMcpService(
        config=ConfigStore(paths),
        store=StateStore.open(paths),
        registry=AdapterRegistry(builtin_factories=(lambda: FakeAdapter(harness),)),
        id_factory=_Ids(),
    )


def _meta(result: CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    marker = "\n```subagent-mcp-meta\n"
    _, separator, encoded = content.text.partition(marker)
    assert separator == marker
    assert encoded.endswith("\n```")
    return json.loads(encoded[:-4])


def _spawn_arguments(workspace: Path, request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "runtime_id": "fake",
        "variant_id": "configured",
        "task": {
            "title": "Fake MCP lifecycle",
            "prompt": "Exercise the deterministic native-harness seam.",
            "acceptance_criteria": ["Return normalized status."],
            "role": "sub-agent",
        },
        "cwd": str(workspace.resolve()),
        "mode": "review",
        "transport": "managed-sdk",
        "required_capabilities": ["repo_read"],
        "workspace": "current",
        "response_mode": "full",
    }


async def _exercise_protocol(
    client: Client,
    workspace: Path,
    *,
    expect_interrupt_success: bool,
) -> dict[str, Any]:
    discovered = await client.list_tools()
    assert len(discovered.tools) == 13

    runtimes = await client.call_tool("runtime_list", {})
    checked = await client.call_tool("runtime_check", {"runtime_id": "fake"})
    spawned = await client.call_tool(
        "agent_spawn", _spawn_arguments(workspace, "spawn-stdio-1")
    )
    spawn_meta = _meta(spawned)
    conversation_id = spawn_meta["result"]["conversation_id"]
    status = await client.call_tool(
        "agent_status",
        {"conversation_id": conversation_id, "response_mode": "full"},
    )
    sent = await client.call_tool(
        "agent_send",
        {
            "request_id": "send-stdio-1",
            "conversation_id": conversation_id,
            "prompt": "Continue the same bounded session.",
            "response_mode": "full",
        },
    )
    waited = await client.call_tool(
        "agent_wait",
        {
            "targets": [
                {
                    "conversation_id": conversation_id,
                    "after_revision": 0,
                    "after_cursor": 0,
                }
            ],
            "timeout_seconds": 0,
            "response_mode": "full",
        },
    )
    interrupted = await client.call_tool(
        "agent_interrupt",
        {
            "request_id": "interrupt-stdio-1",
            "conversation_id": conversation_id,
            "response_mode": "full",
        },
    )
    closed = await client.call_tool(
        "agent_close",
        {
            "request_id": "close-stdio-1",
            "conversation_id": conversation_id,
            "response_mode": "full",
        },
    )

    for result in (
        runtimes,
        checked,
        spawned,
        status,
        sent,
        waited,
        interrupted,
        closed,
    ):
        assert result.structured_content is None
        assert len(result.content) == 1
    send_meta = _meta(sent)
    assert sent.is_error is False, send_meta
    interrupt_meta = _meta(interrupted)
    if expect_interrupt_success:
        assert send_meta["result"]["execution_state"] == "running", send_meta
        assert interrupted.is_error is False, interrupt_meta
    else:
        assert send_meta["result"]["execution_state"] == "succeeded", send_meta
        assert interrupted.is_error is True, interrupt_meta
        assert interrupt_meta["error"]["code"] == "SESSION_BUSY"
    close_meta = _meta(closed)
    assert closed.is_error is False, close_meta
    assert close_meta["result"]["conversation_state"] == "closed"
    return {
        "spawn": spawn_meta["result"],
        "send": send_meta["result"],
        "wait": _meta(waited)["result"][0],
        "interrupt": interrupt_meta,
    }


def test_in_memory_legacy_protocol_preserves_session_model_and_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_fake_config(home)
    harness = FakeHarness()
    harness.enqueue("done", result="spawn complete")
    harness.enqueue("running")
    service = _service(home, harness)

    async def run() -> dict[str, Any]:
        async with Client(
            create_server(service),
            mode="legacy",
            read_timeout_seconds=5,
        ) as client:
            return await _exercise_protocol(
                client,
                workspace,
                expect_interrupt_success=True,
            )

    results = asyncio.run(run())

    assert results["spawn"]["external_session_id"] == results["send"][
        "external_session_id"
    ]
    assert results["spawn"]["workspace_path"] == str(workspace.resolve())
    assert results["send"]["workspace_path"] == str(workspace.resolve())
    assert results["spawn"]["descriptor"]["model_display_name"] == "future/model-v9"
    assert results["send"]["descriptor"]["model_display_name"] == "future/model-v9"
    assert results["wait"]["conversation_id"] == results["spawn"][
        "conversation_id"
    ]
    assert results["interrupt"]["ok"] is True
    assert harness.call_count("spawn") == 1
    assert harness.call_count("send") == 1


def test_real_stdio_subprocess_uses_temp_home_and_protocol_only_stdout(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    stderr_path = tmp_path / "server-stderr.txt"
    workspace.mkdir()
    _write_fake_config(home)

    async def run() -> dict[str, Any]:
        test_server = "\n".join(
            (
                "from subagent_harness_mcp.adapters.fake import FakeAdapter",
                "from subagent_harness_mcp.adapters.registry import AdapterRegistry",
                "from subagent_harness_mcp.config import ConfigStore",
                "from subagent_harness_mcp.paths import resolve_paths",
                "from subagent_harness_mcp.server import create_server",
                "from subagent_harness_mcp.service import SubagentMcpService",
                "from subagent_harness_mcp.store import StateStore",
                "paths = resolve_paths()",
                "service = SubagentMcpService(",
                "    config=ConfigStore(paths),",
                "    store=StateStore.open(paths),",
                "    registry=AdapterRegistry(builtin_factories=(FakeAdapter,)),",
                ")",
                "create_server(service).run('stdio')",
            )
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", test_server],
            env={"SUBAGENT_MCP_HOME": str(home)},
            cwd=ROOT,
            encoding="utf-8",
            encoding_error_handler="replace",
        )
        with stderr_path.open("w+", encoding="utf-8") as stderr:
            async with Client(
                stdio_client(parameters, errlog=stderr),
                mode="legacy",
                read_timeout_seconds=10,
            ) as client:
                return await _exercise_protocol(
                    client,
                    workspace,
                    expect_interrupt_success=False,
                )

    results = asyncio.run(run())
    stderr = stderr_path.read_text(encoding="utf-8")

    assert results["spawn"]["external_session_id"] == results["send"][
        "external_session_id"
    ]
    assert results["spawn"]["workspace_path"] == str(workspace.resolve())
    assert "Traceback" not in stderr
    assert "secret" not in stderr.lower()
    assert os.environ.get("SUBAGENT_MCP_HOME") != str(home)
