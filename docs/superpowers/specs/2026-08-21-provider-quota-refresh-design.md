# Provider Quota Refresh Design

**Date:** 2026-08-21
**Status:** Approved by the user's explicit refresh and no-credit requirements

> **2026-08-22 measured amendment:** Claude Code 2.1.224 with Agent SDK
> 0.2.142 does not reliably publish exact rate evidence on a connect-only
> session. Refresh therefore reports `Unknown` after verified initialization
> when no pre-response event exists. A task uses its own useful provider
> response as the no-overage authority; it does not make a separate status
> request.

## Goal

Let a user explicitly refresh current provider availability from the localhost UI or `runtime_check`, while every managed spawn still rechecks the provider before accepting model output. Never infer availability from a fixed five-hour or weekly clock, and never continue through usage credits or API billing.

## Source of truth

Claude documents interactive allocation views but does not publish a stable machine-readable subscription-quota endpoint or a pre-request Agent SDK control method. Subagent MCP therefore does not parse private account/cache files, terminal screen output, or browser UI, and does not persist guessed reset times.

Subscription identity requires `claude auth status` to report `claude.ai`, no documented higher-precedence credential route to be present, and `system/init.apiKeySource` to be `none` or `oauth`.

Claude Agent SDK's live `RateLimitEvent` is the no-overage control contract. A
safe event must arrive before the task result is accepted and attest all of the
following:

- primary status is `allowed` or `allowed_warning`;
- `raw.isUsingOverage` is exactly `false`;
- overage status is absent or exactly `rejected`; an explicit `allowed` or
  `allowed_warning` remains unsafe;
- no billing, credit, or rate error is reported.

Missing or ambiguous identity/rate evidence never authorizes output. The
optional absence of `overageStatus` is not missing evidence when the same event
still reports exact `isUsingOverage=false`; the SDK schema makes that field
optional. An absent rate event on Refresh is reported as `Unknown`, not
fabricated, and does not rewrite the circuit. An explicit primary rejection is
`QUOTA_PAUSED`; available or active overage is `USAGE_CREDITS_FORBIDDEN`;
malformed or missing evidence is `CAPABILITY_MISSING` and never durably
rewrites a ready circuit as quota-exhausted.

## Explicit refresh flow

- Initial page load remains local and does not contact a model provider.
- Clicking **Refresh status** sends an authenticated, same-origin, CSRF-protected `POST /api/v1/refresh` with no body.
- The backend checks every configured runtime through `runtime_check(refresh_quota=True)`.
- The Claude adapter performs a bounded connect-only check with no query and no model invocation. If the native harness publishes no structured pre-request quota evidence, Refresh reports **Unknown**.
- A paused runtime is not restored by UI Refresh. A later explicitly requested
  task may test only its exact variant; its safe typed response restores that
  variant, while unsafe evidence leaves it paused.
- The response returns a new sanitized UI snapshot. It contains availability and action text, never credentials, raw provider events, prompts, output, or account identifiers.

Refreshing after a plan upgrade therefore uses current provider evidence rather than the previous plan's reset time.

## Spawn flow

UI evidence is advisory and may become stale immediately. `agent_spawn` and
`agent_send` recheck credential precedence and subscription auth before their
useful query, then require exact stream init identity plus a safe live rate
event from the same response before accepting output. They interrupt
immediately on unsafe evidence. A UI refresh never grants durable permission to
spend.

## Billing boundary

The Claude adapter may consume only provider-included allowance available to
the authenticated subscription. It never opts into, purchases, reloads, or
consents to usage credits. If authoritative no-overage evidence is unavailable,
the task output is rejected; connect-only Refresh remains `Unknown`.

## UI states

- **Available · overage blocked** — current provider event passed all no-overage checks.
- **Unavailable · quota paused** — the provider denied included usage or offered an unsafe path.
- **Check required** — configured runtime has no current provider evidence.
- **Configure a runtime first** — no variant exists to probe.
- **Unknown** — the adapter has no safe quota capability or probing failed without reliable provider evidence.

No countdown or reset timestamp is shown unless a future adapter publishes a documented value directly.

## Acceptance

- Page boot causes zero provider client creation.
- One Refresh click causes at most one bounded connect-only check per configured variant and zero model queries.
- Refresh contains no provider output; it reports `Unknown` when the native
  connect-only surface supplies no exact rate event.
- Unsafe or ambiguous hard-stop/identity evidence blocks output; any unsafe rate event interrupts and reports quota paused.
- A paused exact variant can recover only from fresh safe evidence on its own
  requested task response.
- Spawn/send require task-response quota evidence independently of Refresh and
  make no separate paid status request.
- UI refresh endpoint enforces loopback, host, origin, session, CSRF, empty body, response redaction, and size limits.
