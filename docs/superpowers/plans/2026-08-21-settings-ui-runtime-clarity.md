# Settings UI Runtime Clarity Implementation Plan

> Execute test-first in the existing `phase0a-contract-hardening` worktree. Keep model identifiers in adapter manifests, not frontend code.

**Goal:** Make the localhost UI show only real runtimes and plain controls for model, reasoning, status, and availability to Codex.

**Architecture:** Extend the provider-neutral manifest with optional model suggestions, project manifest schemas into generic UI field definitions, persist nested reasoning fields into the existing config shape, and render suggestions with a native select plus a conditional custom-ID input.

**Tech stack:** Python 3.10+, vanilla HTML/CSS/JavaScript, MCP adapter contracts, pytest.

---

## Task 1: Publish provider-owned model choices

**Files:**
- Modify: `tests/unit/test_contracts.py`
- Modify: `src/subagent_harness_mcp/contracts.py`
- Modify: `schemas/adapter-v1.json`
- Modify: `src/subagent_harness_mcp/adapters/claude_code.py`

1. Add failing assertions for optional `model_schema` serialization and Claude canonical suggestions.
2. Run focused contract/adapter tests and confirm RED.
3. Add the optional manifest field and Claude schema for Opus 5, Sonnet 5, Fable 5, plus custom exact IDs.
4. Re-run focused tests to GREEN.

## Task 2: Project a truthful production runtime card

**Files:**
- Modify: `tests/integration/test_ui_service.py`
- Modify: `src/subagent_harness_mcp/ui.py`

1. Change the fresh-backend test to require only `claude-code`, friendly subtitle/help, model suggestions, reasoning select, and no transport/selection/raw-JSON fields.
2. Require saving only model and nested reasoning effort to persist the existing policy shape.
3. Run the focused UI integration test and confirm RED.
4. Remove `FakeAdapter` from `create_local_backend()`, add generic schema projection, infer single transport/variant values, and validate nested reasoning enum values.
5. Re-run focused integration tests to GREEN.

## Task 3: Remove meaningless frontend content

**Files:**
- Modify: `tests/unit/test_ui_security.py`
- Modify: `src/subagent_harness_mcp/static/index.html`
- Modify: `src/subagent_harness_mcp/static/app.js`
- Modify: `src/subagent_harness_mcp/static/app.css`

1. Add failing static assertions for `Refresh status`, hidden revision/update/circuits defaults, native model suggestions, capability disclosure, and plain availability help.
2. Run focused UI security tests and confirm RED.
3. Implement native select/custom-input/details rendering; hide empty safety stops and never-checked updates; remove visible revision and internal transport text.
4. Keep CSP, CSRF, loopback, no-storage, no-telemetry, keyboard, and screen-reader behavior unchanged.
5. Re-run focused UI tests to GREEN.

## Task 4: Verify and preview

1. Run all UI/contract/adapter focused tests, then the full safe suite excluding real Git worktree mutations.
2. Build and install the next alpha artifact without changing global Codex/Claude configuration.
3. Restart only the Subagent MCP localhost preview process and open the fresh UI in the user's Chrome session.
4. Verify visually that the card shows Claude only, model suggestions, reasoning select, clear help, and no fake/runtime internals.
5. Security-scan the diff and artifact; commit with the user's Git identity.
