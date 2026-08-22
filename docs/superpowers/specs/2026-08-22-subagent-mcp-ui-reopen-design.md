# Subagent MCP Managed UI Reopen Design

**Release target:** `0.1.0a21`

## Problem

The managed localhost UI can remain healthy on fixed port `8765` after its
single-use bootstrap URL has been consumed. Opening the bare loopback URL in a
new browser tab cannot authenticate and correctly fails with `Session
unavailable`. The CLI currently has status and stop controls but no safe way to
request another browser session from an already-running background UI.

## Design

Add `subagent-harness-mcp ui --open`. It reads the existing private canonical
control record, verifies the requested port is the managed Subagent MCP UI, and
sends one empty loopback POST authenticated by the same control token already
used for graceful stop. The UI atomically rotates a fresh bootstrap token and
returns one exact loopback fragment URL. The CLI validates that URL and passes
it directly to the system browser without printing or persisting it.

The new endpoint is available only to a managed UI with a control token. It
requires literal loopback peer/Host/Origin, rejects query/path ambiguity, caps
headers and response bytes, and returns no config, state, prompt, transcript,
credential, or provider data. Existing browser sessions remain valid; each new
bootstrap token is still single-use. A wrong/stale control record, foreground
UI, browser refusal, malformed response, or port mismatch fails closed.

## Direct managed URL amendment

The fixed-port managed UI must also be usable by opening or reloading
`http://127.0.0.1:8765/` directly after that browser profile has been
authorized. On boot, the package-owned page posts to the session endpoint
without a token; the server may only restore a valid HttpOnly session cookie.
Missing or stale cookies remain rejected, so another local process cannot mint
a session by spoofing HTTP headers. The POST still requires an exact loopback
Host and same-origin Origin. A new browser profile receives its one-time token
from the background launch or `ui --open`; the control token is never sent to
the browser.

## Non-goals

- No Windows login/startup registration.
- No remote bind, HTTP MCP transport, account page, or browser extension.
- No provider/model/quota call and no configuration or SQLite mutation.
- No framework or dependency addition.

## Acceptance

- `ui --open` opens an already-running managed UI and prints only a stable
  success message without the bootstrap token.
- The returned token authenticates once; replay and wrong control tokens fail.
- Foreground/unmanaged/mismatched/malformed cases fail before browser open.
- Existing `--background`, `--status`, `--stop`, and bare foreground behavior
  stay compatible.
- An authorized browser profile opens and reloads the bare managed URL without
  another CLI command; every new profile still requires one bootstrap.
