# Provider Quota Refresh Implementation Plan

> **Measured amendment (2026-08-22):** the current Claude SDK does not
> reliably emit exact rate evidence during connect-only Refresh. Refresh now
> returns `Unknown` after initialization when no such event exists; task output
> is authorized by safe typed evidence from its own useful response. The older
> pre-output-probe steps below record the original implementation plan.

> Execute test-first in `phase0a-contract-hardening`. This is a billing-safety feature: ambiguous provider evidence must fail closed.

**Goal:** Add explicit quota refresh without clocks, scraping, API credentials, usage-credit consent, or a model invocation from the UI.

**Architecture:** Add an optional provider-neutral quota-probe adapter protocol, expose it through `runtime_check(refresh_quota=True)`, and connect the UI button to a CSRF-protected POST callback. Reuse existing canary/circuit recovery and pre-spawn guards.

**Tech stack:** Python asyncio, Claude Agent SDK events, stdlib HTTP server, vanilla JavaScript, pytest.

---

## Task 1: Adapter pre-output quota probe

1. Add failing fake-SDK tests for safe, unsafe, missing, and model-output-before-evidence sequences.
2. Add `QuotaProbeAdapter` and implement Claude `quota_probe(CanaryRequest)` as a bounded connect-only check with existing bound-pair/model/effort checks.
3. Send no query. Accept only structured evidence emitted before model output; otherwise report unknown or quota paused and confirm disconnect cleanup.
4. Run focused adapter tests to green.

## Task 2: Runtime check and circuit recovery

1. Add failing service/server tests for `runtime_check(refresh_quota=True)` and the default no-provider path.
2. For ready circuits, run the light quota probe and pause on unsafe evidence.
3. For `needs_canary` or `auto_paused`, do not run a live canary from Refresh; preserve the circuit unless fresh connect-only evidence is available.
4. Return only sanitized quota state and no-overage attestation.
5. Keep spawn/send's existing fresh rate guard unchanged.

## Task 3: Explicit UI refresh endpoint

1. Add failing endpoint tests for success, missing CSRF, non-empty body, and sanitized response.
2. Add optional provider-refresh callback to the loopback server and `LocalUiBackend.refresh_provider()`.
3. Make page boot use local GET only; make the Refresh button use `POST /api/v1/refresh` then render its returned snapshot.
4. Render plain quota availability/action text without reset estimates.

## Task 4: Verification

1. Run focused adapter/service/server/UI tests and JavaScript syntax check.
2. Run the full safe suite excluding real Git worktree mutation.
3. Confirm Refresh creates no provider query, then inspect only compact status fields in Chrome.
4. Security-scan diff/artifact and commit with the user's Git identity.
