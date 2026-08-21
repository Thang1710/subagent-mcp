# Subagent MCP Phase 0b Adapter Prototypes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the disposable Claude Code adapter behaviors needed before production Subagent MCP code is allowed: the managed SDK transport, the existing visible-background transport, native session continuity, promotion, policy hooks, lifecycle cleanup, circuits, and restart/rollback behavior.

**Architecture:** Keep every artifact under `spikes/phase0b`, `tests/phase0b`, or the ignored `.phase0b` evidence root. Reuse the accepted Phase 0a identity, approval, process, redaction, and lifecycle contracts instead of creating a second safety layer. The default public Claude Agent SDK transport is the compatibility baseline; Windows Job Object ownership is optional and remains unavailable unless the exact public Transport surface and real canary pass.

**Tech Stack:** Python 3.10, standard library, `claude-agent-sdk==0.2.142` in a dedicated uv dependency group, standalone Claude Code CLI 2.1.224 through explicit `cli_path`, pytest 8, PowerShell 5.1, Git.

---

## Execution boundary

Amended 2026-08-21 for the current deterministic build lane:

- The full generated Phase 0a report remains `BLOCKED`. Group B ended after its init subset with a post-init timeout and produced neither a terminal result nor evidence that usage credits were used. Managed/background live steps remain unavailable, and no unknown or missing evidence becomes PASS.
- Deterministic build/install/import/fake/no-model Tasks 0-2, Task 4 deterministic classification, Task 7 pure circuits, and Task 8 simulation are authorized. Installing a dependency does not authorize a provider call, background row, stop/resume/promotion action, or live evidence write.
- The user's continuous end-to-end authority supersedes older per-gate wait language only for actions already inside this bounded scope. It does not widen the scope, enable usage credits, or waive a required capability/evidence result.
- No task registers an MCP client, changes Codex/Claude settings, reads private daemon/transcript state, enables usage credits, or creates production `src/subagent_harness_mcp` modules.
- One writer owns each task. Run tests once at the bounded task end. Use one Critical-only review wave; defer Important/Minor polish.
- The official PyPI release page was checked on 2026-08-21: `claude-agent-sdk==0.2.142` remains latest, its Windows wheel is approximately 103.4 MB, and the SDK supports an explicit `cli_path`. The bundled CLI must never be selected implicitly.

Never float the dependency. A future change to the reviewed pin or public API requires an explicit plan amendment and matching deterministic fixtures before installation.

## File map

- `spikes/phase0b/contracts.py`: versioned Phase 0b specs, normalized observations, and exact acceptance gate names.
- `spikes/phase0b/sdk_options.py`: pure construction/validation of `ClaudeAgentOptions`; no process launch.
- `spikes/phase0b/managed_probe.py`: approval-bound managed SDK initialization, multi-execution, interrupt, and resume runner.
- `spikes/phase0b/transport_probe.py`: default-transport baseline and optional public-Transport/Job-Object capability classification.
- `spikes/phase0b/needs_input_probe.py`: bounded Python `can_use_tool` bridge for AskUserQuestion.
- `spikes/phase0b/promotion_probe.py`: visible-background-to-managed one-way promotion state machine.
- `spikes/phase0b/policy_probe.py`: nested-agent counters, depth/workspace checks, and normalized circuit transitions.
- `spikes/phase0b/update_probe.py`: temporary immutable-runtime, restart, locked-file, PID-reuse, and rollback simulations.
- `spikes/phase0b/evidence.py`: sanitized candidate/accepted evidence and exact Phase 0b decision.
- `tests/phase0b/`: deterministic tests and fixture replay matching the modules above.
- `tests/fixtures/phase0b/current/`: committed sanitized evidence only; never raw SDK/provider output.

### Task 0: Gate and pin the isolated SDK dependency

**Files:**
- Modify after approval: `pyproject.toml`
- Modify after approval: `uv.lock`
- Modify after approval: `.gitignore`
- Create: `tests/phase0b/test_dependency_contract.py`

- [ ] **Step 1: Re-read and record the entry gate**

Run:

~~~powershell
Select-String -LiteralPath docs\phase0a\phase0a-report.md -Pattern 'phase_0b_may_begin'
git status --short
~~~

Expected: the committed report remains BLOCKED and the tracked tree is clean. Record that state without regenerating the report. The execution-boundary amendment above authorizes only the listed deterministic lane; it does not authorize a live provider step or change the report decision.

- [ ] **Step 2: Add the dependency-contract test without running it**

~~~python
from importlib.metadata import version


def test_phase0b_uses_the_reviewed_sdk_version() -> None:
    assert version("claude-agent-sdk") == "0.2.142"
~~~

- [ ] **Step 3: Apply the bounded dependency-install authority**

The reviewed Windows wheel is approximately 103.4 MB and bundles a CLI that Subagent MCP will never select implicitly. The current continuous authority covers only:

~~~powershell
uv add --group phase0b --no-sync "claude-agent-sdk==0.2.142"
~~~

It also covers creating the ignored immutable environment `.phase0b/runtime/sdk-0.2.142` from the resulting lock. It does not cover an SDK import canary, provider call, background/lifecycle action, live evidence write, or host/config mutation, and it never syncs the repository's existing `.venv`.

