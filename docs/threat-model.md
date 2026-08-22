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
| Provider/model downgrade | Explicit ordered selection, stable variant identity, pair-specific canary, and demotion only after terminal quota/credit evidence |
| Claude billing/overage | Subscription-auth preflight, credential-source attestation, live no-overage event before output, circuit pause |
| Pre-authorized provider balance | Exact provider/model route, explicit runtime enablement, and no purchase, reload, or limit-changing capability |
| Recursive orchestration | Strict declared MCP and explicit client/Subagent MCP deny rules |
| Untrusted repository execution | Preview disables project/local Claude context and hooks; later enablement requires canonical path + content-hash trust |
| Local UI attack | Literal loopback bind, one-time fragment bootstrap, HttpOnly strict cookie, CSRF, Host/Origin checks, restrictive CSP, no CORS; background open/stop additionally require a random token from the product-owned Local control record, and open returns only a newly rotated exact loopback URL |
| Destructive lifecycle action | Immutable runtimes, atomic pointer, ownership journal/read-back, identity-matching uninstall/rollback |
| PID reuse | PID plus creation identity and executable digest; otherwise `RECOVERY_REQUIRED` |
| Sensitive output | Bounded key-aware redaction; no credentials, hidden thinking, or raw provider evidence persistence |
| Cross-agent result confusion | One hash-bound successful source, distinct conversations, same verified workspace, and an explicit untrusted-data wrapper expanded only in memory |
| Native catalog leakage | Bounded catalog child emits only provider/model IDs and labels; it never reads the credential document or calls a provider |

## Explicit non-boundaries

Subagent MCP does not secure an already-compromised user account, sandbox a
native harness, guarantee provider availability, infer an exact quota balance,
or provide native Codex Subagents-panel integration through private APIs. It
does not own existing client/native-harness configuration, auth, cache,
transcripts, state, worktrees, or processes.

The deterministic fake adapter and CI prove only local contracts. Provider
readiness requires an installed-artifact live canary and real end-to-end task.
The SDK exposes current rate state only after a turn starts. Every managed
process disables 1M context, fast mode, and the in-session usage-credits
command. The adapter rejects documented credential routes above subscription
OAuth and requires the native init event to attest an OAuth/none API-key source.
Refresh never starts a turn and reports unknown when the native harness emits
no pre-turn evidence. Canary and ordinary output additionally require a safe
live rate event before model output; missing or ambiguous evidence fails closed.
Any later unsafe rate/error event causes bounded interrupt plus circuit pause.

The in-development DeepSeek Harness adapter has no native quota-evidence seam.
Enabling it therefore means the user explicitly authorizes the selected exact
route to consume an existing subscription, promotional allowance, or prepaid
balance. The adapter cannot buy or reload credits, change account limits, or
claim that a promotion remains active. It must not be presented as a
subscription-only or no-cost route.

DeepSeek ACP sessions are connection-owned and currently cannot resume after an
MCP restart. Logical close is allowed only for a terminal persisted execution
that records both that ownership and the explicit resume gap; it never deletes
provider history or substitutes for stopping active native work. A nonterminal
orphan may be failed only when the current harness binding still matches and a
read-only Windows process inventory proves the exact Node, ACP entrypoint, and
conversation config process is absent. Missing or ambiguous evidence leaves
the execution running and requires recovery; this check never kills a process
or calls a provider.

In the current preview, project/local `CLAUDE.md`, `.claude` hooks, agents, skills, and
declared project MCP are unavailable; only the native user setting source is
selected until `project_scan`/`project_trust` can enforce that gate.
Report vulnerabilities privately as described in `SECURITY.md`; never attach
credentials, account identifiers, private transcripts, or raw provider output.
