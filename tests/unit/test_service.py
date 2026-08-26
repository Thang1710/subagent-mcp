from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import subagent_harness_mcp.service as service_module

from subagent_harness_mcp.adapters.base import (
    AdapterFailure,
    AdapterSendRequest,
    AdapterSnapshot,
    CanaryRequest,
    CanaryResult,
    ProbeResult,
)
from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.contracts import (
    PROMPT_MAX_BYTES,
    ActionRequest,
    ArtifactReference,
    ResultReadRequest,
    ROUGH_TOKEN_ESTIMATE_BASIS,
    SendRequest,
    ServiceError,
    SpawnRequest,
    StatusRequest,
    TaskInput,
    TaskPacket,
    WaitRequest,
    WaitTarget,
)
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity contract")
def test_workspace_extended_path_uses_one_canonical_lease_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ordinary_display, ordinary_key = service_module._workspace(str(workspace))
    extended = "\\\\?\\" + str(workspace.resolve(strict=True))

    extended_display, extended_key = service_module._workspace(extended)

    assert extended_display == ordinary_display
    assert extended_key == ordinary_key


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity contract")
def test_extended_workspace_alias_cannot_bypass_writer_lease(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    service, _ = _service(tmp_path, harness)

    first = asyncio.run(
        service.agent_spawn(_spawn_request(workspace, write=True))
    )
    extended = "\\\\?\\" + str(workspace.resolve(strict=True))

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                replace(
                    _spawn_request(
                        workspace,
                        request_id="extended-workspace-overlap",
                        write=True,
                    ),
                    cwd=extended,
                )
            )
        )

    assert first.execution_state == "running"
    assert captured.value.code == "WRITE_SET_BUSY"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (r"\\?\C:\work\tree", r"C:\work\tree"),
        (r"\\?\UNC\server\share\tree", r"\\server\share\tree"),
    ],
)
def test_windows_extended_prefix_normalization(value: str, expected: str) -> None:
    assert service_module._without_windows_extended_prefix(value) == expected


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

    async def spawn(self, request):
        snapshot = await super().spawn(request)
        return replace(
            snapshot,
            evidence={
                **dict(snapshot.evidence),
                "rate_evidence_seen": True,
                "is_using_overage": False,
                "overage_blocked": True,
                "cleanup_confirmed": True,
            },
        )


