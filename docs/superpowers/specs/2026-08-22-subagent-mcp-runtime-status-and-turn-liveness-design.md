# Subagent MCP Runtime Status and Turn Liveness Design

## Problem

The Claude adapter currently treats one event ordering as a billing gate: a
task is rejected when model output arrives before the turn's
`rate_limit_event`, even if a preceding provider canary reported
`isUsingOverage=false` and `overageStatus=rejected`. This both spends provider
quota and discards valid output. Missing or late status evidence must not be
converted into a durable claim that quota is exhausted.

DeepSeek Harness also applies a fixed 900-second product turn deadline. Real
tasks have completed close to that boundary, while longer tasks are cancelled
and recorded as recovery failures even though the native harness may still be
working normally. A fixed elapsed-time checkpoint is not provider status.

## Status authority

Runtime availability comes from a fresh native provider surface, never a
hard-coded reset hour, elapsed-time guess, cached browser value, or event-order
assumption.

For Claude Code 2.1.224 with the pinned Agent SDK, the provider surface is the
typed `RateLimitEvent` delivered after a managed SDK connection. A safe start
requires all of the following before the model query is sent:

- exact init identity for model, workspace, OAuth source, session, and empty
  MCP configuration;
- a provider `allowed` or `allowed_warning` status;
- `isUsingOverage=false`; and
- `overageStatus=rejected`.

The startup loop accepts init and rate events in either order. It sends no
query while evidence is absent or unsafe. A bounded connection/cleanup timeout
remains an operational safety limit, not a quota checkpoint.

Every Claude task performs this startup preflight on the same native
connection immediately before its query. The localhost Refresh action performs
the existing connect-only probe and never sends a model query. Later rate
events update or stop the active turn when the provider explicitly reports an
unsafe condition; valid output is not rejected merely because an informational
event is delivered later in the stream.

## Circuit behavior

The durable circuit represents adapter compatibility and explicit provider
results, not a time schedule.

- A fresh safe provider probe reopens an `auto_paused` variant immediately.
- Explicit terminal quota or overage evidence pauses only that variant.
- Missing, malformed, or unavailable status evidence reports **Unknown** for
  the current check and does not rewrite a ready circuit as exhausted.
- A later user request or Refresh always probes again; no reset timestamp or
  five-hour/weekly checkpoint is synthesized.
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
circuit recovery, and DeepSeek turn liveness. It does not add providers,
change billing policy, enable overage, change global configuration, or claim
new native capabilities.

Deterministic tests must prove event-order independence, no query before safe
provider evidence, safe recovery from a prior pause, ambiguous-status
non-persistence, explicit-quota persistence, and a DeepSeek turn completing
after the former deadline. Full safe and artifact suites run before release.
Live provider proof is a separate post-build gate and must not be replaced by
fixtures.
