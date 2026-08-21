# Settings UI Runtime Clarity Design

**Date:** 2026-08-21
**Status:** Approved by the user's requested behavior

## Goal

Make the localhost page a small control surface for a Codex user configuring real external agent runtimes. Every visible item must answer one of three questions:

1. Is this runtime safe and available?
2. Which model and reasoning level will Codex use?
3. What should the user do next?

Test fixtures and internal transport/config vocabulary must not appear in the normal UI.

## Audience and page job

The audience is a developer using Codex as the lead agent. The page's single job is to make a real runtime ready for Codex and show truthful local status. It is not a chat surface, transcript viewer, or developer diagnostics console.

The existing quiet control-room visual language remains. This change spends its visual emphasis on one runtime readiness card rather than redesigning the page or adding dependencies.

## Runtime discovery

- The production UI registers production adapters only.
- `FakeAdapter` remains available to deterministic tests and MCP test fixtures but is not registered by `create_local_backend()`.
- Third-party production adapters discovered through the adapter registry remain eligible for display.
- Missing configured adapters may still appear as unavailable diagnostics; the deterministic fake is not created in normal user config.

## Adapter-published model choices

`AdapterManifest` gains an optional JSON `model_schema`, symmetrical with `reasoning_schema`. The UI does not contain Anthropic-specific model IDs.

The Claude adapter publishes exact canonical model suggestions using JSON Schema `anyOf` entries with `const` and `title`:

- `claude-opus-5` — Opus 5
- `claude-sonnet-5` — Sonnet 5
- `claude-fable-5` — Fable 5

The schema also permits a non-empty custom exact model ID. The UI renders a native select with adapter-published suggestions and a **Custom exact model ID…** choice that reveals a text input, so a future exact ID works without a UI release. Canonical IDs are stored because the current no-drift guard compares requested and effective model identity exactly; aliases such as `opus` are display conveniences, not persisted policy values.

Model availability remains provider-reported. A saved model is not permission to use credits or overage. Runtime canary/quota gates still fail closed before managed work.

## Reasoning control

For a reasoning schema with a single enum property, the UI renders a normal select instead of raw JSON. Claude therefore shows `low`, `medium`, `high`, `xhigh`, and `max` as adapter-published choices.

Complex future provider schemas may fall back to an advanced JSON field, but the Claude card does not expose raw JSON.

## Runtime card content

The Claude card shows:

- **Claude sub-agent**
- **Anthropic model · Claude Code native harness** instead of `managed-sdk`
- A status pill plus a plain next-action sentence
- **Available to Codex** with the explanation that enabling allows Codex to delegate after safety checks pass
- **Model** with adapter-published suggestions and custom exact ID support
- **Reasoning effort** as a select
- A compact, optional **What this runtime supports** disclosure with plain descriptions for safety check, native session, resume, and workspace capabilities
- **Save changes**

The card hides fields that have no user choice:

- transport when the adapter publishes exactly one transport
- selection mode when there is exactly one configured variant
- raw provider reasoning JSON when the schema can render normal controls
- config revision numbers
- internal adapter/SDK labels

## Other page content

- Rename the refresh action to **Refresh status** and explain that it refreshes runtime/provider state.
- Hide the update row while update status is only `not_checked`.
- Hide the safety-circuit section when there are no active circuit records; when present, label it **Automatic safety stops**.
- Keep project trust and activity because each has a clear user action or audit purpose.
- Keep package name and preview channel because they identify what is installed.

## Accessibility and security

- Use native input, select, checkbox, and details elements.
- Preserve keyboard focus, labels, validation, live status, CSRF, loopback-only binding, redaction, and no-storage behavior.
- Do not add remote fonts, scripts, telemetry, images, or dependencies.

## Acceptance

- Normal fresh UI contains `claude-code` and not `fake`.
- The Claude model field offers Opus 5, Sonnet 5, Fable 5 and accepts a custom exact ID.
- The Claude reasoning field is a select; no raw reasoning JSON is visible.
- `managed-sdk`, `rev 0`, empty circuits, and never-checked update state are absent from the rendered page.
- Every remaining runtime control has visible plain-language help.
- Saving canonical model and effort persists the existing config shape without weakening exact model/no-overage validation.
