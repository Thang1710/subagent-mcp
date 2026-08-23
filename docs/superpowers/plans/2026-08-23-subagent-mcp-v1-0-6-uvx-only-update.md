# Subagent MCP 1.0.6 Isolated uvx Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release 1.0.6 with a public install/update path that never mutates an environment held by a running Windows MCP or UI process.

**Architecture:** Every documented Subagent MCP process runs from an exact `uvx --isolated --from subagent-harness-mcp==<version>` environment. Updating changes only the explicit Codex registration and starts a new cached version; old processes and caches remain untouched until they end naturally.

**Tech Stack:** Python packaging, pytest, uv/uvx, Codex public MCP lifecycle commands, Markdown, GitHub Actions.

---

### Task 1: Lock the safe public contract with a RED test

**Files:**
- Modify: `tests/unit/test_package_contract.py:245`
- Test: `tests/unit/test_package_contract.py`

- [x] **Step 1: Change the package-contract assertions before README edits**

Replace the current uvx command and ordering assertions with the exact isolated
contract:

```python
uvx_prefix = f"uvx --isolated --from {DIST_NAME}=={VERSION} {DIST_NAME}"
assert f"codex mcp add subagent-mcp -- {uvx_prefix} serve" in readme
assert f"{uvx_prefix} ui --background" in readme
assert "uvx --isolated --from" in readme
assert f"{DIST_NAME} ui --stop" in readme
assert f"uv tool install {DIST_NAME}" not in readme
assert "uv tool install --reinstall" not in readme
assert "codex mcp add subagent-mcp -- subagent-harness-mcp serve" not in readme
```

- [x] **Step 2: Run the exact test and prove RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_package_contract.py::test_readme_isolates_persistent_ui_from_codex_stdio_updates -q
```

Expected: FAIL because 1.0.5 lacks `--isolated` and still documents persistent
`uv tool install`/`--reinstall`.

- [x] **Step 3: Do not commit a RED-only tree**

Proceed directly to Task 2 with the failing contract visible in the current
diff.

### Task 2: Make the docs contract GREEN

**Files:**
- Modify: `README.md:41-66,147-162`
- Modify: `docs/architecture.md:74-82`
- Modify: `docs/superpowers/specs/2026-08-22-subagent-mcp-windows-update-isolation-design.md`
- Modify: `docs/superpowers/specs/2026-08-17-subagent-mcp-design.md`
- Test: `tests/unit/test_package_contract.py`

- [x] **Step 1: Replace fresh install and UI commands**

Document only these release-pinned process commands:

```powershell
codex mcp add subagent-mcp -- uvx --isolated --from subagent-harness-mcp==1.0.5 subagent-harness-mcp serve
uvx --isolated --from subagent-harness-mcp==1.0.5 subagent-harness-mcp ui --background
```

Task 3 changes both commands and the test's shared `VERSION` constant to 1.0.6
in one metadata step.

- [x] **Step 2: Replace update/rollback with non-destructive version switching**

The update block stops the old exact UI, replaces the MCP entry through public
Codex commands, and starts the new exact UI. It contains no reinstall, cache
cleanup, or process kill.

- [x] **Step 3: Align architecture and design authority**

State that `--isolated` prevents reuse of a persistent tool; persistent install
is optional, and legacy users close old Codex windows only before optionally
removing that unused tool. Add the 2026-08-23 amendment to the main design.

- [x] **Step 4: Run the focused contract suite and prove GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_package_contract.py -q
```

Expected: `14 passed`.

- [x] **Step 5: Commit the bounded contract fix with the user's Git identity**

```powershell
git add README.md docs/architecture.md docs/superpowers/specs/2026-08-17-subagent-mcp-design.md docs/superpowers/specs/2026-08-22-subagent-mcp-windows-update-isolation-design.md docs/superpowers/specs/2026-08-23-subagent-mcp-uvx-only-update-design.md docs/superpowers/plans/2026-08-23-subagent-mcp-v1-0-6-uvx-only-update.md tests/unit/test_package_contract.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "fix: isolate Windows update runtimes"
```

### Task 3: Bump exact 1.0.6 release metadata

**Files:**
- Modify: `pyproject.toml:7`
- Modify: `uv.lock:976`
- Modify: `src/subagent_harness_mcp/__init__.py:5`
- Modify: `server.json:11,16`
- Modify: `tests/unit/test_package_contract.py:27`
- Modify: `tests/integration/test_wheel_e2e.py:27`
- Modify: `README.md:23, registration/UI/update commands`
- Modify: `CHANGELOG.md:5`