- [ ] **Step 4: Pin the dependency after approval**

The resulting TOML must contain:

~~~toml
[dependency-groups]
dev = ["pytest>=8,<9"]
phase0b = ["claude-agent-sdk==0.2.142"]
~~~

Add exactly `.phase0b/` to `.gitignore` before creating the staged runtime. Do not broaden any existing ignore rule.

- [ ] **Step 5: Verify once at task end**

Run:

~~~powershell
$previousPhase0bEnv = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = ".phase0b/runtime/sdk-0.2.142"
    uv sync --frozen --group dev --group phase0b
} finally {
    if ($null -eq $previousPhase0bEnv) {
        Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
    } else {
        $env:UV_PROJECT_ENVIRONMENT = $previousPhase0bEnv
    }
}
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_dependency_contract.py
git status --short
git diff --check
~~~

Expected: `1 passed`; the 100+ MB staged runtime is absent from Git status; no Claude process/session/row is created.

- [ ] **Step 6: Commit**

~~~powershell
git add .gitignore pyproject.toml uv.lock tests\phase0b\test_dependency_contract.py
git commit -m "build: pin Phase 0b Claude SDK"
~~~

### Task 1: Define exact Phase 0b contracts and sanitized evidence

**Files:**
- Create: `spikes/phase0b/__init__.py`
- Create: `spikes/phase0b/contracts.py`
- Create: `spikes/phase0b/evidence.py`
- Create: `tests/phase0b/__init__.py`
- Create: `tests/phase0b/test_contracts.py`
- Create: `tests/phase0b/test_evidence.py`

- [ ] **Step 1: Write contract tests**

The tests instantiate only the following exact public shapes:

~~~python
from spikes.phase0b.contracts import (
    AdapterPair,
    Capability,
    ManagedObservation,
    PHASE0B_GATES,
)


def test_adapter_pair_rejects_unbound_cli() -> None:
    pair = AdapterPair(
        sdk_version="0.2.142",
        cli_version="2.1.224 (Claude Code)",
        cli_sha256="a" * 64,
    )
    pair.validate()


def test_gate_set_is_exact() -> None:
    assert PHASE0B_GATES == (
        "managed_sdk_context",
        "managed_sdk_cleanup",
        "managed_needs_input",
        "managed_resume",
        "managed_interrupt",
        "visible_managed_promotion",
        "nested_agent_policy",
        "circuit_transitions",
        "restart_rollback",
        "default_transport",
    )
    assert Capability("capability_missing") is Capability.CAPABILITY_MISSING
~~~

- [ ] **Step 2: Implement the minimal versioned types**

~~~python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Capability(str, Enum):
    PASS = "pass"
    BLOCKED = "blocked"
    CAPABILITY_MISSING = "capability_missing"


PHASE0B_GATES = (
    "managed_sdk_context",
    "managed_sdk_cleanup",
    "managed_needs_input",
    "managed_resume",
    "managed_interrupt",
    "visible_managed_promotion",
    "nested_agent_policy",
    "circuit_transitions",
    "restart_rollback",
    "default_transport",
)


@dataclass(frozen=True)
class AdapterPair:
    sdk_version: str
    cli_version: str
    cli_sha256: str

    def validate(self) -> None:
        if self.sdk_version != "0.2.142":
            raise ValueError("unreviewed SDK version")
        if not self.cli_version or SHA256.fullmatch(self.cli_sha256) is None:
            raise ValueError("invalid adapter pair")


@dataclass(frozen=True)
class ManagedObservation:
    pair: AdapterPair
    status: Capability
    session_present: bool
    context_equal: bool
    model_equal: bool
    cleanup_confirmed: bool
    result_terminal: bool
    error_category: str | None = None
~~~

- [ ] **Step 3: Implement exact adjudication**

`evidence.py` must require every gate above to be present. `default_transport` must PASS. The optional `windows_job_transport` field may be `capability_missing` and is not added to the exact required set.

~~~python
from collections.abc import Mapping
from .contracts import Capability, PHASE0B_GATES


def adjudicate_phase0b(gates: Mapping[str, str]) -> dict[str, object]:
    if set(gates) != set(PHASE0B_GATES):
        raise ValueError("Phase 0b gate set mismatch")
    normalized = {name: Capability(gates[name]) for name in PHASE0B_GATES}
    accepted = all(value is Capability.PASS for value in normalized.values())
    return {
        "phase_0b_may_begin_phase_1a": accepted,
        "gates": {name: normalized[name].value for name in PHASE0B_GATES},
    }
~~~

- [ ] **Step 4: Verify once at task end**

Run:

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_contracts.py tests\phase0b\test_evidence.py
git diff --check
~~~

Expected: all focused tests pass and unknown/missing/extra gates fail closed.

- [ ] **Step 5: Commit**

~~~powershell
git add spikes\phase0b tests\phase0b
git commit -m "test: define Phase 0b adapter contracts"
~~~

### Task 2: Build and validate managed SDK options without launching

**Files:**
- Create: `spikes/phase0b/sdk_options.py`
- Create: `tests/phase0b/test_sdk_options.py`

- [ ] **Step 1: Write option-mapping tests**

