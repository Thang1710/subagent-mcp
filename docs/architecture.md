# Subagent MCP architecture

Subagent MCP has one control plane and three thin local surfaces:

```text
stdio MCP -------\
CLI/lifecycle ----> SubagentMcpService -> config + SQLite + leases/circuits
localhost UI ----/                    \-> versioned native-harness adapters
```

`SubagentMcpService` owns idempotency, state transitions, redaction, event
cursors, circuits, workspace/writer leases, and final-result deduplication.
Adapters translate the public async contract to one native harness; they do not
write shared state. The UI and MCP therefore cannot diverge into separate
provider lifecycle implementations.

## Normalized lifecycle

The stdio server declares 14 tools from `schemas/tools-v1.json`. Core lifecycle
is `runtime_list/check`, `agent_spawn`, `agent_status`, `agent_result_read`,
`agent_send`, `agent_wait`, `agent_interrupt`, and `agent_close`; configuration,
trust, canary, scan, and workspace-release operations retain their explicit
approval metadata. Side-effecting requests use idempotency keys.

Terminal text is redacted and retained in the existing execution state. Compact
status replaces that text with an execution-bound artifact ID, SHA-256,
character count, and a bounded capsule or preview. `agent_result_read` requires
the exact conversation, execution, and hash, then returns only a bounded
character slice without opening a native session or calling a provider. Full
response mode remains available for compatibility. This keeps all bounded
result information locally retrievable without repeating it in every Codex
controller turn.

`agent_send` may carry one hash-bound reference to a successful result from a
different conversation in the same verified workspace. The service verifies
conversation, execution, state, workspace identity, and SHA-256 before wrapping
the report as untrusted data for the target adapter. Full text exists only in
that in-memory adapter request; idempotency state and events retain the compact
reference. Exact UTF-8 byte counts and clearly labelled rough token estimates
make controller-versus-relay transfer costs visible without claiming provider
billing precision.

`agent_spawn.task.inputs` and `agent_send.inputs` may declare up to sixteen
repository-relative files with exact lowercase SHA-256 values. The controller
resolves each path inside the selected workspace, hashes it read-only off the
event loop immediately before native work, and rejects missing, escaping, or
changed inputs before the adapter runs. Verified path/hash/byte-count metadata
is included in the native prompt and normalized status; no shell, write, or
network authority is added. Execution status also distinguishes adapter-bound
effective reasoning configuration from provider-reported telemetry.

Every execution records requested/effective model, reasoning, transport,
workspace, session, and context identity. Provider differences appear only in
the descriptor and explicit capability gaps. Unknown or mismatched critical
identity fails closed. An adapter may publish its native model catalog without
reading credentials or calling a provider. The user's ordered variants remain
the source of truth. An explicit terminal `QUOTA_PAUSED` or forbidden-credit
classification moves that exact model to the bottom persistently for future
tasks; it never retries the failed task or changes order after an ambiguous
failure.

Each runtime can publish a neutral `delegation_priority` from 0 to 100. Higher
values appear first in `runtime_list` and the localhost UI so the orchestrator
can prefer one external runtime over another without hard-coded provider roles.
Selection remains explicit: the MCP does not silently reroute a request or
control the Codex host's native-subagent fallback.

Restart handling follows the adapter's advertised capability, never a simulated
resume. A terminal session may be closed logically without reopening the native
harness only when persisted evidence says its lifetime belonged to the old
connection, `resume_after_restart` is an explicit gap, and the current adapter
does not advertise resume. A connection-owned active execution can transition
to failed only through an adapter verifier that proves its exact native process
is absent; an unverified or ambiguous cleanup stays fail-closed.

## State and ownership

`SUBAGENT_MCP_HOME` creates `config/`, `state/`, and `data/` children for tests
and portable use. Otherwise OS-native roots are used. Config writes are
revisioned atomic replacements; SQLite migrations are additive. Versioned
runtimes are immutable and selected through an atomic pointer. Conservative
uninstall removes only byte/identity-matching owned resources and preserves
user config, state, sessions, and worktrees.

A write-capable execution declares canonical repository-relative tree roots in
`write_set`. Lease acquisition resolves and compares canonical absolute roots
atomically across all declared workspaces: equal paths and ancestor/descendant
paths conflict, while disjoint paths may run concurrently. Lane names and task
labels never participate in locking. Terminal idempotent replay recognizes its
released deterministic lease rows without reacquiring or duplicating them.
Omitting `write_set` keeps the backwards-compatible exclusive whole-workspace
scope. Each adapter attests the normalized set and must enforce it through its
native harness boundary or return `CAPABILITY_MISSING`; the lease is
coordination between Subagent MCP executions, not an OS sandbox against other
same-user processes. The manifest publishes `write_root_mode`: `path-prefix`
permits exact file or directory prefixes, while `existing-directory` requires
an existing directory. Root-shape validation runs before root-count recovery so
a directory-only runtime never tells a caller to split exact files into more
requests that cannot run. The controller may select a parent directory only
when that broader authority is explicitly accepted; the service never widens
the request automatically.

