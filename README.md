# Subagent MCP

<!-- mcp-name: io.github.Thang1710/subagent-mcp -->

**Independent harnesses. One Codex orchestrator.**

When one model plans a change, implements it, and reviews it, the reviewer shares
the author's context and blind spots. It can end up confirming its own plan
instead of testing it.

Subagent MCP keeps Codex as the main agent and final decision-maker while
delegating bounded work to external agent runtimes. Each runtime is a model
paired with its native harness, so Codex can get implementation or review from
an independent model with different context and assumptions.

That expands Codex's effective sub-agent pool and can use provider quota you
already have. Subagent MCP never enables, purchases, auto-reloads, or silently
opts into usage credits or paid overage.

Adapters translate every native harness into the same lifecycle: delegate,
observe, steer, and close. The core hard-codes no provider role or model name.

> **Stable:** `1.0.11` targets Windows. The MCP, package, localhost UI, and
> Claude Code and DeepSeek native-harness integrations are ready.

## Runtime status

- **Claude Code — Ready.** Uses the native Claude Code harness, provider-native
  model and reasoning settings, subscription OAuth identity, and live
  no-overage evidence before accepting its output.
- **DeepSeek Harness — Ready.** Uses its native ACP transport and
  harness-published model catalog for bounded tasks. Resume after an MCP restart,
  exact provider quota evidence, interactive input, and declared MCP remain
  explicit capability gaps.

No other runtime is supported yet. Future runtimes use adapters rather than
provider-specific branches in the core.

## Quick start

### 1. Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed,
then register the exact isolated release and start its background UI:

```powershell
winget install --id=astral-sh.uv -e
codex mcp add subagent-mcp -- uvx --isolated --from subagent-harness-mcp==1.0.11 subagent-harness-mcp serve
uvx --isolated --from subagent-harness-mcp==1.0.11 subagent-harness-mcp ui --background
```

Start a new Codex task after registration.

### 2. Configure runtimes

Open `http://127.0.0.1:8765` in a browser. If the background UI was stopped,
start the same exact release again:

```powershell
uvx --isolated --from subagent-harness-mcp==1.0.11 subagent-harness-mcp ui --background
```

The settings and read-only activity UI stays on the fixed loopback port and
does not depend on an active MCP connection.

Current and recent activity is listed by external agent. Select a row to inspect
its model, native harness, workspace, permissions, write set, current stage,
lifecycle, elapsed time, and redacted terminal result. Prompts, transcripts,
hidden thinking, and raw provider events are never displayed.

### 3. Delegate

Ask Codex in plain language:

> Use Subagent MCP to ask an external agent to review this change, then
> evaluate its findings independently.

Codex chooses what to delegate, observes the result, and keeps the final
judgment. Lifecycle responses are compact by default; full redacted reports
remain in local product state and can be read or relayed later by hash-bound
reference.

## Models and fallback order

Each native harness publishes its own model choices. The UI shows friendly names
and an ordered priority stack; exact provider IDs remain available for advanced
routes.

When a provider explicitly reports exhausted quota or credit
(`QUOTA_PAUSED`), Subagent MCP moves that exact model to the bottom for future
tasks. It does not retry the failed task. Ambiguous failures, crashes, and
timeouts do not reorder models or trigger another paid request.

That failed task stays terminal, but the next explicit delegation checks the
provider again live; no cached reset clock or wait-until checkpoint substitutes
for that check. When the native harness exposes no safe pre-request quota
endpoint, status stays unknown rather than inferred exhausted.

Operational recovery is capped at three actions. Only local state work or an
explicitly retryable pre-provider failure may be retried; a provider task that
already failed is never sent again automatically.

DeepSeek routes may use an existing subscription, unlimited offer, or funded
balance that the user authorizes. Subagent MCP never purchases, reloads, or
increases that balance.

## Concurrent writers

A write task can declare up to 32 repository-relative file or directory roots
in `write_set`. External writers may run concurrently when their canonical
absolute sets are disjoint. Equal paths and parent/child paths conflict; task
and lane names do not affect locking.

Omitting `write_set` gives the execution the whole workspace for backwards
compatibility. Each adapter also enforces the normalized paths at its native
harness boundary. The current DeepSeek Harness native session accepts one
writable root; a multi-root task is decomposed by the controller into several
disjoint writer calls rather than widened. True single-session multi-root stays
reserved for a future harness that advertises official ACP
`additionalDirectories` and enforces it natively. These leases coordinate
Subagent MCP executions; they are not an operating-system sandbox for unrelated
local processes.

## How it fits together

```mermaid
flowchart LR
    C["Codex<br/>Main agent & orchestrator"]
    M["Subagent MCP<br/>Gateway"]
    UI["Localhost UI<br/>Settings & activity"]

    C -->|"delegate · steer · observe"| M
    UI --> M

    subgraph E["External agent runtimes — adapter-driven"]
        R1["Model<br/>+<br/>native harness"]
        R2["Model<br/>+<br/>native harness"]
        RN["Future runtimes<br/>via adapters"]
    end

    M -->|"normalized lifecycle"| R1
    M -->|"normalized lifecycle"| R2
    M -->|"normalized lifecycle"| RN
```

Subagent MCP owns lifecycle normalization, status, redaction, leases, and
circuits. Each adapter translates that contract to its native harness. See
[Architecture](docs/architecture.md) for the full contract.

## Update or roll back on Windows

Switch versions without reinstalling an environment that may still be running.
The first command uses the source version; the add/start commands use the target
version. This example upgrades 1.0.10 to 1.0.11:

```powershell
uvx --isolated --from subagent-harness-mcp==1.0.9 subagent-harness-mcp ui --stop
codex mcp remove subagent-mcp
codex mcp add subagent-mcp -- uvx --isolated --from subagent-harness-mcp==1.0.11 subagent-harness-mcp serve
uvx --isolated --from subagent-harness-mcp==1.0.11 subagent-harness-mcp ui --background
```

Start a fresh Codex task after changing the entry. Existing tasks keep their old
runtime until they end. Use the same sequence with the exact versions reversed
to roll back.

An already-running MCP that reports `UPDATE_QUARANTINED` cannot hot-load the
replacement safely. Do not retry that resident; finish with native fallback and
use the new exact registration from a fresh task.

For a one-time migration from a direct `subagent-harness-mcp serve` entry,
replace the registration first and let the legacy task end naturally. After
replacing it, close every Codex window once before optionally removing the
now-unused persistent tool. Leaving it installed is safe because every new
command above uses `uvx --isolated`.

Subagent MCP does not edit Codex configuration, kill Codex/provider processes,
or clear uv caches on its own.

## Safety and billing

- Subagent MCP never enables usage credits or changes billing settings.
- Claude tasks can consume included subscription quota. Each task validates the
  bound CLI, subscription authentication, credential precedence, and control
  connection, then requires safe rate evidence from the same response before
  accepting its output.
- Provider Refresh sends no model prompt. If the native harness cannot expose
  exact rate evidence before a response, status remains unknown rather than
  inventing a quota result or using a reset clock.
- Fallback occurs only after explicit quota exhaustion. Unsafe or ambiguous
  evidence never triggers another paid request.
- Native transcripts remain owned by the native harness. Product state stays in
  explicit local roots, and agent output must be treated as untrusted advice.

Read [Security](SECURITY.md) and the
[Threat model](docs/threat-model.md) before enabling write access.

## Project

- [Architecture](docs/architecture.md)
- [Adapter authoring](docs/adapter-authoring.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [MIT License](LICENSE)
