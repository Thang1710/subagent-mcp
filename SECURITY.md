# Security policy

## Reporting a vulnerability

Use the repository's private security-advisory channel when it is available.
Do not publish credentials, tokens, account identifiers, private transcripts,
or raw provider output in an issue. If no private channel is visible, open a
minimal issue asking the maintainers for a private contact without including
the sensitive details.

## Preview boundary

The Windows Managed Preview must fail closed when model, harness, workspace,
authentication source, terminal lifecycle, or no-overage evidence is missing.
Subagent MCP never enables usage credits or modifies billing settings. Existing
Codex, Claude, and AgentBridge configuration, authentication, caches,
transcripts, state, and processes are outside its ownership boundary.

Only a release installed from published artifacts and passing its documented
real-host gates may be treated as provider-ready.
