# Subagent MCP Bounded Quota Recovery Design

Status: approved under the user's standing Critical-fix and release authority
Date: 2026-08-23
Release target: 1.0.7 on Windows

## Problem

Claude can return an explicit terminal `QUOTA_PAUSED` or
`USAGE_CREDITS_FORBIDDEN` verdict after the MCP service captured a ready circuit.
While that native turn is running, the localhost UI or another MCP process can
advance the same circuit. The current `_pause_after_quota_error` catches every
deviation, including a normal compare-and-swap race or SQLite contention, and
replaces the provider verdict with a synthetic `RECOVERY_REQUIRED` error.

That substitution is destructive. `agent_spawn` and `agent_send` interpret the
synthetic code as unverified native cleanup, retain writer leases, and persist a
conversation that cannot be reused or closed through the normal path. The user
sees a quota/controller prerequisite failure even when the provider task was
cleanly stopped and the account still has included plan quota.

A second path can wedge the circuit without sending a model prompt. Claude's
connect-only quota refresh cannot obtain response-only rate evidence. If its
control connection also fails to disconnect cleanly, the adapter currently
returns `RECOVERY_REQUIRED`; `runtime_check(refresh_quota=True)` then changes a
ready circuit to `recovery_required`, even though no native task ran and the
default product has no cleanup receipt that can reopen it.

Separately, a Codex task whose already-running stdio MCP was mutated by a legacy
persistent-tool update returns `UPDATE_QUARANTINED` before every provider call.
That resident cannot safely hot-load replacement Python files. Release 1.0.6's
exact isolated uvx registration prevents recurrence after migration, but cannot
retroactively repair an MCP process that was already loaded.

## Decision

### Preserve provider truth

An explicit provider quota or no-overage verdict always remains the public
terminal error. A local circuit/config persistence failure must never replace
it with `RECOVERY_REQUIRED`.

`_pause_after_quota_error` may retry only the local circuit pause operation. It
reloads the exact circuit and makes at most three bounded compare-and-swap
attempts. These attempts:

- never call a provider;
- never repeat, resume, or switch the failed task;
- stop immediately on pair drift or a non-ready circuit owned by another
  recovery action;
- treat an already paused exact circuit as success;
- catch only state/database failures, never cancellation or process-control
  exceptions.

After the bounded attempts it performs the existing best-effort model-priority
update once. If the circuit pause still was not recorded, the service returns
the original quota code with a sanitized warning and an exact next action. The
failed task stays terminal, and its leases are released because native cleanup
was not made ambiguous by a controller-local write failure.

### Keep refresh status-only

A connect-only Claude quota refresh that cannot confirm disconnect returns
`CAPABILITY_MISSING` with an explicit “no model task was sent” message. The UI
reports quota `unknown`; it does not move a ready circuit into
`recovery_required`. Real task/canary disconnect ambiguity keeps its existing
fail-closed behavior.

### Three-action ceiling

The existing common constant `RECOVERY_MAX_ATTEMPTS = 3` remains the only
ceiling. Recovery is phase-aware:

- `retry`: only an advertised transient pre-provider failure, with a new
  request ID;
- `refresh`: an explicit user/UI status check, never a task retry;
- `repair`: an advertised payload or verified local-state repair, with no
  provider replay.

The controller performs no more than three total advertised recovery actions
for one failed delegation. The server may use up to the same three attempts for
one local idempotent persistence step. Terminal quota, billing, authentication,
safety, context drift, ambiguous launch/cleanup, and update quarantine are never
retried.

### Update quarantine remains terminal

`UPDATE_QUARANTINED` explicitly states that the resident cannot hot-reload and
must not be retried. The safe path is the exact isolated uvx registration from
the public README plus a fresh Codex task. Existing tasks may finish with native
fallback. Subagent MCP does not edit the owner's Codex configuration, kill host
processes, or mutate a live environment automatically.

## Alternatives rejected

- Automatically resending the failed provider task can duplicate work and
  consume more quota after an ambiguous boundary.
- Converting a state race into cleanup ambiguity loses the authoritative
  provider verdict and leaks leases.
- Allowing a legacy mutated resident to continue can mix package versions in
  one Python process.
- Adding a new recovery tool or database schema is unnecessary for the verified
  failures.

## Acceptance criteria

- A deterministic race between a useful task's explicit quota verdict and a
  concurrent circuit transition returns the original quota code, never
  `RECOVERY_REQUIRED`.
- Local circuit pause persistence is attempted at most three times and no
  provider call is repeated.
- Exhausted local persistence reports a sanitized state warning, releases
  writer leases, and leaves the failed execution terminal.
- Cancellation and process-control exceptions are not swallowed.
- A Claude quota-refresh disconnect failure reports quota `unknown`, leaves a
  ready circuit ready, and sends zero model prompts.
- Existing terminal retry prohibitions and exact isolated uvx package contracts
  remain enforced.
- Focused tests, the full safe suite, fresh Claude/OX read-only tasks, privacy
  scans, and public release readback pass before 1.0.7 is claimed released.
