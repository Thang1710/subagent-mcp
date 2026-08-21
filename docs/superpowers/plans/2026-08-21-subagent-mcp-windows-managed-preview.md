# Subagent MCP Windows Managed Preview Implementation Plan

> **Execution:** Use `subagent-driven-development` or `executing-plans`, one writer per bounded task. Test once at the end of each task, then perform one fresh Critical-only review wave. Defer Important/Minor polish to the post-preview debt ledger.

**Goal:** Ship `subagent-harness-mcp==0.1.0a2` as an MIT-licensed Windows Managed Preview with a real stdio MCP, revisioned local state, a normalized adapter contract, a deterministic fake adapter, a capability-gated Claude Code managed adapter, and an opt-in localhost settings/activity UI.

**Architecture:** One Python package exposes CLI, stdio MCP, and localhost UI as thin surfaces over one `SubagentMcpService`. The service owns SQLite state, JSON config, idempotency, lifecycle transitions, redaction, circuits, and adapter selection. Adapters translate the common async contract to their native harness and never write shared state directly. Version 0.1.0a2 supports the managed transport on Windows only after its exact CLI/SDK/no-overage canary passes; visible-background, promotion, native Codex-panel injection, and other operating systems remain explicit capability gaps.

**Release principle:** Build the usable product before broad hardening. A pre-release blocker is only a direct Critical defect: any security defect; crash; core lifecycle unusable; duplicate external work; wrong session/model/workspace; severe UX break; credential/overage violation; data loss/corruption; destructive install/update/uninstall; or impact across most users. Everything else is recorded and shipped later.

**Tech stack:** Python 3.10+, standard library `sqlite3`/`http.server`, official MCP Python SDK v2 (`mcp>=2.0.0,<2.1`, lock resolves 2.0.0), `claude-agent-sdk==0.2.142`, package-owned static HTML/CSS/JavaScript, pytest 8, uv, PowerShell 5.1.

Official dependency baseline rechecked 2026-08-21:

- https://pypi.org/project/mcp/2.0.0/
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/index.md
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/first-steps.md

The official v2 docs require Python 3.10+, expose `MCPServer` from `mcp.server`, support stdio, and provide an in-memory client for deterministic server tests. Do not use the Node-based Inspector as a product or test dependency.

---

## Execution boundary and accepted gaps

- Phase 0a honestly remains `BLOCKED`: the final Group B run initialized but produced no terminal result and no exact `isUsingOverage=false` observation. Do not turn that unknown into PASS and do not repeat the same timeout experiment.
- The user's continuous end-to-end authority allows deterministic production implementation, isolated dependency resolution, package builds, temporary-home tests, and the final bounded canary/publication sequence without another chat approval. It does not enable usage credits or authorize mutation of unrelated Codex/Claude/AgentBridge state.
- Until the final canary passes, `claude-code` reports `needs_canary`; the deterministic fake adapter proves the whole local lifecycle without provider quota. A missing capability quarantines only that adapter/mode.
- `runtime_canary` is the sole launch path permitted from `needs_canary`, and only after the exact standalone CLI/SDK identity, subscription auth, credential-override absence, and current no-overage prerequisite are bound. Ordinary `agent_spawn`/`agent_send` stay blocked. A terminal canary atomically marks only that exact adapter pair ready when it attests `isUsingOverage=false` plus the exact requested/effective model, session, context, and cleanup; missing evidence remains `needs_canary`.
- Model IDs are bounded opaque provider-native strings. The common core has no Sonnet/Opus/provider model allowlist and no fallback. Claude effort is validated by the exact adapter/SDK schema; other adapters may publish different reasoning schemas.
- Production never selects the SDK-bundled Claude binary. Every Claude option set binds the execute-validated standalone CLI identity through explicit `cli_path`.
- No task changes global Codex/Claude settings, reads private daemon/transcript state, or enables usage credits. Registration uses only an official client command plus read-back and is performed at the final integration task.
- All deterministic tests set `SUBAGENT_MCP_HOME` to a pytest-temporary directory. They never touch real config/state/data roots.

## MVP lifecycle and support boundary

The preview must complete this path before publication:

```text
localhost UI/config -> shared service/SQLite <- stdio MCP -> managed adapter
                                      \-> deterministic fake adapter
```

The stdio MCP registers the exact public tool names from the design. Unsupported preview operations return a normalized `CAPABILITY_MISSING`; they are not omitted or silently downgraded. The minimum fully working lifecycle is `runtime_list/check`, `agent_spawn`, `agent_status`, `agent_send`, `agent_wait`, `agent_interrupt`, and `agent_close`. Configuration/trust/canary/release tools use the same service and explicit approval metadata.

## File ownership map