- [x] **Step 1: Replace product version metadata with `1.0.6`**

Every exact current-release field above becomes `1.0.6`; historical changelog
entries and the update block's source-version `ui --stop` command remain at
1.0.5.

- [x] **Step 2: Add the 1.0.6 changelog entry**

Record only verified product behavior: isolated uvx is now the default
install/UI/update boundary, legacy processes remain untouched, and no
destructive persistent-tool reinstall is documented.

- [x] **Step 3: Run version and package contract tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py -q
```

Expected: all selected tests pass with no failure/error.

- [x] **Step 4: Commit metadata with the user's Git identity**

```powershell
git add CHANGELOG.md README.md pyproject.toml uv.lock server.json src/subagent_harness_mcp/__init__.py tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "chore: prepare Subagent MCP 1.0.6"
```

### Task 4: Verify, review, mirror, and release

**Files:**
- Verify: repository and built wheel/sdist
- Mirror: the separately verified public `subagent-mcp` checkout
- Publish: GitHub Release, PyPI, MCP Registry

- [x] **Step 1: Run the focused and full safe suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py -q
.\.venv\Scripts\python.exe -m pytest -m "not real_git_worktree" -q
```

Expected: zero failures/errors; the four real-Git worktree cases remain
deselected by the second command.

- [x] **Step 2: Build and test final distributions**

Run:

```powershell
uv build --out-dir dist/v106-final
$env:SUBAGENT_MCP_TEST_DIST_DIR = (Resolve-Path 'dist/v106-final').Path
.\.venv\Scripts\python.exe -m pytest tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py -q
Remove-Item Env:SUBAGENT_MCP_TEST_DIST_DIR
```

Expected: wheel and sdist both identify as 1.0.6 and expose the documented
CLI/MCP entry point.

- [x] **Step 3: Prove the installed isolated uvx boundary without a provider**

From a fresh temporary `SUBAGENT_MCP_HOME`, start the exact wheel built in Step
2 through `uvx --isolated --from` and the public `subagent-harness-mcp serve`
entry point.

```powershell
$wheel = (Resolve-Path 'dist/v106-final/subagent_harness_mcp-1.0.6-py3-none-any.whl').Path
uvx --isolated --from $wheel subagent-harness-mcp serve
```

Use an MCP client to list exactly 14 tools and call only `runtime_list`. Inspect
the launched process path to prove it is not under the persistent uv tool
environment. Do not call Claude, DeepSeek, canary, spawn, send, or any provider.

Then use one temporary product home and fixed test port to start/status/fetch/
stop the source UI, the target UI, and the source UI again as rollback. Require
HTTP 200 for `/` and `/app.js` after each uvx launcher exits, no remaining
process or port after each stop, zero provider calls, and zero Codex config
changes.

- [x] **Step 4: Run bounded Critical-only review and privacy scans**

Give the exact base/head diff to OX Alpha. Attempt Claude only after one fresh
quota refresh proves safe no-overage evidence; terminal/unknown evidence is
reported and not retried. Fix only Critical or repeated Major findings, with at
most one correction wave. Scan tracked files and built artifacts for local
paths, emails other than the public noreply identity, credentials, tokens, and
private tool names.

- [ ] **Step 5: Mirror exact tracked content and verify equality**

Mirror the canonical product diff to the separately verified public checkout
without copying ignored local evidence. Verify clean trees, matching
tracked-file hashes, and the user's commit author identity in both repositories.

- [ ] **Step 6: Publish and read back every registry**

Push the public commit and tag `v1.0.6`. Require the GitHub release workflow and
MCP Registry workflow to finish successfully, publish the already-built exact
artifacts to PyPI, then read back GitHub Release, PyPI 1.0.6, and MCP Registry
1.0.6 as active/latest. Finally repeat the no-provider MCP smoke with the exact
public command `uvx --isolated --from subagent-harness-mcp==1.0.6
subagent-harness-mcp serve`. Do not alter Codex global configuration.

- [ ] **Step 7: Update the ignored SDD checkpoint**

Record exact commits, test counts, artifact hashes, workflow IDs, registry
readbacks, unresolved provider status, and the next product goal. Never claim
the overall goal complete while any required acceptance lane remains.