class _TerminalQuotaAdapter(_QuotaFakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.before_failure = None
        self.spawn_calls = 0

    async def spawn(self, request):
        del request
        self.spawn_calls += 1
        if self.before_failure is not None:
            self.before_failure()
        raise ServiceError(
            "USAGE_CREDITS_FORBIDDEN",
            "provider refused usage credits",
            category="quota",
        )


class _CatalogFakeAdapter(FakeAdapter):
    async def model_catalog(self) -> tuple[dict[str, str], ...]:
        return (
            {
                "value": "vendor::model-a",
                "label": "Model A",
                "provider": "vendor",
                "model": "model-a",
            },
            {
                "value": "vendor::model-b",
                "label": "Model B",
                "provider": "vendor",
                "model": "model-b",
            },
        )


class _OneRootFakeAdapter(FakeAdapter):
    """Generic stand-in for a harness that natively enforces one write root."""

    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self._manifest = replace(self._manifest, max_write_roots_per_session=1)


class _ExistingDirectoryRootFakeAdapter(FakeAdapter):
    """Stand-in for a harness whose native sandbox only accepts one directory."""

    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self._manifest = replace(
            self._manifest,
            max_write_roots_per_session=1,
            write_root_mode="existing-directory",
        )


class _RestartGapAdapter(FakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.cleanup_confirmed = False

    async def resolve_context(self, request):
        context = await super().resolve_context(request)
        return replace(context, capability_gaps=("live_status_after_restart",))

    async def spawn(self, request):
        snapshot = await super().spawn(request)
        return replace(
            snapshot,
            evidence={**dict(snapshot.evidence), "cleanup_confirmed": False},
        )

    async def open_session(self, request):
        del request
        raise ServiceError(
            "CAPABILITY_MISSING",
            "live session belongs to the previous controller process",
        )

    async def orphan_cleanup_confirmed(self, request, context):
        del request, context
        return self.cleanup_confirmed


class _StartupRecoveryAdapter(FakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.fail_spawn = True
        self.cleanup_confirmed = False

    async def spawn(self, request):
        if self.fail_spawn:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "startup cleanup was not confirmed",
                category="adapter",
            )
        return await super().spawn(request)

    async def orphan_cleanup_confirmed(self, request, context):
        del request, context
        return self.cleanup_confirmed


class _PostNativeFailureAdapter(FakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.fail_spawn = False
        self.fail_send = False

    async def spawn(self, request):
        if self.fail_spawn:
            raise ServiceError(
                "PROVIDER_ERROR",
                "native spawn failed after invocation",
                category="provider",
            )
        return await super().spawn(request)

    async def send(self, request):
        if self.fail_send:
            raise ServiceError(
                "PROVIDER_ERROR",
                "native send failed after invocation",
                category="provider",
            )
        return await super().send(request)


class _RecordingSpawnAdapter(FakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.spawn_requests = []

    async def spawn(self, request):
        self.spawn_requests.append(request)
        return await super().spawn(request)


class _PreNativeContextFailureAdapter(FakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.fail_context = True

    async def resolve_context(self, request):
        if self.fail_context:
            raise ServiceError("CONTEXT_DRIFT", "context resolution failed")
        return await super().resolve_context(request)


class _StaleExecutionSnapshotAdapter(FakeAdapter):
    async def spawn(self, request):
        snapshot = await super().spawn(request)
        return replace(snapshot, external_execution_id="stale-controller-execution")


class _StaleCloseSnapshotAdapter(FakeAdapter):
    async def spawn(self, request):
        snapshot = await super().spawn(request)
        return replace(
            snapshot,
            evidence={**dict(snapshot.evidence), "cleanup_confirmed": False},
        )

    async def close(self, request):
        snapshot = await super().close(request)
        return replace(
            snapshot,
            external_execution_id="stale-controller-execution",
            evidence={**dict(snapshot.evidence), "cleanup_confirmed": True},
        )


class _RestartClosePlaceholderAdapter(FakeAdapter):
    async def close(self, request):
        snapshot = await super().close(request)
        return replace(
            snapshot,
            effective_model="unavailable-without-resume",
            effective_reasoning={},
            workspace_path="",
            workspace_key="",
            context_hash="",
            evidence={"source": "persisted-session-id"},
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
                    "delegation_priority": 73,
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


def test_spawn_hash_binds_declared_input_and_surfaces_attestations(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    input_path = workspace / "docs" / "specs" / "review.md"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("approval input\n", encoding="utf-8")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    harness = FakeHarness()
    harness.enqueue("done", result="CAPSULE: approved")
    adapter = _RecordingSpawnAdapter(harness)
    service, _ = _service(tmp_path, harness, adapter=adapter)
    request = _spawn_request(workspace)
    request = replace(
        request,
        task=replace(
            request.task,
            inputs=(TaskInput("docs/specs/review.md", digest),),
        ),
    )

    status = asyncio.run(service.agent_spawn(request))

    assert harness.call_count("spawn") == 1
    assert adapter.spawn_requests[0].context.attestation["input_attestations"] == [
        {
            "path": "docs/specs/review.md",
            "sha256": digest,
            "byte_count": len(input_path.read_bytes()),
            "source": "subagent-mcp-read-only-sha256",
        }
    ]
    assert status.input_attestations == tuple(
        adapter.spawn_requests[0].context.attestation["input_attestations"]
    )
    assert status.reasoning_attestation["effective"] == {
        "provider_depth": "deep"
    }
    assert status.reasoning_attestation["context_hash"] == (
        adapter.spawn_requests[0].context.context_hash
    )


def test_spawn_rejects_input_hash_drift_before_adapter_work(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    input_path = workspace / "docs" / "specs" / "review.md"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("changed input\n", encoding="utf-8")
    harness = FakeHarness()
    service, _ = _service(tmp_path, harness)
    request = _spawn_request(workspace)
    request = replace(
        request,
        task=replace(
            request.task,
            inputs=(TaskInput("docs/specs/review.md", "a" * 64),),
        ),
    )

    with pytest.raises(ServiceError) as captured:
        asyncio.run(service.agent_spawn(request))

    assert captured.value.code == "INPUT_CHANGED"
    assert captured.value.retryable is False
    assert harness.call_count("resolve_context") == 0
    assert harness.call_count("spawn") == 0


def test_spawn_input_attestation_replay_does_not_rehash_changed_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    input_path = workspace / "docs" / "spec.md"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"reviewed")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    harness = FakeHarness()
    harness.enqueue("done", result="CAPSULE: approved")
    service, _ = _service(tmp_path, harness)
    request = _spawn_request(workspace)
    request = replace(
        request,
        task=replace(
            request.task,
            inputs=(TaskInput("docs/spec.md", digest),),
        ),
    )

    first = asyncio.run(service.agent_spawn(request))
    input_path.write_bytes(b"changed after terminal result")
    replay = asyncio.run(service.agent_spawn(request))

    assert replay.execution_id == first.execution_id
    assert replay.input_attestations == first.input_attestations
    assert harness.call_count("spawn") == 1


def test_snapshot_result_surfaces_bounded_adapter_next_action() -> None:
    snapshot = AdapterSnapshot(
        external_session_id="native-session",
        external_execution_id="execution-1",
        conversation_state="idle",
        execution_state="failed",
        effective_model="vendor/model",
        effective_reasoning={},
        workspace_path="workspace",
        workspace_key="workspace",
        context_hash="a" * 64,
        error=AdapterFailure(
            "RATE_LIMITED",
            "provider",
            True,
            "Provider is temporarily rate-limited",
            "Continue the same live conversation after availability changes.",
        ),
    )

    assert service_module._snapshot_result(snapshot)["error"]["next_action"] == (
        "Continue the same live conversation after availability changes."
    )


def _scoped_spawn_request(
    workspace: Path,
    *,
    request_id: str,
    write_set: tuple[str, ...],
) -> SpawnRequest:
    return SpawnRequest(
        request_id=request_id,
        runtime_id="fake",
        variant_id="future-deep",
        task=TaskPacket(
            title="Bounded implementation",
            prompt="Implement only the declared write set.",
            acceptance_criteria=("Return one normalized result.",),
            role="sub-agent",
        ),
        cwd=str(workspace),
        mode="implement",
        transport="managed-sdk",
        permissions=("repo_read", "workspace_write"),
        write_set=write_set,
    )


def test_runtime_list_publishes_external_delegation_priority(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, FakeHarness())

    runtimes = asyncio.run(service.runtime_list())

    assert runtimes[0]["runtime_id"] == "fake"
    assert runtimes[0]["delegation_priority"] == 73


def test_runtime_list_publishes_adapter_owned_model_catalog(tmp_path: Path) -> None:
    harness = FakeHarness()
    service, _ = _service(tmp_path, harness, adapter=_CatalogFakeAdapter(harness))

    runtime = asyncio.run(service.runtime_list())[0]

    assert runtime["model_catalog"] == [
        {
            "value": "vendor::model-a",
            "label": "Model A",
            "provider": "vendor",
            "model": "model-a",
        },
        {
            "value": "vendor::model-b",
            "label": "Model B",
            "provider": "vendor",
            "model": "model-b",
        },
    ]


def test_runtime_list_publishes_ordered_model_fallback_policy(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, FakeHarness())
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )
    config = ConfigStore(paths)
    document = config.load()
    policy = document["runtimes"]["fake"]
    policy["selection_mode"] = "lead-selects"
    policy["variants"].append(
        {
            "id": "fallback-1",
            "model": "vendor/fallback-model",
            "reasoning": {"provider_depth": "deep"},
        }
    )
    config.save(document, expected_revision=1)

    runtime = asyncio.run(service.runtime_list())[0]

    assert runtime["model_policy"] == {
        "selection_mode": "lead-selects",
        "ordered_variants": [
            {
                "variant_id": "future-deep",
                "model": "vendor/future-model:preview-01",
            },
            {"variant_id": "fallback-1", "model": "vendor/fallback-model"},
        ],
        "fallback_on": ["QUOTA_PAUSED"],
    }


@pytest.mark.parametrize(
    ("error_code", "expected_order"),
    [
        ("QUOTA_PAUSED", ["fallback-1", "future-deep"]),
        ("USAGE_CREDITS_FORBIDDEN", ["fallback-1", "future-deep"]),
        ("FAKE_FAILURE", ["future-deep", "fallback-1"]),
    ],
)
def test_terminal_quota_failure_demotes_only_the_exact_model_for_future_tasks(
    tmp_path: Path,
    error_code: str,
    expected_order: list[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    service, _ = _service(tmp_path, harness)
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )
    config = ConfigStore(paths)
    document = config.load()
    document["runtimes"]["fake"]["variants"].append(
        {
            "id": "fallback-1",
            "model": "vendor/fallback-model",
            "reasoning": {"provider_depth": "deep"},
        }
    )
    config.save(document, expected_revision=1)
    harness.enqueue("failure", error_code=error_code)

    status = asyncio.run(service.agent_spawn(_spawn_request(workspace)))

    assert status.execution_state == "failed"
    assert harness.call_count("spawn") == 1
    variants = config.load()["runtimes"]["fake"]["variants"]
    assert [item["id"] for item in variants] == expected_order
    failed = next(item for item in variants if item["id"] == "future-deep")
    if error_code in {"QUOTA_PAUSED", "USAGE_CREDITS_FORBIDDEN"}:
        assert failed["availability"] == {
            "state": "quota_paused",
            "reason_code": error_code,
        }
    else:
        assert "availability" not in failed


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


def test_runtime_check_gates_ready_circuit_without_claiming_quota_is_exhausted(
    tmp_path: Path,
) -> None:
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
        adapter.quota_error_code = "CAPABILITY_MISSING"
        return await service.runtime_check("fake", refresh_quota=True)

    refreshed = asyncio.run(run())

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
    assert refreshed["state"] == "ready"
    assert store.load_circuit("fake", "future-deep").state == "ready"
    assert adapter.canary_calls == adapter.quota_calls == 1


def test_runtime_check_uses_no_model_probe_for_required_and_paused_quota(
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
    assert required["state"] == "needs_canary"
    assert paused["quota"]["state"] == "quota_paused"
    assert paused["state"] == "needs_canary"
    assert recovered["quota"]["state"] == "available"
    assert recovered["state"] == "needs_canary"
    assert store.load_circuit("fake", "future-deep").state == "needs_canary"
    assert adapter.canary_calls == 0
    assert adapter.quota_calls == 3


def test_runtime_check_fails_closed_on_ambiguous_quota_evidence(tmp_path: Path) -> None:
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )
    config = ConfigStore(paths)

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
        adapter.quota_cleanup_confirmed = False
        return await service.runtime_check("fake", refresh_quota=True)

    refreshed = asyncio.run(run())

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
    circuit = store.load_circuit("fake", "future-deep")
    assert circuit.state == "ready"
    assert "error_code" not in circuit.details
    variants = config.load()["runtimes"]["fake"]["variants"]
    assert [item["id"] for item in variants] == ["future-deep"]
    assert "availability" not in variants[0]


def test_safe_task_response_reopens_an_explicit_quota_pause_without_extra_probe(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
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
        paused = await service.runtime_check("fake", refresh_quota=True)
        adapter.quota_error_code = None
        harness.enqueue("done", result="recovered task")
        task = await service.agent_spawn(_spawn_request(workspace, request_id="spawn-recovered"))
        return paused, task

    paused, task = asyncio.run(run())

    assert paused["state"] == "ready"
    assert paused["can_start_explicit_task"] is True
    assert paused["quota"]["state"] == "quota_paused"
    assert task.execution_state == "succeeded"
    assert store.load_circuit("fake", "future-deep").state == "ready"
    assert adapter.quota_calls == 1


def test_cached_quota_pause_is_reported_as_nonblocking_for_a_new_explicit_task(
    tmp_path: Path,
) -> None:
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
        await service.runtime_check("fake", refresh_quota=True)
        local = await service.runtime_check("fake")
        listed = (await service.runtime_list())[0]
        return local, listed

    local, listed = asyncio.run(run())

    assert store.load_circuit("fake", "future-deep").state == "auto_paused"
    assert local["state"] == "ready"
    assert local["can_start_explicit_task"] is True
    assert local["details"]["circuits"] == [
        {
            "variant_id": "future-deep",
            "state": "auto_paused",
            "revision": 3,
            "blocks_explicit_task": False,
        }
    ]
    assert listed["circuits"][0]["state"] == "auto_paused"
    assert listed["circuits"][0]["blocks_explicit_task"] is False


def test_explicit_task_rechecks_non_probe_quota_pause(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    service, _ = _service(tmp_path, harness)
    service._config.set_variant_quota_state(
        "fake",
        "future-deep",
        paused=True,
        reason_code="QUOTA_PAUSED",
    )
    harness.enqueue("done", result="current provider task succeeded")

    task = asyncio.run(
        service.agent_spawn(
            _spawn_request(workspace, request_id="spawn-half-open-non-probe")
        )
    )

    assert task.execution_state == "succeeded"
    assert harness.call_count("spawn") == 1
    variant = service._config.load()["runtimes"]["fake"]["variants"][0]
    assert "availability" not in variant


def test_non_probe_usage_credits_stop_remains_forbidden(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    service, _ = _service(tmp_path, harness)
    service._config.set_variant_quota_state(
        "fake",
        "future-deep",
        paused=True,
        reason_code="USAGE_CREDITS_FORBIDDEN",
    )

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _spawn_request(workspace, request_id="spawn-credit-stop-non-probe")
            )
        )

    assert captured.value.code == "USAGE_CREDITS_FORBIDDEN"
    assert harness.call_count("spawn") == 0


def test_safe_task_clears_config_only_pause_on_ready_circuit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)

    async def run():
        await service.runtime_check("fake")
        await service.runtime_canary(
            {
                "request_id": "ready-circuit-canary",
                "runtime_id": "fake",
                "variant_id": "future-deep",
                "transport": "managed-sdk",
            }
        )
        service._config.set_variant_quota_state(
            "fake",
            "future-deep",
            paused=True,
            reason_code="QUOTA_PAUSED",
        )
        harness.enqueue("done", result="safe current provider task")
        return await service.agent_spawn(
            _spawn_request(workspace, request_id="spawn-config-only-recovery")
        )

    task = asyncio.run(run())

    assert task.execution_state == "succeeded"
    assert store.load_circuit("fake", "future-deep").state == "ready"
    variant = service._config.load()["runtimes"]["fake"]["variants"][0]
    assert "availability" not in variant


def test_background_success_clears_non_probe_quota_pause(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    service, store = _service(tmp_path, harness)
    service._config.set_variant_quota_state(
        "fake",
        "future-deep",
        paused=True,
        reason_code="QUOTA_PAUSED",
    )

    async def run():
        started = await service.agent_spawn(
            _spawn_request(workspace, request_id="spawn-half-open-background")
        )
        session = harness._sessions[str(started.external_session_id)]
        session.snapshot = replace(
            session.snapshot,
            conversation_state="idle",
            execution_state="succeeded",
            result_text="current provider task succeeded",
        )

        while True:
            record = store.load_execution(started.execution_id)
            if record.execution_state == "succeeded":
                return
            await asyncio.sleep(0)

    asyncio.run(asyncio.wait_for(run(), timeout=2.0))

    variant = service._config.load()["runtimes"]["fake"]["variants"][0]
    assert "availability" not in variant


def _prepare_ready_quota_circuit(service: SubagentMcpService) -> None:
    async def run() -> None:
        await service.runtime_check("fake")
        await service.runtime_canary(
            {
                "request_id": "quota-race-canary",
                "runtime_id": "fake",
                "variant_id": "future-deep",
                "transport": "managed-sdk",
            }
        )

    asyncio.run(run())


def _active_writer_leases(store: StateStore) -> int:
    with store.transaction() as database:
        return int(
            database.execute(
                "SELECT COUNT(*) FROM leases "
                "WHERE kind = 'writer' AND released_at_utc IS NULL"
            ).fetchone()[0]
        )


def test_post_native_spawn_service_error_retains_writer_until_cleanup(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _PostNativeFailureAdapter(harness)
    adapter.fail_spawn = True
    service, store = _service(tmp_path, harness, adapter=adapter)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(service.agent_spawn(_spawn_request(workspace, write=True)))

    failed = asyncio.run(
        service.agent_status(StatusRequest("conversation-1", refresh=False))
    )
    assert captured.value.code == "PROVIDER_ERROR"
    assert failed.recovery_required is True
    assert failed.result["error"]["code"] == "PROVIDER_ERROR"
    assert store.load_execution("execution-2").observed["evidence"][
        "cleanup_confirmed"
    ] is False
    assert _active_writer_leases(store) == 1

    with pytest.raises(ServiceError) as overlap:
        asyncio.run(
            service.agent_spawn(
                _spawn_request(
                    workspace,
                    request_id="post-native-spawn-overlap",
                    write=True,
                )
            )
        )
    assert overlap.value.code == "WRITE_SET_BUSY"


def test_post_native_send_service_error_retains_writer_until_cleanup(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _PostNativeFailureAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    spawned = asyncio.run(
        service.agent_spawn(_spawn_request(workspace, write=True))
    )
    assert _active_writer_leases(store) == 0
    adapter.fail_send = True

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    "post-native-send-failure",
                    spawned.conversation_id,
                    "Continue the bounded writer task.",
                )
            )
        )

    failed = asyncio.run(
        service.agent_status(StatusRequest(spawned.conversation_id, refresh=False))
    )
    assert captured.value.code == "PROVIDER_ERROR"
    assert failed.recovery_required is True
    assert failed.result["error"]["code"] == "PROVIDER_ERROR"
    assert _active_writer_leases(store) == 1

    with pytest.raises(ServiceError) as overlap:
        asyncio.run(
            service.agent_spawn(
                _spawn_request(
                    workspace,
                    request_id="post-native-send-overlap",
                    write=True,
                )
            )
        )
    assert overlap.value.code == "WRITE_SET_BUSY"


def test_pre_native_context_failure_releases_writer_lease(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _PreNativeContextFailureAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(service.agent_spawn(_spawn_request(workspace, write=True)))

    assert captured.value.code == "CONTEXT_DRIFT"
    assert _active_writer_leases(store) == 0

    adapter.fail_context = False
    replacement = asyncio.run(
        service.agent_spawn(
            _spawn_request(
                workspace,
                request_id="pre-native-context-repaired",
                write=True,
            )
        )
    )
    assert replacement.execution_state == "succeeded"


def test_adapter_cannot_bind_a_stale_controller_execution_snapshot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    service, store = _service(
        tmp_path,
        harness,
        adapter=_StaleExecutionSnapshotAdapter(harness),
    )

    with pytest.raises(ServiceError) as captured:
        asyncio.run(service.agent_spawn(_spawn_request(workspace, write=True)))

    assert captured.value.code == "CONTEXT_DRIFT"
    record = store.load_execution("execution-2")
    assert record.external_execution_id is None
    assert record.observed["evidence"]["cleanup_confirmed"] is False
    assert _active_writer_leases(store) == 1


def test_close_rejects_stale_cleanup_identity_before_releasing_writer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    service, store = _service(
        tmp_path,
        harness,
        adapter=_StaleCloseSnapshotAdapter(harness),
    )
    started = asyncio.run(service.agent_spawn(_spawn_request(workspace, write=True)))

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_close(
                ActionRequest("stale-close-identity", started.conversation_id)
            )
        )

    assert captured.value.code == "CONTEXT_DRIFT"
    record = store.load_execution(started.execution_id)
    assert record.observed["evidence"]["cleanup_confirmed"] is False
    assert _active_writer_leases(store) == 1


def test_close_allows_exact_terminal_placeholder_when_cleanup_was_not_unverified(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    service, store = _service(
        tmp_path,
        harness,
        adapter=_RestartClosePlaceholderAdapter(harness),
    )
    started = asyncio.run(service.agent_spawn(_spawn_request(workspace)))

    closed = asyncio.run(
        service.agent_close(
            ActionRequest("restart-placeholder-close", started.conversation_id)
        )
    )

    assert closed.conversation_state == "closed"
    assert _active_writer_leases(store) == 0


def test_concurrent_circuit_recovery_never_masks_terminal_provider_quota(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    harness = FakeHarness()
    adapter = _TerminalQuotaAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    _prepare_ready_quota_circuit(service)

    def ui_refresh_wins() -> None:
        current = store.load_circuit("fake", "future-deep")
        store.require_ready_circuit_recovery(
            runtime_id="fake",
            variant_id="future-deep",
            pair_key=current.pair_key,
            expected_revision=current.revision,
            error_code="RECOVERY_REQUIRED",
        )

    adapter.before_failure = ui_refresh_wins

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _scoped_spawn_request(
                    workspace,
                    request_id="quota-race-spawn",
                    write_set=("src",),
                )
            )
        )

    assert captured.value.code == "USAGE_CREDITS_FORBIDDEN"
    assert captured.value.retryable is False
    assert adapter.spawn_calls == 1
    record = store.load_execution("execution-2")
    assert record.result["error"]["code"] == "USAGE_CREDITS_FORBIDDEN"
    assert (record.observed or {}).get("evidence", {}).get("cleanup_confirmed") is False
    assert _active_writer_leases(store) == 1
    assert store.load_circuit("fake", "future-deep").state == "recovery_required"
    variant = ConfigStore(
        resolve_paths(
            {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
            os_name="nt",
        )
    ).load()["runtimes"]["fake"]["variants"][0]
    assert variant["availability"] == {
        "state": "quota_paused",
        "reason_code": "USAGE_CREDITS_FORBIDDEN",
    }


def test_quota_pause_retries_only_local_state_three_times_without_provider_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _TerminalQuotaAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    _prepare_ready_quota_circuit(service)
    original = store.pause_ready_circuit
    attempts = 0

    def flaky_pause(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return original(**kwargs)

    monkeypatch.setattr(store, "pause_ready_circuit", flaky_pause)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(service.agent_spawn(_spawn_request(workspace)))

    assert captured.value.code == "USAGE_CREDITS_FORBIDDEN"
    assert attempts == 3
    assert adapter.spawn_calls == 1
    assert store.load_circuit("fake", "future-deep").state == "auto_paused"


def test_exhausted_local_pause_repair_surfaces_warning_and_retains_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    harness = FakeHarness()
    adapter = _TerminalQuotaAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    _prepare_ready_quota_circuit(service)
    attempts = 0

    def locked_pause(**kwargs):
        del kwargs
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "pause_ready_circuit", locked_pause)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _scoped_spawn_request(
                    workspace,
                    request_id="quota-locked-spawn",
                    write_set=("src",),
                )
            )
        )

    error = captured.value
    assert error.code == "USAGE_CREDITS_FORBIDDEN"
    assert error.retryable is False
    assert "local quota pause state was not recorded after 3 attempts" in str(error)
    assert error.next_action is not None and "Do not retry this task" in error.next_action
    assert attempts == 3
    assert adapter.spawn_calls == 1
    assert _active_writer_leases(store) == 1


def test_quota_pause_persistence_never_swallows_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = FakeHarness()
    adapter = _TerminalQuotaAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    _prepare_ready_quota_circuit(service)
    circuit = store.load_circuit("fake", "future-deep")

    def cancelled_pause(**kwargs):
        del kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "pause_ready_circuit", cancelled_pause)

    with pytest.raises(asyncio.CancelledError):
        service._pause_after_quota_error(
            circuit,
            ServiceError(
                "QUOTA_PAUSED",
                "provider quota paused",
                category="quota",
            ),
        )


def test_ready_runtime_does_not_spend_a_separate_provider_status_request(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
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
        harness.enqueue("done", result="one useful task request")
        return await service.agent_spawn(
            _spawn_request(workspace, request_id="spawn-no-extra-status")
        )

    task = asyncio.run(run())

    assert task.execution_state == "succeeded"
    assert adapter.quota_calls == 0
    assert harness.call_count("spawn") == 1
    assert store.load_circuit("fake", "future-deep").state == "ready"


def test_runtime_check_explains_unknown_no_model_quota_failure(tmp_path: Path) -> None:
    harness = FakeHarness()
    adapter = _QuotaFakeAdapter(harness)
    adapter.quota_error_code = "CAPABILITY_MISSING"
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
        await service.runtime_check("fake")
        await service.runtime_canary(
            {
                "request_id": "initial-canary",
                "runtime_id": "fake",
                "variant_id": "future-deep",
                "transport": "managed-sdk",
            }
        )
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


def test_wait_timeout_returns_running_without_interrupting_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    service, _ = _service(tmp_path, harness)

    async def run():
        started = await service.agent_spawn(_spawn_request(workspace))
        waited = await service.agent_wait(
            WaitRequest(
                (WaitTarget(started.conversation_id),),
                timeout_seconds=0.01,
            )
        )
        return started, waited[0]

    started, waited = asyncio.run(run())

    assert started.execution_state == waited.execution_state == "running"
    assert harness.call_count("interrupt") == 0


def test_service_supervisor_persists_background_terminal_without_caller_poll(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("running")
    service, store = _service(tmp_path, harness)

    async def run():
        started = await service.agent_spawn(_spawn_request(workspace))
        session = harness._sessions[str(started.external_session_id)]
        session.snapshot = replace(
            session.snapshot,
            conversation_state="idle",
            execution_state="succeeded",
            result_text="supervised result",
        )

        async def persisted():
            while True:
                record = store.load_execution(started.execution_id)
                if record.execution_state == "succeeded":
                    return record
                await asyncio.sleep(0)

        return await asyncio.wait_for(persisted(), timeout=0.1)

    record = asyncio.run(run())

    assert record.result == {"text": "supervised result"}
    assert harness.call_count("snapshot") >= 1


def test_restart_gap_becomes_persisted_recovery_instead_of_stuck_running(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_harness = FakeHarness()
    first_harness.enqueue("running")
    first_adapter = _RestartGapAdapter(first_harness)
    first_service, store = _service(tmp_path, first_harness, adapter=first_adapter)

    started = asyncio.run(
        first_service.agent_spawn(_spawn_request(workspace, write=True))
    )
    fresh_harness = FakeHarness()
    fresh_adapter = _RestartGapAdapter(fresh_harness)
    fresh_ids = itertools.count(100)
    fresh_service = SubagentMcpService(
        config=first_service._config,
        store=store,
        registry=AdapterRegistry(builtin_factories=(lambda: fresh_adapter,)),
        id_factory=lambda prefix: f"{prefix}-{next(fresh_ids)}",
    )

    recovered = asyncio.run(
        fresh_service.agent_status(
            StatusRequest(started.conversation_id, refresh=True)
        )
    )

    assert recovered.execution_state == "failed"
    assert recovered.recovery_required is True
    assert recovered.result["error"]["code"] == "RECOVERY_REQUIRED"

    with pytest.raises(ServiceError) as still_busy:
        asyncio.run(
            fresh_service.agent_spawn(
                _spawn_request(
                    workspace,
                    request_id="restart-overlap-before-cleanup",
                    write=True,
                )
            )
        )
    assert still_busy.value.code == "WRITE_SET_BUSY"

    with pytest.raises(ServiceError) as unverified_close:
        asyncio.run(
            fresh_service.agent_close(
                ActionRequest("restart-close-unverified", started.conversation_id)
            )
        )
    assert unverified_close.value.code == "RECOVERY_REQUIRED"

    fresh_adapter.cleanup_confirmed = True
    closed = asyncio.run(
        fresh_service.agent_close(
            ActionRequest("restart-close-verified", started.conversation_id)
        )
    )

    fresh_harness.enqueue("running")
    replacement = asyncio.run(
        fresh_service.agent_spawn(
            _spawn_request(
                workspace,
                request_id="restart-overlap-after-cleanup",
                write=True,
            )
        )
    )

    assert closed.conversation_state == "closed"
    assert closed.recovery_required is False
    assert replacement.execution_state == "running"


def test_restart_gap_releases_writer_after_verified_process_absence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_harness = FakeHarness()
    first_harness.enqueue("running")
    first_service, store = _service(
        tmp_path,
        first_harness,
        adapter=_RestartGapAdapter(first_harness),
    )
    started = asyncio.run(
        first_service.agent_spawn(_spawn_request(workspace, write=True))
    )

    fresh_harness = FakeHarness()
    fresh_harness.enqueue("running")
    fresh_adapter = _RestartGapAdapter(fresh_harness)
    fresh_adapter.cleanup_confirmed = True
    fresh_ids = itertools.count(200)
    fresh_service = SubagentMcpService(
        config=first_service._config,
        store=store,
        registry=AdapterRegistry(builtin_factories=(lambda: fresh_adapter,)),
        id_factory=lambda prefix: f"{prefix}-{next(fresh_ids)}",
    )

    reconciled = asyncio.run(
        fresh_service.agent_status(
            StatusRequest(started.conversation_id, refresh=True)
        )
    )
    replacement = asyncio.run(
        fresh_service.agent_spawn(
            _spawn_request(
                workspace,
                request_id="restart-auto-release",
                write=True,
            )
        )
    )

    assert reconciled.execution_state == "failed"
    assert reconciled.recovery_required is False
    assert reconciled.result["error"]["code"] == "CONTROLLER_DISCONNECTED"
    assert replacement.execution_state == "running"


def test_startup_recovery_holds_writer_until_verified_close(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _StartupRecoveryAdapter(harness)
    service, _ = _service(tmp_path, harness, adapter=adapter)

    with pytest.raises(ServiceError) as startup:
        asyncio.run(service.agent_spawn(_spawn_request(workspace, write=True)))
    assert startup.value.code == "RECOVERY_REQUIRED"

    with pytest.raises(ServiceError) as still_busy:
        asyncio.run(
            service.agent_spawn(
                _spawn_request(
                    workspace,
                    request_id="startup-overlap-before-cleanup",
                    write=True,
                )
            )
        )
    assert still_busy.value.code == "WRITE_SET_BUSY"

    failed = asyncio.run(
        service.agent_status(StatusRequest("conversation-1", refresh=False))
    )
    with pytest.raises(ServiceError) as unverified_close:
        asyncio.run(
            service.agent_close(
                ActionRequest("startup-close-unverified", failed.conversation_id)
            )
        )
    assert unverified_close.value.code == "RECOVERY_REQUIRED"

    adapter.cleanup_confirmed = True
    closed = asyncio.run(
        service.agent_close(
            ActionRequest("startup-close-verified", failed.conversation_id)
        )
    )
    adapter.fail_spawn = False
    harness.enqueue("running")
    replacement = asyncio.run(
        service.agent_spawn(
            _spawn_request(
                workspace,
                request_id="startup-overlap-after-cleanup",
                write=True,
            )
        )
    )

    assert closed.conversation_state == "closed"
    assert closed.recovery_required is False
    assert replacement.execution_state == "running"


def _simulate_crashed_start(
    service: SubagentMcpService,
    store: StateStore,
    request: SpawnRequest,
    *,
    native_invoked: bool,
) -> None:
    workspace_path, workspace_key = service_module._workspace(request.cwd)
    write_set = service_module._normalize_write_set(
        workspace_path,
        request.permissions,
        request.write_set,
    )
    variant = service._config.load()["runtimes"]["fake"]["variants"][0]
    requested = service_module._requested_metadata(
        request,
        variant=variant,
        transport="managed-sdk",
        workspace_path=workspace_path,
        workspace_key=workspace_key,
        write_set=write_set,
    )
    claim = store.claim_execution_request(
        tool="agent_spawn",
        request_id=request.request_id,
        request_payload=service_module._spawn_digest_payload(request, write_set),
        conversation_id="crashed-conversation",
        execution_id="crashed-execution",
        runtime_id="fake",
        requested=requested,
    )
    assert store.claim_execution_start(claim.execution_id).should_launch is True
    store.acquire_writer_scope_leases(
        workspace_key=workspace_key,
        write_set=write_set,
        execution_id=claim.execution_id,
    )
    if native_invoked:
        store.record_launch_attempt(
            claim.execution_id,
            {
                "model": requested["model"],
                "reasoning": requested["reasoning"],
                "workspace_path": workspace_path,
                "workspace_key": workspace_key,
                "transport": "managed-sdk",
                "context_hash": "simulated-prior-process-context",
                "capability_gaps": [],
                "attestation": {"write_set": list(write_set)},
                "native_invoked": True,
                "evidence": {"cleanup_confirmed": False},
            },
        )


def test_prior_process_start_before_native_call_recovers_without_clock(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    service, store = _service(tmp_path, harness)
    request = _spawn_request(
        workspace,
        request_id="prior-process-before-native",
        write=True,
    )
    _simulate_crashed_start(
        service,
        store,
        request,
        native_invoked=False,
    )

    recovered = asyncio.run(
        service.agent_status(StatusRequest("crashed-conversation", refresh=False))
    )
    replay = asyncio.run(service.agent_spawn(request))

    assert recovered.execution_state == "failed"
    assert replay.execution_state == "failed"
    assert recovered.recovery_required is True
    assert recovered.result["error"]["code"] == "RECOVERY_REQUIRED"
    assert _active_writer_leases(store) == 0
    assert harness.call_count("spawn") == 0


def test_prior_process_native_attempt_stays_leased_until_verified_cleanup(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    adapter = _StartupRecoveryAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    request = _spawn_request(
        workspace,
        request_id="prior-process-after-native",
        write=True,
    )
    _simulate_crashed_start(
        service,
        store,
        request,
        native_invoked=True,
    )

    recovered = asyncio.run(service.agent_spawn(request))

    assert recovered.execution_state == "failed"
    assert recovered.recovery_required is True
    assert _active_writer_leases(store) == 1
    with pytest.raises(ServiceError) as unverified:
        asyncio.run(
            service.agent_close(
                ActionRequest("prior-process-close-unverified", recovered.conversation_id)
            )
        )
    assert unverified.value.code == "RECOVERY_REQUIRED"

    adapter.cleanup_confirmed = True
    closed = asyncio.run(
        service.agent_close(
            ActionRequest("prior-process-close-verified", recovered.conversation_id)
        )
    )

    assert closed.conversation_state == "closed"
    assert _active_writer_leases(store) == 0
    assert harness.call_count("spawn") == 0


def test_prior_process_send_start_is_recovered_before_session_busy_check(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="initial task")
    service, store = _service(tmp_path, harness)
    spawned = asyncio.run(service.agent_spawn(_spawn_request(workspace, write=True)))
    previous = store.load_execution(spawned.execution_id)
    request = SendRequest(
        "prior-process-send",
        spawned.conversation_id,
        "Continue after restart.",
    )
    claim = store.claim_execution_request(
        tool="agent_send",
        request_id=request.request_id,
        request_payload={
            "conversation_id": request.conversation_id,
            "prompt": request.prompt,
            "reply_to": request.reply_to,
            "answers": {},
        },
        conversation_id=request.conversation_id,
        execution_id="crashed-send-execution",
        runtime_id=None,
        requested=previous.requested,
    )
    assert store.claim_execution_start(claim.execution_id).should_launch is True
    store.acquire_writer_scope_leases(
        workspace_key=str(previous.requested["workspace_key"]),
        write_set=tuple(previous.requested["write_set"]),
        execution_id=claim.execution_id,
    )

    with pytest.raises(ServiceError) as recovered:
        asyncio.run(service.agent_send(request))

    assert recovered.value.code == "RECOVERY_REQUIRED"
    latest = store.load_execution(claim.execution_id)
    assert latest.execution_state == "failed"
    assert _active_writer_leases(store) == 0
    assert harness.call_count("send") == 0


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


def test_terminal_writer_spawn_replay_does_not_reacquire_released_lease(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="implemented")
    service, store = _service(tmp_path, harness)
    request = _scoped_spawn_request(
        workspace,
        request_id="writer-replay",
        write_set=("src",),
    )

    first = asyncio.run(service.agent_spawn(request))
    replay = asyncio.run(service.agent_spawn(request))

    assert replay.execution_id == first.execution_id
    assert replay.execution_state == "succeeded"
    assert harness.call_count("spawn") == 1
    with store.transaction() as database:
        assert database.execute(
            "SELECT COUNT(*) FROM leases WHERE released_at_utc IS NULL"
        ).fetchone()[0] == 0


def test_terminal_writer_send_replay_does_not_reacquire_released_lease(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="spawned")
    harness.enqueue("done", result="continued")
    service, store = _service(tmp_path, harness)
    spawned = asyncio.run(
        service.agent_spawn(
            _scoped_spawn_request(
                workspace,
                request_id="writer-send-spawn",
                write_set=("src",),
            )
        )
    )
    request = SendRequest("writer-send-replay", spawned.conversation_id, "Continue.")

    first = asyncio.run(service.agent_send(request))
    replay = asyncio.run(service.agent_send(request))

    assert replay.execution_id == first.execution_id
    assert replay.execution_state == "succeeded"
    assert harness.call_count("send") == 1
    with store.transaction() as database:
        assert database.execute(
            "SELECT COUNT(*) FROM leases WHERE released_at_utc IS NULL"
        ).fetchone()[0] == 0


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


def test_writer_scopes_allow_disjoint_executions_in_same_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src" / "context").mkdir(parents=True)
    (workspace / "docs" / "status").mkdir(parents=True)
    harness = FakeHarness()
    harness.enqueue("running")
    harness.enqueue("done")
    service, _ = _service(tmp_path, harness)

    first = asyncio.run(
        service.agent_spawn(
            _scoped_spawn_request(
                workspace, request_id="writer-1", write_set=("src/context",)
            )
        )
    )
    second = asyncio.run(
        service.agent_spawn(
            _scoped_spawn_request(
                workspace, request_id="writer-2", write_set=("docs/status",)
            )
        )
    )

    assert first.execution_state == "running"
    assert second.execution_state == "succeeded"
    assert harness.call_count("spawn") == 2


def test_writer_scopes_block_parent_child_overlap_before_adapter_launch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src" / "context").mkdir(parents=True)
    harness = FakeHarness()
    harness.enqueue("running")
    harness.enqueue("done")
    service, _ = _service(tmp_path, harness)

    first = asyncio.run(
        service.agent_spawn(
            _scoped_spawn_request(
                workspace, request_id="writer-1", write_set=("src/context",)
            )
        )
    )
    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _scoped_spawn_request(
                    workspace, request_id="writer-2", write_set=("src",)
                )
            )
        )

    assert first.execution_state == "running"
    assert captured.value.code == "WRITE_SET_BUSY"
    assert captured.value.retryable is True
    assert captured.value.next_action is not None
    assert "new request_id" in captured.value.next_action
    assert captured.value.recovery == {
        "action": "retry",
        "reason": "transient_pre_provider",
        "max_attempts": 3,
    }
    assert harness.call_count("spawn") == 1


def test_writer_scopes_block_overlap_across_nested_workspace_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    harness = FakeHarness()
    harness.enqueue("running")
    harness.enqueue("done")
    service, _ = _service(tmp_path, harness)

    first = asyncio.run(
        service.agent_spawn(
            _scoped_spawn_request(
                workspace,
                request_id="writer-root",
                write_set=("src",),
            )
        )
    )
    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _scoped_spawn_request(
                    nested,
                    request_id="writer-nested",
                    write_set=(".",),
                )
            )
        )

    assert first.execution_state == "running"
    assert captured.value.code == "WRITE_SET_BUSY"
    assert harness.call_count("spawn") == 1


def _store_row_counts(store: StateStore) -> tuple[int, int, int]:
    with store.transaction() as connection:
        conversations = connection.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
        executions = connection.execute(
            "SELECT COUNT(*) FROM executions"
        ).fetchone()[0]
        leases = connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    return conversations, executions, leases


def test_multi_root_preflight_rejects_before_readiness_idempotency_leases_or_provider_work(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "context").mkdir(parents=True)
    (workspace / "docs" / "status").mkdir(parents=True)
    harness = FakeHarness()
    service, store = _service(tmp_path, harness, adapter=_OneRootFakeAdapter(harness))

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _scoped_spawn_request(
                    workspace,
                    request_id="multi-root-1",
                    write_set=("src/context", "docs/status"),
                )
            )
        )

    error = captured.value
    assert error.code == "CAPABILITY_MISSING"
    assert error.category == "capability"
    assert error.retryable is False
    assert error.recovery == {
        "action": "repair",
        "reason": "decompose_write_set",
        "max_attempts": 3,
        "max_write_roots_per_session": 1,
    }
    assert "non-overlapping writer calls" in error.next_action
    assert "new request_id" in error.next_action
    public = error.to_dict()
    assert public["recovery"]["max_attempts"] == 3
    assert harness.call_count("probe") == 0
    assert harness.call_count("resolve_context") == 0
    assert harness.call_count("spawn") == 0
    assert _store_row_counts(store) == (0, 0, 0)


def test_directory_only_preflight_rejects_files_before_impossible_decomposition(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    first_file = workspace / "src" / "first.py"
    second_file = workspace / "src" / "second.py"
    first_file.write_text("first\n", encoding="utf-8")
    second_file.write_text("second\n", encoding="utf-8")
    harness = FakeHarness()
    service, store = _service(
        tmp_path, harness, adapter=_ExistingDirectoryRootFakeAdapter(harness)
    )

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _scoped_spawn_request(
                    workspace,
                    request_id="directory-root-files",
                    write_set=("src/first.py", "src/second.py"),
                )
            )
        )

    error = captured.value
    assert error.code == "CAPABILITY_MISSING"
    assert error.category == "capability"
    assert error.retryable is False
    assert "existing directory" in str(error)
    assert "broader write authority" in error.next_action
    assert "another runtime" in error.next_action
    assert error.recovery == {
        "action": "repair",
        "reason": "select_supported_write_root",
        "max_attempts": 3,
        "max_write_roots_per_session": 1,
        "write_root_mode": "existing-directory",
    }
    assert harness.call_count("probe") == 0
    assert harness.call_count("resolve_context") == 0
    assert harness.call_count("spawn") == 0
    assert _store_row_counts(store) == (0, 0, 0)


def test_directory_only_preflight_still_decomposes_multiple_valid_directories(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "docs").mkdir()
    harness = FakeHarness()
    service, _ = _service(
        tmp_path, harness, adapter=_ExistingDirectoryRootFakeAdapter(harness)
    )

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_spawn(
                _scoped_spawn_request(
                    workspace,
                    request_id="directory-root-directories",
                    write_set=("src", "docs"),
                )
            )
        )

    assert captured.value.recovery["reason"] == "decompose_write_set"


def test_one_root_limit_still_launches_single_and_disjoint_writers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "context").mkdir(parents=True)
    (workspace / "docs" / "status").mkdir(parents=True)
    harness = FakeHarness()
    harness.enqueue("running")
    harness.enqueue("done")
    service, _ = _service(tmp_path, harness, adapter=_OneRootFakeAdapter(harness))

    first = asyncio.run(
        service.agent_spawn(
            _scoped_spawn_request(
                workspace, request_id="single-root-1", write_set=("src/context",)
            )
        )
    )
    second = asyncio.run(
        service.agent_spawn(
            _scoped_spawn_request(
                workspace, request_id="single-root-2", write_set=("docs/status",)
            )
        )
    )

    assert first.execution_state == "running"
    assert second.execution_state == "succeeded"
    assert harness.call_count("probe") == 2
    assert harness.call_count("resolve_context") == 2
    assert harness.call_count("spawn") == 2


def test_prompt_credentials_and_pii_are_not_persisted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    pem_secret = (
        "-----BEGIN PRIVATE KEY-----\n"
        "cGVtLXNlY3JldC1tYXJrZXI=\n"
        "-----END PRIVATE KEY-----"
    )
    aws_key_id = "AKIAIOSFODNN7EXAMPLE"
    signed_value = "0123456789abcdef0123456789abcdef"
    harness.enqueue(
        "done",
        result=(
            "Bearer abcdefghijklmnopqrstuvwxyz user@example.com "
            f"{pem_secret} {aws_key_id} "
            f"https://example.invalid/object?X-Amz-Signature={signed_value} finished"
        ),
    )
    service, store = _service(tmp_path, harness)
    prompt = "private transcript credential-marker hidden-thinking"

    status = asyncio.run(
        service.agent_spawn(_spawn_request(workspace, prompt=prompt))
    )

    public_result = str(status.result["text"])
    assert "Bearer [REDACTED]" in public_result
    assert "[REDACTED_EMAIL]" in public_result
    assert pem_secret not in public_result
    assert aws_key_id not in public_result
    assert signed_value not in public_result
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
    assert "credential-marker" not in durable
    assert "user@example.com" not in durable
    assert "hidden-thinking" not in durable
    assert "private transcript" not in durable
    assert pem_secret not in durable
    assert aws_key_id not in durable
    assert signed_value not in durable


def test_redact_text_covers_private_keys_cloud_ids_and_assigned_secrets() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "bGl0ZXJhbC1wZW0tc2VjcmV0\n"
        "-----END RSA PRIVATE KEY-----"
    )
    escaped_pem = pem.replace("\n", "\\n")
    aws_key_id = "ASIAIOSFODNN7EXAMPLE"
    signature = "abcdef0123456789abcdef0123456789"
    access_token = "ya29.abcdefghijklmnopqrstuvwxyz"
    client_secret = "client-secret-value-123456"
    basic = "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
    raw = (
        f"{pem}\n{escaped_pem}\n{aws_key_id}\n"
        f"https://example.invalid/?X-Amz-Signature={signature}"
        f"&access_token={access_token}\n"
        f"client_secret={client_secret}\n"
        f"Authorization: Basic {basic}"
    )

    redacted = service_module._redact_text(raw)

    for secret in (
        "bGl0ZXJhbC1wZW0tc2VjcmV0",
        aws_key_id,
        signature,
        access_token,
        client_secret,
        basic,
    ):
        assert secret not in redacted
    assert "[REDACTED" in redacted


def test_redact_text_preserves_identity_and_token_metrics() -> None:
    raw = (
        "rough_tokens_full=12345 token_count=4096 "
        "total_tokens=12345678901 tokens_used=42 signature_verified=true "
        "pair_key=abc123def456 normal prose"
    )

    assert service_module._redact_text(raw) == raw


def test_redact_text_scrubs_secret_crossing_the_output_boundary() -> None:
    secret = "sk-ant-api03-" + ("a" * 32)
    raw = ("x" * (service_module._REDACTED_TEXT_MAX_CHARS - 4)) + secret

    redacted = service_module._redact_text(raw)

    assert "sk-ant" not in redacted
    assert len(redacted) <= service_module._REDACTED_TEXT_MAX_CHARS


def test_redact_text_covers_serialized_and_compound_credentials() -> None:
    secrets = {
        "json_signature": "deadbeefcafe1234",
        "json_basic": "dXNlcjpwYXNzd29yZA==",
        "session_token": "session-token-value-1234",
        "x_amz_signature": "zzzyyyyxxxxwwww",
        "digest_response": "abcdef123456",
        "negotiate": "TlRMTVNTUAABAAAAB4IIog==",
        "userinfo": "S3cretPass",
        "query_auth": "abcd1234ABCD==",
    }
    raw = (
        f'{{"signature": "{secrets["json_signature"]}", '
        f'"authorization": "Basic {secrets["json_basic"]}"}}\n'
        f'session_token={secrets["session_token"]}\n'
        f'x_amz_signature={secrets["x_amz_signature"]}\n'
        'Authorization: Digest username="x", '
        f'response="{secrets["digest_response"]}", nonce="bbccdd00"\n'
        f'Authorization: Negotiate {secrets["negotiate"]}\n'
        f'postgres://admin:{secrets["userinfo"]}@127.0.0.1:5432/prod\n'
        f'https://example.invalid/?auth=Basic+{secrets["query_auth"]}'
    )

    redacted = service_module._redact_text(raw)

    for secret in secrets.values():
        assert secret not in redacted


def test_redact_normalizes_only_exact_sensitive_mapping_keys() -> None:
    secret = "opaque-secret-value-123456"
    redacted = service_module._redact(
        {
            "x-api-key": secret,
            "Set-Cookie": secret,
            "client_secret": secret,
            "session_key": secret,
            "refresh_token": secret,
            "private_key": secret,
            "aws_secret_access_key": secret,
            "proxy-authorization": secret,
            "pair_key": "abc123def456",
            "workspace_key": "workspace-123",
            "token_count": 4096,
        }
    )

    for key in (
        "x-api-key",
        "Set-Cookie",
        "client_secret",
        "session_key",
        "refresh_token",
        "private_key",
        "aws_secret_access_key",
        "proxy-authorization",
    ):
        assert redacted[key] == "[REDACTED]"
    assert redacted["pair_key"] == "abc123def456"
    assert redacted["workspace_key"] == "workspace-123"
    assert redacted["token_count"] == 4096


def test_task_title_is_redacted_bounded_and_prompt_is_not_persisted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="finished")
    service, store = _service(tmp_path, harness)
    request = _spawn_request(workspace, prompt="prompt-secret-marker")
    request = replace(
        request,
        task=replace(
            request.task,
            title=(
                "Review user@example.com Bearer abcdefghijklmnopqrstuvwxyz "
                + "x" * 400
            ),
        ),
    )

    asyncio.run(service.agent_spawn(request))

    with store.transaction() as database:
        requested = json.loads(
            database.execute("SELECT requested_json FROM executions").fetchone()[0]
        )
    assert requested["task_title"].startswith("Review [REDACTED_EMAIL] Bearer [REDACTED]")
    assert len(requested["task_title"]) <= 240
    encoded = json.dumps(requested)
    assert "prompt-secret-marker" not in encoded
    for absent in ("prompt", "acceptance_criteria", "role", "authority"):
        assert absent not in requested


