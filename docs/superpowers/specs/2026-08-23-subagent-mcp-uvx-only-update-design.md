# Subagent MCP Isolated uvx Update Design

Status: approved under the user's standing end-to-end release authority
Date: 2026-08-23
Release target: 1.0.6 on Windows

## Problem

The 1.0.5 README installs a persistent `uv tool` environment for the CLI/UI and
then updates it with `uv tool install --reinstall`. A legacy Codex registration
can still be running `subagent-harness-mcp serve` from that environment. Windows
keeps the executable open; uv can remove package files before replacement of
the locked executable fails, leaving the global command broken.

The warning to close Codex did not make the destructive operation safe. The
failure is in the public update contract, not the normalized agent lifecycle or
provider adapters.

## Decision

The documented public path uses one exact isolated uvx environment per release
for every Subagent MCP process:

```powershell
uvx --isolated --from subagent-harness-mcp==1.0.6 subagent-harness-mcp <command>
```

`--isolated` explicitly prevents uvx from reusing a separately installed
persistent tool. The MCP server and background UI for an old release may keep
running from their old cache while a new exact release is resolved in a
different cache environment. No update command removes or rewrites the active
environment.

Fresh installs no longer require `uv tool install`. A persistent tool remains
an optional advanced convenience outside the safe default and is not part of
the update or rollback contract.

This is intentionally a documentation and packaging-contract correction. A
self-updater cannot make a raw package-manager command transactional, and a
process killer would broaden product ownership into Codex host processes.

## Fresh install and fixed-port UI

After uv is installed, the user explicitly registers the exact isolated command
through Codex's public lifecycle command:

```powershell
codex mcp add subagent-mcp -- uvx --isolated --from subagent-harness-mcp==1.0.6 subagent-harness-mcp serve
uvx --isolated --from subagent-harness-mcp==1.0.6 subagent-harness-mcp ui --background
```

The second command keeps the UI independently available on
`http://127.0.0.1:8765`. It does not add a login/startup entry.

## Update and rollback

For an update from 1.0.5 to 1.0.6:

```powershell
uvx --isolated --from subagent-harness-mcp==1.0.5 subagent-harness-mcp ui --stop
codex mcp remove subagent-mcp
codex mcp add subagent-mcp -- uvx --isolated --from subagent-harness-mcp==1.0.6 subagent-harness-mcp serve
uvx --isolated --from subagent-harness-mcp==1.0.6 subagent-harness-mcp ui --background
```

The user starts a fresh Codex task after changing the entry. Existing tasks keep
their old runtime until they end. Rollback repeats the same sequence with the
two exact versions reversed.

No step invokes `uv tool install --reinstall`, clears uv caches, kills a Codex
or provider process, edits a configuration file directly, or changes provider
billing.

## One-time legacy migration

If the existing MCP entry directly invokes `subagent-harness-mcp serve`, the
user first replaces the registration through the public `codex mcp remove/add`
commands above. The legacy stdio process is allowed to finish naturally. The
user closes every old Codex window once before removing the now-unused
persistent tool; leaving that tool installed is safe because every documented
new invocation includes `uvx --isolated`.

Subagent MCP never rewrites Codex global configuration automatically.

## Bounded error recovery

Provider/task retry, refresh, or repair remains bounded to three controller
attempts. Update recovery never repeats a destructive install. A failed UI
stop, registration readback, or fresh-start check may be refreshed or repaired
at most three times; the third failure is terminal and reports the exact manual
next action. Ambiguous quota or billing evidence never causes a provider retry,
model fallback, or paid request.

## Alternatives rejected

- Reordering the existing reinstall plus a preflight still leaves a destructive
  command whose safety depends on the user running the preflight.
- A self-updater runs from the environment it is replacing and cannot make uv's
  Windows deletion atomic.
- Automatically terminating Codex/MCP/provider processes risks unrelated work
  and violates exact ownership boundaries.

## Acceptance criteria

- README fresh install, UI, update, and rollback commands use an exact
  `uvx --isolated --from` requirement.
- README contains no `uv tool install` or `uv tool install --reinstall` command
  for Subagent MCP.
- Direct installed-tool MCP registration remains absent.
- Architecture and the Windows update-isolation authority describe the same
  uvx-only default and one-time legacy boundary.
- A packaged no-provider smoke starts the exact isolated uvx MCP, lists its 14
  tools, and calls only `runtime_list`.
- A no-provider UI drill starts the source release in background, serves `/`
  and `/app.js` after the uvx launcher exits, reports healthy status, stops
  cleanly, repeats with the target release, then repeats with the source release
  as rollback on the same temporary home and fixed test port.
- Wheel/sdist, full safe tests, privacy scan, public mirror, GitHub release,
  PyPI, and MCP Registry gates pass before 1.0.6 is claimed released.
- The owner's Codex global configuration is not changed by this task.
