# Compact External-Agent Supervision Design

**Date:** 2026-08-21
**Status:** Approved by the user's optimization request

## Problem

The measured Codex interval around one real Claude task added 11,972 goal tokens, but that interval also included multiple commentary turns, a large browser DOM snapshot, quota refresh, and result inspection. It is an upper bound rather than pure supervision cost. Even so, repeated model-driven polling and duplicated lifecycle payloads are unacceptable defaults.

The current MCP already keeps `agent_wait` polling inside the local Python process, so waiting itself does not need repeated Codex turns. The main avoidable token costs are short wait timeouts and full `AgentStatus` envelopes that repeat descriptors, paths, events, and terminal result text.

## Goal

Make the native path one concise delegation turn and one concise completion turn:

- no Codex model turns while the external runtime is merely working;
- one bounded local wait for ordinary tasks;
- compact status deltas by default;
- full diagnostics only on explicit request;
- never truncate or hide an error state needed for recovery.

## Compact response mode

Lifecycle tools that return `AgentStatus` accept `response_mode` with:

- `compact` — default;
- `full` — the current complete normalized envelope for diagnostics and advanced clients.

The compact projection contains only:

- `conversation_id`;
- `conversation_state`, so clients can observe logical close state;
- `execution_state` and `status`;
- `state_revision`;
- `next_event_cursor`;
- terminal `result`, when present;
- `needs_input`, when non-empty;
- `recovery_required`, only when true.

The projection omits workspace paths, descriptor repetition, external IDs, empty arrays, and terminal-result duplication inside events. Full mode preserves the complete contract.

The provider result remains capped by the existing adapter bound and is not summarized by a second paid model call. Prompting an external worker to return a concise final result remains the preferred task-level optimization.

## Waiting behavior

- Raise the default `agent_wait` timeout from 30 to 300 seconds, retaining the existing 300-second maximum.
- The local process may poll adapter/store state internally; this consumes no Codex tokens.
- Callers may pass `after_revision` and `after_cursor` to describe the state they already saw; cursors keep old history out of the response.
- A running revision alone does not wake the controller. The default wait returns for terminal state, required input, or timeout.
- A timeout returns one compact current snapshot. It must not fabricate progress or provider output.

## UI and quota separation

The localhost UI talks directly to the local MCP backend. Refreshing
quota/runtime status must not create a Codex or provider model turn. If a
provider cannot confirm entitlement safely without a response, the UI reports
unknown. A managed task validates exact native identity before its useful query
only where the native control surface publishes it. Claude binds its CLI,
subscription auth, credentials, and requested options first, then accepts output
only after exact stream identity and safe rate evidence from that same response.

No reset clock is hard-coded. A later requested task checks current provider
evidence, so plan upgrades become visible on its next native response.

## Measurement

Verification reports both:

- serialized byte size of compact versus full status for a terminal result;
- real Codex goal-token delta in a fresh Codex-to-MCP-to-Claude task when the MCP is loaded.

The deterministic acceptance target is:

- compact lifecycle metadata no larger than 2 KiB excluding `result.text`;
- no duplicated result text;
- one `agent_wait` call for a task completing within five minutes;
- full mode remains available and contract-complete.

The empirical token result is evidence, not a hard portable guarantee, because Codex model, context length, caching, task result size, and host behavior affect usage.

## Session measurement

The original monitored Claude task interval consumed 11,972 controller tokens and included repeated commentary, browser state, refresh, and result inspection. After compact-default lifecycle responses, one local wait, a narrow diff prompt, and one concise Claude result, the comparable successful interval consumed 5,654 controller tokens: 6,318 fewer, or about 52.8% lower. A stricter later safety probe consumed 4,810 controller tokens but was interrupted before returning a review result, so it is not used as the successful-task comparison.
