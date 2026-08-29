# Changelog

All notable changes to Subagent MCP are documented here.

## 1.0.24 - 2026-08-29

- Preserve truthful `auth_required`, `not_installed`, and `incompatible`
  runtime-check results when quota refresh has no exact adapter pair.
- After a user signs back in, reopen only the matching authentication-blocked
  circuit as `needs_canary`; never skip the fresh canary or infer readiness.
- Assemble secret-shaped redaction fixtures from fragments so repository and
  release scans stay clean without weakening runtime redaction coverage.

## 1.0.23 - 2026-08-26

- Require a positive, non-negated retirement statement before treating a model
  route as terminal; unrelated 404 diagnostics remain retryable provider
  errors.
- Match the completed testing-period form only when it also names the model
  replacement route, preserving the no-silent-substitution decision boundary.

## 1.0.22 - 2026-08-26

- Treat an explicit upstream 404 model-retirement signal as terminal
  `CAPABILITY_MISSING` instead of retryable `PROVIDER_ERROR`.
- Require an explicit user decision before selecting another model route; never
  retry the failed turn, substitute a model, or change credits automatically.

## 1.0.21 - 2026-08-26

- Preserve bounded native ACP JSON-RPC code, provider code, and redacted detail
  in terminal `PROVIDER_ERROR` results instead of collapsing every failure to a
  generic message.
- Keep `quota=unknown` and conflicting/ambiguous quota text out of
  `QUOTA_PAUSED`; only explicit exhaustion evidence changes quota state.

## 1.0.20 - 2026-08-26

- Publish each runtime's native `write_root_mode` and reject unsupported file or
  missing-path scopes before root-count recovery, readiness, leases, or provider
  work. DeepSeek Harness now truthfully advertises its single existing-directory
  boundary, and recovery never widens exact-file authority automatically.
- Clarify the public `agent_spawn` contract: writable requests use
  `workspace_write` (not `repo_write`) and should inspect `runtime_list` before
  constructing `write_set`.
- Tell fresh MCP controllers to continue waiting while an external execution is
  running; local wait expiry does not interrupt, retry, or trigger fallback.

## 1.0.19 - 2026-08-25

- Reject unsupported context policy IDs before provider work and report exact
  project/local-context and auto-compaction attestation gaps.
- Keep Claude user-level native settings and skills available for scoped
  writers; explicitly expose the `Skill` tool without enabling project/local
  settings, shell access, or broader writes.

## 1.0.18 - 2026-08-25

- Make a generic DeepSeek provider failure actionable without replaying native
  work: read-only tasks may start a new bounded explicit attempt, while write
  tasks remain non-retryable until their declared effects are reconciled.
- Include execution and available native-session IDs in compact status so a
  terminal incident can be diagnosed without requesting the full result.

## 1.0.17 - 2026-08-25

- Hash-bind review inputs on both `agent_spawn` and `agent_send`: Subagent MCP
  resolves repository-relative files, computes SHA-256 read-only before native
  work, rejects drift, and surfaces the verified attestation in prompt/status.
- Surface effective Claude reasoning as configured SDK/environment evidence,
  explicitly distinguished from provider-reported telemetry.
- Preserve ACP SDK `data.details` so exhausted temporary OX upstream 429s report
  `RATE_LIMITED` with a safe continuation action instead of generic
  `PROVIDER_ERROR`; cover successful first-turn then follow-up lifecycle.

## 1.0.16 - 2026-08-25

- Retry an explicit transient DeepSeek upstream HTTP 429 at most three total
  attempts, while leaving quota, credit, billing, and ambiguous failures
  non-retryable.
- Report a bounded `RATE_LIMITED` result when the shared provider pool remains
  unavailable, without exposing provider payloads or changing model priority.

## 1.0.15 - 2026-08-25

- Accept valid Claude Code stream frames up to 8 MiB so large native Read
  results do not become ambiguous terminal JSON failures.
- Run DeepSeek Harness binding discovery and launch-file revalidation outside
  the MCP event loop, bound the initial pre-provider check to 15 seconds, and
  reuse the attested binding until native launch rechecks its exact files.
  Provider turns still have no elapsed completion deadline.

## 1.0.14 - 2026-08-25

- Surface a resident update quarantine in local runtime status and the
  localhost UI before Codex attempts provider work, while preserving the
  terminal identity fence and the real adapter/circuit state.

## 1.0.13 - 2026-08-24

- Let an exact same-origin localhost page create or replace its browser session,
  so the fixed settings URL works directly after a resident restart.

## 1.0.12 - 2026-08-24

- Keep the managed localhost UI alive when a Windows Python launcher hands off
  to an authenticated child interpreter with a different process ID.

## 1.0.11 - 2026-08-24

- Keep persisted quota-pause evidence as history without exposing it as a
  controller-side block: each later explicit task checks the native harness
  again, and no cached reset time or wait-until choice is used.
- Report whether a circuit blocks a new explicit task and keep the localhost
  runtime/model status aligned with that executable gate.
- Recover a prior controller's incomplete `starting` execution through a
  per-execution OS lock, without a timeout or relaunch. Writer leases release
  only when native work provably never started or cleanup is later verified.

## 1.0.10 - 2026-08-24

- Allow the Windows sharing canary's PowerShell helper up to 30 seconds to
  signal readiness on a cold CI runner; file-lock and release assertions are
  unchanged.
- Replace the unpublished 1.0.9 candidate, whose build stopped before the
  PyPI, GitHub release, and MCP Registry publication jobs.

## 1.0.9 - 2026-08-24

- Allow the deterministic background-supervision regression enough time on
  slower Windows CI runners; runtime behavior is unchanged.
