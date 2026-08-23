# Concurrent writer scopes

**Status:** Approved by the user on 2026-08-22 as a Critical core-usability fix.

## Problem

Subagent MCP currently stores one active writer lease against the canonical
workspace. A long-running OX Alpha task in one lane therefore prevents Claude or
another runtime from editing a disjoint lane. This defeats the product's primary
parallel-delegation use case.

## Contract

`agent_spawn` accepts an optional `write_set` containing 1-32
repository-relative path prefixes. `write_set` is meaningful only with the
`workspace_write` capability. Omitting it preserves compatibility by declaring
the whole workspace (`.`).

The service canonicalizes separators and dot segments, rejects absolute paths,
parent traversal, empty/NUL values, and paths that resolve outside the verified
workspace. Nested entries in one request collapse to the least redundant set.
Windows comparison is case-insensitive; POSIX comparison is case-sensitive.

The store resolves each declared root to a canonical absolute comparison key.
Two active writers conflict only when a root is equal to, an ancestor of, or a
descendant of a root in the other set, even when callers declare different or
nested workspace roots. Acquisition of the entire set occurs in one SQLite
write transaction. Every execution keeps its own lease rows for ownership,
terminal release, and recovery. Terminal idempotent replay treats its matching
released deterministic row as a no-op. Pre-scoped whole-workspace rows remain
evidence but do not block a new scoped lease; an active v2 scoped row blocks new
scoped writers conservatively until its owning execution finishes.

`write_set` is persisted in the idempotency digest, requested metadata, and
adapter context attestation. `agent_send` reuses the original set. A display lane
or task title is never used as a path or collision key.

## Adapter enforcement

- Claude Code installs an SDK `PreToolUse` hook over `Edit|Write`. It resolves the
  native tool path against the attested workspace and denies paths outside every
  declared root before the tool executes.
- DeepSeek Harness currently exposes one native writable workspace root per ACP
  session. It therefore accepts one normalized directory root per writer and
  supplies that exact directory as the native session `cwd`; multiple disjoint
  roots return `CAPABILITY_MISSING` instead of being widened. The repository root
  remains explicit in the task prompt for read/test context.
- Every manifest publishes a generic `max_write_roots_per_session` bound
  (positive, at most 32; safe default 1). The shared service rejects a writable
  request whose normalized root count exceeds the selected adapter's bound
  before readiness probing, idempotency, execution creation, leases, or provider
  work. The rejection stays `CAPABILITY_MISSING`, non-retryable, category
  `capability`, and carries a machine-readable recovery directive
  (`action: repair`, `reason: decompose_write_set`, `max_attempts: 3`,
  `max_write_roots_per_session`) so the controller decomposes the task into
  multiple independent non-overlapping writer calls within the limit instead of
  silently abandoning the error.
- Today's DeepSeek native session remains one root. Multi-root work is expressed
  as several disjoint writer calls. True single-session multi-root is reserved
  for a future harness that advertises official ACP `additionalDirectories` and
  enforces it natively.
- Read-only executions have an empty write set and acquire no writer lease.

Adapters may later publish a richer write-set capability, but the shared
collision semantics do not change.

## Failure and compatibility behavior

- Actual overlap: `WRITE_SET_BUSY`, retryable.
- Retryable pre-provider contention carries `action: retry`, reason
  `transient_pre_provider`, and `max_attempts: 3`. An idempotent transport replay
  keeps the original request ID and never reacquires a released lease; a
  deliberate new execution after the blocker clears uses a new request ID.
- Invalid or escaping path: `REQUEST_INVALID` before adapter launch.
- Adapter cannot enforce the shape: `CAPABILITY_MISSING` before model query.
- A legacy client omitting `write_set`: whole-workspace exclusivity.
- Terminal/cancelled/interrupted executions release all their scoped leases by
  execution ID exactly as before.

No provider status, quota checkpoint, reset time, or model call participates in
writer scheduling.

## Acceptance

1. Two running executions can acquire disjoint roots in the same workspace and
   both adapters launch.
2. Equal, parent/child, and whole-workspace overlap is rejected before adapter
   launch.
3. Path traversal and absolute paths are rejected.
4. Idempotent replay does not duplicate leases or model work.
5. Claude path gating and DeepSeek single-root native cwd are covered by
   deterministic adapter tests.
6. Nested workspace roots cannot bypass equal/ancestor/descendant conflicts.
7. Existing terminal release/recovery tests remain green.
