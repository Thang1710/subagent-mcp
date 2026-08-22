# Subagent MCP Windows Update Isolation Design

## Problem

On Windows, Codex can keep the executable and Python environment created by
`uv tool install` open for the lifetime of an MCP stdio process. Reinstalling
that same tool environment may remove the package and then fail to replace the
locked launcher, leaving the command temporarily unusable.

Stopping the localhost UI is insufficient because the UI and Codex MCP process
are independent. Automatically killing every Codex-owned MCP process from the
package installer would also be an unsafe and provider-host-specific contract.

## Smallest safe boundary

Use two uv-managed environments for two different lifetimes:

- `uv tool install subagent-harness-mcp==<version>` provides the persistent
  user-facing CLI and localhost UI.
- Codex starts the stdio server with
  `uvx --from subagent-harness-mcp==<version> subagent-harness-mcp serve`.
  `uvx` uses a separately cached environment outside the persistent tool
  installation.

This follows the official Codex stdio form `codex mcp add <name> -- <command>`
and uv's documented isolated `uvx --from <package==version> <command>` form.
The exact version remains pinned for reproducibility and rollback.

The product never edits Codex configuration automatically. The README gives
the explicit add/update commands and tells the user to start a new Codex task
after changing the MCP entry.

## Install, update, and rollback

Fresh installation installs the CLI/UI tool and registers the pinned uvx
command. Updating stops the UI, reinstalls only the persistent CLI/UI tool,
then replaces the Codex MCP entry with the new pinned uvx version. Existing
users whose entry directly invokes `subagent-harness-mcp serve` must close
Codex once before the first migration because that old process already holds
the persistent tool environment.

Rollback uses the same sequence with the previous exact version. No command
cleans uv caches, kills providers, enables billing, or mutates provider state.

## Evidence boundary

Deterministic tests require the README to keep the persistent-tool and uvx
commands separate. Release acceptance still installs wheel and sdist. A local
no-provider smoke must start the packaged MCP through the documented uvx
command, list its tools, and call only `runtime_list`.
