from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from pathlib import Path

from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.contracts import (
    ActionRequest,
    SendRequest,
    SpawnRequest,
    StatusRequest,
    ServiceError,
    TaskPacket,
    WaitRequest,
    WaitTarget,
)
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore


class _Ids:
    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def __call__(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter)}"


def _configured(
    tmp_path: Path,
    harness: FakeHarness,
    ids: _Ids,
    adapter_factory=None,
):
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )
    config = ConfigStore(paths)
    config.save(
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
    store = StateStore.open(paths)

    def create_service() -> SubagentMcpService:
        return SubagentMcpService(
            config=config,
            store=StateStore.open(paths),
            registry=AdapterRegistry(
                builtin_factories=(adapter_factory or (lambda: FakeAdapter(harness)),)
            ),
            id_factory=ids,
        )

    return create_service, store


def _spawn(workspace: Path, request_id: str) -> SpawnRequest:
    return SpawnRequest(
        request_id=request_id,
        runtime_id="fake",
        variant_id="configured",
        task=TaskPacket(
            title="Fake lifecycle",
            prompt="Exercise the deterministic native-harness seam.",
            acceptance_criteria=("Return normalized status.",),
            role="sub-agent",
        ),
        cwd=str(workspace),
        mode="review",
        transport="managed-sdk",
        permissions=("repo_read",),
    )


def test_done_failure_and_wait_use_one_normalized_shape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="done result")
    harness.enqueue("failure", error_code="FAKE_FAILURE")
    create_service, _ = _configured(tmp_path, harness, _Ids())
    service = create_service()

    done = asyncio.run(service.agent_spawn(_spawn(workspace, "done")))
    failed = asyncio.run(service.agent_spawn(_spawn(workspace, "failed")))
    waited = asyncio.run(
        service.agent_wait(
            WaitRequest(
                targets=(WaitTarget(done.conversation_id, after_revision=0),),
                timeout_seconds=0,
            )
        )
    )[0]

    assert done.execution_state == "succeeded"
    assert done.result == {"text": "done result"}
    assert failed.execution_state == "failed"
    assert failed.result["error"]["code"] == "FAKE_FAILURE"
    assert set(done.to_dict()) == set(failed.to_dict()) == set(waited.to_dict())


def test_needs_input_follow_up_reuses_native_session_with_new_execution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("needs_input", question="Choose a bounded option")
    harness.enqueue("done", result="follow-up complete")
    create_service, _ = _configured(tmp_path, harness, _Ids())
    service = create_service()

    first = asyncio.run(service.agent_spawn(_spawn(workspace, "needs-input")))
    followed = asyncio.run(
        service.agent_send(
            SendRequest(
                request_id="answer-1",
                conversation_id=first.conversation_id,
                prompt="Use option A.",
                reply_to="question-1",
                answers={"choice": "A"},
            )
        )
    )

    assert first.conversation_state == "needs_input"
    assert first.needs_input == ({"id": "question-1", "prompt": "Choose a bounded option"},)
    assert followed.execution_state == "succeeded"
    assert followed.execution_id != first.execution_id
    assert followed.external_session_id == first.external_session_id
    assert harness.call_count("send") == 1


def test_follow_up_running_execution_is_bound_and_interruptible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("needs_input", question="Continue?")
    harness.enqueue("running")
    create_service, _ = _configured(tmp_path, harness, _Ids())
    service = create_service()

    first = asyncio.run(service.agent_spawn(_spawn(workspace, "follow-up-running")))
    running = asyncio.run(
        service.agent_send(
            SendRequest(
                request_id="continue-1",
                conversation_id=first.conversation_id,
                prompt="Continue.",
                reply_to="question-1",
                answers={"continue": True},
            )
        )
    )
    interrupted = asyncio.run(
        service.agent_interrupt(
            ActionRequest("interrupt-follow-up", first.conversation_id)
        )
    )

    assert running.execution_id != first.execution_id
    assert running.execution_state == "running"
    assert running.external_session_id == first.external_session_id
    assert interrupted.execution_id == running.execution_id
    assert interrupted.execution_state == "interrupted"


def test_running_execution_interrupts_then_closes_without_deleting_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    create_service, _ = _configured(tmp_path, harness, _Ids())
    service = create_service()

    running = asyncio.run(service.agent_spawn(_spawn(workspace, "running")))
    interrupted = asyncio.run(
        service.agent_interrupt(
            ActionRequest("interrupt-1", running.conversation_id)
        )
    )
    closed = asyncio.run(
        service.agent_close(ActionRequest("close-1", running.conversation_id))
    )

    assert interrupted.execution_state == "interrupted"
    assert closed.conversation_state == "closed"
    assert harness.has_session(running.external_session_id)


def test_service_restart_opens_exact_persisted_session_and_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    ids = _Ids()
    create_service, _ = _configured(tmp_path, harness, ids)

    first_service = create_service()
    running = asyncio.run(first_service.agent_spawn(_spawn(workspace, "restart")))
    second_service = create_service()
    resumed = asyncio.run(
        second_service.agent_status(
            StatusRequest(running.conversation_id, after_cursor=0, refresh=True)
        )
    )

    assert resumed.external_session_id == running.external_session_id
    assert resumed.workspace_path == str(workspace.resolve())
    assert resumed.descriptor.model_display_name == "future/model-v9"
    assert harness.call_count("open_session") == 1


def test_service_restart_logically_closes_terminal_connection_owned_session(
    tmp_path: Path,
) -> None:
    class ConnectionOwnedNoResumeAdapter(FakeAdapter):
        def __init__(self, harness: FakeHarness) -> None:
            super().__init__(harness)
            self._manifest = replace(
                self._manifest,
                capabilities=self._manifest.capabilities - {"resume"},
            )

        async def resolve_context(self, request):
            context = await super().resolve_context(request)
            return replace(context, capability_gaps=("resume_after_restart",))

        async def spawn(self, request):
            snapshot = await super().spawn(request)
            return replace(
                snapshot,
                evidence={
                    "source": "connection-owned-fake",
                    "connection_owned_session": True,
                },
            )

        async def open_session(self, request):
            del request
            raise ServiceError(
                "CAPABILITY_MISSING",
                "connection-owned sessions cannot resume after restart",
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="terminal before restart")
    ids = _Ids()
    create_service, _ = _configured(
        tmp_path,
        harness,
        ids,
        adapter_factory=lambda: ConnectionOwnedNoResumeAdapter(harness),
    )

    first_service = create_service()
    terminal = asyncio.run(first_service.agent_spawn(_spawn(workspace, "terminal")))
    restarted_service = create_service()
    closed = asyncio.run(
        restarted_service.agent_close(
            ActionRequest("close-after-restart", terminal.conversation_id)
        )
    )

    assert terminal.execution_state == "succeeded"
    assert closed.conversation_state == "closed"
    assert harness.call_count("close") == 0