- `src/subagent_harness_mcp/contracts.py`: public normalized types and state transitions.
- `src/subagent_harness_mcp/paths.py`: OS roots and `SUBAGENT_MCP_HOME` override.
- `src/subagent_harness_mcp/config.py`: revisioned config, atomic replace, model/reasoning policy.
- `src/subagent_harness_mcp/store.py`: additive SQLite schema and idempotent transactions.
- `src/subagent_harness_mcp/service.py`: sole lifecycle/control-plane owner.
- `src/subagent_harness_mcp/adapters/base.py`: versioned async adapter protocol.
- `src/subagent_harness_mcp/adapters/registry.py`: built-in and entry-point discovery/quarantine.
- `src/subagent_harness_mcp/adapters/fake.py`: deterministic conformance adapter.
- `src/subagent_harness_mcp/adapters/claude_code.py`: managed Claude adapter using the reviewed SDK option builder.
- `src/subagent_harness_mcp/server.py`: thin official MCP SDK v2 adapter.
- `src/subagent_harness_mcp/ui.py`: loopback HTTP surface only.
- `src/subagent_harness_mcp/static/`: package-owned no-build UI assets.
- `src/subagent_harness_mcp/cli.py`: `serve`, `ui`, health, install/update/rollback/uninstall dry-run surfaces.
- `src/subagent_harness_mcp/launcher.py`: immutable runtime pointer and owned-resource journal.
- `schemas/`: versioned public config/adapter/tool schemas.
- `tests/unit/`, `tests/integration/`, `tests/fixtures/production/`: deterministic product verification.

---

### Task 0: Commit this release boundary

**Files:**
- Create: `docs/superpowers/plans/2026-08-21-subagent-mcp-windows-managed-preview.md`
- Modify: `docs/superpowers/specs/2026-08-17-subagent-mcp-design.md`

- [ ] Add one design amendment stating that deterministic common-core/fake/UI/package work may proceed while provider-live gates stay unavailable. Do not weaken any live acceptance claim.
- [ ] Run `git diff --check`.
- [ ] Commit: `docs: authorize Windows managed preview build`.

### Task 1: Create the publishable package spine

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `.gitignore`
- Create: `src/subagent_harness_mcp/__init__.py`, `src/subagent_harness_mcp/cli.py`, `src/subagent_harness_mcp/py.typed`
- Create: `README.md`, `LICENSE`, `SECURITY.md`, `CHANGELOG.md`, `tests/unit/test_package_contract.py`

- [ ] Convert the project to a real `src` package named `subagent-harness-mcp`, display name “Subagent MCP”, version `0.1.0a2`, MIT license, and script `subagent-harness-mcp = subagent_harness_mcp.cli:main`.
- [ ] Add `mcp>=2.0.0,<2.1` and exact `claude-agent-sdk==0.2.142`; resolve once, require lock version 2.0.0, add exactly `.preview/` to `.gitignore`, and use only `.preview/runtime/test` for product-task verification—never sync an existing user environment.
- [ ] Implement `--version` and placeholder command routing that fails with concise actionable errors, never a traceback for user mistakes.
- [ ] Test metadata, wheel contents, console entry point, and source/wheel import from isolated temporary environments.
- [ ] Verify once: focused package tests, `uv build`, wheel/sdist inspection, `git diff --check`.
- [ ] Commit: `build: create Subagent MCP preview package`.

### Task 2: Implement revisioned config and SQLite state

**Files:**
- Create: `src/subagent_harness_mcp/paths.py`, `config.py`, `store.py`
- Create: `schemas/config-v1.json`
- Create: `tests/unit/test_paths.py`, `test_config.py`, `test_store.py`

- [ ] Resolve Windows config/state/data roots without touching them at import; support only `SUBAGENT_MCP_HOME` as an override.
- [ ] Validate config schema version, runtime enablement, opaque model ID, adapter-defined reasoning object, and no fallback. Preserve unknown additive fields.
- [ ] Write config with stage/fsync/atomic replace and monotonic revision; malformed config is preserved and reported, never overwritten silently.
- [ ] Create additive SQLite tables for conversations, executions, requests, events, circuits, leases, and schema migrations. Enable foreign keys and WAL only after opening a product-owned DB.
- [ ] Enforce unique `(tool, request_id)` so a retried MCP call returns/resumes the recorded execution instead of duplicating work.
- [ ] Test concurrent request claims, crash rollback, corrupt config/DB handling, and temporary-home isolation.
- [ ] Commit: `feat: add revisioned local state`.

### Task 3: Implement normalized adapters and shared service

**Files:**
- Create: `src/subagent_harness_mcp/contracts.py`, `service.py`
- Create: `src/subagent_harness_mcp/adapters/{__init__,base,registry,fake}.py`
- Create: `schemas/adapter-v1.json`, `schemas/agent-descriptor-v1.json`
- Create: `tests/unit/test_contracts.py`, `test_registry.py`, `test_service.py`, `tests/integration/test_fake_lifecycle.py`

