# Subagent MCP 1.0.4 Local Activity Detail Design

**Date:** 2026-08-23  
**Status:** Approved by the user's explicit direction to show external sub-agents in detail at `127.0.0.1:8765`, after the documented native Codex-panel limitation was explained.  
**Release base:** public `v1.0.3` is already published on GitHub and PyPI.

## 1. Goal

Make **Current & recent activity** at `127.0.0.1:8765` useful in the same way as Codex's native sub-agent detail surface, while staying truthful about what external native harnesses actually publish.

For every Subagent MCP execution, the local UI shows:

- a deterministic runtime icon;
- the bounded task title;
- runtime, model, reasoning, and native harness identity;
- current normalized state and elapsed time;
- workspace label, mode, permissions, and declared write set;
- a lifecycle timeline derived only from persisted normalized events;
- recovery or needs-input status;
- the terminal, already-redacted external-agent result and its hash-bound artifact metadata.

The view updates automatically from local state while it is visible. It never starts or refreshes a provider merely to paint the UI.

## 2. Host-integration decision

As of 2026-08-23, the official Codex Subagents documentation describes host-owned agent threads, and the documented App Server can list/read those threads. It does not provide a public operation for an MCP server to register an externally owned native-harness session as a `subAgent` thread, set its parent thread, or assign an icon in the native Subagents roster.

`thread/inject_items` is not a registration contract: it mutates a Codex thread's model-visible history. Using it to fabricate an external session as a native Codex sub-agent would misreport lifecycle ownership and violate this repository's no-private-state-injection invariant.

Therefore 1.0.4 keeps:

```text
native_host_panel = unsupported
mcp_app = unsupported
localhost_activity = supported
```

If OpenAI later documents an external-agent registration capability, a separate versioned integration may flip that capability only after a real host canary. Version 1.0.4 does not patch, inject, or write Codex app-server/session/UI state.

## 3. User-visible behavior

### 3.1 Activity list

The existing panel becomes a responsive split view:

```text
+------------------------------+----------------------------------+
| Current & recent activity    | Selected external sub-agent      |
|                              |                                  |
| [C] Task title      Working  | [C] Task title          Working  |
|     Claude / Opus 5   8m 42s | Claude Code / Opus 5 / max       |
|                              | workspace / mode / write set     |
| [OX] Task title        Done  |                                  |
|      OX Alpha         6m 11s | Current stage                    |
|                              | Lifecycle                        |
|                              | Sanitized terminal result        |
+------------------------------+----------------------------------+
```

The newest active execution is selected by default. Selecting another row updates the right panel without navigation. At narrow widths, the detail panel stacks below the list.

Each row is a real keyboard-accessible button and contains only:

- deterministic icon or monogram;
- bounded task title, with a generic fallback for pre-1.0.4 rows;
- display runtime and model;
- normalized state;
- elapsed time while active or total duration when terminal.

### 3.2 Detail panel

The detail panel contains five sections:

1. **Identity:** task title, runtime display name, native harness, provider-neutral icon, state.
2. **Execution facts:** model, reasoning, transport, workspace basename, mode, permissions, write set, timestamps, elapsed time.
3. **Current stage:** a fixed state-derived label such as `Starting native harness`, `External harness working`, `Waiting for input`, `Completed`, `Interrupted`, `Failed`, or `Recovery required`.
4. **Lifecycle:** cursor-ordered event kind plus timestamp. Event payload is never returned to the browser.
5. **Result:** terminal result text already processed by the service redactor and bounded to 16,384 characters, plus artifact SHA-256 and character count. It is inserted with `textContent`/plain pre-wrapped text, never interpreted as HTML.

The UI explicitly says when a runtime does not publish tool-level progress. It never invents filenames, commands, checkpoints, or provider activity between `started` and a later persisted event.

### 3.3 Automatic local refresh

- With a selected execution, activity summary and detail refresh together every two seconds while the page is visible.
- With no selected execution, activity summary refreshes every five seconds while the page is visible.
- Refresh pauses when `document.visibilityState !== "visible"`.
- These GETs read local SQLite/config state only. They never call `runtime_check(refresh_quota=true)`, canary, spawn, send, or any provider API.
- On a transient read failure, the last good data remains visible with a stale indicator. No duplicate polling loop is created.

The existing explicit **Check providers** action remains the only UI path that probes providers.

## 4. Presentation metadata and icons

`AgentDescriptor.icon` already exists and is persisted per conversation. Version 1.0.4 renders that contract instead of adding a second identity system.

The minimal release-safe icon contract is:

```json
{
  "kind": "monogram",
  "text": "OX",
  "tone": "teal"
}
```

Rules:

- `text` is one or two alphanumeric characters after validation;
- `tone` is one value from a small package-owned enum;
- missing or invalid metadata falls back deterministically from `runtime_id` and `display_name`;
- no remote URL, arbitrary filesystem path, adapter-supplied markup, provider trademark, or executable image format is accepted;
- the browser creates the badge with text nodes and package CSS only.

This is intentionally smaller and safer than introducing a new icon-file route in 1.0.4. Package-owned SVG resources with hash and license provenance remain a future compatible extension of the existing descriptor contract.

## 5. Persisted task title

The current execution metadata deliberately omits `task.title`, leaving every activity row named `External-agent execution`. Version 1.0.4 adds one bounded presentation field to `executions.requested_json`:

