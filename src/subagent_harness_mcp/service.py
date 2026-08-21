"""Shared lifecycle service used by every Subagent MCP surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

from .adapters.base import (
    Adapter,
    AdapterContextRequest,
    AdapterSendRequest,
    AdapterSessionRequest,
    AdapterSnapshot,
    AdapterSpawnRequest,
    CanaryAdapter,
    CanaryRequest,
    ProbeResult,
    QuotaProbeAdapter,
    ResolvedContext,
)
from .adapters.registry import AdapterRegistry, RegistryError
from .config import ConfigError, ConfigStore
from .contracts import (
    ActionRequest,
    AgentDescriptor,
    AgentEvent,
    AgentStatus,
    SendRequest,
    ServiceError,
    SpawnRequest,
    StatusRequest,
    TERMINAL_EXECUTION_STATES,
    WaitRequest,
)
from .store import (
    CircuitRecord,
    ExecutionRecord,
    StateError,
    StateStore,
    VerifiedCleanupReceipt,
)


_ACTIVE_STATES = frozenset({"queued", "starting", "running", "needs_input"})
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN = re.compile(r"(?i)\b(?:sk|api|token|secret)[-_][A-Za-z0-9._-]{12,}")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "auth_token",
        "access_token",
        "password",
        "secret",
        "cookie",
        "transcript",
        "system_prompt",
        "thinking_content",
        "hidden_thinking",
        "chain_of_thought",
    }
)


class SubagentMcpService:
    def __init__(
        self,
        *,
        config: ConfigStore,
        store: StateStore,
        registry: AdapterRegistry,
        id_factory: Callable[[str], str] | None = None,
        canary_cleanup_verifier: Callable[
            [Mapping[str, Any], CircuitRecord], VerifiedCleanupReceipt | None
        ]
        | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._registry = registry
        self._id_factory = id_factory or _new_id
        self._canary_cleanup_verifier = (
            canary_cleanup_verifier or _refuse_canary_cleanup
        )

    async def runtime_list(self) -> tuple[dict[str, Any], ...]:
        try:
            policies = self._config.load()["runtimes"]
        except ConfigError as exc:
            raise _public_error(exc) from exc
        result: list[dict[str, Any]] = []
        for record in self._registry.records():
            policy = policies.get(record.runtime_id, {})
            result.append(
                {
                    "runtime_id": record.runtime_id,
                    "state": record.state,
                    "enabled": policy.get("enabled", False),
                    "manifest": None if record.manifest is None else record.manifest.to_dict(),
                    "reason": record.reason,
                    "circuits": [
                        {
                            "variant_id": circuit.variant_id,
                            "state": circuit.state,
                            "revision": circuit.revision,
                            "pair_key": circuit.pair_key,
                        }
                        for circuit in self._store.list_circuits(record.runtime_id)
                    ],
                }
            )
        return tuple(result)

    async def runtime_check(
        self,
        runtime_id: str,
        refresh_quota: bool = False,
    ) -> dict[str, Any]:
        try:
            adapter = self._registry.get(runtime_id)
            probe = await adapter.probe()
            state = probe.state
            details = dict(_redact(probe.details))
            policy = self._config.load()["runtimes"].get(runtime_id, {})
            configured = policy.get("variants", ()) if isinstance(policy, Mapping) else ()
            variants = [item for item in configured if isinstance(item, Mapping)]
            circuits: list[CircuitRecord] = []
            if isinstance(adapter, CanaryAdapter) and probe.state == "needs_canary":
                base_pair_key = _pair_key(probe.details)
                for variant in variants:
                    if not isinstance(variant, Mapping) or not isinstance(variant.get("id"), str):
                        continue
                    if len(adapter.manifest.supported_transports) != 1:
                        raise ServiceError(
                            "CAPABILITY_MISSING",
                            "runtime_check cannot infer an exact canary transport",
                        )
                    transport = adapter.manifest.supported_transports[0]
                    pair_key = _variant_pair_key(
                        base_pair_key,
                        str(variant["model"]),
                        dict(variant["reasoning"]),
                        transport,
                    )
                    circuits.append(
                        self._store.ensure_circuit_pair(
                            runtime_id=runtime_id,
                            variant_id=str(variant["id"]),
                            pair_key=pair_key,
                            details={**details, "base_pair_key": base_pair_key},
                        )
                    )
                if circuits:
                    state = circuits[0].state
                    details["circuits"] = [
                        {
                            "variant_id": item.variant_id,
                            "state": item.state,
                            "revision": item.revision,
                        }
                        for item in circuits
                    ]
            quota: dict[str, Any] = {
                "state": "check_required" if variants else "configure_first"
            }
            if refresh_quota:
                quota = await self._refresh_runtime_quota(
                    adapter=adapter,
                    runtime_id=runtime_id,
                    probe=probe,
                    variants=variants,
                    circuits=circuits,
                )
                circuits = list(self._store.list_circuits(runtime_id))
                if circuits:
                    state = circuits[0].state
                    details["circuits"] = [
                        {
                            "variant_id": item.variant_id,
                            "state": item.state,
                            "revision": item.revision,
                        }
                        for item in circuits
                    ]
            return {
                "runtime_id": runtime_id,
                "state": state,
                "details": details,
                "manifest": adapter.manifest.to_dict(),
                "quota": quota,
            }
        except ServiceError:
            raise
        except (RegistryError, StateError, ConfigError) as exc:
            raise _public_error(exc) from exc

    async def _refresh_runtime_quota(
        self,
        *,
        adapter: Adapter,
        runtime_id: str,
        probe: ProbeResult,
        variants: list[Mapping[str, Any]],
        circuits: list[CircuitRecord],
    ) -> dict[str, Any]:
        if not variants:
            return {"state": "configure_first"}
        if not isinstance(adapter, CanaryAdapter):
            return {"state": "unknown"}
        base_pair_key = _pair_key(probe.details)
        by_variant = {item.variant_id: item for item in circuits}
        results: list[dict[str, Any]] = []
        for variant in variants:
            variant_id = variant.get("id")
            model = variant.get("model")
            reasoning = variant.get("reasoning")
            if (
                not isinstance(variant_id, str)
                or not isinstance(model, str)
                or not isinstance(reasoning, Mapping)
            ):
                continue
            circuit = by_variant.get(variant_id)
            if circuit is None:
                results.append({"variant_id": variant_id, "state": "unknown"})
                continue
            transport = adapter.manifest.supported_transports[0]
            evidence: Mapping[str, Any] | None = None
            if circuit.state in {"needs_canary", "auto_paused"}:
                try:
                    response = await self.runtime_canary(
                        {
                            "request_id": self._id_factory("quota-refresh"),
                            "runtime_id": runtime_id,
                            "variant_id": variant_id,
                            "transport": transport,
                        }
                    )
                    candidate = response.get("attestation")
                    if isinstance(candidate, Mapping):
                        evidence = candidate
                except ServiceError as exc:
                    results.append(
                        {
                            "variant_id": variant_id,
                            "state": (
                                "quota_paused"
                                if exc.code in {"QUOTA_PAUSED", "USAGE_CREDITS_FORBIDDEN"}
                                else "unknown"
                            ),
                            "error_code": exc.code,
                        }
                    )
                    continue
            elif circuit.state == "ready" and isinstance(adapter, QuotaProbeAdapter):
                result = await adapter.quota_probe(
                    CanaryRequest(
                        runtime_id=runtime_id,
                        variant_id=variant_id,
                        model=model,
                        reasoning=dict(reasoning),
                        transport=transport,
                        base_pair_key=base_pair_key,
                        pair_key=circuit.pair_key,
                    )
                )
                if result.pair_key != circuit.pair_key:
                    raise ServiceError("CONTEXT_DRIFT", "runtime quota pair changed")
                if result.passed:
                    evidence = result.details
                else:
                    code = result.error.code if result.error is not None else "QUOTA_PAUSED"
                    if code == "RECOVERY_REQUIRED":
                        self._store.require_ready_circuit_recovery(
                            runtime_id=runtime_id,
                            variant_id=variant_id,
                            pair_key=circuit.pair_key,
                            expected_revision=circuit.revision,
                            error_code=code,
                        )
                        results.append({"variant_id": variant_id, "state": "unknown"})
                        continue
                    self._store.pause_ready_circuit(
                        runtime_id=runtime_id,
                        variant_id=variant_id,
                        pair_key=circuit.pair_key,
                        expected_revision=circuit.revision,
                        error_code=(
                            code
                            if code in {"QUOTA_PAUSED", "USAGE_CREDITS_FORBIDDEN"}
                            else "QUOTA_PAUSED"
                        ),
                    )
                    results.append({"variant_id": variant_id, "state": "quota_paused"})
                    continue
            else:
                results.append({"variant_id": variant_id, "state": "unknown"})
                continue
            if _safe_quota_evidence(evidence):
                results.append(
                    {
                        "variant_id": variant_id,
                        "state": "available",
                        "overage_blocked": True,
                    }
                )
            else:
                current = self._store.load_circuit(runtime_id, variant_id)
                if current.state == "ready":
                    self._store.pause_ready_circuit(
                        runtime_id=runtime_id,
                        variant_id=variant_id,
                        pair_key=current.pair_key,
                        expected_revision=current.revision,
                        error_code="QUOTA_PAUSED",
                    )
                results.append({"variant_id": variant_id, "state": "quota_paused"})
        states = {item["state"] for item in results}
        if results and states == {"available"}:
            return {
                "state": "available",
                "overage_blocked": True,
                "variants": results,
            }
        if "quota_paused" in states:
            return {
                "state": "quota_paused",
                "overage_blocked": True,
                "variants": results,
            }
        return {"state": "unknown", "variants": results}

    async def agent_spawn(self, request: SpawnRequest) -> AgentStatus:
        conversation_id = self._id_factory("conversation")
        execution_id = self._id_factory("execution")
        ready_circuit: CircuitRecord | None = None
        try:
            adapter, variant, transport = self._selection(
                request.runtime_id,
                request.variant_id,
                request.transport,
                request.permissions,
            )
            ready_circuit = await self._require_runtime_ready(
                adapter,
                request.variant_id,
                variant,
                transport,
            )
            workspace_path, workspace_key = _workspace(request.cwd)
            requested = _requested_metadata(
                request,
                variant=variant,
                transport=transport,
                workspace_path=workspace_path,
                workspace_key=workspace_key,
            )
            claim = self._store.claim_execution_request(
                tool="agent_spawn",
                request_id=request.request_id,
                request_payload=_spawn_digest_payload(request),
                conversation_id=conversation_id,
                execution_id=execution_id,
                runtime_id=request.runtime_id,
                requested=requested,
            )
            conversation_id = claim.conversation_id
            execution_id = claim.execution_id
            if "workspace_write" in request.permissions:
                self._store.acquire_writer_lease(
                    lease_id=f"writer-{execution_id}",
                    resource_key=f"workspace:{workspace_key}",
                    execution_id=execution_id,
                )
            launch = self._store.claim_execution_start(execution_id)
            if not launch.should_launch:
                return self._status(self._store.load_execution(execution_id), after_cursor=0)
            context = await adapter.resolve_context(
                AdapterContextRequest(
                    runtime_id=request.runtime_id,
                    variant_id=request.variant_id,
                    model=str(variant["model"]),
                    reasoning=dict(variant["reasoning"]),
                    workspace_path=workspace_path,
                    workspace_key=workspace_key,
                    transport=transport,
                    permissions=request.permissions,
                    context_policy_id=request.context_policy_id,
                    permission_policy_id=request.permission_policy_id,
                )
            )
            _require_context(context, requested)
            snapshot = await adapter.spawn(
                AdapterSpawnRequest(conversation_id, execution_id, request.task, context)
            )
            record = self._apply_snapshot(
                adapter,
                execution_id=execution_id,
                context=context,
                snapshot=snapshot,
            )
            status = self._status(record, after_cursor=0)
            self._store.save_request_response(
                tool="agent_spawn",
                request_id=request.request_id,
                response=status.to_dict(),
            )
            return status
        except ServiceError as exc:
            public = self._pause_after_quota_error(ready_circuit, exc)
            self._record_failure(execution_id, public)
            raise public
        except (StateError, RegistryError, ConfigError) as exc:
            public = _public_error(exc)
            self._record_failure(execution_id, public)
            raise public from exc
        except BaseException as exc:
            public = ServiceError(
                "RECOVERY_REQUIRED",
                f"adapter launch outcome is ambiguous ({type(exc).__name__})",
                category="adapter",
                next_action="inspect_status",
            )
            self._record_failure(execution_id, public, release_leases=False)
            raise public from exc

    async def agent_status(self, request: StatusRequest) -> AgentStatus:
        try:
            record = self._store.load_latest_execution(request.conversation_id)
            if (
                request.refresh
                and record.execution_state in {"running", "needs_input"}
                and record.external_session_id is not None
            ):
                adapter = self._registry.get(record.runtime_id)
                session = _session_request(record)
                await adapter.open_session(session)
                snapshot = await adapter.snapshot(session)
                context = _context_from_record(record)
                record = self._apply_snapshot(
                    adapter,
                    execution_id=record.execution_id,
                    context=context,
                    snapshot=snapshot,
                )
            return self._status(record, after_cursor=request.after_cursor)
        except ServiceError:
            raise
        except (StateError, RegistryError, ConfigError) as exc:
            raise _public_error(exc) from exc
        except BaseException as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                f"native session could not be reconciled ({type(exc).__name__})",
                category="adapter",
                next_action="inspect_status",
            ) from exc

    async def agent_send(self, request: SendRequest) -> AgentStatus:
        execution_id = self._id_factory("execution")
        ready_circuit: CircuitRecord | None = None
        try:
            previous = self._store.load_latest_execution(request.conversation_id)
            if (
                isinstance(previous.result, Mapping)
                and isinstance(previous.result.get("error"), Mapping)
                and previous.result["error"].get("code") == "RECOVERY_REQUIRED"
            ):
                raise ServiceError("RECOVERY_REQUIRED", "native session cleanup is unverified")
            if previous.conversation_state == "closed":
                raise ServiceError("SESSION_CLOSED", "conversation is closed")
            if previous.execution_state in {"queued", "starting", "running"}:
                raise ServiceError("SESSION_BUSY", "conversation has an active execution")
            if previous.execution_state == "needs_input":
                previous = self._store.transition_execution(
                    execution_id=previous.execution_id,
                    execution_state="succeeded",
                    conversation_state="idle",
                    observed=previous.observed or {},
                    result={"continued": True},
                    event_kind="completed",
                    event_payload={"continued": True},
                )
            adapter, variant, transport = self._selection(
                previous.runtime_id,
                str(previous.requested["variant_id"]),
                str(previous.requested["transport"]),
                tuple(previous.requested.get("permissions", ())),
            )
            ready_circuit = await self._require_runtime_ready(
                adapter,
                str(previous.requested["variant_id"]),
                variant,
                transport,
            )
            requested = dict(previous.requested)
            claim = self._store.claim_execution_request(
                tool="agent_send",
                request_id=request.request_id,
                request_payload={
                    "conversation_id": request.conversation_id,
                    "prompt": request.prompt,
                    "reply_to": request.reply_to,
                    "answers": dict(request.answers),
                },
                conversation_id=request.conversation_id,
                execution_id=execution_id,
                runtime_id=None,
                requested=requested,
            )
            execution_id = claim.execution_id
            if "workspace_write" in requested.get("permissions", ()):
                self._store.acquire_writer_lease(
                    lease_id=f"writer-{execution_id}",
                    resource_key=f"workspace:{requested['workspace_key']}",
                    execution_id=execution_id,
                )
            launch = self._store.claim_execution_start(execution_id)
            if not launch.should_launch:
                return self._status(self._store.load_execution(execution_id), after_cursor=0)
            context = _context_from_record(previous)
            session = _session_request(previous)
            await adapter.open_session(session)
            snapshot = await adapter.send(
                AdapterSendRequest(
                    request.conversation_id,
                    execution_id,
                    str(previous.external_session_id),
                    request.prompt,
                    request.reply_to,
                    request.answers,
                    context,
                )
            )
            record = self._apply_snapshot(
                adapter,
                execution_id=execution_id,
                context=context,
                snapshot=snapshot,
            )
            status = self._status(record, after_cursor=0)
            self._store.save_request_response(
                tool="agent_send",
                request_id=request.request_id,
                response=status.to_dict(),
            )
            return status
        except ServiceError as exc:
            public = self._pause_after_quota_error(ready_circuit, exc)
            self._record_failure(execution_id, public)
            raise public
        except (StateError, RegistryError, ConfigError) as exc:
            public = _public_error(exc)
            self._record_failure(execution_id, public)
            raise public from exc
        except BaseException as exc:
            public = ServiceError(
                "RECOVERY_REQUIRED",
                f"adapter send outcome is ambiguous ({type(exc).__name__})",
                category="adapter",
                next_action="inspect_status",
            )
            self._record_failure(execution_id, public, release_leases=False)
            raise public from exc

    async def agent_wait(self, request: WaitRequest) -> tuple[AgentStatus, ...]:
        deadline = time.monotonic() + float(request.timeout_seconds)
        while True:
            collected: list[AgentStatus] = []
            for target in request.targets:
                collected.append(
                    await self.agent_status(
                    StatusRequest(
                        target.conversation_id,
                        after_cursor=target.after_cursor,
                        refresh=True,
                    )
                )
                )
            statuses = tuple(collected)
            if any(
                status.execution_state in TERMINAL_EXECUTION_STATES
                or status.execution_state == "needs_input"
                for status in statuses
            ):
                return statuses
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return statuses
            await asyncio.sleep(min(0.05, remaining))

    async def agent_interrupt(self, request: ActionRequest) -> AgentStatus:
        return await self._action(request, operation="interrupt")

    async def agent_close(self, request: ActionRequest) -> AgentStatus:
        return await self._action(request, operation="close")

    async def runtime_configure(self, payload: Mapping[str, Any]) -> NoReturn:
        _capability_gap("runtime_configure is added in the settings task")

    async def runtime_canary(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = payload.get("request_id")
        runtime_id = payload.get("runtime_id")
        variant_id = payload.get("variant_id")
        transport = payload.get("transport", "managed-sdk")
        if isinstance(runtime_id, str) and runtime_id:
            try:
                if not isinstance(self._registry.get(runtime_id), CanaryAdapter):
                    _capability_gap("runtime does not implement a live canary")
            except RegistryError as exc:
                raise _public_error(exc) from exc
        if not all(isinstance(value, str) and value for value in (
            request_id,
            runtime_id,
            variant_id,
            transport,
        )):
            raise ServiceError("REQUEST_INVALID", "runtime_canary identity is invalid")
        try:
            adapter, variant, selected_transport = self._selection(
                runtime_id,
                variant_id,
                transport,
                (),
            )
            if not isinstance(adapter, CanaryAdapter):
                _capability_gap("runtime does not implement a live canary")
            probe = await adapter.probe()
            if probe.state != "needs_canary":
                raise _runtime_state_error(runtime_id, probe.state)
            base_pair_key = _pair_key(probe.details)
            model = str(variant["model"])
            reasoning = dict(variant["reasoning"])
            pair_key = _variant_pair_key(
                base_pair_key,
                model,
                reasoning,
                selected_transport,
            )
            circuit = self._store.ensure_circuit_pair(
                runtime_id=runtime_id,
                variant_id=variant_id,
                pair_key=pair_key,
                details={
                    **dict(_redact(probe.details)),
                    "base_pair_key": base_pair_key,
                },
            )
            cleanup_receipt = payload.get("cleanup_receipt")
            if circuit.state in {"probing", "recovery_required"}:
                if not isinstance(cleanup_receipt, Mapping):
                    raise ServiceError(
                        "RECOVERY_REQUIRED",
                        "prior canary cleanup is unverified; external work was not repeated",
                        category="adapter",
                        next_action="verify_cleanup",
                    )
                verified = self._canary_cleanup_verifier(cleanup_receipt, circuit)
                if not isinstance(verified, VerifiedCleanupReceipt):
                    raise ServiceError(
                        "RECOVERY_REQUIRED",
                        "cleanup receipt could not be verified",
                        category="adapter",
                        next_action="verify_cleanup",
                    )
                recovered = self._store.recover_canary_after_cleanup(
                    runtime_id=runtime_id,
                    variant_id=variant_id,
                    pair_key=circuit.pair_key,
                    expected_revision=circuit.revision,
                    receipt=verified,
                )
                return {
                    "runtime_id": runtime_id,
                    "variant_id": variant_id,
                    "state": recovered.state,
                    "pair_key": recovered.pair_key,
                    "revision": recovered.revision,
                    "recovered": True,
                }
            if cleanup_receipt is not None:
                raise ServiceError(
                    "REQUEST_INVALID",
                    "cleanup receipt is accepted only for orphan canary recovery",
                )
            claim = self._store.claim_canary_request(
                request_id=request_id,
                request_payload={
                    "runtime_id": runtime_id,
                    "variant_id": variant_id,
                    "base_pair_key": base_pair_key,
                    "pair_key": pair_key,
                    "model": model,
                    "reasoning": reasoning,
                    "transport": selected_transport,
                },
                runtime_id=runtime_id,
                variant_id=variant_id,
                pair_key=pair_key,
            )
            if claim.response is not None:
                error = claim.response.get("error")
                if isinstance(error, Mapping):
                    raise ServiceError(
                        str(error.get("code", "CAPABILITY_MISSING")),
                        str(error.get("message", "runtime canary failed")),
                        category=str(error.get("category", "adapter")),
                        retryable=bool(error.get("retryable", False)),
                    )
                return claim.response
            if not claim.created:
                if claim.state == "ready":
                    return {
                        "runtime_id": runtime_id,
                        "variant_id": variant_id,
                        "state": "ready",
                        "pair_key": pair_key,
                    }
                if claim.state in {"probing", "recovery_required"}:
                    raise ServiceError(
                        "RECOVERY_REQUIRED",
                        "prior canary cleanup is unverified; external work was not repeated",
                        category="adapter",
                        next_action="verify_cleanup",
                    )
                raise _runtime_state_error(runtime_id, claim.state)
            canary = await adapter.runtime_canary(
                CanaryRequest(
                    runtime_id=runtime_id,
                    variant_id=variant_id,
                    model=model,
                    reasoning=reasoning,
                    transport=selected_transport,
                    base_pair_key=base_pair_key,
                    pair_key=pair_key,
                )
            )
            if canary.pair_key != pair_key:
                raise ServiceError("CONTEXT_DRIFT", "runtime canary pair changed")
            if canary.passed:
                circuit = self._store.complete_canary(
                    runtime_id=runtime_id,
                    variant_id=variant_id,
                    pair_key=pair_key,
                    expected_revision=claim.revision,
                    state="ready",
                    details=dict(_redact(canary.details)),
                )
                response = {
                    "runtime_id": runtime_id,
                    "variant_id": variant_id,
                    "state": circuit.state,
                    "pair_key": pair_key,
                    "revision": circuit.revision,
                    "attestation": dict(_redact(canary.details)),
                }
                self._store.save_request_response(
                    tool="runtime_canary",
                    request_id=request_id,
                    response=response,
                )
                return response
            error = canary.error or ServiceError(
                "CAPABILITY_MISSING", "runtime canary did not pass"
            )
            public = (
                error
                if isinstance(error, ServiceError)
                else ServiceError(
                    error.code,
                    error.message,
                    category=error.category,
                    retryable=error.retryable,
                )
            )
            failure_state = {
                "AUTH_REQUIRED": "auth_required",
                "QUOTA_PAUSED": "auto_paused",
                "USAGE_CREDITS_FORBIDDEN": "auto_paused",
            }.get(public.code, "needs_canary")
            self._store.complete_canary(
                runtime_id=runtime_id,
                variant_id=variant_id,
                pair_key=pair_key,
                expected_revision=claim.revision,
                state=failure_state,
                details={"error_code": public.code},
            )
            self._store.save_request_response(
                tool="runtime_canary",
                request_id=request_id,
                response={"error": public.to_dict()},
            )
            raise public
        except ServiceError:
            raise
        except (StateError, RegistryError, ConfigError) as exc:
            raise _public_error(exc) from exc

    async def project_scan(self, payload: Mapping[str, Any]) -> NoReturn:
        _capability_gap("project_scan is not available in this preview slice")

    async def project_trust(self, payload: Mapping[str, Any]) -> NoReturn:
        _capability_gap("project_trust is not available in this preview slice")

    async def workspace_release(self, payload: Mapping[str, Any]) -> NoReturn:
        _capability_gap("workspace_release is added in the installer task")

    async def _action(self, request: ActionRequest, *, operation: str) -> AgentStatus:
        try:
            record = self._store.load_latest_execution(request.conversation_id)
            claim = self._store.claim_action_request(
                tool=f"agent_{operation}",
                request_id=request.request_id,
                request_payload={"conversation_id": request.conversation_id},
                conversation_id=request.conversation_id,
                execution_id=record.execution_id,
            )
            if not claim.created:
                return self._status(
                    self._store.load_latest_execution(request.conversation_id),
                    after_cursor=0,
                )
            adapter = self._registry.get(record.runtime_id)
            if operation == "interrupt":
                if record.execution_state not in {"running", "needs_input"}:
                    raise ServiceError("SESSION_BUSY", "execution is not interruptible")
                await adapter.open_session(_session_request(record))
                snapshot = await adapter.interrupt(_session_request(record))
                record = self._apply_snapshot(
                    adapter,
                    execution_id=record.execution_id,
                    context=_context_from_record(record),
                    snapshot=snapshot,
                )
            else:
                if record.execution_state not in TERMINAL_EXECUTION_STATES:
                    raise ServiceError("SESSION_BUSY", "active execution cannot be closed")
                if record.external_session_id is not None:
                    await adapter.open_session(_session_request(record))
                    snapshot = await adapter.close(_session_request(record))
                    if snapshot.conversation_state != "closed":
                        raise ServiceError("RECOVERY_REQUIRED", "native session did not close")
                record = self._store.close_conversation(request.conversation_id)
            status = self._status(record, after_cursor=0)
            self._store.save_request_response(
                tool=f"agent_{operation}",
                request_id=request.request_id,
                response=status.to_dict(),
            )
            return status
        except ServiceError:
            raise
        except (StateError, RegistryError, ConfigError) as exc:
            raise _public_error(exc) from exc
        except BaseException as exc:
            raise ServiceError(
                "RECOVERY_REQUIRED",
                f"native {operation} outcome is ambiguous ({type(exc).__name__})",
                category="adapter",
                next_action="inspect_status",
            ) from exc

    def _selection(
        self,
        runtime_id: str,
        variant_id: str,
        requested_transport: str,
        permissions: tuple[str, ...],
    ) -> tuple[Adapter, Mapping[str, Any], str]:
        document = self._config.load()
        policy = document["runtimes"].get(runtime_id)
        if not isinstance(policy, dict) or not policy.get("enabled"):
            raise ServiceError("RUNTIME_DISABLED", f"runtime {runtime_id!r} is disabled")
        variant = next(
            (item for item in policy["variants"] if item.get("id") == variant_id),
            None,
        )
        if variant is None:
            raise ServiceError("POLICY_REJECTED", "variant is outside runtime policy")
        adapter = self._registry.get(runtime_id)
        unsupported = set(permissions) - set(adapter.manifest.semantic_permissions)
        if unsupported:
            raise ServiceError(
                "CAPABILITY_MISSING",
                f"adapter lacks semantic permissions: {', '.join(sorted(unsupported))}",
            )
        supported = adapter.manifest.supported_transports
        if requested_transport == "auto":
            if len(supported) != 1:
                raise ServiceError(
                    "CAPABILITY_MISSING",
                    "transport auto-selection is ambiguous",
                )
            transport = supported[0]
        else:
            transport = requested_transport
        if transport not in supported:
            raise ServiceError("CAPABILITY_MISSING", "requested transport is unsupported")
        return adapter, variant, transport

    async def _require_runtime_ready(
        self,
        adapter: Adapter,
        variant_id: str,
        variant: Mapping[str, Any],
        transport: str,
    ) -> CircuitRecord | None:
        probe = await adapter.probe()
        if isinstance(adapter, CanaryAdapter) and probe.state == "needs_canary":
            base_pair_key = _pair_key(probe.details)
            pair_key = _variant_pair_key(
                base_pair_key,
                str(variant["model"]),
                dict(variant["reasoning"]),
                transport,
            )
            circuit = self._store.ensure_circuit_pair(
                runtime_id=adapter.manifest.runtime_id,
                variant_id=variant_id,
                pair_key=pair_key,
                details={
                    **dict(_redact(probe.details)),
                    "base_pair_key": base_pair_key,
                },
            )
            if circuit.state == "ready":
                details = circuit.details
                effort = variant.get("reasoning", {}).get("effort")
                if not (
                    details.get("cleanup_confirmed") is True
                    and details.get("is_using_overage") is False
                    and details.get("overage_blocked") is True
                    and details.get("model") == variant.get("model")
                    and details.get("effort") == effort
                ):
                    self._store.pause_ready_circuit(
                        runtime_id=circuit.runtime_id,
                        variant_id=circuit.variant_id,
                        pair_key=circuit.pair_key,
                        expected_revision=circuit.revision,
                        error_code="USAGE_CREDITS_FORBIDDEN",
                    )
                    raise ServiceError(
                        "USAGE_CREDITS_FORBIDDEN",
                        "runtime no-overage attestation is incomplete",
                        category="quota",
                    )
                return circuit
            raise _runtime_state_error(adapter.manifest.runtime_id, circuit.state)
        if probe.state != "ready":
            raise _runtime_state_error(adapter.manifest.runtime_id, probe.state)
        return None

    def _apply_snapshot(
        self,
        adapter: Adapter,
        *,
        execution_id: str,
        context: ResolvedContext,
        snapshot: AdapterSnapshot,
    ) -> ExecutionRecord:
        _require_snapshot(snapshot, context)
        descriptor = AgentDescriptor.from_manifest(
            adapter.manifest,
            model=context.effective_model,
            transport=context.transport,
            capability_gaps=context.capability_gaps,
        )
        observed = _snapshot_observation(snapshot, context)
        current = self._store.load_execution(execution_id)
        if current.external_execution_id is None:
            current = self._store.bind_execution(
                execution_id=execution_id,
                external_session_id=snapshot.external_session_id,
                external_execution_id=snapshot.external_execution_id,
                workspace_key=context.workspace_key,
                descriptor=descriptor.to_dict(),
                observed=observed,
            )
        if snapshot.execution_state == "running":
            return current
        event_kind = {
            "needs_input": "needs_input",
            "succeeded": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
        }.get(snapshot.execution_state)
        if event_kind is None:
            raise ServiceError("ADAPTER_INVALID", "adapter returned an unknown state")
        result = _snapshot_result(snapshot)
        return self._store.transition_execution(
            execution_id=execution_id,
            execution_state=snapshot.execution_state,
            conversation_state=snapshot.conversation_state,
            observed=observed,
            result=result,
            event_kind=event_kind,
            event_payload={} if result is None else {"result": result},
        )

    def _status(self, record: ExecutionRecord, *, after_cursor: int) -> AgentStatus:
        descriptor = _descriptor_from_record(record, self._registry)
        observed = record.observed or {}
        workspace_path = str(
            observed.get("workspace_path", record.requested.get("workspace_path", ""))
        )
        needs = observed.get("needs_input", ())
        if not isinstance(needs, list):
            needs = []
        events = tuple(
            AgentEvent(event.cursor, event.kind, event.payload)
            for event in self._store.load_events(
                record.execution_id,
                after_cursor=after_cursor,
            )
        )
        recovery = bool(
            isinstance(record.result, Mapping)
            and isinstance(record.result.get("error"), Mapping)
            and record.result["error"].get("code") == "RECOVERY_REQUIRED"
        )
        return AgentStatus(
            conversation_id=record.conversation_id,
            execution_id=record.execution_id,
            external_session_id=record.external_session_id,
            workspace_path=workspace_path,
            conversation_state=record.conversation_state,
            execution_state=record.execution_state,
            state_revision=record.conversation_revision,
            descriptor=descriptor,
            result=record.result,
            needs_input=tuple(dict(item) for item in needs if isinstance(item, Mapping)),
            events=events,
            next_event_cursor=record.next_event_cursor,
            recovery_required=recovery,
        )

    def _record_failure(
        self,
        execution_id: str,
        error: ServiceError,
        *,
        release_leases: bool = True,
    ) -> None:
        try:
            record = self._store.load_execution(execution_id)
            if record.execution_state in TERMINAL_EXECUTION_STATES:
                return
            self._store.transition_execution(
                execution_id=execution_id,
                execution_state="failed",
                conversation_state="idle",
                observed=record.observed or {},
                result={"error": _redact(error.to_dict())},
                event_kind="failed",
                event_payload={"error": {"code": error.code}},
                release_leases=release_leases,
            )
        except (StateError, ValueError):
            return

    def _pause_after_quota_error(
        self,
        circuit: CircuitRecord | None,
        error: ServiceError,
    ) -> ServiceError:
        if error.code not in {"QUOTA_PAUSED", "USAGE_CREDITS_FORBIDDEN"}:
            return error
        if circuit is None:
            return error
        try:
            paused = self._store.pause_ready_circuit(
                runtime_id=circuit.runtime_id,
                variant_id=circuit.variant_id,
                pair_key=circuit.pair_key,
                expected_revision=circuit.revision,
                error_code=error.code,
            )
        except BaseException:
            return ServiceError(
                "RECOVERY_REQUIRED",
                "runtime quota pause could not be persisted",
                category="state",
                next_action="inspect_status",
            )
        if paused.state != "auto_paused" or paused.pair_key != circuit.pair_key:
            return ServiceError(
                "RECOVERY_REQUIRED",
                "runtime quota pause could not be verified",
                category="state",
                next_action="inspect_status",
            )
        return error


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _workspace(raw: str) -> tuple[str, str]:
    try:
        path = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise ServiceError("WORKSPACE_NOT_FOUND", "workspace does not exist") from exc
    if not path.is_dir():
        raise ServiceError("WORKSPACE_NOT_FOUND", "workspace is not a directory")
    display = str(path)
    key = os.path.normcase(display)
    if os.name == "nt":
        key = key.casefold()
    return display, key


def _requested_metadata(
    request: SpawnRequest,
    *,
    variant: Mapping[str, Any],
    transport: str,
    workspace_path: str,
    workspace_key: str,
) -> dict[str, Any]:
    return {
        "runtime_id": request.runtime_id,
        "variant_id": request.variant_id,
        "model": variant["model"],
        "reasoning": _redact(variant["reasoning"]),
        "transport": transport,
        "workspace_path": workspace_path,
        "workspace_key": workspace_key,
        "permissions": list(request.permissions),
        "context_policy_id": request.context_policy_id,
        "permission_policy_id": request.permission_policy_id,
        "mode": request.mode,
    }


def _spawn_digest_payload(request: SpawnRequest) -> dict[str, Any]:
    return {
        "runtime_id": request.runtime_id,
        "variant_id": request.variant_id,
        "task": {
            "title": request.task.title,
            "prompt": request.task.prompt,
            "acceptance_criteria": list(request.task.acceptance_criteria),
            "role": request.task.role,
            "authority": list(request.task.authority),
            "repository_base": request.task.repository_base,
            "repository_head": request.task.repository_head,
        },
        "cwd": request.cwd,
        "mode": request.mode,
        "transport": request.transport,
        "permissions": list(request.permissions),
        "context_policy_id": request.context_policy_id,
        "permission_policy_id": request.permission_policy_id,
    }


def _require_context(context: ResolvedContext, requested: Mapping[str, Any]) -> None:
    if (
        context.runtime_id != requested["runtime_id"]
        or context.requested_model != requested["model"]
        or context.effective_model != requested["model"]
        or dict(context.requested_reasoning) != requested["reasoning"]
        or dict(context.effective_reasoning) != requested["reasoning"]
        or context.workspace_path != requested["workspace_path"]
        or context.workspace_key != requested["workspace_key"]
        or context.transport != requested["transport"]
    ):
        raise ServiceError("CONTEXT_DRIFT", "adapter context attestation does not match request")


def _require_snapshot(snapshot: AdapterSnapshot, context: ResolvedContext) -> None:
    if (
        not snapshot.external_session_id
        or not snapshot.external_execution_id
        or snapshot.effective_model != context.effective_model
        or dict(snapshot.effective_reasoning) != dict(context.effective_reasoning)
        or snapshot.workspace_path != context.workspace_path
        or snapshot.workspace_key != context.workspace_key
        or snapshot.context_hash != context.context_hash
    ):
        raise ServiceError("CONTEXT_DRIFT", "adapter result attestation does not match request")
    expected_conversation = {
        "running": "active",
        "needs_input": "needs_input",
        "succeeded": "idle",
        "failed": "idle",
        "cancelled": "idle",
        "interrupted": "idle",
    }
    if expected_conversation.get(snapshot.execution_state) != snapshot.conversation_state:
        raise ServiceError("ADAPTER_INVALID", "adapter returned inconsistent lifecycle state")


def _snapshot_observation(
    snapshot: AdapterSnapshot,
    context: ResolvedContext,
) -> dict[str, Any]:
    resume_attestation = {
        key: _redact(context.attestation[key])
        for key in (
            "source",
            "variant_id",
            "permissions",
            "context_policy_id",
            "permission_policy_id",
        )
        if key in context.attestation
    }
    return {
        "model": context.effective_model,
        "reasoning": _redact(context.effective_reasoning),
        "workspace_path": context.workspace_path,
        "workspace_key": context.workspace_key,
        "transport": context.transport,
        "context_hash": context.context_hash,
        "capability_gaps": list(context.capability_gaps),
        "external_session_id": snapshot.external_session_id,
        "external_execution_id": snapshot.external_execution_id,
        "needs_input": _redact(list(snapshot.needs_input)),
        "evidence": _redact(snapshot.evidence),
        "attestation": resume_attestation,
    }


def _snapshot_result(snapshot: AdapterSnapshot) -> Mapping[str, Any] | None:
    if snapshot.result_text is not None:
        return {"text": _redact_text(snapshot.result_text)}
    if snapshot.error is not None:
        return {
            "error": {
                "code": snapshot.error.code,
                "category": snapshot.error.category,
                "retryable": snapshot.error.retryable,
                "message": _redact_text(snapshot.error.message),
            }
        }
    return None


def _safe_quota_evidence(details: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(details, Mapping)
        and details.get("is_using_overage") is False
        and details.get("overage_blocked") is True
        and details.get("cleanup_confirmed") is True
    )


def _context_from_record(record: ExecutionRecord) -> ResolvedContext:
    observed = record.observed or {}
    attestation = observed.get("attestation", {})
    if not isinstance(attestation, Mapping):
        attestation = {}
    return ResolvedContext(
        runtime_id=record.runtime_id,
        requested_model=str(record.requested["model"]),
        effective_model=str(observed.get("model", record.requested["model"])),
        requested_reasoning=dict(record.requested["reasoning"]),
        effective_reasoning=dict(observed.get("reasoning", record.requested["reasoning"])),
        workspace_path=str(observed.get("workspace_path", record.requested["workspace_path"])),
        workspace_key=str(observed.get("workspace_key", record.requested["workspace_key"])),
        transport=str(observed.get("transport", record.requested["transport"])),
        context_hash=str(observed.get("context_hash", "")),
        capability_gaps=tuple(observed.get("capability_gaps", ())),
        attestation=dict(attestation),
    )


def _session_request(record: ExecutionRecord) -> AdapterSessionRequest:
    if record.external_session_id is None:
        raise ServiceError("RECOVERY_REQUIRED", "native session identity is missing")
    return AdapterSessionRequest(
        record.conversation_id,
        record.execution_id,
        record.external_session_id,
        record.external_execution_id,
    )


def _descriptor_from_record(
    record: ExecutionRecord,
    registry: AdapterRegistry,
) -> AgentDescriptor:
    if record.descriptor:
        return AgentDescriptor.from_dict(record.descriptor)
    adapter = registry.get(record.runtime_id)
    return AgentDescriptor.from_manifest(
        adapter.manifest,
        model=str(record.requested["model"]),
        transport=str(record.requested["transport"]),
        capability_gaps=(),
    )


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value[:128]]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _redact_text(value: str) -> str:
    bounded = value[:16_384]
    bounded = _BEARER.sub("Bearer [REDACTED]", bounded)
    bounded = _TOKEN.sub("[REDACTED]", bounded)
    return _EMAIL.sub("[REDACTED_EMAIL]", bounded)


def _public_error(error: BaseException) -> ServiceError:
    code = getattr(error, "code", "INTERNAL_ERROR")
    category = "state" if isinstance(error, StateError) else "configuration"
    if isinstance(error, RegistryError):
        category = "adapter"
    retryable = code in {"WORKSPACE_BUSY", "SESSION_BUSY", "CONFIG_LOCK_TIMEOUT"}
    return ServiceError(code, str(error), category=category, retryable=retryable)


def _pair_key(details: Mapping[str, Any]) -> str:
    pair_key = details.get("pair_key")
    if not isinstance(pair_key, str) or len(pair_key) != 64:
        raise ServiceError(
            "TRANSPORT_INCOMPATIBLE",
            "runtime probe did not bind an exact adapter pair",
            category="adapter",
        )
    return pair_key


def _variant_pair_key(
    base_pair_key: str,
    model: str,
    reasoning: Mapping[str, Any],
    transport: str,
) -> str:
    try:
        encoded = json.dumps(
            {
                "base_pair_key": base_pair_key,
                "model": model,
                "reasoning": reasoning,
                "transport": transport,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ServiceError("POLICY_REJECTED", "variant identity is not canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _runtime_state_error(runtime_id: str, state: str) -> ServiceError:
    code = {
        "not_installed": "INSTALL_REQUIRED",
        "auth_required": "AUTH_REQUIRED",
        "auto_paused": "QUOTA_PAUSED",
        "incompatible": "TRANSPORT_INCOMPATIBLE",
        "probing": "RECOVERY_REQUIRED",
        "recovery_required": "RECOVERY_REQUIRED",
    }.get(state, "CAPABILITY_MISSING")
    return ServiceError(
        code,
        f"runtime {runtime_id!r} is {state}",
        category="runtime",
    )


def _refuse_canary_cleanup(
    receipt: Mapping[str, Any],
    circuit: CircuitRecord,
) -> VerifiedCleanupReceipt | None:
    del receipt, circuit
    return None


def _capability_gap(message: str) -> NoReturn:
    raise ServiceError("CAPABILITY_MISSING", message, retryable=False)