Tests must assert the exact standalone path, Claude Code system-prompt preset, model, effort, setting sources, strict empty MCP, tools, recursion denies, cwd, settings file, turn/budget caps, and no fallback model.

~~~python
from dataclasses import replace
from pathlib import Path
import pytest


@pytest.fixture
def bound_spec(tmp_path: Path) -> ManagedSpec:
    cli = tmp_path / "claude.exe"
    settings = tmp_path / "settings.json"
    cwd = tmp_path / "repo"
    cli.write_bytes(b"bound standalone")
    settings.write_text("{}", encoding="utf-8")
    cwd.mkdir()
    return ManagedSpec(
        cli_path=cli,
        cwd=cwd,
        settings=settings,
        model="provider-current-model",
        effort="low",
        tools=("Read", "Glob", "Grep"),
        max_turns=2,
        max_budget_usd=1.0,
    )


def test_options_bind_the_standalone_cli(bound_spec):
    options = build_managed_options(bound_spec)
    assert options.cli_path == bound_spec.cli_path
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.setting_sources == ["user", "project", "local"]
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {}
    assert options.fallback_model is None
    assert options.disallowed_tools == [
        "mcp__codex__*",
        "mcp__agent_bridge__*",
        "mcp__subagent_harness_mcp__*",
    ]


def test_options_preserve_a_future_provider_model(bound_spec):
    future = replace(
        bound_spec,
        model="provider-future-model-2030",
        effort="xhigh",
    )
    options = build_managed_options(future)
    assert options.model == future.model
    assert options.effort == future.effort
    assert options.fallback_model is None
~~~

- [ ] **Step 2: Implement the pure builder**

~~~python
from dataclasses import dataclass
from pathlib import Path
from typing import get_args
import unicodedata
from claude_agent_sdk import ClaudeAgentOptions, EffortLevel


REVIEWED_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ManagedSpec:
    cli_path: Path
    cwd: Path
    settings: Path
    model: str
    effort: str
    tools: tuple[str, ...]
    max_turns: int
    max_budget_usd: float


def build_managed_options(spec: ManagedSpec, *, can_use_tool=None, hooks=None):
    if spec.max_turns < 1 or spec.max_budget_usd <= 0:
        raise ValueError("managed limits must be positive")
    if (
        not isinstance(spec.model, str)
        or not spec.model.strip()
        or len(spec.model.encode("utf-8")) > 256
        or any(unicodedata.category(character) == "Cc" for character in spec.model)
    ):
        raise ValueError("invalid provider-native model")
    if get_args(EffortLevel) != REVIEWED_CLAUDE_EFFORTS:
        raise RuntimeError("Claude SDK effort schema differs from the reviewed pair")
    if spec.effort not in REVIEWED_CLAUDE_EFFORTS:
        raise ValueError("invalid Claude effort")
    if not spec.cli_path.is_file() or not spec.settings.is_file() or not spec.cwd.is_dir():
        raise ValueError("managed paths must already exist")
    return ClaudeAgentOptions(
        cli_path=spec.cli_path,
        cwd=spec.cwd,
        settings=str(spec.settings),
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user", "project", "local"],
        strict_mcp_config=True,
        mcp_servers={},
        tools=list(spec.tools),
        disallowed_tools=[
            "mcp__codex__*",
            "mcp__agent_bridge__*",
            "mcp__subagent_harness_mcp__*",
        ],
        permission_mode="dontAsk",
        model=spec.model,
        effort=spec.effort,
        fallback_model=None,
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        can_use_tool=can_use_tool,
        hooks=hooks,
        include_hook_events=True,
        extra_args={
            "autocompact": "274000",
            "prompt-suggestions": "false",
        },
    )
~~~

The builder receives only an execute-validated direct executable. A caller must load the Phase 0a bound identity and compare canonical path, file identity, SHA-256, and version before construction. Model remains an exact bounded provider-native opaque value, so future model IDs pass through without a name allowlist. Claude-specific effort is adapter-schema-defined and must match the exact public `EffortLevel` schema exposed by the reviewed SDK 0.2.142 pair (`low`, `medium`, `high`, `xhigh`, or `max`); an unknown future effort requires a reviewed adapter update instead of silent pass-through. Fallback remains disabled.

- [ ] **Step 3: Add fail-closed tests**

Cover changed CLI hash, missing settings, empty/control-containing/over-256-byte model, a future unknown model preserved exactly, every accepted public Claude effort, unknown/future effort rejection, empty limits, credential override environment, non-empty undeclared MCP, recursion deny drift, attempts to select the SDK-bundled executable, and any non-`None` fallback model.

- [ ] **Step 4: Verify once at task end and commit**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_sdk_options.py
git diff --check
git add spikes\phase0b\sdk_options.py tests\phase0b\test_sdk_options.py
git commit -m "test: bind managed SDK options"
~~~

### Task 3: Prove default managed transport, cleanup, interrupt, and resume

**Files:**
- Create: `spikes/phase0b/managed_probe.py`
- Create: `tests/phase0b/test_managed_probe.py`
- Create after approved live evidence: `tests/fixtures/phase0b/current/live-managed-sdk.json`

- [ ] **Step 1: Implement a fake-client lifecycle test first**

The fake exposes `connect`, `query`, `receive_response`, `interrupt`, and `disconnect`. Assert:

