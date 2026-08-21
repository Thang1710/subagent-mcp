# Contributing to Subagent MCP

Thank you for improving Subagent MCP. Keep changes small, provider-agnostic, and
inside the ownership boundary described in `docs/threat-model.md`.

## Local setup

Use an isolated environment; never reuse or mutate a user tool environment:

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".preview/runtime/test"
uv sync --frozen --group dev
uv run --frozen pytest -p no:cacheprovider -q -m "not real_git_worktree"
uv build --out-dir .preview/dist
```

Deterministic tests must set `SUBAGENT_MCP_HOME` to a temporary directory. A PR
must not call a provider, register a client, install a system runtime, open a
real browser, alter billing, or read private transcript/authentication state.

## Pull requests

- Add a focused regression or contract test for behavior changes.
- Preserve opaque provider model/reasoning values and explicit capability gaps.
- Keep shared lifecycle/state logic out of adapters and thin surfaces.
- Do not commit credentials, account identifiers, raw provider evidence, local
  absolute paths, build artifacts, or generated state.
- Update public schemas/docs when a public contract changes.
- Separate deterministic evidence from live-host evidence in the report.

Security-sensitive findings should follow `SECURITY.md`, not a public issue.
