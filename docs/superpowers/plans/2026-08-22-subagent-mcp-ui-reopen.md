# Subagent MCP Managed UI Reopen Plan

> **Writer rule:** one bounded writer, TDD, standard library only, no provider
> invocation, no config/state mutation, one Critical-only review wave.

**Goal:** Safely reopen an already-running background UI from one CLI command.

### Task 1: Authenticated bootstrap rotation

- [x] Add RED tests for control-authenticated bootstrap issuance, exact
  loopback URL shape, single use, replay rejection, wrong token, and no-control
  rejection.
- [x] Add the smallest atomic bootstrap rotation method and one control endpoint
  beside the existing stop endpoint.

### Task 2: CLI open command

- [x] Add RED tests for control-record/port/probe/response validation, browser
  refusal, unmanaged UI, and token-free terminal output.
- [x] Implement `ui --open` with bounded stdlib HTTP and direct browser handoff.
- [x] Document the fixed-port background/start/status/open/stop flow.

### Task 3: Verify and release a21

- [x] Run focused UI/CLI/security tests, full safe suite, artifact acceptance,
  JavaScript/Python/diff/privacy checks, and one bounded Critical-only review.
- [ ] Commit/mirror with the owner's exact identity, publish GitHub/PyPI a21,
  reinstall, and prove `ui --open` in Chrome without exposing its token.