- the complete response iterator is drained;
- one terminal result is required per execution;
- interrupt drains the interrupted result before a follow-up;
- disconnect runs in `finally`;
- unknown SDK messages are bounded and ignored, not persisted raw;
- assistant-final/result duplication yields one normalized terminal event.

~~~python
import asyncio
from claude_agent_sdk import ResultMessage, SystemMessage


class FakeClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.queries = []
        self.interrupted = False
        self.disconnected = False

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        for message in self.batches.pop(0):
            yield message

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.disconnected = True


def test_execution_requires_one_terminal():
    fake_system = SystemMessage(
        subtype="init",
        data={"session_id": "session-1", "model": "claude-sonnet-5"},
    )
    fake_result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-1",
    )
    client = FakeClient([[fake_system, fake_result]])
    result = asyncio.run(run_execution(client, "bounded prompt"))
    assert result["session_id"] == fake_result.session_id
    assert result["subtype"] == "success"
    assert client.queries == ["bounded prompt"]
~~~

- [ ] **Step 2: Implement the managed runner**

~~~python
from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage


async def run_execution(client: ClaudeSDKClient, prompt: str) -> dict[str, object]:
    await client.query(prompt)
    session_id = None
    terminal = None
    model = None
    async for message in client.receive_response():
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id")
            model = message.data.get("model")
        elif isinstance(message, ResultMessage):
            if terminal is not None:
                raise RuntimeError("duplicate terminal result")
            terminal = message
            session_id = message.session_id
    if terminal is None or not isinstance(session_id, str):
        raise RuntimeError("managed execution lacks terminal/session identity")
    return {
        "session_id": session_id,
        "model": model,
        "subtype": terminal.subtype,
        "is_error": terminal.is_error,
        "terminal_reason": terminal.terminal_reason,
    }
~~~

The production runner stores only booleans/categories/digests. It never stores result text, hidden thinking, cost, tokens, transcript path, account identity, or session ID in committed evidence.

- [ ] **Step 3: Add preview and approval scope**

Preview binds:

- clean HEAD and complete executable manifest;
- SDK distribution version/hash plus standalone CLI identity;
- exact `ManagedSpec`, prompt hash, cwd/repository identity, settings hash;
- `max_provider_session_launches=3`: initialization/first execution, interrupt execution, resumed follow-up;
- zero worktree/create/remove and zero host configuration mutation;
- unchanged usage-credit setting plus immediate post-result no-overage confirmation.

Do not create a receipt until the user approves the exact digest.

- [ ] **Step 4: Run the live canary only after separate approval**

The canary must prove:

- default SDK transport uses the explicit standalone `cli_path`;
- actual model/effort/context initialization matches the request;
- requested/effective provider compaction window and exact trigger remain separate; PASS requires an official structured surface to attest `effective_auto_compaction_trigger_tokens=274000`, otherwise `managed_sdk_context` stays CAPABILITY_MISSING;
- first turn returns a native session ID;
- interrupt reaches a terminal interrupted category and no process survives;
- a new client resumes the same native session/cwd and completes a second execution;
- subscription auth is used and `isUsingOverage=false`;
- no global config or existing transcript is mutated by Subagent MCP.

- [ ] **Step 5: Verify once at task end and commit sanitized evidence**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_managed_probe.py
git diff --check
git add spikes\phase0b\managed_probe.py tests\phase0b\test_managed_probe.py tests\fixtures\phase0b\current\live-managed-sdk.json
git commit -m "test: prove managed SDK lifecycle"
~~~

### Task 4: Classify optional Windows Job Object transport

**Files:**
- Create: `spikes/phase0b/transport_probe.py`
- Create: `tests/phase0b/test_transport_probe.py`
- Create after approved live evidence: `tests/fixtures/phase0b/current/live-transport-capabilities.json`

- [ ] **Step 1: Test the public Transport surface**

The reviewed SDK exposes the abstract methods:

~~~python
EXPECTED_TRANSPORT_METHODS = {
    "connect",
    "write",
    "read_messages",
    "close",
    "is_ready",
    "end_input",
}
~~~

Fail closed if the public abstract method set differs. Never import `claude_agent_sdk._internal` or copy its bundled subprocess transport.

- [ ] **Step 2: Implement capability classification**

~~~python
from claude_agent_sdk import Transport


def classify_job_transport(public_process_factory: object | None) -> str:
    methods = set(getattr(Transport, "__abstractmethods__", ()))
    if methods != EXPECTED_TRANSPORT_METHODS or public_process_factory is None:
        return "capability_missing"
    return "needs_live_canary"
~~~

For the reviewed SDK, pass `public_process_factory=None`: the public `Transport` is documented as low-level and unstable and does not expose a public subprocess factory/child handle. Therefore version 1 keeps Job Object transport disabled unless a later reviewed public factory makes process ownership implementable without private imports. `default_transport` remains the required core gate.

- [ ] **Step 3: Test default behavior**

Assert that `transport=None` is passed to `ClaudeSDKClient` unless the optional capability record says PASS for the exact SDK/CLI pair. `capability_missing` is visible and never relabeled PASS.

