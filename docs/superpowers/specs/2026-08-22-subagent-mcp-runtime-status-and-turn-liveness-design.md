# Subagent MCP Runtime Status and Turn Liveness Design

## Problem

The Claude adapter currently treats one event ordering as a billing gate: a
task is rejected when model output arrives before the turn's
`rate_limit_event`, even if a preceding provider canary reported
`isUsingOverage=false` and an absent or rejected `overageStatus`. This both
spends provider quota and discards valid output. Missing or late status evidence
must not be converted into a durable claim that quota is exhausted.

DeepSeek Harness also applies a fixed 900-second product turn deadline. Real
tasks have completed close to that boundary, while longer tasks are cancelled
and recorded as recovery failures even though the native harness may still be
working normally. A fixed elapsed-time checkpoint is not provider status.

## Status authority

Runtime availability comes from a fresh native provider surface, never a
hard-coded reset hour, elapsed-time guess, cached browser value, or event-order
assumption.

For Claude Code 2.1.224 with the pinned Agent SDK, `connect()` completes the
control-protocol initialization, while exact stream `system/init` identity and
typed `RateLimitEvent` evidence are delivered only with a provider response.
Subagent MCP therefore separates no-model launch authorization from response
authorization instead of waiting for response-only events or treating silence
as quota exhaustion.

Before a model query, the adapter binds the standalone CLI/SDK pair, rejects
non-subscription credential routes, validates `claude.ai` auth, and constructs
the exact model/workspace/tool options. Before accepting the response, it
requires all of the following on that same connection:

- exact init identity for model, workspace, OAuth source, session, and empty
  MCP configuration;
- a provider `allowed` or `allowed_warning` status;
- `isUsingOverage=false`; and
- `overageStatus` is absent or `rejected`; explicit `allowed` or
  `allowed_warning` remains unsafe.

After control initialization, the lifecycle sends the one useful query. The
response loop accepts a rate event before or after stream initialization and
never accepts assistant output or a result until exact identity and safe rate
evidence have both been observed. Unsafe evidence interrupts the request and
discards its output. Explicit plan rejection, forbidden overage, and ambiguous
evidence remain distinct public states rather than sharing a fabricated quota
pause. A bounded connection/cleanup timeout remains an operational safety limit,
not a quota checkpoint.

Every Claude task uses one native connection and one useful model query; there
is no second model request for status. The localhost Refresh action remains
connect-only and never sends a model query. It returns **Unknown** immediately
after successful control initialization because the current SDK has no exact
pre-response quota surface. This absence does not pause a circuit.

An ordinary Claude turn has no product-imposed elapsed completion deadline.
Connection, initialization, query submission, interrupt, and disconnect remain
bounded, but silence while the native model is doing useful work is not a quota
or failure signal. Tests may inject a short turn timeout for cleanup paths.

## Circuit behavior

The durable circuit represents adapter compatibility and explicit provider
results, not a time schedule.

- A requested task is allowed to test an `auto_paused` exact variant. A safe
  task response reopens that variant immediately.
- Explicit terminal quota or overage evidence pauses only that variant.
- Missing, malformed, or unavailable status evidence reports **Unknown** for
  the current check and does not rewrite a ready circuit as exhausted.
- A later user request tries the exact requested variant again; Refresh checks
  initialization and reports `Unknown` when no pre-response rate event exists.
  No reset timestamp or five-hour/weekly checkpoint is synthesized.
- Existing model-priority demotion still occurs only after explicit terminal
  quota or credit exhaustion, never after an ambiguous transport failure.

The product never enables, purchases, reloads, or consents to Claude usage
credits. Authentication must remain subscription OAuth with API-key overrides
absent.

## DeepSeek turn liveness

The production DeepSeek adapter has no elapsed-time deadline for a model turn.
The ACP request remains active until one of these native lifecycle outcomes:

- `end_turn`;
- an explicit provider/quota error;
- native process failure;
- controller/user `session/cancel`; or
- exact process cleanup during close/disconnect recovery.

Initialization, cancellation, and process cleanup remain bounded. Tests may
inject a short optional turn timeout to exercise ambiguous cleanup paths, but
the shipped adapter default is `None` and therefore cannot cancel healthy work
at a fixed minute boundary.

## Release boundary

This is a Critical stability fix limited to Claude status authorization,
circuit recovery, Claude/DeepSeek turn liveness, and scoped writer ownership.
It does not add providers, change billing policy, enable overage, change global
configuration, or claim new native capabilities.

Deterministic tests must prove event-order independence, no query before control
initialization, no accepted output before exact stream identity plus safe rate
evidence, safe task-response recovery from a prior pause, ambiguous Refresh
non-persistence, explicit-quota persistence, and a DeepSeek turn completing
after the former deadline. Full safe and artifact suites run before release.
Live provider proof is a separate post-build gate and must not be replaced by
fixtures.
