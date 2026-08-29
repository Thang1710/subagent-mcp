# Subagent MCP Windows Write Preflight Hotfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a fresh MCP controller construct the first valid Windows writable DeepSeek Harness request without consuming its bounded repair budget on sequential workspace and write-root shape errors.

**Architecture:** Keep the existing security and native capability boundary unchanged: Windows uses `workspace="current"`; `cwd` is the checkout root; DeepSeek `existing-directory` accepts exactly one repository-relative directory that already exists, with `.` representing the whole checkout only when that broader authority was explicitly granted. Fix the public `agent_spawn` description and both preflight recovery messages so the complete shape is visible before any provider call.

**Tech Stack:** Python 3.10+, MCP Python SDK v2, pytest, Hatch/uv packaging.

**Spec:** `docs/superpowers/specs/2026-08-17-subagent-mcp-design.md`

## Global Constraints

- Never widen an exact-file request to its parent directory automatically.
- Do not add a dry-run/provider probe or invoke DeepSeek/GLM during diagnosis or proof.
- Do not change model selection, credits, overage, billing, host config, or RandomTowerDefender.
- Preserve `write_root_mode="path-prefix"` semantics for adapters that can enforce exact files.
- Release as immutable `1.0.27` only after source, wheel, sdist, privacy, and public-install proof pass.

---

### Task 1: Publish One Complete Writable Request Contract

**Files:**
- Modify: `tests/unit/test_server.py`
- Modify: `src/subagent_harness_mcp/server.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: `agent_spawn` public MCP tool description and existing `runtime_list` manifest fields.
- Produces: one provider-neutral description containing the complete Windows/current and write-root shape contract.

- [ ] **Step 1: Write the failing public-description test**

Extend `test_lifecycle_tools_publish_compact_response_mode_and_long_local_wait`:

```python
assert "workspace='current'" in spawn_description
assert "cwd is the checkout root" in spawn_description
assert "write_set=['.']" in spawn_description
assert "repository-relative existing directory" in spawn_description
assert "exact files require write_root_mode='path-prefix'" in spawn_description
```

- [ ] **Step 2: Run RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_server.py::test_lifecycle_tools_publish_compact_response_mode_and_long_local_wait
```

Expected: FAIL because the current description only says to inspect `runtime_list`.

- [ ] **Step 3: Implement the minimum description change**

Replace only the `agent_spawn` docstring with concise provider-neutral guidance that includes all five tested phrases. Add the same exact payload rules to README/architecture; do not change request parsing or adapter enforcement.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Expected: `1 passed`.

### Task 2: Return Holistic Preflight Repair Instructions

**Files:**
- Modify: `tests/unit/test_server.py`
- Modify: `tests/unit/test_service.py`
- Modify: `src/subagent_harness_mcp/server.py`
- Modify: `src/subagent_harness_mcp/service.py`

**Interfaces:**
- Consumes: existing `CAPABILITY_MISSING` envelopes for unsupported Windows workspace and `existing-directory` root shape.
- Produces: `next_action` text that specifies `cwd`, `workspace`, permission and valid relative directory examples without automatically widening authority, including the currently actionless absolute-path rejection.

- [ ] **Step 1: Write failing error-envelope assertions**

In `test_invalid_version_request_id_and_workspace_fail_before_service_call`, decode the workspace error and assert its `next_action` contains:

```python
assert "workspace='current'" in workspace_error["next_action"]
assert "cwd" in workspace_error["next_action"]
assert "runtime_list" in workspace_error["next_action"]
assert "write_root_mode" in workspace_error["next_action"]
```

In `test_directory_only_preflight_rejects_files_before_impossible_decomposition`, additionally assert:

```python
assert "workspace='current'" in error.next_action
assert "cwd" in error.next_action
assert "write_set=['.']" in error.next_action
assert "repository-relative existing directory" in error.next_action
```

Add a focused `_normalize_write_set` assertion proving an absolute Windows path
remains `REQUEST_INVALID` but now directs the caller to make it relative to
`cwd` and inspect `runtime_list.write_root_mode` before the next materially
changed request.

- [ ] **Step 2: Run RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_server.py::test_invalid_version_request_id_and_workspace_fail_before_service_call tests/unit/test_service.py::test_directory_only_preflight_rejects_files_before_impossible_decomposition
```

Expected: both tests fail only on missing holistic guidance.

- [ ] **Step 3: Implement the minimum error-text change**

Change `_require_current_workspace`, `_normalize_write_set`, and
`_validate_write_root_mode` `next_action` strings only. Keep error code,
category, retryability, recovery reason, max attempts, normalization, root
count and provider-call ordering byte-for-byte compatible.

- [ ] **Step 4: Run GREEN and focused regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_server.py tests/unit/test_service.py tests/unit/test_deepseek_harness_adapter.py
```

Expected: zero failures and no provider process.

### Task 3: Prove and Release the Installed Public Surface

**Files:**
- Modify: `tests/integration/test_wheel_e2e.py`
- Modify: `tests/unit/test_package_contract.py`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `server.json`
- Modify: `src/subagent_harness_mcp/__init__.py`

**Interfaces:**
- Consumes: installed MCP `tools/list` and immutable release workflow.
- Produces: public `1.0.27` wheel/sdist whose `agent_spawn` description exposes the complete request contract.

- [ ] **Step 1: Add installed-artifact assertion before version edits**

In `_fake_stdio_smoke`, assert the installed `agent_spawn` tool description contains the same five Task 1 phrases. Run the exact wheel E2E test against a pre-hotfix artifact and confirm RED.

- [ ] **Step 2: Update release identity**

Update the current public version from `1.0.26` to `1.0.27` in `pyproject.toml`, `uv.lock`, `server.json`, package `__init__`, package contract tests, wheel E2E, README pins, and a dated changelog entry. Preserve historical upgrade examples intentionally naming older source versions.

- [ ] **Step 3: Run source verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -m "not real_git_worktree"
git diff --check
```

Expected: zero failures; only `real_git_worktree` tests deselected.

- [ ] **Step 4: Build and test exact artifacts**

Run:

```powershell
uv build --out-dir .preview/release/1.0.27
$env:SUBAGENT_MCP_TEST_DIST_DIR=(Resolve-Path .preview/release/1.0.27)
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests/integration/test_wheel_e2e.py
Remove-Item Env:SUBAGENT_MCP_TEST_DIST_DIR
```

Expected: wheel and sdist install outside the checkout and expose the fixed tool description; no provider call.

- [ ] **Step 5: Security, commit, publish and public proof**

Run package privacy/secret scans and inspect archive member names. Commit with `Thang1710 <50268205+Thang1710@users.noreply.github.com>`, merge to clean `main`, tag `v1.0.27`, and use the existing release workflow. Verify GitHub Release, PyPI hashes, MCP Registry, then force-refresh a public exact install and list tools through pipe-based MCP.

Fresh caller payload to publish with the receipt:

```json
{
  "runtime_id": "deepseek-harness",
  "variant_id": "default",
  "transport": "native-acp",
  "cwd": "E:\\UnityProject\\RandomTowerDefender",
  "workspace": "current",
  "required_capabilities": ["repo_read", "workspace_write"],
  "write_set": ["Assets/_Project/Core/Scripts/GameSettings"]
}
```

The example is the narrowest existing directory containing the authorized
target file. `write_set=["."]` is also shape-valid only when the user explicitly
granted write authority across the entire checkout; never replace an exact file
with a broader directory without that authority.