- [ ] **Step 4: Verify once at task end and commit**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_transport_probe.py
git diff --check
git add spikes\phase0b\transport_probe.py tests\phase0b\test_transport_probe.py tests\fixtures\phase0b\current\live-transport-capabilities.json
git commit -m "test: classify managed transport ownership"
~~~

### Task 5: Prove managed needs-input and multiple executions

**Files:**
- Create: `spikes/phase0b/needs_input_probe.py`
- Create: `tests/phase0b/test_needs_input_probe.py`
- Create after approved live evidence: `tests/fixtures/phase0b/current/live-needs-input.json`

- [ ] **Step 1: Implement the bounded input bridge**

~~~python
import asyncio
from dataclasses import dataclass
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny


@dataclass(frozen=True)
class PendingQuestion:
    tool_use_id: str
    payload: dict[str, object]


class InputBridge:
    def __init__(self) -> None:
        self.pending: asyncio.Queue[PendingQuestion] = asyncio.Queue(maxsize=1)
        self.answer: asyncio.Future[dict[str, object]] | None = None

    async def can_use_tool(self, tool_name, input_data, context):
        if tool_name != "AskUserQuestion" or not context.tool_use_id:
            return PermissionResultDeny(message="tool not approved", interrupt=False)
        if self.answer is not None:
            return PermissionResultDeny(message="question already pending", interrupt=True)
        loop = asyncio.get_running_loop()
        self.answer = loop.create_future()
        await self.pending.put(PendingQuestion(context.tool_use_id, input_data))
        updated = await self.answer
        return PermissionResultAllow(updated_input=updated)
~~~

Validate and redact question/option fields before exposing them. Reject free-form unknown schema, duplicate pending questions, mismatched tool IDs, oversized text, cancellation, and answers after terminal.

- [ ] **Step 2: Test the Python capability gap honestly**

Python `can_use_tool` may remain pending in-process, but Python SDK 0.2.142 has no deferred decision that survives process exit. Evidence must report `deferred_needs_input_across_restart=capability_missing`; do not simulate the TypeScript-only defer feature.

- [ ] **Step 3: Preview and run one approved live question**

Use tools `["Read", "Glob", "Grep", "AskUserQuestion"]` and a prompt that requires choosing between two harmless labels before producing a read-only summary. Prove:

- the callback surfaces one sanitized question;
- the supplied answer resumes the same execution;
- the ResultMessage session ID is retained;
- a second normal execution resumes the same session;
- no TUI/private pipe/transcript parsing occurs.

- [ ] **Step 4: Verify once at task end and commit**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_needs_input_probe.py
git diff --check
git add spikes\phase0b\needs_input_probe.py tests\phase0b\test_needs_input_probe.py tests\fixtures\phase0b\current\live-needs-input.json
git commit -m "test: prove managed needs-input"
~~~

### Task 6: Prove one-way visible-to-managed promotion

**Files:**
- Create: `spikes/phase0b/promotion_probe.py`
- Create: `tests/phase0b/test_promotion_probe.py`
- Create after approved live evidence: `tests/fixtures/phase0b/current/live-promotion.json`

- [ ] **Step 1: Write deterministic state-machine tests**

~~~python
from enum import Enum


class PromotionState(str, Enum):
    VISIBLE_WORKING = "visible_working"
    STOP_REQUESTED = "stop_requested"
    STABLE_STOPPED = "stable_stopped"
    MANAGED_INITIALIZING = "managed_initializing"
    MANAGED_READY = "managed_ready"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class PromotionObservation:
    row_owned: bool
    worker_absent_twice: bool
    session_equal: bool
    workspace_equal: bool
    model_equal: bool
    context_equal: bool
    writer_count: int


def begin_managed(observation: PromotionObservation) -> PromotionState:
    if not (
        observation.row_owned
        and observation.worker_absent_twice
        and observation.session_equal
        and observation.workspace_equal
        and observation.model_equal
        and observation.context_equal
        and observation.writer_count == 1
    ):
        return PromotionState.RECOVERY_REQUIRED
    return PromotionState.MANAGED_INITIALIZING
~~~

Tests prove no transition reaches `MANAGED_INITIALIZING` until:

- the same owned visible row is stopped;
- no live PID/worker is observed twice across the stabilization interval;
- native session ID, canonical cwd/workspace, model, context fingerprint, and writer lease match;
- no background respawn or second writer appears.

Any mismatch returns `RECOVERY_REQUIRED`, keeps the lease, and never guesses a PID.

~~~python
from dataclasses import replace


def test_promotion_rejects_context_drift():
    valid = PromotionObservation(
        row_owned=True,
        worker_absent_twice=True,
        session_equal=True,
        workspace_equal=True,
        model_equal=True,
        context_equal=True,
        writer_count=1,
    )
    drifted = replace(valid, context_equal=False)
    assert begin_managed(drifted) is PromotionState.RECOVERY_REQUIRED
~~~

- [ ] **Step 2: Implement with existing Phase 0a surfaces**

Use the bound CLI, sanitized `agents --json --all`, approved `stop`, the Phase 0a lease/journal, and `ClaudeAgentOptions(resume=session_id, cli_path=bound_cli)`. Do not parse `claude logs`, TUI output, daemon pipes, or native transcript content.

