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

Use one exact isolated uvx environment per release and never update it in
place:

- Codex starts stdio with `uvx --isolated --from
  subagent-harness-mcp==<version> subagent-harness-mcp serve`.
- The CLI and fixed-port localhost UI use the same exact isolated form.
- A different exact version resolves in a different cache environment, so the
  old process may finish without its files being removed or rewritten.

This follows the official Codex stdio form `codex mcp add <name> -- <command>`
and uv's documented `uvx --isolated --from <package==version> <command>` form.
The exact version remains pinned for reproducibility and rollback, while
`--isolated` prevents reuse of a persistent tool environment.

The product never edits Codex configuration automatically. The README gives
the explicit add/update commands and tells the user to start a new Codex task
after changing the MCP entry.

## Install, update, and rollback

Fresh installation registers the pinned isolated uvx command and starts the UI
through the same exact release. Updating stops the old exact UI, replaces the
Codex MCP entry with the new exact uvx version, and starts the new UI. It never
reinstalls an environment held by a running process.

Existing users whose entry directly invokes `subagent-harness-mcp serve`
replace that registration first and let the old task finish naturally. They
close Codex once only before optionally removing the now-unused persistent
tool. Leaving it installed cannot affect new documented invocations because
they include `--isolated`.

Rollback uses the same sequence with the previous exact version. No command
cleans uv caches, kills providers, enables billing, or mutates provider state.

## Evidence boundary

Deterministic tests require every README process command to use the exact
isolated uvx form and forbid a persistent-tool reinstall. Release acceptance
still installs wheel and sdist. A local no-provider smoke must start the
packaged MCP through the documented isolated uvx command, prove its process is
outside the persistent tool environment, list its tools, and call only
`runtime_list`.
