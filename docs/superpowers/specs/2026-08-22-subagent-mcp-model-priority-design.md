# Subagent MCP Native Model Priority Design

## Problem

The localhost UI currently splits one primary model dropdown from a free-text
fallback list. That makes DeepSeek Harness users copy opaque `provider::model`
identifiers even though the native harness already publishes a model directory,
and it hides the actual order Codex will use.

## Native catalog boundary

Adapters may implement one optional read-only model-catalog capability. Catalog
rows contain only a stable provider/model value and a display label. They never
contain credentials, endpoints, balances, prompts, or transcripts.

DeepSeek Harness discovery runs a bounded Node child over the installed
harness's own `ctx.llm.listProviders()` and `ctx.llm.listModels()` APIs. It loads
only the two model-configuration sections needed to reconstruct the native
directory and emits only IDs and labels. It does not start an agent, open an ACP session, call a model,
interrogate a provider endpoint, or read the credential document. The catalog
is cached until the exact harness binding or settings-file identity changes.

Claude Code continues to publish its adapter-owned model catalog. Future
adapters use the same provider-neutral catalog rows; the UI contains no
provider-specific model branch.

## Priority dialog

Each runtime shows one **Model priority** button instead of a model dropdown and
fallback textarea. The button opens a native modal dialog:

```text
+ Model priority ----------------------------------+
| Drag models into the order Codex should use.     |
|                                                   |
|  1  ::  OX Alpha - OpenRouter      Available  ↑↓ |
|  2  ::  DeepSeek-V4-Flash                     ↑↓ |
|  3  ::  DeepSeek-V4-Pro                       ↑↓ |
|  4  ::  DeepSeek-V4-Flash-Vision-Exp          ↑↓ |
|                                                   |
| Advanced: add an exact model id                   |
|                              Cancel  Apply order  |
+---------------------------------------------------+
```

Rows are draggable. Up/down buttons provide the same operation for keyboard and
touch users. Apply updates the card draft; the existing **Save changes** button
persists it. Configured models keep their current order, then newly discovered
native rows follow in native order. A configured row missing from the current
directory remains visible instead of being deleted. Exact-id entry stays under
Advanced for dynamic providers, but DeepSeek and Claude require no typing when
their catalogs are available.

The existing visual system remains authoritative: Segoe typography, teal
accent, 44 px controls, ten-pixel radii, dark/light variables, visible focus,
and reduced-motion support. The sortable stack is the only new visual motif.

## Durable fallback semantics

The ordered config `variants` array remains the source of truth, so no schema
migration is required. Saving the dialog preserves existing stable variant IDs
by model and creates a content-derived ID only for a newly added model.

When an adapter explicitly returns `QUOTA_PAUSED` or
`USAGE_CREDITS_FORBIDDEN` for an exact variant, Subagent MCP first persists the
safety circuit and then moves that variant to the end of the configured order.
For `[A, B, C]`, exhaustion of `A` persists `[B, C, A]`. The failed task is not
retried. A later delegation reads `B` as priority one. Refresh may mark `A`
available again, but never moves it back; only the user changes its rank.

The same rule applies after an explicit UI quota refresh. Ambiguous adapter,
transport, authentication, timeout, and cleanup failures never rotate models.
No route enables usage credits, overage, purchase, reload, or auto-top-up.

## Release boundary

This release does not add weighted routing, percentages, per-call fallback,
provider spending automation, catalog polling, or a new JavaScript framework.
It reuses the existing variant contract, config revision lock, circuit state,
and localhost security model.
