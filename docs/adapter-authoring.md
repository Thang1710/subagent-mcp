# Subagent MCP adapter authoring

Third-party adapters are ordinary Python distributions discovered only through
the `subagent_harness_mcp.adapters` entry-point group. Installation of adapter
code is an explicit user action; repositories and model output cannot request
automatic installation.

```toml
[project.entry-points."subagent_harness_mcp.adapters"]
my-runtime = "my_adapter:create_adapter"
```

The factory takes no arguments and returns an object implementing the public
async protocol in `subagent_harness_mcp.adapters.base`. Its
`subagent_harness_mcp.contracts.AdapterManifest` must use adapter API `1.0.0`
and declare one stable runtime/provider/harness identity, platforms,
transports, semantic permissions, capabilities, and provider-native reasoning
schema. Model IDs are bounded opaque strings; never impose a core model list or
silent fallback.

Required async operations are `probe`, `resolve_context`, `spawn`, `send`,
`snapshot`, `interrupt`, `close`, and `open_session`. Return only normalized,
bounded, redacted public values. Do not expose hidden thinking, credentials, or
raw transcript/tool output. Do not write the config/database, invent sessions,
parse private daemon state, load repository MCP configuration, or target a
process from PID alone.

An adapter that can bootstrap a live compatibility check may also implement
`CanaryAdapter.runtime_canary`. A canary must bind the exact adapter/runtime
identity and cannot mark another model, reasoning policy, or transport ready.
Quota, billing, terminal, identity, or cleanup uncertainty fails closed.

Entry-point conflicts, import errors, invalid manifests, and incompatible API
versions quarantine only the affected runtime. Use the built-in deterministic
fake adapter as the behavioral reference. Published resource schemas are
available at `importlib.resources.files("subagent_harness_mcp") / "schemas"`.

Before release, test discovery from an installed wheel, every terminal state,
needs-input/follow-up, restart/open-session, interruption/cleanup, exact
model/workspace attestation, idempotent retries, and redaction. Live-provider
evidence remains a separate, explicit gate.
