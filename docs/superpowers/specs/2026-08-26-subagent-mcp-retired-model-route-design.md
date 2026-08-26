# Retired provider model route design

## Problem

A native ACP turn can return a JSON-RPC internal error whose bounded detail
contains an upstream HTTP 404 and an explicit statement that a temporary model
testing route has ended. Subagent MCP 1.0.21 correctly preserves that detail,
but classifies every non-quota, non-rate-limit ACP failure as retryable
`PROVIDER_ERROR`. That instruction is wrong for a retired exact route: another
request cannot restore it, and silently selecting the revealed replacement
would violate exact-model attestation.

The harness-published model catalog is local capability metadata rather than a
live provider availability API. A provider-side retirement therefore may not
change the catalog files that Subagent MCP hashes and caches.

## Decision

Classify only explicit model-route unavailability evidence at the shared
DeepSeek terminal-error branch. A bounded error must include an upstream 404
plus either an explicit model-not-found/no-longer-available statement or the
temporary testing-period completion form that names a replacement route.

The normalized result is:

- code `CAPABILITY_MISSING`;
- category `provider`;
- `retryable=false`;
- message stating that the exact configured provider route is unavailable;
- next action requiring an explicit user decision to wait for that exact route
  or configure another route;
- an explicit prohibition on retrying the failed turn, reusing it, silently
  substituting a model, or changing credits.

Existing bounded `result.error.details` provenance remains unchanged. Generic
404 text without model-route retirement evidence remains retryable
`PROVIDER_ERROR`, so the adapter does not invent model retirement from an
unrelated upstream failure.

## Scope

This hotfix changes only DeepSeek ACP terminal classification, focused tests,
public release notes, and package version metadata. It adds no provider query,
catalog API, fallback, alias, state migration, circuit state, dependency, or
background retry.

The exact retired route cannot be restored by Subagent MCP. A task that requires
that exact identity remains blocked until the user explicitly changes the
requirement or the provider restores the route.

## Verification

Deterministic fixtures must prove:

1. the observed retirement-shaped 404 becomes terminal `CAPABILITY_MISSING`;
2. the provider/RPC detail remains available and redacted through the existing
   public projection;
3. a generic 404 stays `PROVIDER_ERROR` and does not become a false retirement;
4. exactly one native prompt is attempted in either case;
5. focused, full safe, package, diff, and privacy gates remain green without a
   provider or canary call.
