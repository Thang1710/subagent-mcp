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

Every execution records requested/effective model, reasoning, transport,
workspace, session, and context identity. Provider differences appear only in
the descriptor and explicit capability gaps. Unknown or mismatched critical
identity fails closed. Model fallback is never implicit: an adapter may expose
the user's explicit ordered variants, and Codex may advance only after the
current variant returns the terminal `QUOTA_PAUSED` classification.

Each runtime can publish a neutral `delegation_priority` from 0 to 100. Higher
values appear first in `runtime_list` and the localhost UI so the orchestrator
can prefer one external runtime over another without hard-coded provider roles.
Selection remains explicit: the MCP does not silently reroute a request or
control the Codex host's native-subagent fallback.

Restart handling follows the adapter's advertised capability, never a simulated
resume. A terminal session may be closed logically without reopening the native
harness only when persisted evidence says its lifetime belonged to the old
connection, `resume_after_restart` is an explicit gap, and the current adapter
does not advertise resume. Active executions never use this path.

## State and ownership

`SUBAGENT_MCP_HOME` creates `config/`, `state/`, and `data/` children for tests
and portable use. Otherwise OS-native roots are used. Config writes are
revisioned atomic replacements; SQLite migrations are additive. Versioned
runtimes are immutable and selected through an atomic pointer. Conservative
uninstall removes only byte/identity-matching owned resources and preserves
user config, state, sessions, and worktrees.

The UI is foreground by default. `ui --background` starts one detached local
process on a fixed loopback port; `ui --status` identifies whether it is the
managed process, and `ui --stop` requests graceful shutdown through a random
control token. The token lives only in a bounded product-owned Local state file,
the stop endpoint still requires the exact loopback Origin and Host, and the
file is removed only when its bytes still match the process that published it.
There is no automatic login/startup entry.

Native harness transcripts remain native-harness-owned. Private client state,
credentials, unrelated local tooling, caches, and billing settings stay outside
product ownership and are not parsed as a control contract.

## Preview boundary

The deterministic fake adapter proves the local contract without provider
quota. The Claude managed adapter cannot run normal work until its exact
standalone CLI/SDK/model/reasoning/transport pair passes the dedicated live
no-overage canary. Visible-background, promotion, and native Codex-panel
integration are explicit preview gaps. Ordinary `0.1.0a14` Claude turns select
only the native user setting source: project/local `CLAUDE.md`, `.claude`
hooks, agents, skills, and declared project MCP stay disabled until the
canonical path + content-hash trust gate exists. User skills remain available.

The native SDK emits its rate-limit attestation only after a turn starts, not
during `connect(None)`. The provider Refresh path therefore performs only a
connection-level probe and reports `Unknown` when no pre-turn rate evidence is
available; it never launches a canary or provider task. Before any turn, the
adapter rejects documented non-subscription credential routes. The native
`system/init` event must attest OAuth/none as the active API-key source, and a
live rate event must report `isUsingOverage=false` with overage rejected before
output is accepted. Missing or ambiguous evidence leaves the runtime gated.
Canary and ordinary turns also disable 1M context, fast mode, and the
usage-credits command per process. Any later unsafe rate event interrupts and
pauses the circuit. The requested effort is pinned through both the SDK option
and provider-native process environment because `system.init` does not publish
an effort field. Complete redacted final text is bounded to 65,536 characters
in local state; compact controller responses carry only artifact metadata and
on-demand reads are limited to 8,192 characters per call.
