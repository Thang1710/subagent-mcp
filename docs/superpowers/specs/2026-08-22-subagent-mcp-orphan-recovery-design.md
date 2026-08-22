# Subagent MCP Orphan Recovery Design

## Problem

A connection-owned external runtime can lose its MCP controller while a turn is running. The native ACP process exits when that connection disappears, but the durable execution remains `running` because no controller is left to persist the terminal transition. A restarted MCP cannot resume the native session, so status, close, and the localhost activity UI remain stale.

## Decision

Add a small optional adapter capability for verifying that an orphaned connection-owned process is absent. On a refreshed status for a non-resumable active execution:

1. Try the existing native session reopen path.
2. If the adapter reports that restart resume is unavailable, ask the adapter to verify cleanup.
3. DeepSeek Harness verifies the exact native process identity using a read-only
   psutil inventory, its bound Node executable, ACP script, and the
   conversation-specific config path. It never matches by a broad process name
   and never terminates a process. The persisted context hash also lets the
   verifier reject harness-binding drift for legacy rows that do not store the
   pair key separately.
4. When no exact process exists, persist a terminal `CONTROLLER_DISCONNECTED` failure, release owned leases, and return the corrected status. The existing terminal connection-owned logical-close path may then close the conversation.
5. When an exact process survives, process inspection is unavailable, or identity is ambiguous, return `RECOVERY_REQUIRED` without changing execution state or releasing leases.

## Boundaries

- No automatic retry, provider invocation, process termination, database repair, or billing action.
- Existing resumable adapters and terminal connection-owned sessions keep their current behavior.
- The verifier is provider/harness-specific; the normalized service only consumes a boolean cleanup attestation.
- This recovery path also handles legacy DeepSeek rows because the exact config path is derived from the durable conversation ID.

## Verification

- RED integration test: restart an active non-resumable connection-owned session with cleanup confirmed; expect terminal failure, released lifecycle state, and logical close.
- RED integration test: cleanup unconfirmed; expect `RECOVERY_REQUIRED` and the durable row still `running`.
- DeepSeek unit tests: exact process match blocks recovery; no match confirms cleanup; opaque matching executable, inventory failure, and binding drift fail closed; a legacy context hash remains verifiable.
- Focused service/adapter/UI regression, then the complete safe suite and artifact acceptance before release.