def test_terminal_result_is_hash_addressed_and_read_in_bounded_slices(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    full_text = "CAPSULE: critical result\nDETAILS:\n" + ("evidence " * 900)
    harness.enqueue("done", result=full_text)
    service, _ = _service(tmp_path, harness)

    status = asyncio.run(service.agent_spawn(_spawn_request(workspace)))
    artifact = status.to_compact_dict()["result"]["artifact"]
    first = asyncio.run(
        service.agent_result_read(
            ResultReadRequest(
                status.conversation_id,
                status.execution_id,
                artifact["sha256"],
                offset=0,
                limit=127,
            )
        )
    )
    second = asyncio.run(
        service.agent_result_read(
            ResultReadRequest(
                status.conversation_id,
                status.execution_id,
                artifact["sha256"],
                offset=first["next_offset"],
                limit=8192,
            )
        )
    )

    assert status.result == {"text": full_text}
    assert artifact["char_count"] == len(full_text)
    assert artifact["capsule"] == "critical result"
    assert first["text"] + second["text"] == full_text
    assert first["eof"] is False
    assert second["eof"] is True
    assert harness.call_count("spawn") == 1
    with pytest.raises(ServiceError) as changed:
        asyncio.run(
            service.agent_result_read(
                ResultReadRequest(
                    status.conversation_id,
                    status.execution_id,
                    "f" * 64,
                )
            )
        )
    assert changed.value.code == "RESULT_CHANGED"


def test_unimplemented_preview_surface_is_explicit_capability_gap(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, FakeHarness())

    with pytest.raises(ServiceError) as captured:
        asyncio.run(service.runtime_canary({"runtime_id": "fake"}))

    assert captured.value.code == "CAPABILITY_MISSING"
    assert captured.value.retryable is False


_SOURCE_REPORT = (
    "CAPSULE: relay source\n"
    "DETAILS:\nunique-relay-payload-42 plus complete provider evidence"
)
_RELAY_OPERATIONS = ("spawn", "send", "open_session", "probe")


class _RecordingSendAdapter(FakeAdapter):
    def __init__(self, harness: FakeHarness) -> None:
        super().__init__(harness)
        self.sent_prompts: list[str] = []
        self.send_requests: list[AdapterSendRequest] = []

    async def send(self, request: AdapterSendRequest):
        self.sent_prompts.append(request.prompt)
        self.send_requests.append(request)
        return await super().send(request)


def test_agent_send_hash_binds_changed_input_before_followup_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    input_path = workspace / "docs" / "spec.md"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"round one")
    harness = FakeHarness()
    adapter = _RecordingSendAdapter(harness)
    harness.enqueue("done", result="CAPSULE: round one")
    service, _ = _service(tmp_path, harness, adapter=adapter)
    started = asyncio.run(service.agent_spawn(_spawn_request(workspace)))
    input_path.write_bytes(b"round two")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    harness.enqueue("done", result="CAPSULE: round two")

    status = asyncio.run(
        service.agent_send(
            SendRequest(
                "send-attested-round-two",
                started.conversation_id,
                "Review the changed input.",
                inputs=(TaskInput("docs/spec.md", digest),),
            )
        )
    )

    assert harness.call_count("send") == 1
    assert adapter.send_requests[0].context.attestation["input_attestations"] == [
        {
            "path": "docs/spec.md",
            "sha256": digest,
            "byte_count": len(b"round two"),
            "source": "subagent-mcp-read-only-sha256",
        }
    ]
    assert status.input_attestations == tuple(
        adapter.send_requests[0].context.attestation["input_attestations"]
    )