The UI is foreground by default. `ui --background` starts one detached local
process on a fixed loopback port; `ui --status` identifies whether it is the
managed process, `ui --open` requests a fresh single-use browser bootstrap, and
`ui --stop` requests graceful shutdown through a random control token. The
token lives only in a bounded product-owned Local state file. Both control
endpoints require the exact loopback Origin and Host; the open path returns only
an exact validated loopback fragment directly to the CLI/browser, and the file
is removed only when its bytes still match the process that published it. There
is no automatic login/startup entry. The bare fixed-port URL creates, replaces,
or restores its in-memory browser session through an exact same-origin empty
POST. `ui --open` remains an optional one-time bootstrap path; an explicitly
supplied invalid or replayed bootstrap token never falls back to automatic
session creation.

## Runtime installation isolation

The safe public path starts the MCP, CLI, and localhost UI through an exact
`uvx --isolated --from subagent-harness-mcp==<version>` requirement. The
`--isolated` flag prevents reuse of a persistent tool installation; each exact
release resolves in a different cache environment. A running old MCP or UI may
therefore finish from its old environment while a new release starts without
removing or rewriting it.

A persistent `uv tool` or pipx install is optional convenience, not part of the
documented update/rollback boundary. One-time legacy migration replaces a
direct installed-tool MCP entry through the client's public lifecycle command;
it never kills the resident task. The package never rewrites MCP client
configuration or clears uv caches automatically.

A legacy resident whose package files changed returns terminal
`UPDATE_QUARANTINED`; it cannot hot-load a replacement safely and is never
retried. The exact isolated registration makes this a one-time migration
boundary rather than a recurring update operation.

Local `runtime_list` and non-refreshing `runtime_check` calls remain available
for diagnosis, but their MCP envelope includes `update_quarantine` whenever
provider delegation is blocked by that resident fence. The localhost UI shows
the same resident as update-quarantined and degraded without rewriting the
underlying adapter or circuit state.

The runtime `state` values in those local results describe adapter and circuit
observations; they are not permission to delegate. A present
`update_quarantine` envelope is the authoritative resident-level block.

Native harness transcripts remain native-harness-owned. Private client state,
credentials, unrelated local tooling, caches, and billing settings stay outside
product ownership and are not parsed as a control contract.

## Stable boundary

The deterministic fake adapter proves the local contract without provider
quota. The Claude managed adapter cannot run normal work until its exact
standalone CLI/SDK/model/reasoning/transport pair passes the dedicated live
no-overage canary. Visible-background, promotion, and native Codex-panel
integration are explicit capability gaps. Current Claude turns select
only the native user setting source: project/local `CLAUDE.md`, `.claude`
hooks, agents, skills, and declared project MCP stay disabled until the
canonical path + content-hash trust gate exists. User skills remain available.
The common `declared-native` policy is capability-scoped in this Windows
release: adapters reject every other policy ID and report project/local context
plus exact auto-compaction-trigger attestation as explicit gaps.

Claude's public Agent SDK publishes typed initialization and rate-limit events.
An explicit provider Refresh opens a connect-only session and validates native
initialization without submitting a model query. The current SDK publishes
exact rate status only with a provider response, so a Refresh that receives no
pre-response rate event reports `Unknown` immediately instead of waiting or
inventing availability. Every Claude execution validates the bound CLI,
subscription auth, credential precedence, and control connection, sends its
one useful task query, then requires exact typed stream identity plus a safe
rate event from that same response before accepting the result. A rate event
may arrive before or after stream initialization; explicit
quota exhaustion or forbidden-credit evidence discards output and auto-pauses
only the affected variant. A later requested task may test that exact variant,
and a safe response reopens it. No reset clock, cached checkpoint, elapsed-time
guess, or separate paid status query controls availability. Canary and ordinary
turns disable 1M context, fast mode, and the usage-credits command per process.
The requested effort is pinned through both the SDK option and provider-native
process environment because `system.init` does not publish an effort field.

Persisting a terminal quota pause is controller-local state work. The service
may retry that idempotent circuit write at most three times, but it never repeats
the provider task. A concurrent circuit/config writer or exhausted SQLite retry
cannot replace the provider verdict with cleanup ambiguity; the original
terminal quota code remains visible and execution leases are released. A
connect-only Refresh that cannot confirm cleanup reports quota unknown and does
not move a ready circuit into recovery state.
Ordinary Claude managed and DeepSeek ACP turns have no product-imposed elapsed
completion deadline; initialization, query submission, cancellation, and
connection cleanup remain bounded. DeepSeek binding discovery and launch-file
revalidation run outside the MCP event loop; discovery has a 15-second
pre-provider deadline, and native launch still locks the exact attested files.
That deadline never applies to a model turn. An explicit DeepSeek upstream HTTP
429 marked temporary may retry the same prompt up to three total attempts. It
does not demote the model; exhaustion returns `RATE_LIMITED` as retryable.
Quota, credit, billing, timeout, and ambiguous failures are never retried.
Claude managed transport accepts complete NDJSON frames up to
8 MiB and still fails closed above that bound or on malformed JSON. Complete
redacted final text is bounded to 65,536 characters in local state; compact
controller responses carry only artifact metadata and on-demand reads are
limited to 8,192 characters per call.