- Replace the unpublished 1.0.8 candidate, whose build stopped before the
  PyPI, GitHub release, and MCP Registry publication jobs.

## 1.0.8 - 2026-08-24

- Recheck a previously quota-paused model on each later explicit delegation
  instead of caching a reset time. Unknown provider state remains unknown and
  never becomes inferred exhaustion.
- Preserve native session ownership and writer leases across ambiguous startup,
  cleanup, and controller-restart paths until cleanup is verified.
- Redact additional credential and account-identity forms before external agent
  output reaches public status, durable activity, or artifact relays.
- Authenticate localhost UI control messages and reject forged start, stop, or
  session-bootstrap requests while keeping the fixed loopback UI available.
- Pin privileged release actions by commit and validate exact release tags
  before checkout, build, PyPI publication, GitHub release, or registry publish.
- Isolate DeepSeek Harness child environments and hold verified executable
  identities across native ACP startup.
- Normalize Windows extended-path aliases for writer leases and verify the
  exact staged runtime tree before the stable PowerShell launcher executes
  Python.

## 1.0.7 - 2026-08-23

- Preserve explicit Claude quota/no-overage failures when a concurrent MCP or
  localhost UI process advances the circuit; a controller-local state race no
  longer becomes synthetic cleanup ambiguity or retains writer leases.
- Retry only the local idempotent circuit pause, at most three times, without
  repeating the provider task. Exhausted local reconciliation reports the
  original terminal provider code plus a sanitized state warning.
- Keep connect-only Provider Refresh status-only when cleanup is unavailable:
  it reports quota unknown, sends no model prompt, and cannot wedge a ready
  circuit in unrecoverable state.
- Clarify that a legacy `UPDATE_QUARANTINED` resident is terminal and cannot
  hot-load replacement package files; exact isolated uvx registration prevents
  recurrence for fresh tasks.

## 1.0.6 - 2026-08-23

- Run the documented MCP, CLI, and fixed-port localhost UI through exact
  `uvx --isolated --from` release environments, so an old Windows process may
  finish without its files being removed or rewritten.
- Remove the destructive persistent-tool reinstall from the default public
  update and rollback path. Version changes now stop the old exact UI, replace
  the MCP entry through Codex's public commands, and start the new exact UI.
- Keep one-time legacy migration conservative: replace the direct registration
  first, let old tasks end naturally, and never kill Codex/provider processes,
  clear uv caches, or change billing.

## 1.0.5 - 2026-08-23

- Expose a generic `max_write_roots_per_session` adapter manifest bound
  (default 1, maximum 32; Claude and the deterministic fake advertise 32,
  DeepSeek Harness stays at its native one root) in `runtime_list`.
- Reject a writable multi-root request before readiness probing, idempotency,
  execution creation, leases, or provider work with a machine-readable repair
  directive capped at three attempts instead of an unexplained terminal error.
- Publish capped controller instructions: inspect every error's `retryable`,
  `next_action`, and `recovery`, never exceed three total retry/refresh/repair
  actions per failed delegation, keep quota/billing/auth/safety/context-drift/
  quarantine/ambiguous failures terminal, and never widen write authority.
- Keep idempotent replays from reacquiring released writer leases. Explicitly
  retryable pre-provider contention now carries a three-attempt retry directive;
  each deliberate new execution uses a fresh request ID.

## 1.0.4 - 2026-08-23

- Add a selectable localhost activity list and responsive detail panel for
  external agents, including model, native harness, safe workspace metadata,
  current stage, lifecycle, elapsed time, and redacted terminal results.
- Persist bounded task titles and deterministic runtime monograms without
  exposing prompts, transcripts, hidden thinking, or raw provider events.
- Treat Claude rate-limit events as informational until the provider emits a
  terminal outcome, while preserving no-overage enforcement and exact quota
  exhaustion handling.
- Dispatch MCP Registry publication explicitly after the matching GitHub
  release so token-created releases cannot silently skip registry publishing.

## 1.0.3 - 2026-08-23

- Return a bound `running` Claude session after exact startup and no-overage
  evidence, then supervise the native turn in the background without imposing a
  model completion deadline. Native output that arrives before the matching
  rate event is held in memory and accepted only after exact safe evidence.
- Keep `agent_wait` local and non-cancelling; its four-minute controller window
  returns current status while the external agent continues working.
- Make status, terminal persistence, and interrupt races deterministic. Preserve
  completed output and explicit quota failures even when cleanup is ambiguous,
  and keep unsafe writer leases held until native cleanup is verified.
- Surface a controller-restart loss as durable recovery instead of leaving an
  execution permanently stuck in `running`. `agent_close` can retry cleanup,
  atomically release held writer leases, and clear recovery without database
  repair; verified process absence also recovers restart or startup ambiguity.

## 1.0.2 - 2026-08-23

- Let an exact same-origin localhost page create an in-memory browser session,
  so the fixed settings port opens directly without a one-time URL token.
- Keep loopback binding, exact Host and Origin checks, HttpOnly/SameSite cookies,
  per-session CSRF, no CORS, and single-use rejection for supplied bootstrap
  tokens unchanged.

## 1.0.1 - 2026-08-23

- Accept Claude Code's documented optional omission of `overageStatus` when the
  same live event reports `allowed` or `allowed_warning` and exact
  `isUsingOverage=false`. Explicitly available overage, active overage, rejected
  plan quota, and billing errors remain blocked.
- Report exhausted plan quota, forbidden usage credits, and unavailable safety
  evidence as distinct states; ambiguous evidence no longer fabricates a durable
  quota pause.
- Publish MCP Registry metadata through GitHub OIDC with a checksum-pinned
  official publisher and no repository secret.

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
