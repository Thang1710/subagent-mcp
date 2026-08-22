# Subagent MCP Native Model Priority Implementation Plan

> **Writer rule:** one bounded writer, TDD, no provider/model call, no credential
> read, no global configuration change, no publish until root verification.

**Goal:** Replace typed model fallback controls with a native-catalog-backed,
sortable priority dialog and persistently demote explicitly exhausted models.

**Release target:** `0.1.0a20` together with the already-started artifact relay.

### Task 1: Adapter-neutral catalog

- [x] Add an optional catalog protocol and publish catalog rows from
  `runtime_list` without changing the adapter manifest contract.
- [x] Implement bounded DeepSeek discovery through native Harness APIs, cache by
  exact binding/settings identity, and prove no provider or credential action.
- [x] Mount the official DeepSeek adapter in generated ACP compositions so every
  advertised native route is actually executable.
- [x] Keep Claude's adapter-published options and configured unknown models.

### Task 2: Priority config behavior

- [x] Add atomic config demotion preserving variant IDs and revision safety.
- [x] Demote only after explicit quota/credit safety codes; never retry the same
  task and never rotate ambiguous failures.
- [x] Apply the same demotion after explicit refresh evidence.
- [x] Add focused config/service tests for order, persistence, and no-op cases.

### Task 3: Localhost dialog

- [x] Project one `model_priority` field from configured variants plus catalog.
- [x] Implement a native dialog, drag/drop, keyboard/touch up/down controls,
  paused state labels, cancel/apply draft behavior, and Advanced exact-id entry.
- [x] Remove the ordinary dropdown/fallback textarea without changing unrelated
  runtime controls or the existing visual system.
- [x] Add integration/security tests and visually inspect light/dark responsive
  behavior in the authorized localhost tab.

### Task 4: Finish a20

- [x] Finish artifact-relay MCP/schema/docs/version surfaces.
- [x] Run focused tests, full safe suite, artifact acceptance, and one bounded
  DeepSeek Critical-only review attempt. If the provider fails, use one native
  Critical-only fallback and do not retry. Fix only verified Critical findings.
- [ ] Commit with the owner's exact author and committer, mirror public, publish,
  reinstall from PyPI, and verify the localhost UI plus a catalog read that
  starts no provider work.
