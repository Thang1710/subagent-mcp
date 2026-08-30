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

> **Stable:** `1.0.29` targets Windows. The MCP, package, localhost UI, and
> Claude Code and DeepSeek native-harness integrations are ready.

## Runtime status

- **Claude Code — Ready.** Uses the native Claude Code harness, provider-native
  model and reasoning settings, subscription OAuth identity, and live
  no-overage evidence before accepting its output. Project/local context and
  exact auto-compaction-trigger attestation remain explicit capability gaps.
- **DeepSeek Harness — Ready.** Uses its native ACP transport and
  harness-published model catalog for bounded tasks. Resume after an MCP restart,
  exact provider quota evidence, interactive input, and declared MCP remain
  explicit capability gaps. A provider-retired model route fails terminally and
  is never replaced without the user's explicit selection.
- **Grok Build — Ready for read-only review.** The Windows adapter uses cached
  native login only and allows no credits, paid overage, or model fallback.
  Version 1.0.29 advertises and accepts `repo_read` only. Bounded writing remains
  **In development** and `workspace_write` requests fail before a native process
  starts. Other explicit gaps are terminal/test/Git execution,
  network/web/browser, MCP/plugins/hooks, nested agents, native worktrees,
  restart recovery, macOS/Linux support, and exact pre-request quota.

Runtime behavior comes from adapters rather than provider-specific branches in
the core.

## Quick start

### 1. Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed,
then register the exact isolated release and start its background UI:

```powershell
winget install --id=astral-sh.uv -e
codex mcp add subagent-mcp -- uvx --isolated --from subagent-harness-mcp==1.0.29 subagent-harness-mcp serve
uvx --isolated --from subagent-harness-mcp==1.0.29 subagent-harness-mcp ui --background
```

Start a new Codex task after registration.

### 2. Configure runtimes

Open `http://127.0.0.1:8765` in a browser. If the background UI was stopped,
start the same exact release again:

```powershell
uvx --isolated --from subagent-harness-mcp==1.0.29 subagent-harness-mcp ui --background
```

The settings and read-only activity UI stays on the fixed loopback port and
does not depend on an active MCP connection.

If a runtime shows **Sign in required**, click **Sign in**. Its native harness
opens the operating system's default browser; Subagent MCP never asks for or
stores credentials. Complete login there, then click **Refresh**. A status
check never opens a browser by itself.

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

For an approval tied to an exact file, pass `inputs` on `agent_spawn` (inside
`task`) or `agent_send`:

```json
[{"path":"docs/specs/review.md","expected_sha256":"<lowercase SHA-256>"}]
```

The MCP hashes each repository-relative file read-only immediately before the
native turn. Status returns the verified hash and configured reasoning
attestation; a changed file fails before the external agent runs.

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

Operational recovery is capped at three actions. Local state work and explicit
pre-provider failures may be retried. DeepSeek also retries an explicit
temporary upstream HTTP 429 at most three total attempts. A generic provider
failure is never replayed automatically: a read-only task returns bounded
guidance for a new explicit attempt, while a write task remains non-retryable
until its write set and possible effects are reconciled. Quota, credit, billing,
timeout, and ambiguous lifecycle failures never trigger another provider call.
Native ACP failures include bounded redacted RPC/provider details when the
harness exposes them; unknown quota remains a provider error, not exhaustion.

DeepSeek routes may use an existing subscription, unlimited offer, or funded
balance that the user authorizes. Subagent MCP never purchases, reloads, or
increases that balance.

## Long-running work

`agent_wait` is a bounded local observation, not a model deadline. If it returns
`running` with `wait_policy=continue_while_running`, the external agent may still
be working or thinking. Keep observing the same conversation; elapsed time alone
never triggers retry, fallback, interruption, or another provider request.

## Concurrent writers

A write task can declare up to 32 repository-relative file or directory roots
in `write_set`. External writers may run concurrently when their canonical
absolute sets are disjoint. Equal paths and parent/child paths conflict; task
and lane names do not affect locking.

Omitting `write_set` gives the execution the whole workspace for backwards
compatibility. Each adapter also enforces the normalized paths at its native
harness boundary. Inspect `runtime_list` before creating a write request:
`write_root_mode=path-prefix` supports exact file or directory prefixes, while
`existing-directory` requires an existing directory. The current DeepSeek
Harness native session advertises `existing-directory` and one writable root.
Subagent MCP never widens an exact-file scope to its parent directory; use that
broader directory only when it is explicitly acceptable, otherwise choose a
runtime that enforces `path-prefix`. Multiple valid directory roots are split
into disjoint writer calls. These leases coordinate Subagent MCP executions;
they are not an operating-system sandbox for unrelated local processes.

On the current Windows public slice, set `cwd` to the checkout root and use
`workspace="current"`. For a DeepSeek write, pass `workspace_write` and exactly
one existing directory relative to `cwd`, for example
`write_set=["Assets/_Project/Core/Scripts/GameSettings"]`. An absolute path,
missing path, or exact file is not a valid `existing-directory` root.

On Windows, the DeepSeek native sandbox must materialize a restricted-token ACE,
so the current user also needs `WRITE_DAC` on the selected directory. Subagent
MCP checks that capability read-only before ACP or provider work. It does not
change ownership or ACLs. A failed check stays `CAPABILITY_MISSING`; it never
falls back to `danger-full-access`.

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
version. This example upgrades 1.0.28 to 1.0.29:

```powershell
uvx --isolated --from subagent-harness-mcp==1.0.28 subagent-harness-mcp ui --stop
codex mcp remove subagent-mcp
codex mcp add subagent-mcp -- uvx --isolated --from subagent-harness-mcp==1.0.29 subagent-harness-mcp serve
uvx --isolated --from subagent-harness-mcp==1.0.29 subagent-harness-mcp ui --background
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