- [ ] Define versioned async `probe`, `resolve_context`, `spawn`, `send`, `snapshot`, `interrupt`, `close`, and `open_session` operations.
- [ ] Keep idempotency, persistence, redaction, event cursors, workspace leases, circuits, and final-result deduplication in the service only.
- [ ] Discover third-party factories only through `subagent_harness_mcp.adapters` entry points; conflict/import/manifest failures quarantine that adapter.
- [ ] Implement a deterministic fake adapter covering Done, Needs-input, failure, cancellation, follow-up, restart/resume, and exact model/workspace attestation.
- [ ] Prove the same normalized descriptor/status/result shape for every fake outcome and no raw thinking/credential/transcript persistence.
- [ ] Commit: `feat: add shared lifecycle service`.

### Task 4: Expose the exact stdio MCP contract

**Files:**
- Create: `src/subagent_harness_mcp/server.py`, `schemas/tools-v1.json`
- Modify: `src/subagent_harness_mcp/cli.py`
- Create: `tests/unit/test_server.py`, `tests/integration/test_stdio_fake.py`

- [ ] Build `MCPServer("Subagent MCP")` and register the exact 13 design tool names: `runtime_list`, `runtime_check`, `runtime_configure`, `runtime_canary`, `project_scan`, `project_trust`, `agent_spawn`, `agent_status`, `agent_send`, `agent_wait`, `agent_interrupt`, `agent_close`, `workspace_release`.
- [ ] Make handlers thin service calls. Require bounded `request_id` on side-effecting lifecycle methods and return text Markdown plus a fenced `subagent-mcp-meta` JSON block.
- [ ] Keep stdout protocol-only; diagnostics use stderr or bounded product-owned artifacts.
- [ ] Test tool discovery and a complete fake spawn/send/wait/interrupt/close sequence first in memory, then through a real stdio subprocess.
- [ ] Commit: `feat: expose Subagent MCP over stdio`.

### Task 5: Add the capability-gated Claude managed adapter

**Files:**
- Create: `src/subagent_harness_mcp/adapters/claude_code.py`
- Create: `tests/unit/test_claude_adapter.py`, `tests/integration/test_claude_fake_sdk.py`
- Reuse/adapt reviewed code from: `spikes/phase0b/sdk_options.py`, `transport_probe.py`, `policy_probe.py`

- [ ] Bind standalone CLI canonical identity/version/SHA, SDK 0.2.142, strict declared MCP, exact model, Claude effort schema, system-prompt preset, recursion denies, no credential override, and fallback disabled.
- [ ] Never instantiate the client during import/probe. `runtime_check` is no-model and returns `INSTALL_REQUIRED`, `AUTH_REQUIRED`, `needs_canary`, or `ready` without reading private auth/transcript files.
- [ ] Normalize stream envelopes without parsing assistant thinking. Require exact requested/effective model and terminal result; unknown fields remain bounded diagnostics.
- [ ] Ordinary lifecycle launch requires the exact adapter pair to have a current passing canary. Implement one separately routed `runtime_canary` bootstrap from `needs_canary`; it still requires bound no-model identity/auth/credential/no-overage preflight and can mark only that pair ready after a terminal `isUsingOverage=false` result with exact model/session/context and cleanup. It never guesses or silently switches transport/model.
- [ ] Test SDK behavior with a fake client/stream, including init timeout, terminal timeout, interrupt, resume, auth/model/quota errors, and cleanup on every branch.
- [ ] Commit: `feat: add Claude Code managed adapter`.

### Task 6: Build the localhost settings and activity UI

**Files:**
- Create: `src/subagent_harness_mcp/ui.py`
- Create: `src/subagent_harness_mcp/static/{index.html,app.css,app.js}`
- Modify: `src/subagent_harness_mcp/cli.py`
- Create: `tests/unit/test_ui_security.py`, `tests/integration/test_ui_service.py`

- [ ] `subagent-harness-mcp ui` binds only `127.0.0.1`/`::1` on an OS-assigned port, serves package assets, and opens the browser only after successful bind.
- [ ] Use a per-process bootstrap token in the URL fragment, one-time header exchange, HttpOnly/SameSite=Strict cookie, CSRF token, strict Host/Origin checks, restrictive CSP, no CORS, no token logs, and no non-loopback fallback.
- [ ] Show editable runtime/model/reasoning/context/trust settings plus health/circuit/update state and a read-only recent activity list. Do not expose prompts, transcripts, raw events, arbitrary files, chat, or lifecycle controls.
- [ ] Test valid browser flow and reject non-loopback peer, hostile Host/Origin, replayed bootstrap, missing CSRF, and path traversal.
- [ ] Commit: `feat: add localhost settings UI`.

