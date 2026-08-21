# Compact External-Agent Supervision Implementation Plan

> Execute test-first in the existing `phase0a-contract-hardening` worktree. Keep the public lifecycle contract provider-neutral and preserve full diagnostics as an explicit opt-in.

**Goal:** Reduce Codex supervision overhead to one local wait and a compact lifecycle envelope without summarizing or truncating provider output.

**Architecture:** `AgentStatus` owns a lossless compact projection. MCP lifecycle tools select `compact` by default or `full` explicitly, while the service and adapter contracts remain unchanged. `agent_wait` polls locally for up to five minutes by default.

**Tech stack:** Python 3.11+, MCP Python SDK v2, pytest.

---

## Task 1: Specify compact status projection

**Files:**
- Modify: `tests/unit/test_contracts.py`
- Modify: `src/subagent_harness_mcp/contracts.py`

1. Add a terminal `AgentStatus` fixture whose event payload repeats the terminal result.
2. Assert `to_compact_dict()` returns only conversation/state cursors, one terminal result, non-empty input, and true recovery state.
3. Assert empty/false optional fields disappear and serialized metadata excluding result text is at most 2 KiB.
4. Run `pytest -q tests/unit/test_contracts.py` and confirm the new test fails because the compact projection does not exist.
5. Implement the smallest `to_compact_dict()` method and change `WaitRequest.timeout_seconds` default to `300.0`.
6. Re-run the focused test to green.

## Task 2: Make lifecycle MCP responses compact by default

**Files:**
- Modify: `tests/unit/test_server.py`
- Modify: `src/subagent_harness_mcp/server.py`
- Modify: `schemas/tools-v1.json`

1. Add tests proving all status-returning lifecycle tools expose `response_mode`, default to compact, accept explicit full, reject unknown modes before a service call, and expose a 300-second wait default.
2. Assert a real terminal `AgentStatus` has no descriptor, paths, events, or duplicated result in default MCP metadata, while `full` preserves the existing envelope.
3. Run `pytest -q tests/unit/test_server.py` and confirm failures are caused by missing response-mode support and the old timeout.
4. Add one response-mode validator and one recursive JSON projection path; do not change service or adapter behavior.
5. Update public tool descriptions to document compact-default/full-opt-in behavior.
6. Re-run `pytest -q tests/unit/test_server.py tests/unit/test_contracts.py` to green.

## Task 3: Review and verify

**Files:**
- Review only the bounded diff from Tasks 1-2.

1. Re-check Claude subscription auth and that usage credits remain off.
2. Ask Claude Code Opus 5 at xhigh effort for one bounded correctness/compatibility review; do not poll it with model turns.
3. Independently accept only Critical findings: security, crash, core lifecycle failure, severe data loss/corruption, broad unusability, or release/build blocker.
4. If a Critical finding is valid, reproduce it with one failing test and apply one bounded fix. Do not enter a review loop.
5. Run focused tests, then the repository safe suite specified by project authority.
6. Measure compact/full serialized bytes and record exact test counts.
7. Commit with the user's configured Git identity after confirming the diff contains no private paths, tokens, auth material, or internal project names.

