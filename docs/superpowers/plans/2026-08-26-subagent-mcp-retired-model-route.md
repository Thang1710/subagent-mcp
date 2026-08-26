# Retired Provider Model Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an explicit provider-retired model route terminal and user-decision-gated without substituting another model.

**Architecture:** Add one bounded classifier in the DeepSeek adapter's existing terminal-error branch. Reuse the existing ACP provenance and public redaction pipeline; do not add state, catalog mutation, or fallback machinery.

**Tech Stack:** Python 3.10+, asyncio, pytest, existing Subagent MCP adapter/service contracts.

---

### Task 1: Prove the missing retirement taxonomy

**Files:**
- Modify: `tests/unit/test_deepseek_harness_adapter.py`

- [ ] **Step 1: Add the failing retirement regression**

Use the existing structured ACP failure client with a bounded detail containing
HTTP 404, `testing period`, and `Use it now`. Assert terminal
`CAPABILITY_MISSING`, provider category, `retryable is False`, a user-decision
next action, retained provenance, and exactly one prompt.

- [ ] **Step 2: Add the negative generic-404 control**

Feed `404: upstream endpoint returned no body` and assert it remains retryable
`PROVIDER_ERROR`. This prevents a broad status-code heuristic.

- [ ] **Step 3: Run RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_deepseek_harness_adapter.py -k "retired_model_route or generic_404" -q
```

Expected: the retirement regression fails because 1.0.21 returns
`PROVIDER_ERROR`/`retryable=true`; the negative control passes.

### Task 2: Implement the minimum classifier

**Files:**
- Modify: `src/subagent_harness_mcp/adapters/deepseek_harness.py`
- Test: `tests/unit/test_deepseek_harness_adapter.py`

- [ ] **Step 1: Add one compiled retirement pattern**

The pattern must require 404 plus explicit model-route retirement semantics. It
must not match a bare 404.

- [ ] **Step 2: Classify before generic provider failure**

Return `CAPABILITY_MISSING`, provider category, non-retryable state, and this
bounded policy:

```text
User decision required: wait for the exact configured route to return or
explicitly configure another route. Do not retry/reuse this turn, substitute a
model automatically, or change credits.
```

- [ ] **Step 3: Run GREEN and affected tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_deepseek_harness_adapter.py tests\unit\test_service.py -q
```

Expected: all tests pass.

### Task 3: Prepare and verify release 1.0.23

**Files:**
- Modify: `src/subagent_harness_mcp/__init__.py`
- Modify: `pyproject.toml`
- Modify: `server.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: version-pinned tests and release fixtures found by the repository version scan

- [ ] **Step 1: Change every package/server version marker to 1.0.23**

Update only current-version and current-upgrade examples; preserve historical
changelog entries.

- [ ] **Step 2: Document the no-substitution fix**

State that explicit model-route retirement is terminal and requires user
selection. Do not claim that OX Alpha was restored or that GLM-5.3-Flash is the
same selectable route.

- [ ] **Step 3: Run final safe gates**

Run focused tests, the repository's full safe suite with real git worktree tests
deselected, package artifact tests, compile/diff checks, and source/archive
privacy scans. No provider/canary or host configuration action is permitted.

- [ ] **Step 4: Commit with the repository owner's Git identity**

Create bounded docs, fix, and release-prep commits. Verify author and committer,
clean tracked tree, and exact canonical/public file equality.

- [ ] **Step 5: Publish through existing workflows**

Mirror to the public checkout, push public main plus annotated `v1.0.23`, wait
for deterministic CI, publish GitHub/PyPI artifacts, and publish MCP Registry.
Verify public version, hashes, non-yanked status, and fresh exact-version stdio
discovery without invoking a provider.

- [ ] **Step 6: Relay the exact outcome**

Report the 1.0.23 receipt to the source task. Its user has independently chosen
GPT-only implementation staffing, so do not retry OX Alpha or substitute the
revealed GLM route; the package repair remains a generic truthful-taxonomy fix.
