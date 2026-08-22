# Subagent MCP Orphan Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover a connection-owned DeepSeek execution after controller loss only when the exact native ACP process is proven absent.

**Architecture:** Add an optional normalized orphan-cleanup verifier protocol. DeepSeek implements it with a read-only Windows process inventory matched against the exact bound Node executable, ACP script, and conversation-specific config path. The shared service turns an orphan into a terminal `CONTROLLER_DISCONNECTED` failure only after that verifier succeeds; otherwise it leaves state and leases untouched.

**Tech Stack:** Python 3.10, asyncio, Protocol, psutil on Windows, SQLite state store, pytest.

---

### Task 1: Specify restart recovery behavior

**Files:**
- Modify: `tests/integration/test_fake_lifecycle.py`
- Modify: `src/subagent_harness_mcp/adapters/base.py`
- Modify: `src/subagent_harness_mcp/service.py`

- [x] **Step 1: Write the failing cleanup-confirmed integration test**

Add a connection-owned, non-resumable fake adapter whose `orphan_cleanup_confirmed()` returns `True`. Spawn a running execution, create a new service instance, and call `agent_status(StatusRequest(..., refresh=True))`. Assert state `failed`, error code `CONTROLLER_DISCONNECTED`, conversation `idle`, then verify `agent_close` returns `closed` without invoking native close.

- [x] **Step 2: Write the failing cleanup-unconfirmed integration test**

Use the same fake adapter with `orphan_cleanup_confirmed()` returning `False`. Assert refreshed status raises `RECOVERY_REQUIRED`, the persisted execution remains `running`, and logical close remains rejected as `SESSION_BUSY`.

- [x] **Step 3: Run RED**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q tests/integration/test_fake_lifecycle.py -k orphan
```

Expected: failures because the optional verifier protocol and service reconciliation do not exist.

- [x] **Step 4: Add the minimal normalized protocol and service branch**

Define:

```python
@runtime_checkable
class OrphanCleanupAdapter(Protocol):
    async def orphan_cleanup_confirmed(
        self,
        request: AdapterSessionRequest,
        context: ResolvedContext,
    ) -> bool: ...
```

In `agent_status`, handle only `CAPABILITY_MISSING` from an active connection-owned non-resumable session. If the adapter implements the protocol and confirms cleanup, persist `ServiceError("CONTROLLER_DISCONNECTED", ...)` through `_record_failure`, reload, and return status. Otherwise raise `RECOVERY_REQUIRED` without mutation.

- [x] **Step 5: Run GREEN**

Run the Task 1 command. Expected: both orphan tests pass and existing restart tests remain green.

### Task 2: Verify the exact DeepSeek ACP process

**Files:**
- Modify: `tests/unit/test_deepseek_harness_adapter.py`
- Modify: `src/subagent_harness_mcp/adapters/deepseek_harness.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [x] **Step 1: Write RED process-inventory tests**

Inject a process inventory callable into `DeepSeekHarnessAdapter`. Cover three records:

```python
ProcessRecord("node.exe", exact_node_path, exact_acp_and_config_command)
ProcessRecord("node.exe", exact_node_path, unrelated_command)
ProcessRecord("node.exe", exact_node_path, None)
```

Assert exact match returns `False`, no exact match returns `True`, and an opaque matching executable returns `False`.

- [x] **Step 2: Run RED**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q tests/unit/test_deepseek_harness_adapter.py -k orphan
```

Expected: failures because process inventory and orphan verification are absent.

- [x] **Step 3: Implement the smallest Windows verifier**

Add a frozen process observation and a default psutil inventory that returns only process name, executable path, and command line. `orphan_cleanup_confirmed()` must first verify the current adapter pair against the persisted context hash, derive `<data_root>/<conversation_id>/cordis.yml`, and compare normalized exact Node/ACP/config path strings. Any inventory error, missing command line for the matching executable, binding drift, or exact live process returns `False`; only a complete inventory with no exact match returns `True`.

Declare psutil as a direct dependency. Do not use WMI: the real sandboxed Windows runtime can deny its process query even when ordinary per-process inspection works.

- [x] **Step 4: Run GREEN**

Run the Task 2 command plus the full DeepSeek unit file. Expected: all pass.

### Task 3: Release a19 safely

**Files:**
- Modify: `src/subagent_harness_mcp/__init__.py`
- Modify: `src/subagent_harness_mcp/adapters/claude_code.py`
- Modify: `src/subagent_harness_mcp/adapters/deepseek_harness.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `tests/unit/test_package_contract.py`
- Modify: `tests/integration/test_wheel_e2e.py`

- [x] **Step 1: Bump every public adapter/package version to `0.1.0a19`**

Keep the machine distribution and command unchanged. Add one changelog item describing verified orphan recovery and one README sentence explaining that controller-loss recovery never kills an unverified process.

- [x] **Step 2: Run focused verification**

Run service, DeepSeek, server, UI, package-contract, and stdio tests. Expected: all pass.

- [x] **Step 3: Run full safe verification and artifact acceptance**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q -m "not real_git_worktree"
uv build --out-dir .preview/release/a19-final
$env:SUBAGENT_MCP_TEST_DIST_DIR=(Resolve-Path .preview/release/a19-final).Path
uv run --frozen pytest -p no:cacheprovider -q tests/integration/test_wheel_e2e.py tests/unit/test_package_contract.py
```

Expected: zero failures; real Git worktree tests remain deselected.

- [ ] **Step 4: Commit once with the owner's identity**

Commit subject: `fix: recover verified orphaned sessions`. Mirror the exact tracked patch to the public repo, repeat package acceptance there, then tag/publish only after the bounded Critical review reports no verified Critical finding.
