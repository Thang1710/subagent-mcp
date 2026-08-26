# Subagent MCP Native ACP Error Provenance Design

**Status:** Approved hotfix scope from a confirmed public-runtime incident on 2026-08-26.

## Problem

DeepSeek Harness keeps a structured provider failure inside its native agent
loop, but its current ACP bridge can flatten that failure into a JSON-RPC error.
Subagent MCP then flattens the ACP error again: it retains only text long enough
to recognize explicit quota and a narrow temporary HTTP 429, while every other
terminal provider failure becomes the generic message `DeepSeek ACP turn did
not complete`.

That second loss is a Subagent MCP bug. The controller cannot distinguish a
real upstream provider failure from an ACP protocol rejection, cannot report
the native reason that was available, and may incorrectly speculate about
quota. A failed execution is already terminal and must never be replayed merely
to recover diagnostics.

## Decision

Preserve a bounded, scalar projection of each ACP JSON-RPC error at the stdio
boundary:

- the JSON-RPC error code;
- the native error message and supported nested detail strings;
- a provider error code only when the native response explicitly supplies one
  or the bounded native text contains a known stable harness code; and
- the classification source (`native-acp`).

The adapter exposes those facts in redacted terminal error text and structured
`result.error.details`. It keeps the public error code `PROVIDER_ERROR` unless the
native facts explicitly match the existing quota or temporary-rate-limit
contracts. `quota=unknown` and an unclassified provider failure never become
quota exhaustion.

For a read-only task, the existing bounded recovery remains: after installing
the fixed release and starting a fresh MCP process, the caller may create one
new conversation with a new request ID, up to three total explicit attempts.
The failed conversation remains terminal. A write task remains non-retryable
until its declared effects are reconciled.

## Safety and privacy

Subagent MCP does not retain a raw ACP frame, provider transcript, hidden
thinking, prompt, environment, credential, or billing payload. Diagnostic
strings are capped at 2,048 characters and pass through the existing recursive
service redaction before persistence or public output. Unknown mappings and
arrays are not copied into evidence.

The hotfix does not change model selection, quota state, credits, overage,
provider configuration, native-session ownership, automatic retry policy, or
turn timeout behavior. No provider call is required to prove it.

## Compatibility

The public terminal code remains `PROVIDER_ERROR`; the message becomes more
specific and snapshot evidence gains an additive `provider_error` object.
Older clients can ignore the evidence. There is no database migration,
protocol version change, dependency, or upstream Harness patch.

## Verification

Deterministic tests must prove that:

1. a JSON-RPC error keeps its numeric RPC code, bounded native detail, and an
   explicitly supplied or recognized provider code;
2. a native ACP provider failure reaches terminal `PROVIDER_ERROR` with those
   facts and the existing permission-safe recovery action;
3. the same facts are not reclassified as quota without explicit quota text;
4. existing explicit quota and temporary shared-pool 429 behavior remains
   unchanged;
5. service redaction, the full safe suite, installed package checks, artifact
   privacy checks, and source-tree cleanliness pass before release.

The caller proves the repair with a new explicit read-only conversation on the
current input hash after restarting onto the fixed public version. It must not
reuse or replay the failed execution.
