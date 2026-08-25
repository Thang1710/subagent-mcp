# Subagent MCP Explicit Wait Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every fresh MCP controller understand that a quiet `running` external agent must keep running without timeout, retry, fallback, or interruption.

**Architecture:** Add one provider-neutral field during `AgentStatus` serialization and clarify the existing `agent_wait` MCP description. Keep adapters, persistence, and lifecycle ownership unchanged; verify through deterministic service/stdio tests and wheel/sdist installs outside the source tree.

**Tech Stack:** Python 3.10+, dataclasses, asyncio, MCP Python SDK v2, pytest, Hatch/uv packaging.

---

## File map

- Modify `src/subagent_harness_mcp/contracts.py`: serialize the additive running wait policy.
- Modify `src/subagent_harness_mcp/server.py`: publish the local-wait semantics in the MCP tool description.
- Modify `tests/unit/test_contracts.py`: cover compact/full running and non-running projections.
- Modify `tests/unit/test_service.py`: prove local wait expiry does not invoke native actions.
- Modify `tests/unit/test_server.py`: prove a fresh tool listing carries the rule.
- Modify `tests/integration/test_stdio_fake.py`: prove the public pipe-based stdio lifecycle carries the field.
- Modify `tests/integration/test_wheel_e2e.py`: prove installed wheel/sdist behavior outside the checkout.
- Modify `README.md`, `CHANGELOG.md`: explain long-running work and the release change.
- Modify version/provenance files for `1.0.20`: `pyproject.toml`, `uv.lock`, `server.json`, `src/subagent_harness_mcp/__init__.py`, `tests/unit/test_package_contract.py`, and `tests/integration/test_wheel_e2e.py`.

### Task 1: Add the provider-neutral status field with TDD

**Files:**
- Modify: `tests/unit/test_contracts.py`
- Modify: `src/subagent_harness_mcp/contracts.py:130-140,783-839`

- [ ] **Step 1: Write the failing serialization tests**

Add a focused helper and tests in `tests/unit/test_contracts.py`:

```python
def _running_status() -> AgentStatus:
    return AgentStatus(
        conversation_id="conversation-running",
        execution_id="execution-running",
        external_session_id="native-running",
        workspace_path="workspace",
        conversation_state="active",
        execution_state="running",
        state_revision=1,
        descriptor=AgentDescriptor.from_manifest(
            _manifest(), model="vendor/model", transport="managed-sdk"
        ),
        result=None,
        needs_input=(),
        events=(),
        next_event_cursor=1,
    )


def test_running_status_tells_fresh_controllers_to_continue_waiting() -> None:
    status = _running_status()

    assert status.to_compact_dict()["wait_policy"] == "continue_while_running"
    assert status.to_dict()["wait_policy"] == "continue_while_running"


@pytest.mark.parametrize(
    ("execution_state", "conversation_state"),
    (
        ("queued", "open"),
        ("starting", "active"),
        ("needs_input", "needs_input"),
        ("succeeded", "idle"),
        ("failed", "idle"),
        ("cancelled", "idle"),
        ("interrupted", "idle"),
    ),
)
def test_non_running_status_omits_wait_policy(
    execution_state: str, conversation_state: str
) -> None:
    status = dataclasses.replace(
        _running_status(),
        execution_state=execution_state,
        conversation_state=conversation_state,
    )

    assert "wait_policy" not in status.to_compact_dict()
    assert "wait_policy" not in status.to_dict()
```

- [ ] **Step 2: Run RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_contracts.py::test_running_status_tells_fresh_controllers_to_continue_waiting tests/unit/test_contracts.py::test_non_running_status_omits_wait_policy
```

Expected: the running assertion fails with missing key `wait_policy`; the non-running matrix passes.

- [ ] **Step 3: Implement the minimum serialization change**

In `src/subagent_harness_mcp/contracts.py`, add one constant beside the execution-state constants:

```python
RUNNING_WAIT_POLICY = "continue_while_running"
```

In both `AgentStatus.to_compact_dict()` and `AgentStatus.to_dict()`, add after the base payload is built:

```python
if self.execution_state == "running":
    payload["wait_policy"] = RUNNING_WAIT_POLICY
