# Subagent MCP 1.0.7 Bounded Quota Recovery Implementation Plan

> Execute with TDD, one implementation writer, one bounded independent review
> wave, and verification before release.

**Goal:** Preserve explicit provider quota truth across concurrent local state
writers, prevent refresh-induced permanent recovery state, and keep all safe
retry/refresh/repair work bounded to three without repeating provider tasks.

**Architecture:** The adapter remains the no-overage enforcement boundary. The
service retries only an idempotent local circuit pause up to three times and
never converts its failure into provider cleanup ambiguity. Connect-only quota
refresh remains status-only. Legacy update quarantine remains terminal; exact
isolated uvx registration is unchanged.

**Tech stack:** Python 3.10+, asyncio, SQLite/WAL, pytest, MCP stdio, Claude Agent
SDK, DeepSeek Harness ACP, uv/uvx.

## Task 1: Lock the verified failures with RED tests

Files:

- Modify: `tests/unit/test_service.py`
- Modify: `tests/unit/test_claude_adapter.py`
- Modify: `tests/integration/test_claude_fake_sdk.py` only if the existing fake
  exposes the exact foreground path without broad fixture changes.

Add deterministic tests for:

1. a concurrent `ready -> recovery_required` transition before a real quota
   exception, asserting the original quota code and released leases;
2. two transient SQLite/state pause failures followed by success, asserting
   exactly three local attempts and one provider launch;
3. three failed local attempts, asserting the original quota code plus state
   warning and no retained writer lease;
4. connect-only quota-refresh disconnect ambiguity, asserting
   `CAPABILITY_MISSING`, zero query calls, and an unchanged ready circuit;
5. cancellation is not swallowed by the local persistence helper.

Run focused tests and capture the expected RED failures before production code.

## Task 2: Implement the minimal service and adapter correction

Files:

- Modify: `src/subagent_harness_mcp/service.py`
- Modify: `src/subagent_harness_mcp/adapters/claude_code.py`

Implement one bounded local persistence helper using
`RECOVERY_MAX_ATTEMPTS`. Preserve the original terminal provider code on every
state outcome. Add only a sanitized warning/next action when all local attempts
fail. Narrow exception handling to state/database failures.

Change only the connect-only quota probe's disconnect failure to
`CAPABILITY_MISSING` and state explicitly that no model task was sent. Do not
change real task/canary cleanup handling or no-overage parsing.

Run the RED tests to GREEN, then all service/store/Claude integration tests.

## Task 3: Align public contract and version metadata

Files:

- Modify: `src/subagent_harness_mcp/server.py`
- Modify: `docs/superpowers/specs/2026-08-17-subagent-mcp-design.md`
- Modify: `docs/architecture.md`
- Modify: `README.md` only where the update/recovery wording is stale
- Modify: version/release metadata required by the package contract

Clarify that three is a ceiling, not a requirement to retry terminal errors;
local state reconciliation never replays provider work; an already-quarantined
resident cannot hot-reload. Keep the exact isolated uvx registration and do not
edit the owner's Codex configuration.

## Task 4: Verify and review once

Run:

- focused unit/integration tests for service, store, Claude adapter, server, UI,
  package contract, and artifact packaging;
- the full safe suite with live/provider and real-worktree gates deselected;
- `git diff --check` and tracked privacy/secret/path scans.

Ask Claude Opus 5 and OX Alpha for one read-only Critical/repeated-Major review
of the exact diff. Main independently validates findings. Apply at most one
bounded correction wave; defer minor polish.

## Task 5: Live proof and release

Build the exact wheel/sdist. Start the wheel through isolated uvx. Run one
harmless read-only Claude Opus 5 task and one OX Alpha task; verify native model,
harness, terminal result, circuit state, cleanup, zero credit enablement, and no
duplicate provider calls.

Send the affected Codex task a concise smoke instruction that uses a fresh
immutable binding; do not retry its already-quarantined resident. Publish 1.0.7
to GitHub, PyPI, and MCP Registry only after all gates pass, then read every
public surface back.