- [ ] **Step 3: Stop for promotion approval**

Preview binds creation of one new read-only Phase 0b visible-background row in a disposable repository, its exact native session/cwd/model/context, one approved stop, one managed resume, zero worktree creation/removal, exact stabilization deadlines, and recovery behavior. The scope permits at most two provider launches (visible creation and managed resume) and one stop. The user must approve the concrete prompt, row naming policy, and digest; no Phase 0a row is reused.

- [ ] **Step 4: Execute once and verify**

PASS requires same native session, workspace, model, resolved context, and exclusive writer after managed initialization. A missing public session ID or context equality is a capability failure, not a fallback to a new conversation.

- [ ] **Step 5: Run tests once and commit**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_promotion_probe.py
git diff --check
git add spikes\phase0b\promotion_probe.py tests\phase0b\test_promotion_probe.py tests\fixtures\phase0b\current\live-promotion.json
git commit -m "test: prove visible managed promotion"
~~~

### Task 7: Enforce nested-agent policy and normalize circuits

**Files:**
- Create: `spikes/phase0b/policy_probe.py`
- Create: `tests/phase0b/test_policy_probe.py`
- Create: `tests/fixtures/phase0b/current/circuit-cases.json`
- Create after approved live evidence: `tests/fixtures/phase0b/current/live-nested-policy.json`

- [ ] **Step 1: Implement per-execution nested counters**

~~~python
import asyncio
from dataclasses import dataclass


@dataclass
class NestedCount:
    total: int = 0
    active: int = 0


class NestedPolicy:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._counts: dict[tuple[str, str], NestedCount] = {}

    async def before_agent(self, conversation_id, execution_id, agent_id):
        async with self._lock:
            key = (conversation_id, execution_id)
            count = self._counts.setdefault(key, NestedCount())
            if agent_id is not None or count.total >= 4 or count.active >= 4:
                return {"permissionDecision": "deny"}
            count.total += 1
            count.active += 1
            return {"permissionDecision": "allow"}

    async def after_agent(self, conversation_id, execution_id):
        async with self._lock:
            count = self._counts[(conversation_id, execution_id)]
            count.active = max(0, count.active - 1)
~~~

Add final reconciliation and canonical workspace containment. A subagent with `agent_id` cannot spawn another agent; writes outside the leased worktree are denied.

- [ ] **Step 2: Add exact circuit fixtures**

Fixture cases cover:

- `authentication_failed` → auth circuit open;
- `rate_limit` and `billing_error` → quota circuit open;
- `server_error` and official overload transport signal → retryable bounded state;
- official managed `model_not_found` → model capability unavailable;
- unknown/future values → unknown, never success;
- informational `rate_limit_event` followed by successful result with `isUsingOverage=false` → success, no circuit.

Never deliberately trigger billing/auth/quota failure on the live account. Live evidence may consume only naturally occurring structured errors.

Implement the pure transition before reading any live evidence. A non-error closes
the circuit only when it is a known terminal success/informational category and
the structured result explicitly confirms `isUsingOverage=false`; unknown or
incomplete observations never become success. `model_not_found` also reports the
variant-scoped `CAPABILITY_MISSING` result:

~~~python
def normalize_circuit(
    category: str,
    *,
    result_is_error: bool | None,
    result_terminal: bool,
    is_using_overage: bool | None,
) -> CircuitDecision:
    if category in {"authentication_failed", "oauth_org_not_allowed"}:
        return CircuitDecision(CircuitState.AUTH_OPEN)
    if result_is_error is True and result_terminal is True:
        if category in {"rate_limit", "billing_error"}:
            return CircuitDecision(CircuitState.QUOTA_OPEN)
        if category in {"server_error", "overloaded"}:
            return CircuitDecision(CircuitState.RETRYABLE)
        if category == "model_not_found":
            return CircuitDecision(
                CircuitState.MODEL_UNAVAILABLE,
                Capability.CAPABILITY_MISSING,
            )
        return CircuitDecision(CircuitState.UNKNOWN_OPEN)
    if (
        category in {"success", "rate_limit_event"}
        and result_is_error is False
        and result_terminal is True
        and is_using_overage is False
    ):
        return CircuitDecision(CircuitState.CLOSED)
    return CircuitDecision(CircuitState.UNKNOWN_OPEN)
~~~

`circuit-cases.json` is a canonical deterministic transition matrix, not live
provider evidence. It contains only categories, booleans, bounded outcomes, and
a digest of the cases; it contains no session, token, cost, prompt, or raw output.

- [ ] **Step 3: Test concurrent counters and circuits**

Use `asyncio.gather` to prove the fifth concurrent/total Agent call is denied atomically, counts reset per execution, stop reconciliation clears leaked active counts, path escape fails, and circuits survive a simulated service restart.

- [ ] **Step 4: Preview one bounded live hook canary**

The separate scope permits one managed provider session in a disposable worktree, read-only parent tools plus the native Agent tool, at most four nested Agent starts, zero writes, and no lifecycle removal. The prompt asks for five parallel read-only subtasks and instructs one nested agent to attempt another Agent call. PASS requires exactly four allowed starts, the fifth denied, the depth-two attempt denied from a non-null `agent_id`, all active counters reconciled to zero, and every observed cwd inside the approved worktree. Do not ask the model to force auth/quota/billing/model errors.