```

Do not add a dataclass field, persistence column, adapter hook, or schema-version change.

- [ ] **Step 4: Run GREEN**

Run the exact Step 2 command.

Expected: `8 passed` (one running test plus seven parameter cases).

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/subagent_harness_mcp/contracts.py tests/unit/test_contracts.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "feat: publish running wait policy"
```

### Task 2: Make local wait semantics explicit

**Files:**
- Modify: `tests/unit/test_service.py:1539-1560`
- Modify: `tests/unit/test_server.py:538-553`
- Modify: `src/subagent_harness_mcp/server.py:343-360`
- Modify: `README.md:95-121`

- [ ] **Step 1: Extend the existing service test before production edits**

Add these assertions to
`test_wait_timeout_returns_running_without_interrupting_agent`:

```python
assert waited.to_compact_dict()["wait_policy"] == "continue_while_running"
assert harness.call_count("spawn") == 1
assert harness.call_count("send") == 0
assert harness.call_count("interrupt") == 0
```

The test uses a 0.01-second local observation window; it must never wait on a
real provider.

- [ ] **Step 2: Write the failing tool-description test**

Replace the schema-only setup in
`test_lifecycle_tools_publish_compact_response_mode_and_long_local_wait` with:

```python
tools = {tool.name: tool for tool in _run(server.list_tools())}
schemas = {name: tool.input_schema for name, tool in tools.items()}

description = tools["agent_wait"].description or ""
assert "does not interrupt" in description
assert "returns running" in description
assert "continue waiting" in description
```

- [ ] **Step 3: Run RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_service.py::test_wait_timeout_returns_running_without_interrupting_agent tests/unit/test_server.py::test_lifecycle_tools_publish_compact_response_mode_and_long_local_wait
```

Expected: service projection passes after Task 1; server description fails because it does not yet say `returns running` or `continue waiting`.

- [ ] **Step 4: Change only the tool description**

Replace the `agent_wait` docstring in `src/subagent_harness_mcp/server.py` with:

```python
"""Wait locally up to four minutes. Expiry returns running, does not interrupt, and means continue waiting."""
```

Add a short README section after model priority:

```markdown
## Long-running work

`agent_wait` is a bounded local observation, not a model deadline. If it returns
`running` with `wait_policy=continue_while_running`, the external agent may still
be working or thinking. Keep observing the same conversation; elapsed time alone
never triggers retry, fallback, interruption, or another provider request.
```

- [ ] **Step 5: Run GREEN and commit Task 2**

Run the exact Step 3 command.

Expected: `2 passed`.

```powershell
git add src/subagent_harness_mcp/server.py README.md tests/unit/test_server.py tests/unit/test_service.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "docs: clarify long-running agent waits"
```

### Task 3: Prove the installed public surface through pipes

**Files:**
- Modify: `tests/integration/test_stdio_fake.py:107-219`
- Modify: `tests/integration/test_wheel_e2e.py:198-277`

- [ ] **Step 1: Add pipe-based stdio assertions**

In `_exercise_protocol`, after `send_meta` and `wait` are decoded, preserve both
existing deterministic branches and assert the policy only for the running one:

```python
wait_result = _meta(waited)["result"][0]
if expect_interrupt_success:
    assert send_meta["result"]["execution_state"] == "running"
    assert send_meta["result"]["wait_policy"] == "continue_while_running"
    assert wait_result["execution_state"] == "running"
    assert wait_result["wait_policy"] == "continue_while_running"
else:
    assert send_meta["result"]["execution_state"] == "succeeded"
    assert "wait_policy" not in send_meta["result"]
    assert "wait_policy" not in wait_result