def test_agent_send_without_inputs_does_not_reuse_prior_input_attestation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    input_path = workspace / "docs" / "spec.md"
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"round one")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    harness = FakeHarness()
    adapter = _RecordingSendAdapter(harness)
    harness.enqueue("done", result="CAPSULE: round one")
    service, _ = _service(tmp_path, harness, adapter=adapter)
    spawn_request = _spawn_request(workspace)
    spawn_request = replace(
        spawn_request,
        task=replace(
            spawn_request.task,
            inputs=(TaskInput("docs/spec.md", digest),),
        ),
    )
    started = asyncio.run(service.agent_spawn(spawn_request))
    harness.enqueue("done", result="CAPSULE: round two")

    status = asyncio.run(
        service.agent_send(
            SendRequest(
                "send-without-inputs-round-two",
                started.conversation_id,
                "Review a different concern without a hash binding.",
            )
        )
    )

    assert "input_attestations" not in adapter.send_requests[0].context.attestation
    assert status.input_attestations == ()


def _adapter_counts(harness: FakeHarness) -> tuple[int, ...]:
    return tuple(harness.call_count(operation) for operation in _RELAY_OPERATIONS)


def _relay_pair(
    tmp_path: Path,
    harness: FakeHarness,
    *,
    source_outcome: str = "done",
) -> tuple[SubagentMcpService, StateStore, _RecordingSendAdapter, object, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _RecordingSendAdapter(harness)
    service, store = _service(tmp_path, harness, adapter=adapter)
    harness.enqueue(source_outcome, result=_SOURCE_REPORT if source_outcome == "done" else None)
    source = asyncio.run(
        service.agent_spawn(_spawn_request(workspace, request_id="relay-source"))
    )
    harness.enqueue("done", result="target primed")
    target = asyncio.run(
        service.agent_spawn(_spawn_request(workspace, request_id="relay-target"))
    )
    return service, store, adapter, source, target


def test_artifact_relay_expands_full_source_only_in_memory(tmp_path: Path) -> None:
    harness = FakeHarness()
    service, store, adapter, source, target = _relay_pair(tmp_path, harness)
    digest = hashlib.sha256(_SOURCE_REPORT.encode("utf-8")).hexdigest()
    request = SendRequest(
        request_id="relay-send-1",
        conversation_id=target.conversation_id,
        prompt="Summarize the attached prior report.",
        artifact=ArtifactReference(source.conversation_id, source.execution_id, digest),
    )

    status = asyncio.run(service.agent_send(request))

    assert status.execution_state == "succeeded"
    assert len(adapter.sent_prompts) == 1
    expanded = adapter.sent_prompts[0]
    assert _SOURCE_REPORT in expanded
    assert "UNTRUSTED REPORT DATA" in expanded
    assert f"sha256: {digest}" in expanded
    assert f"char_count: {len(_SOURCE_REPORT)}" in expanded
    assert "unique-relay-payload-42" not in json.dumps(status.to_compact_dict())
    with store.transaction() as database:
        copied = {
            "requests": [
                row[0]
                for row in database.execute(
                    "SELECT response_json FROM requests WHERE request_id = ?",
                    (request.request_id,),
                )
            ],
            "events": [
                row[0]
                for row in database.execute(
                    "SELECT payload_json FROM events WHERE execution_id = ?",
                    (status.execution_id,),
                )
            ],
            "observed_and_requested": list(
                database.execute(
                    "SELECT observed_json, requested_json FROM executions"
                    " WHERE execution_id = ?",
                    (status.execution_id,),
                ).fetchone()
            ),
            "descriptor": [
                database.execute(
                    "SELECT descriptor_json FROM conversations"
                    " WHERE conversation_id = ?",
                    (target.conversation_id,),
                ).fetchone()[0]
            ],
        }
        results = [
            row[0]
            for row in database.execute(
                "SELECT result_json FROM executions WHERE result_json IS NOT NULL"
            )
        ]
        stored_input = database.execute(
            "SELECT input_sha256 FROM requests"
            " WHERE tool = 'agent_send' AND request_id = 'relay-send-1'"
        ).fetchone()[0]
    for bucket, blobs in copied.items():
        leaked = [blob for blob in blobs if blob and "unique-relay-payload-42" in blob]
        assert leaked == [], bucket
    assert sum("unique-relay-payload-42" in blob for blob in results) == 1
    durable_payload = {
        "answers": {},
        "artifact": {
            "conversation_id": source.conversation_id,
            "execution_id": source.execution_id,
            "expected_sha256": digest,
        },
        "conversation_id": target.conversation_id,
        "inputs": [],
        "prompt": request.prompt,
        "reply_to": None,
    }
    expected_input = hashlib.sha256(
        (
            json.dumps(
                durable_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    assert stored_input == expected_input

    replay = asyncio.run(service.agent_send(request))

    assert replay.execution_id == status.execution_id
    assert replay.execution_state == "succeeded"
    assert len(adapter.sent_prompts) == 1


def test_artifact_relay_rejects_wrong_conversation_before_native_work(
    tmp_path: Path,
) -> None:
    harness = FakeHarness()
    service, _, _, source, target = _relay_pair(tmp_path, harness)
    digest = hashlib.sha256(_SOURCE_REPORT.encode("utf-8")).hexdigest()
    before = _adapter_counts(harness)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    request_id="relay-bad-conversation",
                    conversation_id=target.conversation_id,
                    prompt="Relay.",
                    artifact=ArtifactReference(
                        "conversation-unknown", source.execution_id, digest
                    ),
                )
            )
        )

    assert captured.value.code == "RESULT_NOT_FOUND"
    assert _adapter_counts(harness) == before


def test_artifact_relay_rejects_changed_hash_before_native_work(tmp_path: Path) -> None:
    harness = FakeHarness()
    service, _, _, source, target = _relay_pair(tmp_path, harness)
    before = _adapter_counts(harness)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    request_id="relay-bad-hash",
                    conversation_id=target.conversation_id,
                    prompt="Relay.",
                    artifact=ArtifactReference(
                        source.conversation_id, source.execution_id, "f" * 64
                    ),
                )
            )
        )

    assert captured.value.code == "RESULT_CHANGED"
    assert _adapter_counts(harness) == before


