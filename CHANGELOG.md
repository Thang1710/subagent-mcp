# Changelog

All notable changes to Subagent MCP are documented here.

## 1.0.0 - 2026-08-23

- Stabilize the public MCP/API contract and Windows package, CLI, localhost UI,
  release workflow, and registry metadata.
- Mark Claude Code and DeepSeek Harness ready after fresh read-only native
  harness smoke tasks completed and their sessions closed cleanly. Adapter
  capability gaps remain explicit and do not change the normalized lifecycle.
- Keep provider billing safety unchanged: Subagent MCP never enables, buys,
  reloads, or opts into usage credits or paid overage.

## 0.1.0a29 - 2026-08-22

- Quarantine provider calls when an installed package changes underneath a
  resident MCP process. Local status remains readable, while Codex is directed
  to start a fresh task instead of risking stale lifecycle or quota behavior.
- Present each runtime's existing enable control as an accessible on/off switch
  in the localhost settings UI.
- Remove the hidden 32-turn cap from production Claude tasks so long native
  harness work can reach its own terminal result. If an upstream turn cap is
  still reported, preserve its reason and turn count instead of labelling it
  as an unsafe canary result. The one-turn safety canary remains bounded.

## 0.1.0a28 - 2026-08-22

- Accept normal multiline formatting in spawn and follow-up prompts. Newlines,
  carriage returns, and tabs are preserved for native harnesses; NUL, escape,
  and every other control character remain rejected.

## 0.1.0a27 - 2026-08-22

- Rotate explicit quota-pause provenance on every current write. A resident
  older process that inherits and copies a prior marker can no longer reuse it
  to bypass the legacy-writer fence.
- Replace the a26 SQLite guard online without changing schema compatibility or
  relaxing same-response no-overage enforcement.

## 0.1.0a26 - 2026-08-22

- Prevent a resident older MCP process from replacing exact safe Claude task
  evidence with an ambiguous `USAGE_CREDITS_FORBIDDEN` pause. Current explicit
  provider failures carry provenance; an online SQLite guard rejects unproven
  legacy writes before they can block the configured model.
- Keep the database schema readable by active older tasks so they can still
  perform useful work while losing only the unsafe state-write path. No quota
  reset clock, usage credit, overage, or synthetic availability signal is used.

## 0.1.0a25 - 2026-08-22

- Keep the managed settings UI directly usable at its fixed
  `http://127.0.0.1:8765/` address without rerunning the CLI for every new tab
  or reload.
- Restore existing browser sessions without weakening the one-time bootstrap
  required to authorize a new browser profile. Loopback Host/Origin, HttpOnly
  cookie, and CSRF checks remain unchanged.

## 0.1.0a24 - 2026-08-22

- Allow write-capable external agents to work concurrently when their canonical
  absolute write roots are disjoint, even when callers use different or nested
  workspace roots; equal and ancestor/descendant scopes remain atomically
  exclusive. Terminal spawn/send replay reuses released lease identity without
  duplicating rows or model work.
- Attest and enforce each write set at the native adapter boundary. Claude
  guards `Edit` and `Write` paths; DeepSeek ACP narrows the native session to
  one declared directory tree and rejects unsupported multi-root scopes.
- Validate Claude's bound CLI, subscription auth, credential precedence, and
  control connection before a useful query, then require exact typed stream
  identity plus safe rate evidence from that same response before accepting
  output. Connect-only UI Refresh reports `Unknown`; explicit quota or
  forbidden-credit evidence pauses only the affected model, while a later safe
  task response reopens it without a reset-time checkpoint or status query.
- Remove product-imposed completion deadlines from ordinary Claude managed and
  DeepSeek ACP turns while retaining bounded initialization, query submission,
  cancellation, and connection cleanup.
- Preserve the last verified conversation context when a provider response is
  rejected so a later safe response can resume that session.

## 0.1.0a23 - 2026-08-22

- Register Codex's stdio MCP through a pinned `uvx --from` environment that is
  separate from the persistent CLI/UI tool, avoiding Windows launcher locks
  during normal updates.
- Document the one-time migration for older direct-launch entries plus explicit
  update and rollback commands; Subagent MCP still never rewrites Codex config
  or clears uv caches automatically.

## 0.1.0a22 - 2026-08-22

- Distinguish models already configured for delegation from additional models
  published by a native harness, instead of presenting catalog suggestions as
  active fallbacks.
- Let **Apply order** turn the complete native catalog order into a saveable
  draft even when the user accepts the harness's default order unchanged.

## 0.1.0a21 - 2026-08-22

- Add `subagent-harness-mcp ui --open` to request a fresh single-use browser
  session from an already-running managed UI on its fixed loopback port.