```

Keep `mcp.client.stdio.stdio_client`; do not replace it with a terminal PTY or
raw `write_stdin` because public MCP stdio uses redirected pipes.

- [ ] **Step 2: Make the installed artifact smoke expose a running fake turn**

In the embedded server code in `_fake_stdio_smoke`, import `FakeHarness`, enqueue
one completed spawn and one running follow-up, and register the exact instance:

```python
from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness

harness = FakeHarness()
harness.enqueue("done", result="installed spawn complete")
harness.enqueue("running")
registry = AdapterRegistry(builtin_factories=(lambda: FakeAdapter(harness),))
```

After `follow_up` is decoded, add:

```python
assert follow_up["execution_state"] == "running"
assert follow_up["wait_policy"] == "continue_while_running"
tools_by_name = {tool.name: tool for tool in tools.tools}
wait_description = tools_by_name["agent_wait"].description or ""
assert "returns running" in wait_description
assert "does not interrupt" in wait_description
```

- [ ] **Step 3: Run focused integration GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_stdio_fake.py
```

Expected: every deterministic stdio test passes with no provider process.

- [ ] **Step 4: Commit Task 3**

```powershell
git add tests/integration/test_stdio_fake.py tests/integration/test_wheel_e2e.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "test: prove installed running wait contract"
```

### Task 4: Prepare and verify release 1.0.20

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `server.json`
- Modify: `src/subagent_harness_mcp/__init__.py`
- Modify: `tests/unit/test_package_contract.py`
- Modify: `tests/integration/test_wheel_e2e.py`
- Modify: `README.md`

- [ ] **Step 1: Update release identity consistently**

Change every current public `1.0.19` release pin to `1.0.20`, except historical
examples that intentionally demonstrate an older version. Add this changelog
entry:

```markdown
## 1.0.20 - 2026-08-25

- Tell fresh MCP controllers to continue waiting while an external execution is
  running; local wait expiry does not interrupt, retry, or trigger fallback.
```

Run:

```powershell
uv lock --offline
```

Expected: `uv.lock` resolves the project itself as `1.0.20` without changing
unrelated dependencies.

- [ ] **Step 2: Run focused and full safe verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_contracts.py tests/unit/test_server.py tests/unit/test_service.py tests/integration/test_stdio_fake.py
.\.venv\Scripts\python.exe -m pytest -m "not real_git_worktree"
git diff --check
```

Expected: zero failures; the full safe suite deselects only tests marked
`real_git_worktree`; `git diff --check` prints nothing.

- [ ] **Step 3: Build and test fresh installed artifacts**

Run in the approved release environment:

```powershell
uv build --out-dir dist
$env:SUBAGENT_MCP_TEST_DIST_DIR=(Resolve-Path dist)
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_wheel_e2e.py
Remove-Item Env:SUBAGENT_MCP_TEST_DIST_DIR
```

Expected: one wheel and one sdist install offline into fresh temporary virtual
environments; both pass pipe-based stdio, resources, UI, package identity, and
wait-policy checks outside the source tree.

- [ ] **Step 4: Run privacy/artifact checks and commit**

Run the existing package and release acceptance tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py
git diff --check
```

Inspect the staged diff and artifact member lists for user paths, credentials,
tokens, transcripts, private cache data, or account identifiers; expected:
none. Then commit using the user's identity:

```powershell
git add CHANGELOG.md README.md pyproject.toml uv.lock server.json src/subagent_harness_mcp/__init__.py tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "chore: prepare release 1.0.20"
```

- [ ] **Step 5: Publish only the verified commit**

Push the clean branch, wait for CI success, then run:

```powershell
gh workflow run release.yml -f tag=v1.0.20
```

Expected: PyPI, GitHub Release, and MCP Registry publish the same verified
version/checksums. Re-check PyPI metadata and a fresh exact
`uvx --isolated --from subagent-harness-mcp==1.0.20` tool listing. Do not call a
provider and do not enable credits/overage for this contract release.