- [ ] **Step 5: Execute only after exact approval**

Retain only count/equality/deny-reason-category evidence. If the native hook schema cannot prove `agent_id`, depth enforcement is CAPABILITY_MISSING and Phase 0b stays blocked.

- [ ] **Step 6: Verify once at task end and commit**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_policy_probe.py
git diff --check
git add spikes\phase0b\policy_probe.py tests\phase0b\test_policy_probe.py tests\fixtures\phase0b\current\circuit-cases.json tests\fixtures\phase0b\current\live-nested-policy.json
git commit -m "test: prove nested policy and circuits"
~~~

### Task 8: Simulate restart, locked files, PID reuse, update, and rollback

**Files:**
- Create: `spikes/phase0b/update_probe.py`
- Create: `tests/phase0b/test_update_probe.py`
- Create: `tests/fixtures/phase0b/current/update-simulation.json`

- [ ] **Step 1: Define immutable runtime records**

~~~python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeRecord:
    version: str
    root: Path
    manifest_sha256: str


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_identity: str
    executable_sha256: str
~~~

- [ ] **Step 2: Implement temporary pointer switching**

The simulator stages `runtime-1` and `runtime-2` below `tmp_path`, fsyncs a new pointer record, atomically replaces `current.json`, validates the selected manifest, and rolls back to the previous pointer on health failure. It never modifies a real launcher or installed environment.

~~~python
import json
import os
from pathlib import Path


def switch_pointer(pointer: Path, candidate: RuntimeRecord, *, health_ok: bool) -> None:
    if not candidate.root.is_dir() or len(candidate.manifest_sha256) != 64:
        raise ValueError("invalid staged runtime")
    if not health_ok:
        raise RuntimeError("staged runtime health failed")
    temp = pointer.with_suffix(".tmp")
    payload = {
        "version": candidate.version,
        "root": candidate.root.name,
        "manifest_sha256": candidate.manifest_sha256,
    }
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, pointer)
~~~

- [ ] **Step 3: Add Critical failure tests**

Cover:

- locked old runtime remains readable while new pointer activates;
- failed staging leaves the old pointer unchanged;
- crash between stage and pointer swap is recoverable;
- PID number reuse with a different creation identity is never killed;
- managed restart resumes the saved native session and does not duplicate execution;
- visible-background restart reattaches only through the public roster;
- corrupt/additive state is quarantined without deleting the prior runtime;
- rollback restores the exact prior manifest.

`RuntimeRecord` validates a direct immutable runtime root and the actual manifest
digest; `ProcessIdentity` requires an exact PID, creation identity, and executable
digest match before the simulator targets anything. Unknown additive restart-state
fields are preserved, while malformed or duplicate state is moved to quarantine.
Managed restart decisions use the durable request key to resume rather than start a
duplicate execution, and visible restart accepts only a caller-supplied aggregate
from the public `agents --json --all` surface. The committed fixture contains only
deterministic boolean/status aggregates—never paths, PIDs, native session IDs, or
raw provider data.

- [ ] **Step 4: Verify once at task end and commit**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b\test_update_probe.py
git diff --check
git add spikes\phase0b\update_probe.py tests\phase0b\test_update_probe.py tests\fixtures\phase0b\current\update-simulation.json
git commit -m "test: simulate adapter restart and rollback"
~~~

### Task 9: Adjudicate Phase 0b and stop before production

**Files:**
- Modify: `spikes/phase0b/evidence.py`
- Modify: `tests/phase0b/test_evidence.py`
- Create after accepted live evidence: `tests/fixtures/phase0b/current/evidence-index.json`
- Create: `docs/phase0b/phase0b-report.md`

- [ ] **Step 1: Build an indexed fixture loader**

Require canonical relative names, SHA-256 equality, exact fixture kinds, one SDK/CLI pair across every live fixture, no absolute paths/identifiers/cost/token fields, and no unindexed files.

Expose only this deterministic CLI:

~~~python
import argparse
import hashlib
import json
import os
from pathlib import Path
from .contracts import PHASE0B_GATES
from spikes.phase0a.fixtures import validate_fixture


GATE_SOURCES = {
    "managed_sdk_context": ("live-managed-sdk.json", "managed_sdk_context"),
    "managed_sdk_cleanup": ("live-managed-sdk.json", "managed_sdk_cleanup"),
    "managed_needs_input": ("live-needs-input.json", "managed_needs_input"),
    "managed_resume": ("live-managed-sdk.json", "managed_resume"),
    "managed_interrupt": ("live-managed-sdk.json", "managed_interrupt"),
    "visible_managed_promotion": ("live-promotion.json", "visible_managed_promotion"),
    "nested_agent_policy": ("live-nested-policy.json", "nested_agent_policy"),
    "circuit_transitions": ("circuit-cases.json", "circuit_transitions"),
    "restart_rollback": ("update-simulation.json", "restart_rollback"),
    "default_transport": ("live-transport-capabilities.json", "default_transport"),
}