- Authenticate bootstrap rotation with the existing private control token and
  exact loopback Host/Origin checks; validate and hand the URL directly to the
  browser without printing or persisting its token.

## 0.1.0a20 - 2026-08-22

- Replace exact-model text entry and fallback textareas with one accessible
  model-priority popup: native catalog names, drag-and-drop ordering, keyboard
  and touch-friendly move controls, plus an advanced exact-ID path.
- Read DeepSeek Harness's own official and configured provider catalog without
  resolving credentials or calling a model, and mount both official DeepSeek
  and pi-ai provider adapters in native ACP sessions.
- Persistently move an exact model to the bottom after terminal quota or
  forbidden-credit evidence, without retrying the failed task or reacting to
  ambiguous errors.
- Keep ambiguous no-overage evidence visibly unknown and safety-paused without
  labelling it as exhausted quota or changing the saved model order.
- Allow `agent_send` to relay one hash-bound successful result between two
  conversations in the same verified workspace. Full report text is expanded
  only in memory; durable state keeps the reference and transfer metrics.

## 0.1.0a19 - 2026-08-22

- Recover a connection-owned DeepSeek execution left running after its MCP
  controller exits, but only after a read-only Windows process inventory proves
  the exact conversation-bound ACP process is gone.
- Fail closed without changing lifecycle state when the harness binding,
  process identity, command line, or process inventory is unavailable or
  ambiguous. Recovery never kills a process or calls a provider.

## 0.1.0a18 - 2026-08-22

- Preserve complete redacted native-agent reports in bounded local state while
  returning only a capsule or preview, execution identity, SHA-256, and size in
  compact lifecycle responses.
- Add read-only `agent_result_read` for hash-bound, on-demand result slices; it
  never opens a native session or calls a provider.
- Replace the 500-word agent-output instruction with a `CAPSULE:` plus complete
  `DETAILS:` contract and raise the durable result bound to 65,536 characters.

## 0.1.0a17 - 2026-08-22

- Keep a DeepSeek turn timeout authoritative when an interrupt races its
  cleanup, and reject any later send after the connection-owned ACP process has
  closed instead of attempting to reuse a dead client.
- Allow a terminal conversation to close logically after an MCP restart only
  when persisted generic capability evidence proves the native session was
  connection-owned and the adapter explicitly cannot resume it.

## 0.1.0a16 - 2026-08-22

- Add an optional detached localhost UI with explicit `--background`,
  `--status`, and graceful `--stop` commands; it remains independent of the MCP
  server and keeps stable port `8765` by default.
- Authenticate background shutdown with a random control token, exact loopback
  Host/Origin checks, and a bounded atomic Local control record removed only
  when its bytes still match the process that published it.
- Add `--no-open` for foreground or background use without launching a browser;
  background mode never creates an automatic Windows login/startup entry.
- Give native DeepSeek turns a separate 15-minute completion budget instead of
  failing slow Ox Alpha work at the five-minute lifecycle-operation boundary;
  the adapter still performs no automatic retry or model change. If that
  deadline is reached, it sends native cancellation and closes the ACP process
  so provider usage cannot continue in the background.

## 0.1.0a15 - 2026-08-21

- Discover standard Windows Node installs from `SystemDrive` when an MCP client
  intentionally filters the `ProgramFiles` environment variable.
- Add a user-ordered fallback-model list without hard-coding a public default;
  Codex selects the next variant only after an explicit `QUOTA_PAUSED` result.
- Preserve bounded ACP error detail long enough to distinguish terminal
  provider credit/quota exhaustion from ambiguous provider failures, while
  returning only a generic public billing notice and never buying or reloading
  credits.
- Reconcile a late interrupt with an already completed native DeepSeek turn so
  the normalized lifecycle records its terminal result instead of leaving a
  stale running execution.

## 0.1.0a14 - 2026-08-21

- Reject documented credential routes above subscription OAuth and verify the
  active credential source from the native `system/init` event.
- Keep the no-overage gate on documented live SDK evidence: a safe rate event
  must arrive before output is accepted; missing evidence interrupts the turn
  and discards its output. Private Claude cache files are not used as a control
  contract.
- Distinguish a confirmed provider quota pause from missing no-overage evidence,
  so the UI never reports an exhausted allowance when the harness simply could
  not expose a safe preflight signal.
- Cap the final text returned to the Codex controller at 4,096 characters and
  ask native agents for concise final-only reports to reduce supervision cost.
- Allow up to 180 seconds for the exact native canary so Opus xhigh startup is
  not rejected by the previous 30-second preview timeout.
- Accept SDK assistant events that omit their optional session ID while still
  requiring any reported assistant ID and the terminal result ID to match.
