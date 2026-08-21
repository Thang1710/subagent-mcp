# Provider Quota Refresh Design

**Date:** 2026-08-21
**Status:** Approved by the user's explicit refresh and no-credit requirements

## Goal

Let a user explicitly refresh current provider availability from the localhost UI or `runtime_check`, while every managed spawn still rechecks the provider before accepting model output. Never infer availability from a fixed five-hour or weekly clock, and never continue through usage credits or API billing.

## Source of truth

Claude documents interactive allocation views but does not publish a stable machine-readable subscription-quota endpoint or a pre-request Agent SDK control method. Subagent MCP therefore does not parse private account/cache files, terminal screen output, or browser UI, and does not persist guessed reset times.

Subscription identity requires `claude auth status` to report `claude.ai`, no documented higher-precedence credential route to be present, and `system/init.apiKeySource` to be `none` or `oauth`.

Claude Agent SDK's live `RateLimitEvent` is the no-overage control contract. A safe event must arrive before assistant output and attest all of the following:

- primary status is `allowed` or `allowed_warning`;
- `raw.isUsingOverage` is exactly `false`;
- overage status is exactly `rejected`;
- no billing, credit, or rate error is reported.

Missing or ambiguous identity/rate evidence fails closed before output. An absent rate event is reported as absent, not fabricated, and leaves the runtime gated. Any unsafe event fails closed as `QUOTA_PAUSED`.

## Explicit refresh flow

- Initial page load remains local and does not contact a model provider.
- Clicking **Refresh status** sends an authenticated, same-origin, CSRF-protected `POST /api/v1/refresh` with no body.
- The backend checks every configured runtime through `runtime_check(refresh_quota=True)`.
- The Claude adapter performs a bounded connect-only check with no query and no model invocation. If the native harness publishes no structured pre-request quota evidence, Refresh reports **Unknown**.
- A paused runtime is not restored by a live canary from the UI. It can recover only if a future documented connect-only surface supplies fresh safe evidence, or through the separately guarded runtime canary path.
- The response returns a new sanitized UI snapshot. It contains availability and action text, never credentials, raw provider events, prompts, output, or account identifiers.

Refreshing after a plan upgrade therefore uses current provider evidence rather than the previous plan's reset time.

## Spawn flow

UI evidence is advisory and may become stale immediately. `agent_spawn` and `agent_send` recheck credential precedence and require fresh subscription init identity plus a safe live rate event before accepting output. They interrupt immediately on unsafe evidence. A UI refresh never grants durable permission to spend.

## Billing boundary

The Claude adapter may consume only provider-included allowance available to the authenticated subscription. It never opts into, purchases, reloads, or consents to usage credits. If authoritative no-overage evidence is unavailable, the runtime stays unavailable.

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
- Unsafe or ambiguous hard-stop/identity evidence blocks output; any unsafe rate event interrupts and reports quota paused.
- A paused circuit can recover only from fresh safe provider evidence.
- Spawn/send still recheck quota even after a successful refresh.
- UI refresh endpoint enforces loopback, host, origin, session, CSRF, empty body, response redaction, and size limits.
