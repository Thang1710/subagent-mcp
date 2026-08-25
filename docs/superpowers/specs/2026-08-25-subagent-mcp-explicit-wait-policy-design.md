# Subagent MCP Explicit Wait Policy Design

**Status:** Written design approved by the user on 2026-08-25.

## Problem

The runtime adapters already impose no elapsed completion deadline on an
ordinary model turn. `agent_wait` also returns `running` without interrupting
the external agent when its local observation window expires. Those semantics
are documented, but the compact MCP status returned to a fresh controller does
not state the required next action.

A controller that has not read the repository documentation can therefore
misclassify a long quiet turn as a timeout, retry it, fall back to another
model, or interrupt healthy provider work. Chat instructions on one machine or
in one Codex task are not a portable contract.

## Decision

Make the existing behavior self-describing on the public MCP wire contract.
When an execution is `running`, compact and full `AgentStatus` payloads include:

```json
{"wait_policy":"continue_while_running"}
```

The field means:

- elapsed time and a local `agent_wait` timeout are not provider failures;
- the controller should continue observing the same conversation;
- it must not retry, duplicate, switch model, fall back, or interrupt solely
  because the turn is quiet or long-running; and
- only a native terminal outcome, `needs_input`, explicit user cancellation,
  verified process/session loss, or recovery failure changes that behavior.

The field is omitted for `queued`, `starting`, `needs_input`, and terminal
states. Startup remains governed by bounded initialization/handshake cleanup,
which is distinct from an already-running model turn.

`agent_wait` remains a bounded local read. Its tool description explicitly
states that expiry returns the latest `running` status and does not interrupt
or classify the provider turn as failed. `agent_status` exposes the same status
shape.

## Compatibility and token cost

This is one additive string field in an existing JSON object. Older clients
ignore it. The contract schema version and persistence schema do not change.
No database column, migration, adapter API, dependency, background heartbeat,
or provider-specific branch is added.

The field is intentionally short and appears only while running. It avoids a
larger policy object and does not expose provider thought text, hidden thinking,
raw events, prompts, transcripts, or billing data.

## Quota and billing

`quota=unknown` remains non-blocking. A later successful explicit turn confirms
availability; only explicit terminal provider evidence can produce a quota
pause. The wait policy never enables, buys, reloads, or consents to usage
credits or overage.

## Fresh-user behavior

The behavior must come from the installed distribution, not local files or
machine-specific state. A fresh user receives it through both:

1. the `agent_wait` tool description published by a newly initialized MCP
   server; and
2. the `wait_policy` field in compact/full status responses.

No user path, account identifier, local port, provider cache, private log, or
existing Subagent MCP database is required to understand the rule.

## Verification

TDD implementation must prove:

1. A running compact status contains
   `wait_policy="continue_while_running"`.
2. A running full status contains the same field.
3. Queued, starting, needs-input, and terminal statuses omit it.
4. An `agent_wait` observation timeout returns the same running execution and
   does not call adapter interrupt, spawn, send, or fallback paths.
5. The public tool description explains that a local wait timeout is not a
   provider failure.
6. Deterministic stdio MCP integration returns the field without provider work.
7. A wheel installed in an isolated temporary environment publishes the same
   tool description and status contract without relying on this development
   checkout or the author's machine state.
8. Focused tests, the full safe suite, package/artifact checks, privacy scan,
   and `git diff --check` pass before release.

Live Claude/OX work is not required to prove this additive controller contract
and must not be fabricated with fixtures. Any separately approved live proof
keeps credits/overage disabled and does not interrupt an already-running turn.

## Scope

In scope: common status serialization, the public `agent_wait` description,
focused documentation, deterministic tests, and isolated-wheel verification.

Out of scope: new providers, heartbeat polling, provider transcript access,
hidden-thinking display, new timeouts, automatic retries, model fallback,
process killing, billing changes, or host configuration changes.