def load_indexed_fixtures(root: Path) -> dict[str, dict[str, object]]:
    root = root.resolve(strict=True)
    index = json.loads((root / "evidence-index.json").read_text("utf-8"))
    entries = index["fixtures"]
    if not isinstance(entries, dict) or not entries:
        raise ValueError("empty Phase 0b evidence index")
    loaded = {}
    for name, metadata in entries.items():
        if Path(name).name != name or name == "evidence-index.json":
            raise ValueError("invalid fixture name")
        if not isinstance(metadata, dict) or set(metadata) != {"sha256", "kind"}:
            raise ValueError("invalid fixture metadata")
        path = (root / name).resolve(strict=True)
        if path.parent != root:
            raise ValueError("fixture escapes root")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("fixture digest mismatch")
        fixture = json.loads(data.decode("utf-8"))
        validate_fixture(fixture)
        if fixture["kind"] != metadata["kind"]:
            raise ValueError("fixture kind mismatch")
        loaded[name] = fixture
    actual = {path.name for path in root.glob("*.json")} - {"evidence-index.json"}
    if actual != set(loaded):
        raise ValueError("unindexed Phase 0b fixture")
    return loaded


def derive_gate_statuses(fixtures: dict[str, dict[str, object]]) -> dict[str, str]:
    gates = {}
    for gate, (name, field) in GATE_SOURCES.items():
        fixture = fixtures.get(name)
        payload = fixture.get("payload") if isinstance(fixture, dict) else None
        value = payload.get(field) if isinstance(payload, dict) else None
        if value not in {"pass", "blocked", "capability_missing"}:
            value = "blocked"
        gates[gate] = value
    return gates


def render_report(fixtures: dict[str, dict[str, object]]) -> str:
    gates = derive_gate_statuses(fixtures)
    decision = adjudicate_phase0b(gates)
    lines = ["# Subagent MCP Phase 0b Report", ""]
    for name in PHASE0B_GATES:
        lines.append(f"- {name}: {decision['gates'][name]}")
    lines.extend([
        "",
        f"phase_0b_may_begin_phase_1a={str(decision['phase_0b_may_begin_phase_1a']).lower()}",
        "",
    ])
    return "\n".join(lines)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = render_report(load_indexed_fixtures(args.fixtures))
    write_text_atomic(args.output, report)
    return 0
~~~

- [ ] **Step 2: Add per-gate negative tests**

Parametrize every name in `PHASE0B_GATES`; changing any single gate to BLOCKED or CAPABILITY_MISSING must keep `phase_0b_may_begin_phase_1a=false`. The optional Job Object capability may be missing only when `default_transport=pass`.

- [ ] **Step 3: Regenerate the report twice**

~~~powershell
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m spikes.phase0b.evidence --fixtures tests\fixtures\phase0b\current --output docs\phase0b\phase0b-report.md
Get-FileHash -Algorithm SHA256 docs\phase0b\phase0b-report.md
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m spikes.phase0b.evidence --fixtures tests\fixtures\phase0b\current --output docs\phase0b\phase0b-report.md
Get-FileHash -Algorithm SHA256 docs\phase0b\phase0b-report.md
~~~

Expected: identical hashes. If any required live fixture is absent, the report says BLOCKED.

- [ ] **Step 4: Run final bounded verification**

~~~powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q -m "not real_git_worktree"
& .\.phase0b\runtime\sdk-0.2.142\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q tests\phase0b
git diff --check
~~~

Also verify zero task-owned process, active background row, worktree, approval receipt, credential/PII hit, and raw provider artifact in the tracked tree.

- [ ] **Step 5: One Critical-only review wave**

Review only false PASS, unusable core lifecycle, wrong-session resume/promotion, surviving process/writer, severe evidence corruption, or widespread breakage. Accept/reject findings once; do not enter an Important/Minor loop.

- [ ] **Step 6: Commit accepted evidence and report**

~~~powershell
git add spikes\phase0b tests\phase0b tests\fixtures\phase0b\current docs\phase0b\phase0b-report.md
git commit -m "test: accept Phase 0b adapter prototypes"
~~~

Stop here. Phase 1a production modules, MCP SDK installation, registration, SQLite service, public package, UI, and publishing require their own approved plan.

## Completion audit

Phase 0b is complete only when authoritative evidence proves:

- the exact SDK/standalone CLI pair and declared-native option mapping;
- default managed transport lifecycle, cleanup, interrupt, resume, and multiple executions;
- structured in-process needs-input plus the honest Python deferred-input capability gap;
- one-way promotion without session/context/workspace/model drift or concurrent writer;
- four-per-execution/depth-one/workspace containment;
- deterministic circuit transitions without inducing account damage;
- restart/update/locked-file/PID-reuse/rollback behavior;
- an exact indexed report with every required gate PASS.

Static tests, a plan, SDK import success, or a direct shell call alone are not acceptance evidence.

## Reviewed upstream surfaces

- Claude Agent SDK Python reference: https://code.claude.com/docs/en/agent-sdk/python
- Claude Agent SDK sessions: https://code.claude.com/docs/en/agent-sdk/sessions
- Claude Agent SDK user input: https://code.claude.com/docs/en/agent-sdk/user-input
- Claude Agent SDK hooks: https://code.claude.com/docs/en/agent-sdk/hooks
- Reviewed PyPI release: https://pypi.org/project/claude-agent-sdk/0.2.142/
