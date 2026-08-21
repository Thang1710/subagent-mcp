# Changelog

All notable changes to Subagent MCP are documented here.

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
