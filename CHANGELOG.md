# Changelog

All notable changes to Subagent MCP are documented here.

## 0.1.0a2 - Unreleased

- Fix Windows private-root creation when the runner's default object owner is
  different from its current user SID.
- Make GitHub Release publication explicitly target this repository after the
  PyPI trusted-publishing step.
- Replace internal release notes in the public README with a short install path,
  verified Codex registration command, and architecture diagram.

- Publish the `subagent-harness-mcp` Python package, typed console entry point,
  four versioned public schemas, static localhost UI assets, and MIT metadata.
- Add revisioned JSON configuration, additive SQLite state, normalized adapter
  contracts, deterministic fake lifecycle, and the exact 13-tool stdio MCP.
- Add a capability-gated Claude Code managed adapter that remains
  `needs_canary` until exact identity, model, context, terminal, cleanup, and
  `isUsingOverage=false` evidence passes for that adapter pair.
- Add a loopback-only settings/activity UI with bootstrap, cookie, CSRF, Host,
  Origin, and content-security controls.
- Add immutable Windows runtime staging, atomic pointer update/rollback,
  official-client read-back, conservative uninstall, and dry-run commands.
- Add clean wheel/sdist artifact smoke tests and deterministic, manually gated
  trusted-publishing workflows.

Explicit preview gaps: live Claude readiness, visible-background transport,
promotion, native Codex-panel integration, and release support outside Windows.