- Add the first in-development DeepSeek Harness native ACP vertical slice with
  exact `provider::model` selection, confined workspace permissions, bounded
  lifecycle output, and automatic native Node/source-checkout discovery.
- Publish an advisory per-runtime delegation priority in the config, MCP
  runtime list, and localhost UI without silently rerouting requests.

## 0.1.0a13 - 2026-08-21

- Match the official MCP Registry namespace to the canonical GitHub owner
  casing so registry ownership verification succeeds.

## 0.1.0a12 - 2026-08-21

- Interrupt Claude lifecycle work before accepting any model output unless a
  fresh rate event explicitly proves that overage is blocked.
- Open the localhost UI on stable port `8765` by default, with an explicit
  `--port` override and no dependency on an active MCP process.
- Add searchable PyPI project links and keywords for Codex, native harnesses,
  MCP, multi-agent orchestration, and Claude Code.

## 0.1.0a11 - 2026-08-21

- Restrict release manifests and checksums to the wheel and source archive so
  every listed integrity artifact is actually published and verifiable.

## 0.1.0a10 - 2026-08-21

- Clamp rollback command and verification budgets to the documented 30-second
  maximum so floating-point rounding cannot fail the Windows release gate.

## 0.1.0a9 - 2026-08-21

- Make provider Refresh a strict no-model preflight: it never launches a live
  canary or task, and reports unknown when the native harness exposes no
  pre-turn quota evidence.
- Keep ready runtimes fail-closed when quota evidence disappears without
  falsely claiming that overage was confirmed blocked.

## 0.1.0a8 - 2026-08-21

- Treat Claude's `allowed_warning` rate state as remaining subscription quota
  only when overage is explicitly rejected and `isUsingOverage` is false.
  Missing or unsafe no-overage evidence still pauses the runtime immediately.

## 0.1.0a7 - 2026-08-21

- Add a public deterministic adapter conformance runner and a separately
  packaged sample adapter discovered through the standard Python entry-point
  group.
- Explain guarded quota-check capability failures in the localhost UI without
  accepting unknown evidence or retrying provider work.

## 0.1.0a6 - 2026-08-21

- Make the stable Windows launcher hash its staged runtime with the .NET
  cryptography API available in PowerShell 5.1 instead of requiring the
  optional `Get-FileHash` cmdlet.
- Register the public MCP identity as `subagent-mcp` while keeping the Python
  distribution and CLI identity `subagent-harness-mcp`.

## 0.1.0a5 - 2026-08-21

- Report an automatically paused safety circuit as unavailable in the localhost
  UI instead of presenting the runtime as ready for delegation.

## 0.1.0a4 - 2026-08-21

- Keep external-agent waits inside the MCP service until completion or a real
  attention boundary, avoiding model-mediated polling while work is running.
- Clarify the localhost settings UI, expose provider-native model and effort
  selection, and hide test-only adapters from normal product views.
- Add an explicit on-demand provider quota refresh that proves subscription
  identity and no-overage status, pauses unavailable runtimes, and never retries
  automatically after a quota signal.
- Fail closed into recovery when provider cleanup cannot be confirmed, blocking
  new work until the exact runtime pair is safe again.
- Reframe the public README around Codex as orchestrator and generic external
  model-plus-native-harness runtimes.

## 0.1.0a3 - 2026-08-21

- Keep release metadata parsing compatible with Python 3.10. The `0.1.0a2` tag
  stopped before artifact publication.
- Fix Windows private-root creation when the runner's default object owner is
  different from its current user SID.
- Make GitHub Release publication explicitly target this repository after the
  PyPI trusted-publishing step.
- Replace internal release notes in the public README with a short install path,
  verified Codex registration command, and architecture diagram.

- Publish the `subagent-harness-mcp` Python package, typed console entry point,
  four versioned public schemas, static localhost UI assets, and MIT metadata.
- Add revisioned JSON configuration, additive SQLite state, normalized adapter
  contracts, deterministic fake lifecycle, and the exact 13-tool stdio MCP.
- Add a capability-gated Claude Code managed adapter that remains
  `needs_canary` until exact identity, model, context, terminal, cleanup, and
  `isUsingOverage=false` evidence passes for that adapter pair.
- Add a loopback-only settings/activity UI with bootstrap, cookie, CSRF, Host,
  Origin, and content-security controls.
- Add immutable Windows runtime staging, atomic pointer update/rollback,
  official-client read-back, conservative uninstall, and dry-run commands.
- Add clean wheel/sdist artifact smoke tests and deterministic, manually gated
  trusted-publishing workflows.

Explicit preview gaps: live Claude readiness, visible-background transport,
promotion, native Codex-panel integration, and release support outside Windows.
