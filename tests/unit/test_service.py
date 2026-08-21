from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest

from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.contracts import ServiceError, SpawnRequest, TaskPacket
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore


class _Ids:
    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def __call__(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter)}"


def _service(tmp_path: Path, harness: FakeHarness) -> tuple[SubagentMcpService, StateStore]:
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )
    ConfigStore(paths).save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "fake": {
                    "enabled": True,
                    "selection_mode": "lead-selects",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "future-deep",
                            "model": "vendor/future-model:preview-01",
                            "reasoning": {"provider_depth": "deep"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    store = StateStore.open(paths)
    registry = AdapterRegistry(builtin_factories=(lambda: FakeAdapter(harness),))
    return (
        SubagentMcpService(
            config=ConfigStore(paths),
            store=store,
            registry=registry,
            id_factory=_Ids(),
        ),
        store,
    )


def _spawn_request(
    workspace: Path,
    *,
    request_id: str = "spawn-1",
    prompt: str = "Review the bounded change.",
    write: bool = False,
) -> SpawnRequest:
    return SpawnRequest(
        request_id=request_id,
        runtime_id="fake",
        variant_id="future-deep",
        task=TaskPacket(
            title="Bounded review",
            prompt=prompt,
            acceptance_criteria=("Return one normalized result.",),
            role="sub-agent",
        ),
        cwd=str(workspace),
        mode="implement" if write else "review",
        transport="managed-sdk",
        permissions=("repo_read", "workspace_write") if write else ("repo_read",),
    )


def test_concurrent_idempotent_spawn_calls_adapter_exactly_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="review complete")
    service, store = _service(tmp_path, harness)
    request = _spawn_request(workspace)

    async def run():
        return await asyncio.gather(
            service.agent_spawn(request),
            service.agent_spawn(request),
        )

    first, replay = asyncio.run(run())

    assert harness.call_count("spawn") == 1
    assert first.conversation_id == replay.conversation_id
    assert first.execution_id == replay.execution_id
    assert first.execution_state == "succeeded"
    with store.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1


def test_request_id_conflict_and_attestation_mismatch_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness(effective_model="wrong/model")
    service, _ = _service(tmp_path, harness)

    with pytest.raises(ServiceError) as mismatch:
        asyncio.run(service.agent_spawn(_spawn_request(workspace)))
    with pytest.raises(ServiceError) as conflict:
        asyncio.run(
            service.agent_spawn(
                _spawn_request(workspace, prompt="Different input with the same id.")
            )
        )

    assert mismatch.value.code == "CONTEXT_DRIFT"
    assert harness.call_count("spawn") == 0
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_writer_lease_blocks_second_execution_before_adapter_launch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    harness.enqueue("done")
    service, _ = _service(tmp_path, harness)

    first = asyncio.run(
        service.agent_spawn(_spawn_request(workspace, request_id="writer-1", write=True))
    )
    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _spawn_request(workspace, request_id="writer-2", write=True)
            )
        )

    assert first.execution_state == "running"
    assert captured.value.code == "WORKSPACE_BUSY"
    assert harness.call_count("spawn") == 1


def test_prompt_credentials_and_pii_are_not_persisted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue(
        "done",
        result="Bearer abcdefghijklmnopqrstuvwxyz user@example.com finished",
    )
    service, store = _service(tmp_path, harness)
    prompt = "private transcript sk-ant-abcdefghijklmnopqrstuvwxyz hidden-thinking"

    status = asyncio.run(
        service.agent_spawn(_spawn_request(workspace, prompt=prompt))
    )

    assert status.result == {
        "text": "Bearer [REDACTED] [REDACTED_EMAIL] finished"
    }
    with store.transaction() as database:
        durable = json.dumps(
            {
                "conversation": database.execute(
                    "SELECT descriptor_json FROM conversations"
                ).fetchall(),
                "execution": database.execute(
                    "SELECT requested_json, observed_json, result_json FROM executions"
                ).fetchall(),
                "events": database.execute("SELECT payload_json FROM events").fetchall(),
            }
        )
    assert "sk-ant-" not in durable
    assert "user@example.com" not in durable
    assert "hidden-thinking" not in durable
    assert "private transcript" not in durable


def test_unimplemented_preview_surface_is_explicit_capability_gap(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, FakeHarness())

    with pytest.raises(ServiceError) as captured:
        asyncio.run(service.runtime_canary({"runtime_id": "fake"}))

    assert captured.value.code == "CAPABILITY_MISSING"
    assert captured.value.retryable is False
