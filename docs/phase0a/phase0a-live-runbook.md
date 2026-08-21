# Phase 0a live-gate runbook

This runbook separates deterministic implementation from host/provider evidence. Fake-process tests never create accepted `live-*.json` fixtures and never make a report gate pass.

## Current deterministic checkpoint

Task 1 supplies the one-shot approval and bounded process boundary. Task 2 supplies the no-model host/auth/manifest/help checks, residual classification, sanitized fixture helpers, and the Windows file-handle cleanup matrix.

The Task 2 host command is intentionally deferred at this checkpoint. Do not run it, a preview, or a provider-capable command from a deterministic task worker.

## Controller-only approval storage preparation

Before any `approve-scope` command, the root controller must receive direct user
approval for this local ACL/mode action. It must then prepare only the fixed
cwd-relative `.phase0a/live` storage root; this never launches Claude or executes
a preview/approval scope. The current inherited-ACL root requires explicit repair:

```powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common prepare-approval-storage --root .phase0a\live --repair-existing
```

For a fresh missing root, omit `--repair-existing`:

```powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common prepare-approval-storage --root .phase0a\live
```

The command accepts no alternate, absolute, indirect, non-directory, or
wrong-owner root. It creates only the fixed storage directories and makes new
`live` and direct `approvals` owner-only. Existing insecure storage fails without
the explicit repair flag; repair refuses nonempty insecure `approvals` and changes
only the direct `live`/`approvals` ACL or mode. It never recurses or alters child
contents. `approve-scope` remains fail-closed and never prepares storage itself.

Any commit invalidates a scope's HEAD-bound pending digest and approval. The prior
Group A pending digest/approval must be discarded: regenerate Group A preview,
show its new digest, obtain a new direct approval, then create its receipt.

## Deferred read-only host lane

After the controller receives the separate authority required by the committed plan, run from a clean tracked tree:

```powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_host --root .phase0a\live\host --cli "$env:USERPROFILE\.local\bin\claude.exe" --project-root .
```

The command may execute only the bound standalone CLI's version, auth status, agents JSON, and explicitly help-safe surfaces. It also runs disposable local Windows file-sharing checks. On a complete pass it writes the private ignored `bound-identity.json` consumed by Group A; Group A refuses preview/execute when that identity/version record is absent or drifted. It must stop on credential override, identity drift, unsupported auth, blocked project manifest, or handle-release failure. It never launches a model, logs in, changes configuration, creates a Claude row/worktree, or enables usage credits.

## Mandatory residual inventory

Before any provider-capable group, the controller runs the committed inventory command:

```powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_host inventory --root .phase0a\live\inventory --cli "$env:USERPROFILE\.local\bin\claude.exe" --project-root .
```

Only `expected_clean` permits a later provider-capable preview. `plan_owned_residual` routes to the separately approved Group G audit. `user_or_unknown_residual` and `recovery_required` are never adopted or removed automatically.

## Evidence publication

Only a controller-authorized host run may project sanitized evidence into `tests/fixtures/phase0a/current/live-host.json` and `live-windows-handles.json`, then rebuild `evidence-index.json`. Never copy raw help, auth, roster, path, process, account, or local identity values into a committed fixture.

Groups A-H2 retain their own digest-specific approval boundaries. Plan approval or this runbook never authorizes a live Claude/provider call, native lifecycle mutation, proof-file action, cleanup, or external review session.

## Group G disposable release

Group G is limited to the fixed Group C and D roots and re-derives their exact
row/worktree ownership from layout, pending scope, consumed approval receipt and
claim, consumed side-effect ledger, local state, lease, WorktreeCreate event,
roster identity, process snapshot, and Git state. It never authorizes from an
inventory count or adopts a root discovered by scanning. Group F is deliberately
reported as retained row-only state because the current live plan requires a
matching WorktreeRemove event and path disappearance for every Group G target,
while Group F creates no worktree.

After the controller has separately authorized a live removal preview, run from
a clean tracked tree:

```powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background cleanup --preview --root .phase0a\live\background-cleanup --cli "$env:USERPROFILE\.local\bin\claude.exe"
```

The preview prints the exact local short IDs, canonical worktree paths, row
fingerprints, creation-approval lineage, full audit, ordered `claude rm` contract,
and Group G scope digest. Scope v1 binds the canonical cleanup-contract SHA-256
as an explicit exact target alongside every canonical path. Any failed audit
produces `RECOVERY_REQUIRED` and no removal scope.

`claude rm` is the provider-owned destructive lifecycle command: it deletes only
the displayed newly created disposable native session and worktree. It is the
sole transcript-deletion carve-out and never applies to a pre-existing, user, or
unknown row. If the user does not immediately approve the displayed exact digest
and targets, retain them. No earlier approval carries over.

Only after the controller creates `approved-G.json` for that exact digest may it
run:

```powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background cleanup --execute --approval .phase0a\live\approvals\approved-G.json --root .phase0a\live\background-cleanup --cli "$env:USERPROFILE\.local\bin\claude.exe"
```

Execute re-discovers, re-audits, and re-hashes the complete batch before the
first removal. Each concrete argv is appended through `consume_side_effect`
before invocation. A refusal or failed WorktreeRemove/path/registry/roster or
unrelated-state postcheck stops the batch and writes ignored recovery state.
There is no Git, unlink, filesystem-delete, retry, or transcript-edit fallback.
Only a real approved execution may later publish the sanitized
`live-worktree-remove.json` fixture and update the evidence index.
