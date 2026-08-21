# Subagent MCP threat model

Subagent MCP protects lifecycle identity, local product state, user-owned
workspaces, and the rule that provider work never opts into usage credits. It
assumes the local OS account and installed native harness are trusted; policy,
hooks, leases, and worktrees are guardrails, not an OS sandbox against malicious
same-user code.

## Main risks and controls

| Risk | Control |
|---|---|
| Duplicate or wrong work | Request idempotency, exact session/model/workspace/context attestation |
| Provider/model downgrade | Opaque exact selection, no fallback, pair-specific canary |
| Billing/overage | Subscription-auth preflight, credential-override rejection, current no-overage gate, circuit pause |
| Recursive orchestration | Strict declared MCP and explicit client/Subagent MCP deny rules |
| Untrusted repository execution | Preview disables project/local Claude context and hooks; later enablement requires canonical path + content-hash trust |
| Local UI attack | Literal loopback bind, one-time fragment bootstrap, HttpOnly strict cookie, CSRF, Host/Origin checks, restrictive CSP, no CORS |
| Destructive lifecycle action | Immutable runtimes, atomic pointer, ownership journal/read-back, identity-matching uninstall/rollback |
| PID reuse | PID plus creation identity and executable digest; otherwise `RECOVERY_REQUIRED` |
| Sensitive output | Bounded key-aware redaction; no credentials, hidden thinking, or raw provider evidence persistence |

## Explicit non-boundaries

Subagent MCP does not secure an already-compromised user account, sandbox a
native harness, guarantee provider availability, infer an exact quota balance,
or provide native Codex Subagents-panel integration through private APIs. It
does not own existing client/native-harness configuration, auth, cache,
transcripts, state, worktrees, or processes.

The deterministic fake adapter and CI prove only local contracts. Provider
readiness requires an installed-artifact live canary and real end-to-end task.
The SDK exposes current overage state only after a turn starts. Every managed
process disables 1M context, fast mode, and the in-session usage-credits
command. Canary output is rejected until an exact safe rate event; ordinary
turns require that persisted ready attestation and any later unsafe rate/error
event causes bounded interrupt plus circuit pause.
In `0.1.0a5`, project/local `CLAUDE.md`, `.claude` hooks, agents, skills, and
declared project MCP are unavailable; only the native user setting source is
selected until `project_scan`/`project_trust` can enforce that gate.
Report vulnerabilities privately as described in `SECURITY.md`; never attach
credentials, account identifiers, private transcripts, or raw provider output.
