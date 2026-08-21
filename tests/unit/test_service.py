from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest

from subagent_harness_mcp.adapters.base import (
    AdapterFailure,
    CanaryRequest,
    CanaryResult,
    ProbeResult,
)
from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.contracts import (
    ActionRequest,
    ServiceError,
    SpawnRequest,
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


class _QuotaFakeAdapter(FakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.canary_calls = 0
        self.canary_error_code: str | None = None
        self.quota_calls = 0
        self.quota_error_code: str | None = None
        self.quota_cleanup_confirmed = True

    async def probe(self) -> ProbeResult:
        return ProbeResult(
            "needs_canary",
            {"mode": "deterministic-quota", "pair_key": "b" * 64},
        )

    async def runtime_canary(self, request: CanaryRequest) -> CanaryResult:
        self.canary_calls += 1
        if self.canary_error_code is not None:
            return CanaryResult(
                False,
                request.pair_key,
                error=AdapterFailure(
                    self.canary_error_code,
                    "adapter",
                    False,
                    "deterministic canary failure",
                ),
            )
        return CanaryResult(
            True,
            request.pair_key,
            {
                "model": request.model,
                "effort": request.reasoning.get("effort"),
                "is_using_overage": False,
                "overage_blocked": True,
                "cleanup_confirmed": True,
            },
        )

    async def quota_probe(self, request: CanaryRequest) -> CanaryResult:
        self.quota_calls += 1
        if self.quota_error_code is not None:
            return CanaryResult(
                False,
                request.pair_key,
                error=AdapterFailure(
                    self.quota_error_code,
                    "quota",
                    False,
                    "deterministic quota failure",
                ),
            )
        return CanaryResult(
            True,
            request.pair_key,
            {
                "is_using_overage": False,
                "overage_blocked": True,
                "cleanup_confirmed": self.quota_cleanup_confirmed,
                "raw": {"account": "must-not-escape"},
                "session_id": "must-not-escape",
            },
        )


def _service(
    tmp_path: Path,
    harness: FakeHarness,
    *,
    adapter: FakeAdapter | None = None,
) -> tuple[SubagentMcpService, StateStore]:
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
    selected = adapter or FakeAdapter(harness)
    registry = AdapterRegistry(builtin_factories=(lambda: selected,))
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


def test_runtime_check_refreshes_ready_quota_only_when_explicit(tmp_path: Path) -> None:
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    service, _ = _service(tmp_path, harness, adapter=adapter)

    async def run():
        initial = await service.runtime_check("fake")
        await service.runtime_canary(
            {
                "request_id": "initial-canary",
                "runtime_id": "fake",
                "variant_id": "future-deep",
                "transport": "managed-sdk",
            }
        )
        local = await service.runtime_check("fake")
        refreshed = await service.runtime_check("fake", refresh_quota=True)
        return initial, local, refreshed

    initial, local, refreshed = asyncio.run(run())

    assert initial["state"] == "needs_canary"
    assert local["quota"]["state"] == "check_required"
    assert refreshed["quota"] == {
        "state": "available",
        "overage_blocked": True,
        "variants": [
            {
                "variant_id": "future-deep",
                "state": "available",
                "overage_blocked": True,
            }
        ],
    }
    assert adapter.canary_calls == adapter.quota_calls == 1
    assert "must-not-escape" not in json.dumps(refreshed)


def test_runtime_check_pauses_ready_circuit_on_unsafe_quota(tmp_path: Path) -> None:
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)

    async def run():
        await service.runtime_check("fake")
        await service.runtime_canary(
            {
                "request_id": "initial-canary",
                "runtime_id": "fake",
                "variant_id": "future-deep",
                "transport": "managed-sdk",
            }
        )
        adapter.quota_error_code = "QUOTA_PAUSED"
        return await service.runtime_check("fake", refresh_quota=True)

    refreshed = asyncio.run(run())

    assert refreshed["quota"]["state"] == "quota_paused"
    assert refreshed["quota"]["variants"] == [
        {"variant_id": "future-deep", "state": "quota_paused"}
    ]
    assert store.load_circuit("fake", "future-deep").state == "auto_paused"
    assert adapter.quota_calls == 1


def test_runtime_check_uses_fresh_canary_for_required_and_paused_quota(
    tmp_path: Path,
) -> None:
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)

    async def run():
        required = await service.runtime_check("fake", refresh_quota=True)
        adapter.quota_error_code = "QUOTA_PAUSED"
        paused = await service.runtime_check("fake", refresh_quota=True)
        adapter.quota_error_code = None
        recovered = await service.runtime_check("fake", refresh_quota=True)
        return required, paused, recovered

    required, paused, recovered = asyncio.run(run())

    assert required["quota"]["state"] == "available"
    assert required["state"] == "ready"
    assert paused["quota"]["state"] == "quota_paused"
    assert paused["state"] == "auto_paused"
    assert recovered["quota"]["state"] == "available"
    assert recovered["state"] == "ready"
    assert store.load_circuit("fake", "future-deep").state == "ready"
    assert adapter.canary_calls == 2
    assert adapter.quota_calls == 1


