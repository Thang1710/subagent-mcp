# Subagent MCP Windows Update Isolation Plan

> **Writer rule:** one bounded writer, TDD, no provider call, no global Codex
> config mutation, and no publish before root verification.

**Goal:** Prevent Windows MCP launcher locks from corrupting a normal public
update by documenting separate persistent UI and pinned uvx MCP environments.

### Task 1: Lock the public contract

- [x] Add a failing package-contract test for the pinned uvx Codex command,
  removal of the direct installed-tool MCP command, and explicit migration.
- [x] Update install, update, rollback, and architecture documentation with the
  two-environment boundary and fresh-task requirement.
- [x] Prove the exact uvx command starts the packaged MCP and returns 14 tools
  plus both runtime IDs without a provider call.

### Task 2: Release

- [x] Bump preview metadata and changelog.
- [ ] Run focused tests, full safe suite, artifact acceptance, one bounded
  Critical-only review, privacy scans, and exact canonical/public mirroring.
- [ ] Publish, install from PyPI, and verify UI plus the documented uvx stdio
  path. Do not alter the owner's current Codex global configuration.