@pytest.mark.parametrize("source_outcome", ["running", "failure"])
def test_artifact_relay_rejects_non_succeeded_source_before_native_work(
    tmp_path: Path,
    source_outcome: str,
) -> None:
    harness = FakeHarness()
    service, _, _, source, target = _relay_pair(
        tmp_path, harness, source_outcome=source_outcome
    )
    before = _adapter_counts(harness)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    request_id=f"relay-{source_outcome}-source",
                    conversation_id=target.conversation_id,
                    prompt="Relay.",
                    artifact=ArtifactReference(
                        source.conversation_id, source.execution_id, "a" * 64
                    ),
                )
            )
        )

    assert captured.value.code == "RESULT_NOT_AVAILABLE"
    assert captured.value.next_action == "inspect_status"
    assert _adapter_counts(harness) == before


def test_artifact_relay_rejects_same_source_and_target_conversation(
    tmp_path: Path,
) -> None:
    harness = FakeHarness()
    service, _, _, source, _ = _relay_pair(tmp_path, harness)
    before = _adapter_counts(harness)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    request_id="relay-self",
                    conversation_id=source.conversation_id,
                    prompt="Relay to myself.",
                    artifact=ArtifactReference(
                        source.conversation_id, source.execution_id, "a" * 64
                    ),
                )
            )
        )

    assert captured.value.code == "REQUEST_INVALID"
    assert _adapter_counts(harness) == before