```text
task_title: service-redacted string, maximum 240 characters
```

It does not persist the prompt, acceptance criteria, instructions, or transcript. This is an additive JSON field and requires no database migration. Existing rows render a generic runtime-based fallback.

## 6. Read API

### 6.1 Snapshot summary

`GET /api/v1/snapshot` keeps its existing authenticated loopback contract. Each `activity[]` item is extended with a strict projection:

```json
{
  "id": "execution-...",
  "conversationId": "conversation-...",
  "title": "Review activity visibility contract",
  "runtime": "claude-code",
  "displayName": "Claude sub-agent",
  "modelDisplayName": "claude-opus-5",
  "transport": "managed-sdk",
  "icon": {"kind": "monogram", "text": "C", "tone": "violet"},
  "state": "running",
  "startedAt": "...",
  "updatedAt": "...",
  "finishedAt": null,
  "durationMs": 522000
}
```

### 6.2 Execution detail

Add one read-only route:

```text
GET /api/v1/activity/{execution_id}
```

The route:

- requires the existing loopback session cookie;
- uses no query string;
- validates the execution identifier before SQL;
- returns 404 for an unknown execution;
- returns at most one bounded object;
- performs no provider or process action.

Response shape:

```json
{
  "id": "execution-...",
  "conversationId": "conversation-...",
  "title": "...",
  "runtime": "claude-code",
  "displayName": "Claude sub-agent",
  "provider": "anthropic",
  "harness": "claude-code",
  "modelDisplayName": "claude-opus-5",
  "reasoning": {"effort": "max"},
  "transport": "managed-sdk",
  "icon": {"kind": "monogram", "text": "C", "tone": "violet"},
  "state": "running",
  "conversationState": "active",
  "stateRevision": 1,
  "currentStage": "external_harness_working",
  "workspace": "phase0a-contract-hardening",
  "mode": "review",
  "permissions": ["repo_read"],
  "writeSet": [],
  "startedAt": "...",
  "updatedAt": "...",
  "finishedAt": null,
  "durationMs": 522000,
  "needsInputCount": 0,
  "recoveryRequired": false,
  "steps": [{"cursor": 1, "kind": "started", "at": "..."}],
  "result": null
}
```

For terminal success, `result` may contain only:

```json
{
  "text": "already-redacted bounded result",
  "artifactId": "result:execution-...:<sha256>",
  "sha256": "...",
  "charCount": 1234,
  "capsule": "..."
}
```

No event payload is copied into `steps`. In particular, the terminal event payload currently contains the result object and must never be dumped as a shortcut.

## 7. Security and privacy invariants

- Keep loopback-only bind, exact Host/Origin checks, HttpOnly SameSite session cookie, no CORS, request/response size limits, and CSRF on mutations.
- The new route is a session-authenticated read; it does not need a CSRF header.
- Never return prompt, acceptance criteria, role instructions, transcript, hidden thinking, raw events, raw provider output, credentials, account identifiers, external session ids, or absolute workspace paths.
- Render every provider-controlled string through DOM text APIs, not `innerHTML`.
- Project event kind and timestamp only.
- Use the stored redacted result, not provider-native logs.
- Invalid descriptors degrade to a neutral monogram without breaking the activity list.
- An absent descriptor on a queued/starting execution degrades to `runtime_id` identity.
- The detail API must have an explicit allowlist independent from the broad snapshot sanitizer.

## 8. Compatibility and failure behavior

- No database migration is required.
- Old conversations with `{}` or a v1 monogram descriptor remain readable.
- Unknown future descriptor fields are ignored.
- A malformed row affects only that row; the rest of the activity panel remains usable.
- Invalid or unknown execution ids return a normalized 404, not a SQL error.
- A stale browser poll does not clear last-known-good content.
- Automatic polling never changes provider quota/circuit state.

## 9. Verification and release gates

1. Red tests for task-title persistence, descriptor icon fallback, activity summary, detail projection, route auth/404, and non-leakage.
2. Backend implementation with no provider calls.
3. Frontend split view, keyboard selection, text-only result rendering, and visibility-aware polling.
4. Security regression tests proving prompt/raw-event/hidden-thinking sentinels never appear in detail responses while the explicitly public redacted result does.
5. Browser verification at desktop and narrow widths, including selecting running, succeeded, interrupted, failed, needs-input, and recovery rows.
6. Independent Claude and OX Alpha review. Fix only verified Critical findings or Major findings that reproduce repeatedly; stop a repeated fix loop.
7. Full safe suite, package contract, wheel install, and fresh isolated public-user install.
8. Real no-overage Claude plus OX Alpha executions create actual rows; verify the local panel from persisted live evidence, close both conversations, and confirm no leaked prompt/transcript/raw events.
9. Build, commit with the user's Git identity, scan the release tree/history/artifacts for secrets and local personal paths, then publish GitHub Release, PyPI, and MCP Registry `v1.0.4` only after every prior gate passes.

## 10. Explicitly deferred

- Native Codex Subagents-roster injection or icon assignment.
- MCP Apps component.
- Adapter-level live tool/checkpoint event production.
- Markdown/HTML rendering of provider output.
- Remote or arbitrary adapter icon resources.
- Lifecycle mutation controls inside the activity panel.