### Task 7: Add Windows launcher and conservative lifecycle commands

**Files:**
- Create: `src/subagent_harness_mcp/launcher.py`, `src/subagent_harness_mcp/install.py`
- Create: `tests/unit/test_launcher.py`, `tests/integration/test_install_lifecycle.py`
- Modify: `src/subagent_harness_mcp/cli.py`

- [ ] Stage immutable versioned runtimes and atomically switch `current.json`; retain the prior runtime and byte-exact rollback pointer.
- [ ] Write the stable PowerShell launcher with UTF-8 BOM, fixed argv arrays, no task-text interpolation/eval, and an append-only ownership journal.
- [ ] Provide `install`, `update`, `rollback`, `register`, and `uninstall` with `--dry-run`. Uninstall removes only still-matching owned resources and preserves state, sessions, worktrees, and user configuration by default.
- [ ] Never target a process from PID alone; require PID plus creation identity and executable digest or return `RECOVERY_REQUIRED`.
- [ ] Test locked old runtimes, failed health, crash recovery, PID reuse, read-back mismatch, and idempotent repeated commands entirely in temporary roots.
- [ ] Commit: `feat: add safe Windows runtime lifecycle`.

### Task 8: Build and verify the preview artifact

**Files:**
- Create/complete: public schemas and docs required by `README.md`
- Create: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `docs/architecture.md`, `docs/adapter-authoring.md`, `docs/threat-model.md`
- Create: `.github/workflows/ci.yml`, `.github/workflows/release.yml`
- Create: `tests/integration/test_wheel_e2e.py`

- [ ] Run the full safe suite once, build wheel/sdist, install both into clean temporary environments, and complete fake lifecycle plus localhost UI HTTP smoke tests from the installed artifact.
- [ ] Verify package data, versioned schemas, no absolute machine paths/secrets/raw provider output, license metadata, entry point, and reproducible checksums.
- [ ] Run one Critical-only product review; fix at most one bounded Critical wave. Record other findings for post-preview.
- [ ] Commit: `release: prepare Windows managed preview`.

### Task 9: Run the final real canary, register, and publish

This is the only production-provider/live-publication task.

- [ ] Re-run no-model CLI identity/auth/preflight. Proceed only when the no-overage prerequisite can be attested without changing billing settings; otherwise do not launch and do not claim Claude-ready.
- [ ] Call the dedicated `runtime_canary` bootstrap while the exact pair is `needs_canary`. Require a terminal result with `isUsingOverage=false`, exact requested/effective model/session/context, and verified cleanup, then atomically transition that pair to `ready`. `agent_spawn`/`agent_send` remain unavailable until this succeeds.
- [ ] Install the built artifact into a fresh staged runtime, start the localhost UI, verify configuration/health/activity in a real browser, and stop it cleanly.
- [ ] Run one bounded fresh Codex -> registered stdio MCP -> Claude Code read-only review, then one write-capable disposable worktree task. Verify model/session/workspace identity, terminal result, cleanup, no duplicate work, no usage credits, and no mutation of existing config/cache/transcripts/processes.
- [ ] Exercise real Windows update, rollback, MCP restart/resume, and conservative uninstall-preserves-data.
- [ ] Save and commit the verified project at the controller-approved preservation path. Build an exact manifest before removing only Subagent MCP-owned installs, registrations, processes, runtimes, state, and caches from the test machine; preserve unrelated tools and the committed backup.
- [ ] With the local source checkout unavailable to the test environment, install as a new public user from the published GitHub/PyPI artifacts, then verify CLI, MCP registration, localhost UI, and one Codex -> Subagent MCP -> Claude Code end-to-end task.
- [ ] Publish matching `0.1.0a2` wheel/sdist and GitHub release with manifest/checksums only after all release-critical gates pass. Label unsupported transports/platforms/capabilities explicitly.
- [ ] Mark the overall goal complete only after installed-artifact E2E and publication are independently verified.

## Final verification command set

Run once after all deterministic tasks are stable:

~~~powershell
$previousPreviewEnv = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = ".preview/runtime/test"
    uv sync --frozen --group dev
    uv run --frozen pytest -p no:cacheprovider -q -m "not real_git_worktree"
    uv build
    git diff --check
    git status --short
} finally {
    if ($null -eq $previousPreviewEnv) {
        Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
    } else {
        $env:UV_PROJECT_ENVIRONMENT = $previousPreviewEnv
    }
}
~~~

The final report must separate deterministic PASS, real-host PASS, explicit capability gaps, and deferred debt. A plan, static inspection, fake fixture, or green unit suite alone is never reported as the finished product.