def test_artifact_relay_rejects_cross_workspace_identity(tmp_path: Path) -> None:
    harness = FakeHarness()
    service, store, _, source, target = _relay_pair(tmp_path, harness)
    digest = hashlib.sha256(_SOURCE_REPORT.encode("utf-8")).hexdigest()
    with store.transaction(write=True) as database:
        database.execute(
            "UPDATE conversations SET workspace_key = ? WHERE conversation_id = ?",
            (r"Z:\elsewhere", source.conversation_id),
        )
    before = _adapter_counts(harness)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    request_id="relay-cross-workspace",
                    conversation_id=target.conversation_id,
                    prompt="Relay.",
                    artifact=ArtifactReference(
                        source.conversation_id, source.execution_id, digest
                    ),
                )
            )
        )

    assert captured.value.code == "WORKSPACE_MISMATCH"
    assert _adapter_counts(harness) == before


def test_artifact_relay_fails_closed_without_workspace_identity(
    tmp_path: Path,
) -> None:
    harness = FakeHarness()
    service, store, _, source, target = _relay_pair(tmp_path, harness)
    digest = hashlib.sha256(_SOURCE_REPORT.encode("utf-8")).hexdigest()
    with store.transaction(write=True) as database:
        database.execute(
            "UPDATE conversations SET workspace_key = NULL WHERE conversation_id = ?",
            (source.conversation_id,),
        )
    before = _adapter_counts(harness)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    request_id="relay-no-workspace",
                    conversation_id=target.conversation_id,
                    prompt="Relay.",
                    artifact=ArtifactReference(
                        source.conversation_id, source.execution_id, digest
                    ),
                )
            )
        )

    assert captured.value.code == "WORKSPACE_MISMATCH"
    assert _adapter_counts(harness) == before