def test_runtime_check_fails_closed_on_ambiguous_quota_evidence(tmp_path: Path) -> None:
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)

    async def run():
        await service.runtime_check("fake", refresh_quota=True)
        adapter.quota_cleanup_confirmed = False
        return await service.runtime_check("fake", refresh_quota=True)

    refreshed = asyncio.run(run())

    assert refreshed["quota"]["state"] == "quota_paused"
    assert store.load_circuit("fake", "future-deep").state == "auto_paused"


def test_runtime_check_explains_unknown_guarded_canary_failure(tmp_path: Path) -> None:
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    adapter.canary_error_code = "CAPABILITY_MISSING"
    service, _ = _service(tmp_path, harness, adapter=adapter)

    refreshed = asyncio.run(service.runtime_check("fake", refresh_quota=True))

    assert refreshed["quota"] == {
        "state": "unknown",
        "variants": [
            {
                "variant_id": "future-deep",
                "state": "unknown",
                "error_code": "CAPABILITY_MISSING",
            }
        ],
    }


def test_runtime_check_requires_cleanup_after_ambiguous_quota_probe(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)

    async def run():
        await service.runtime_check("fake", refresh_quota=True)
        adapter.quota_error_code = "RECOVERY_REQUIRED"
        return await service.runtime_check("fake", refresh_quota=True)

    refreshed = asyncio.run(run())

    assert refreshed["quota"] == {
        "state": "unknown",
        "variants": [{"variant_id": "future-deep", "state": "unknown"}],
    }
    assert refreshed["state"] == "recovery_required"
    assert store.load_circuit("fake", "future-deep").state == "recovery_required"
    with pytest.raises(ServiceError) as blocked:
        asyncio.run(service.agent_spawn(_spawn_request(workspace)))
    assert blocked.value.code == "RECOVERY_REQUIRED"
    assert harness.call_count("spawn") == 0


def test_wait_does_not_wake_codex_for_a_running_revision(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    service, _ = _service(tmp_path, harness)

    async def run():
        started = await service.agent_spawn(_spawn_request(workspace))

        async def interrupt_after_local_wait():
            await asyncio.sleep(0.01)
            return await service.agent_interrupt(
                ActionRequest("interrupt-after-wait", started.conversation_id)
            )

        interrupt = asyncio.create_task(interrupt_after_local_wait())
        waited = await service.agent_wait(
            WaitRequest(
                (WaitTarget(started.conversation_id),),
                timeout_seconds=0.5,
            )
        )
        return waited[0], await interrupt

    waited, interrupted = asyncio.run(run())

    assert waited.execution_state == interrupted.execution_state == "interrupted"


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
