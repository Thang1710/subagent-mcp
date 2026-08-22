# Subagent MCP Runtime Status and Turn Liveness Plan

> **Writer rule:** one bounded writer, TDD, no Grok/Cursor/Qwen work, no live
> provider call during implementation, and no weakening of the no-overage or
> process-identity boundaries.

**Goal:** Restore reliable Claude and DeepSeek delegation by replacing
hard-coded blocking checkpoints with fresh provider status and native lifecycle
outcomes.

### Task 1: Make Claude startup status authoritative

- [ ] Add failing tests where init and safe `RateLimitEvent` arrive in either
  order before query, and where assistant output later arrives before another
  informational rate event.
- [ ] Add failing tests proving no model query is sent when provider status is
  absent or unsafe.
- [ ] Move the startup guard before `client.query`, remove the 0.5-second quota
  checkpoint, and continue validating every later provider rate event.

### Task 2: Make pause recovery request-driven

- [ ] Add failing store/service tests for safe `auto_paused -> ready` recovery
  from a fresh connect-only provider probe.
- [ ] Prove ambiguous status reports Unknown without pausing a ready circuit,
  while explicit quota/overage evidence still pauses and demotes only the exact
  variant.
- [ ] Apply the same recovery behavior to a user task and localhost Refresh;
  do not introduce a reset clock or freshness TTL.

### Task 3: Remove the DeepSeek product turn deadline

- [ ] Add a failing test where a turn remains active beyond an injected former
  deadline and later completes normally.
- [ ] Make the production turn timeout optional with a `None` default; retain
  bounded initialize/cancel/close behavior and injectable timeout cleanup tests.
- [ ] Verify interrupt, provider failure, quota classification, controller
  disconnect, and orphan recovery regressions.

### Task 4: Verify and release

- [ ] Update architecture, runtime-status help, changelog, and preview version
  without exposing private state or claiming unrun live evidence.
- [ ] Run focused tests, the full safe suite, artifact acceptance, privacy scan,
  and one bounded Critical-only review.
- [ ] Publish the next alpha, reinstall from PyPI, verify deterministic stdio
  plus localhost UI, then run separately approved real Claude and DeepSeek
  lifecycle checks without enabling or purchasing usage credits.
