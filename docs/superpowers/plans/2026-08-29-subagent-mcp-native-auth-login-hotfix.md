# Native Harness Sign-in Hotfix Plan

**Goal:** Release Subagent MCP 1.0.26 so an `auth_required` runtime offers an
explicit, user-approved sign-in action that opens the native harness login in
the operating system's default browser.

**Scope:** Claude Code is the first adapter to publish this optional capability.
The core remains provider-neutral. Grok Build, Cursor, Qwen, model selection,
quota policy, credits, overage, and provider execution are out of scope.

## Safety contract

- `runtime_check` remains read-only and never starts login or provider work.
- `runtime_authenticate` requires a request ID and explicit user approval.
- The adapter launches the resolved standalone CLI with the exact argv
  `claude auth login`, `shell=False`, and no inherited console streams.
- Subagent MCP never reads, stores, relays, or submits credentials and never
  changes billing, credits, overage, model, or quota settings.
- Durable idempotency prevents a replay or ambiguous prior receipt from opening
  another login process.
- The localhost endpoint requires the existing authenticated session and CSRF
  token. The UI button itself is the user's confirmation.
- Completion is verified by a later `runtime_check`; login launch alone does
  not claim the runtime is ready.

## Tasks

1. Add failing adapter and service tests for exact argv, no-shell launch,
   already-authenticated behavior, and ambiguous-receipt idempotency.
2. Add the optional adapter protocol, durable service action, public MCP tool,
   schema metadata, and redacted responses.
3. Add the CSRF-protected localhost endpoint and an `auth_required` Sign in
   action that delegates browser opening to the native CLI.
4. Update public documentation and version metadata to 1.0.26.
5. Run focused tests, the full safe suite, formatting/static checks, secret and
   personal-path scans, and wheel/sdist installed-artifact tests.
6. Obtain an independent Claude review, prove the exact fresh artifact, commit
   with the repository owner's identity, then publish and read back GitHub and
   PyPI 1.0.26.

## Acceptance

- Logged-out Claude returns `auth_required` plus the explicit system-browser
  action; no browser opens during a status check.
- One approved action launches exactly one native login process; replays cannot
  duplicate it.
- Logged-in Claude does not open another login process and a subsequent check
  can progress through the existing canary/ready circuit contract.
- No provider task, credit, overage, credential, Codex configuration, or
  user-owned transcript is touched by the hotfix.
