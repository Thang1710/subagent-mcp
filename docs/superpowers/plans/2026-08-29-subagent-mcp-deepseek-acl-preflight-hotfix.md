# Subagent MCP DeepSeek ACL Preflight Hotfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a Windows DeepSeek writable task before ACP/provider construction when the native sandbox cannot materialize its required write-root ACL.

**Architecture:** Preserve DeepSeek's existing one-directory sandbox and use the standard library Win32 API only to open the resolved directory with `WRITE_DAC`; this is a read-only capability check and never changes ownership or ACLs. A denied check becomes terminal `CAPABILITY_MISSING` with a new-request/user-decision action, while read-only tasks and writable roots that pass continue through the unchanged native ACP lifecycle.

**Tech Stack:** Python 3.10+, `ctypes`/Win32 `CreateFileW`, MCP Python SDK v2, pytest, Hatch/uv packaging.

**Spec:** `docs/superpowers/specs/2026-08-17-subagent-mcp-design.md`, especially sections 11.4, 13, and 17.

## Global Constraints

- Do not call DeepSeek/GLM or any provider during diagnosis, tests, or proof.
- Do not read, write, inspect ACLs of, or otherwise touch `E:\UnityProject\RandomTowerDefender`.
- Never mutate ownership/DACLs, select `danger-full-access`, widen a write root, or change model, credits, overage, billing, or host configuration.
- Preserve the existing `existing-directory`, one-root, repository-relative request contract and all read-only behavior.
- Release as immutable `1.0.28` only after source, wheel, sdist, security, and fresh installed-artifact proofs pass.

---

### Task 1: Reject an Unmaterializable Windows Write Root Before ACP

**Files:**
- Modify: `tests/unit/test_deepseek_harness_adapter.py`
- Modify: `src/subagent_harness_mcp/adapters/deepseek_harness.py`

**Interfaces:**
- Consumes: the canonical existing directory returned by `_deepseek_write_root`.
- Produces: `None` when opening the root with `WRITE_DAC` succeeds, otherwise a Win32 error code projected as terminal `CAPABILITY_MISSING` before `client_factory` or provider work.

- [ ] **Step 1: Write the failing adapter test**

Construct a writable `AdapterContextRequest`, inject `write_dacl_probe=lambda _path: 5`, and assert:

```python
with pytest.raises(ServiceError) as captured:
    await adapter.resolve_context(request)

assert captured.value.code == "CAPABILITY_MISSING"
assert captured.value.category == "capability"
assert captured.value.retryable is False
assert "WRITE_DAC" in str(captured.value)
assert "Win32 5" in str(captured.value)
assert "danger-full-access" in captured.value.next_action
assert clients == []
```

Add a sibling read-only case proving the probe is not called.

- [ ] **Step 2: Run RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests/unit/test_deepseek_harness_adapter.py -k "write_dacl"
```

Expected: FAIL because the adapter has no injectable no-model ACL capability probe and accepts the writable context.

- [ ] **Step 3: Implement the minimal Win32 check**

Add `WriteDaclProbe = Callable[[Path], int | None]`, an optional constructor dependency defaulting to `_windows_write_dacl_error`, and call it only for `workspace-write` after `_deepseek_write_root`. Implement `_windows_write_dacl_error` with `CreateFileW(WRITE_DAC, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS)` and always close a valid handle. Do not call `SetNamedSecurityInfoW` or any mutating API.

On denial, raise `ServiceError` with:

```python
code="CAPABILITY_MISSING"
category="capability"
retryable=False
```

The message binds the resolved root and Win32 code without account data. `next_action` requires a materially new request after the user chooses an owned/`WRITE_DAC`-capable existing directory or another compatible runtime; it explicitly forbids automatic retry, scope widening, ACL mutation, and `danger-full-access`.

- [ ] **Step 4: Run GREEN and focused regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/test_deepseek_harness_adapter.py tests/unit/test_service.py tests/unit/test_server.py
```

Expected: zero failures and no external process/provider call.

### Task 2: Publish the Native ACL Precondition

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/unit/test_package_contract.py`

**Interfaces:**
- Consumes: the existing public DeepSeek `existing-directory` contract.
- Produces: public text explaining that Windows native sandbox setup additionally requires the current user to have `WRITE_DAC` on that directory and that failure is detected before provider work.

- [ ] **Step 1: Write the failing package text assertion**

Extend the README/package contract test with exact assertions for `WRITE_DAC`, pre-provider failure, and no automatic ACL mutation or `danger-full-access` fallback.

- [ ] **Step 2: Run RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests/unit/test_package_contract.py -k "write"
```

Expected: FAIL only on missing ACL-precondition documentation.

- [ ] **Step 3: Add the minimum public documentation**

Add one short paragraph to README and architecture beside the current DeepSeek write-root description. Add a dated changelog item; do not add a new schema field or dependency.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Expected: zero failures.

### Task 3: Build, Verify, Review, and Release 1.0.28

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `server.json`
- Modify: `src/subagent_harness_mcp/__init__.py`
- Modify: `tests/unit/test_package_contract.py`
- Modify: `tests/integration/test_wheel_e2e.py`

- [ ] **Step 1: Update immutable version identity**

Change only current-version pins from `1.0.27` to `1.0.28`; preserve historical upgrade examples.

- [ ] **Step 2: Run full source verification**

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -m "not real_git_worktree"
git diff --check
```

Expected: zero failures; only `real_git_worktree` tests deselected.

- [ ] **Step 3: Build and test exact artifacts**

```powershell
uv build --out-dir .preview/release/1.0.28
$env:SUBAGENT_MCP_TEST_DIST_DIR=(Resolve-Path .preview/release/1.0.28)
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py
```

Expected: wheel and sdist install outside the checkout; zero provider calls.

- [ ] **Step 4: Run security and independent review gates**

Compile Python, check JavaScript syntax, run `git diff --check`, scan tracked/archive content for credentials and local paths, and inspect archive members for absolute/traversal paths. One independent reviewer checks only correctness/security of the bounded diff; perform at most one fix wave for verified Critical/Major findings.

- [ ] **Step 5: Merge, publish, and prove the public artifact**

Commit with `Thang1710 <50268205+Thang1710@users.noreply.github.com>`, merge into clean/fetched `main`, tag `v1.0.28`, and use the existing release workflow. Verify GitHub Release, PyPI SHA-256 values, and MCP Registry. Force-refresh `subagent-harness-mcp==1.0.28` and run an isolated installed-artifact script that injects Win32 error 5 into the adapter; assert terminal `CAPABILITY_MISSING`, `retryable=false`, and zero ACP client/provider construction. Remove the temporary script afterward.

The source task may create a new request only after the user chooses a compatible write root/runtime. Never reuse `conversation-a587c8bf72f746fd9b9cc5e5f0cea9f8`, `execution-0befc54fba464ca6a00df2242c74897b`, or the old request ID.
