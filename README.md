# Subagent MCP

Subagent MCP is an MIT-licensed local MCP that lets Codex and other compliant
MCP clients orchestrate native external coding-agent harnesses through one
normalized lifecycle. The distribution, repository, and command name is
`subagent-harness-mcp`.

## Windows Managed Preview

`0.1.0a1` is a pre-release, not a provider-readiness claim. The package contains
the stdio MCP, revisioned local state, deterministic fake adapter, guarded Claude
Code managed adapter, conservative Windows runtime lifecycle, and an opt-in
localhost settings/activity UI.

| Capability | Preview status |
|---|---|
| Normalized fake lifecycle and stdio MCP | Deterministic artifact test |
| Localhost settings/activity UI | Loopback-only artifact test |
| Claude Code managed SDK | `needs_canary`; unavailable until the exact live no-overage gate passes |
| Project/local Claude context and hooks | Disabled until `project_scan`/`project_trust` can bind canonical path + content hash |
| Claude visible-background and promotion | Unsupported |
| Native Codex Subagents-panel row | Unsupported; no documented public host capability |
| Windows release support | Not claimed until installed-artifact Task 9 gates pass |
| macOS/Linux release support | Not claimed in this preview |

Model IDs and reasoning objects are provider-native opaque values. Subagent MCP
does not contain a model allowlist, choose a fallback, enable usage credits, or
change billing settings.

Claude's native SDK publishes its provider rate event only after a turn starts.
Subagent MCP therefore disables 1M context, fast mode, and the in-session usage
credits command for every managed process. The canary refuses output until the
event attests `allowed`, `isUsingOverage=false`, and rejected overage; ordinary
turns require that exact persisted ready attestation and still interrupt on any
later unsafe rate/error event. Missing or unsafe evidence pauses the exact
runtime variant. The selected effort is pinned again through the provider-native
process environment so user settings and skills cannot replace it.

For `0.1.0a1`, ordinary Claude turns load only the native **user** setting
source. User skills remain available, but project/local `CLAUDE.md`, `.claude`
hooks, agents, skills, and declared project MCP are disabled until the trust
gate is implemented; this is reported as an explicit capability gap.

## Install and inspect

After `0.1.0a1` is published, install the exact pre-release into an isolated tool
environment:

```powershell
uv tool install subagent-harness-mcp==0.1.0a1
subagent-harness-mcp --version
subagent-harness-mcp ui
```

`pipx install subagent-harness-mcp==0.1.0a1` is also supported. The UI opens only
on a temporary loopback port and stops with its process. It is settings and
read-only activity, not an agent chat console.

For a generic MCP client, use the installed executable with fixed arguments:

```json
{
  "command": "subagent-harness-mcp",
  "args": ["serve"]
}
```

Windows lifecycle commands are explicit and support `--dry-run`:

```text
subagent-harness-mcp install --runtime <immutable-runtime-dir> --runtime-version <version> --dry-run
subagent-harness-mcp update --runtime <immutable-runtime-dir> --runtime-version <version> --dry-run
subagent-harness-mcp rollback --dry-run
subagent-harness-mcp register --client codex --dry-run
subagent-harness-mcp uninstall --client codex --dry-run
```

Registration uses the client's official command and exact read-back. Uninstall
preserves configuration, state, native sessions, and worktrees by default.

## Public contract

The MCP exposes the same 13 lifecycle/configuration tools for every adapter. See
[architecture](docs/architecture.md), [adapter authoring](docs/adapter-authoring.md),
and the [threat model](docs/threat-model.md). Versioned schemas live in
[`schemas/`](schemas/): config, adapter manifest, normalized agent descriptor,
and MCP tools.

Subagent MCP owns only its explicit config, state, data, and staged runtime
roots. It never uses AgentBridge state or edits existing Codex/Claude
configuration, authentication, caches, transcripts, or processes.

## Development and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for deterministic checks and
[SECURITY.md](SECURITY.md) for private vulnerability reporting. Do not use a
fake adapter, static inspection, or green CI as evidence that a live provider is
ready. Subagent MCP is released under the [MIT License](LICENSE).
