# Subagent MCP Runtime Status and Turn Liveness Plan

> **Writer rule:** one bounded writer, TDD, no Grok/Cursor/Qwen work, no live
> provider call during implementation, and no weakening of the no-overage or
> process-identity boundaries.

**Goal:** Restore reliable Claude and DeepSeek delegation by replacing
hard-coded blocking checkpoints with fresh provider status and native lifecycle
outcomes.

### Task 1: Make Claude task-response status authoritative

- [x] Add failing tests where init and safe `RateLimitEvent` arrive in either
  order and where the rate event follows the useful query.
- [x] Add failing tests proving no model query is sent before native control
  initialization and no output is accepted without exact stream identity plus
  safe response evidence.
- [x] Remove the event-order checkpoint and validate every provider rate event
  on the task's existing native connection.
- [x] Remove the ordinary Claude turn completion deadline while keeping native
  setup, query submission, interrupt, and cleanup bounded.

### Task 2: Make pause recovery request-driven

- [x] Add failing service tests for safe `auto_paused -> ready` recovery from
  the exact requested task's safe response.
- [x] Prove ambiguous status reports Unknown without pausing a ready circuit,
  while explicit quota/overage evidence still pauses and demotes only the exact
  variant.
- [x] Keep localhost Refresh connect-only and allow a user task to recover its
  exact paused variant; do not introduce a reset clock or freshness TTL.

### Task 3: Remove the DeepSeek product turn deadline

- [x] Add a failing test where a turn remains active beyond an injected former
  deadline and later completes normally.
- [x] Make the production turn timeout optional with a `None` default; retain
  bounded initialize/cancel/close behavior and injectable timeout cleanup tests.
- [x] Verify interrupt, provider failure, quota classification, controller
  disconnect, and orphan recovery regressions.

### Task 4: Verify and release

- [x] Update architecture, runtime-status help, changelog, and preview version
  without exposing private state or claiming unrun live evidence.
- [x] Run focused tests, the full safe suite, artifact acceptance, privacy scan,
  and one bounded Critical-only review.
- [ ] Publish the next alpha, reinstall from PyPI, verify deterministic stdio
  plus localhost UI, then run separately approved real Claude and DeepSeek
  lifecycle checks without enabling or purchasing usage credits.
