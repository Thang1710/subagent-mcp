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

The stdio server declares 13 tools from `schemas/tools-v1.json`. Core lifecycle
is `runtime_list/check`, `agent_spawn`, `agent_status`, `agent_send`,
`agent_wait`, `agent_interrupt`, and `agent_close`; configuration, trust,
canary, scan, and workspace-release operations retain their explicit approval
metadata. Side-effecting requests use idempotency keys.

Every execution records requested/effective model, reasoning, transport,
workspace, session, and context identity. Provider differences appear only in
the descriptor and explicit capability gaps. Unknown or mismatched critical
identity fails closed; there is no fallback.

## State and ownership

`SUBAGENT_MCP_HOME` creates `config/`, `state/`, and `data/` children for tests
and portable use. Otherwise OS-native roots are used. Config writes are
revisioned atomic replacements; SQLite migrations are additive. Versioned
runtimes are immutable and selected through an atomic pointer. Conservative
uninstall removes only byte/identity-matching owned resources and preserves
user config, state, sessions, and worktrees.

Native harness transcripts remain native-harness-owned. Private client state,
user authentication stores, unrelated local tooling, caches, and billing
settings are outside the product boundary.

## Preview boundary

The deterministic fake adapter proves the local contract without provider
quota. The Claude managed adapter cannot run normal work until its exact
standalone CLI/SDK/model/reasoning/transport pair passes the dedicated live
no-overage canary. Visible-background, promotion, and native Codex-panel
integration are explicit preview gaps. Ordinary `0.1.0a13` Claude turns select
only the native user setting source: project/local `CLAUDE.md`, `.claude`
hooks, agents, skills, and declared project MCP stay disabled until the
canonical path + content-hash trust gate exists. User skills remain available.

The native SDK emits its rate-limit attestation only after a turn starts, not
during `connect(None)`. The provider Refresh path therefore performs only a
connection-level probe and reports `Unknown` when no pre-turn rate evidence is
available; it never launches a canary or provider task. Canary and ordinary
turns start with 1M context, fast mode, and the usage-credits command disabled
per process, and no
canary output is accepted before an exact safe rate event. Ordinary turns
require the resulting exact ready attestation because the CLI does not promise
a fresh rate event before every assistant message; any later unsafe rate/error
event still interrupts and pauses the circuit. The requested effort is pinned
through both the SDK option and provider-native process environment because
`system.init` does not publish an effort field.