def test_artifact_relay_rejects_oversized_expanded_prompt_before_native_work(
    tmp_path: Path,
) -> None:
    harness = FakeHarness()
    service, _, _, source, target = _relay_pair(tmp_path, harness)
    digest = hashlib.sha256(_SOURCE_REPORT.encode("utf-8")).hexdigest()
    near_limit_prompt = "x" * (PROMPT_MAX_BYTES - 64)
    before = _adapter_counts(harness)

    with pytest.raises(ServiceError) as captured:
        asyncio.run(
            service.agent_send(
                SendRequest(
                    request_id="relay-overflow",
                    conversation_id=target.conversation_id,
                    prompt=near_limit_prompt,
                    artifact=ArtifactReference(
                        source.conversation_id, source.execution_id, digest
                    ),
                )
            )
        )

    assert captured.value.code == "REQUEST_INVALID"
    assert _adapter_counts(harness) == before


def test_result_read_slice_reports_exact_bytes_and_labelled_rough_tokens(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    full_text = "CAPSULE: metrics\nDETAILS:\nhéllo ✓ provider evidence"
    harness.enqueue("done", result=full_text)
    service, _ = _service(tmp_path, harness)

    status = asyncio.run(service.agent_spawn(_spawn_request(workspace)))
    artifact = status.to_compact_dict()["result"]["artifact"]
    read = asyncio.run(
        service.agent_result_read(
            ResultReadRequest(
                status.conversation_id,
                status.execution_id,
                artifact["sha256"],
                offset=0,
                limit=10,
            )
        )
    )
    slice_text = full_text[:10]

    assert read["text"] == slice_text
    assert read["slice_metrics"] == {
        "basis": ROUGH_TOKEN_ESTIMATE_BASIS,
        "chars": len(slice_text),
        "utf8_bytes": len(slice_text.encode("utf-8")),
        "rough_tokens": (len(slice_text.encode("utf-8")) + 2) // 3,
    }
