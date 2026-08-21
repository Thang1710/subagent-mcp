# Provider Quota Refresh Design

**Date:** 2026-08-21
**Status:** Approved by the user's explicit refresh and no-credit requirements

## Goal

Let a user explicitly refresh current provider availability from the localhost UI or `runtime_check`, while every managed spawn still rechecks the provider before accepting model output. Never infer availability from a fixed five-hour or weekly clock, and never continue through usage credits or API billing.

## Source of truth

Claude documents interactive allocation views but does not publish a stable machine-readable subscription-quota endpoint or a pre-request Agent SDK control method. Subagent MCP therefore does not parse terminal screen output, scrape Claude's browser UI, read private account files, or persist guessed reset times.

The supported programmatic evidence is Claude Agent SDK's `RateLimitEvent` emitted by the native Claude Code harness. This event is normally post-request evidence, not proof available before a provider request. A safe event must attest all of the following before any assistant output is accepted:

- primary status is `allowed` or `allowed_warning`;
- `raw.isUsingOverage` is exactly `false`;
- overage status is exactly `rejected`;
- no billing, credit, or rate error is reported.

Any missing, ambiguous, or overage evidence fails closed as `QUOTA_PAUSED`. An `allowed_warning` is safe only when the two independent no-overage fields above remain explicit. Authentication must remain first-party Claude subscription auth with no credential override environment variable.

## Explicit refresh flow

- Initial page load remains local and does not contact a model provider.
- Clicking **Refresh status** sends an authenticated, same-origin, CSRF-protected `POST /api/v1/refresh` with no body.
- The backend checks every configured runtime through `runtime_check(refresh_quota=True)`.
- The Claude adapter performs a bounded connect-only check with no query and no model invocation. If the native harness publishes no structured pre-request quota evidence, Refresh reports **Unknown**.
- A paused runtime is not restored by a live canary from the UI. It can recover only if a future documented connect-only surface supplies fresh safe evidence, or through the separately guarded runtime canary path.
- The response returns a new sanitized UI snapshot. It contains availability and action text, never credentials, raw provider events, prompts, output, or account identifiers.

Refreshing after a plan upgrade therefore uses current provider evidence rather than the previous plan's reset time.

## Spawn flow

UI evidence is advisory and may become stale immediately. `agent_spawn` and `agent_send` retain the existing mandatory lifecycle guard: they wait for a new `RateLimitEvent` and interrupt before assistant output if the no-overage conditions are not all true. A UI refresh never grants durable permission to spend.

## Billing boundary

Current Anthropic policy separates included Agent SDK credit from optional usage credits. Subagent MCP may consume only provider-included allowance available to the authenticated subscription. It never opts into, purchases, reloads, or consents to usage credits. If the included allowance is exhausted, the runtime becomes unavailable until a later explicit refresh succeeds.

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
- Safe refresh output contains `overage_blocked=true` and no provider output.
- Unsafe or ambiguous rate evidence interrupts before assistant output and reports quota paused.
- A paused circuit can recover only from fresh safe provider evidence.
- Spawn/send still recheck quota even after a successful refresh.
- UI refresh endpoint enforces loopback, host, origin, session, CSRF, empty body, response redaction, and size limits.
