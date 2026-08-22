# Subagent MCP Artifact Relay Design

## Problem

`agent_result_read` keeps complete agent reports available without placing them
in every Codex turn, but Codex must still pull and resend the text when another
external agent needs that report. That duplicates controller context and makes
the main agent an expensive content courier.

## Decision

Extend `agent_send` with one optional hash-bound source artifact reference:

```json
{
  "conversation_id": "source conversation",
  "execution_id": "source execution",
  "expected_sha256": "lowercase SHA-256"
}
```

The target conversation remains the existing top-level `conversation_id`.
Before any provider call, the service must prove that the source execution:

1. belongs to the declared source conversation;
2. completed successfully with a readable redacted text result;
3. still has the exact expected SHA-256; and
4. has the same non-empty workspace key as the target conversation.

The service persists only the reference and the user's ordinary follow-up
prompt. It expands the full artifact into the adapter prompt in memory, wrapped
as untrusted report data with its identity, digest, and character count. The
stored report is already bounded and redacted by the result contract. Codex
therefore handles only the compact reference while the native target harness
receives the complete report.

One artifact per send is intentional for the first public contract. Multiple
reports can be relayed in separate turns; this keeps validation, prompt size,
idempotency, and audit behavior obvious.

## Transfer metrics

Compact result artifact metadata also reports exact UTF-8 byte counts for the
full result and the returned capsule/preview. It includes a clearly labelled
content-only rough estimate using `ceil(utf8_bytes / 3)` for full, compact, and
saved tokens. This is not provider billing or a tokenizer claim. It is a stable
comparison signal; exact token use still depends on the controller model and
tool-schema overhead.

`agent_result_read` adds exact slice character/UTF-8-byte counts and the same
content-only estimate for that slice. Metrics are stateless and derived from
the response bytes, so this release needs no telemetry table, transcript log,
or hidden analytics.

## Safety boundaries

- Cross-workspace relay fails closed; no override is added in this release.
- A changed hash, nonterminal/failed source, missing result, missing workspace
  identity, or oversized expanded prompt fails before opening the target native
  session or calling a provider.
- Source and target conversations must differ.
- Full artifact text is never copied into request/idempotency state, events,
  logs, compact MCP responses, or the localhost activity UI.
- Artifact content is data, not authority. It cannot change the target task's
  role, permissions, workspace, model, billing policy, or safety gates.
- No compression/base64 claim is made; those can reduce wire bytes but do not
  inherently reduce model tokens.

## Public boundary

This is adapter-neutral. Claude Code, DeepSeek Harness, and future native
harness adapters receive the same already-validated `AdapterSendRequest`; no
provider role, task, or model is hard-coded.
