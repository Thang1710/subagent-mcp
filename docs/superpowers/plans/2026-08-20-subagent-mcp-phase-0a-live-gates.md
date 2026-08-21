# Subagent MCP Phase 0a Live Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

Status: REVIEWED AND COMMITTED CHECKPOINT — not executable until the user approves this exact plan and the execution context is compacted/fresh.

**Goal:** Run the remaining approval-gated real Windows standalone-Claude-Code canaries, produce sanitized replayable Phase 0a evidence, and make an evidence-backed accept/block decision without enabling usage credits or mutating Claude/Codex/AgentBridge state.

**Architecture:** Add a small, test-first live-canary layer around the already hardened Phase 0a primitives. Every live group first emits a deterministic approval scope; execution requires a one-shot matching ignored receipt, streams raw provider events only through bounded memory, and commits only sanitized fixture envelopes plus the deterministic report. No-model identity, manifest, handle, and strict-MCP checks run before any quota-consuming call; model, background, cleanup, concurrency, and independent-review groups each retain separate approvals.

**Tech Stack:** Python 3.10 standard library, existing Phase 0a modules/tests, PowerShell 5.1, Git, standalone Claude Code CLI 2.1.224. No Node, Claude Agent SDK, MCP SDK, package install, client registration, or production adapter.

**Spec:** docs/superpowers/specs/2026-08-17-subagent-mcp-design.md

## Global Constraints

- Use only the execute-validated standalone Claude Code path. Never use the Desktop cache/wrapper or an SDK-bundled fallback.
- Missing standalone CLI returns INSTALL_REQUIRED; logged-out first-party subscription auth returns AUTH_REQUIRED. The plan never installs or logs in automatically.
- Before the first executable call, open/bind canonical identity and SHA-256 without executing it, then run --version through that bound identity and recheck the file identity/hash afterward. Before every subsequent CLI invocation, recheck the bound identity/hash plus absence of ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, and CLAUDE_CODE_OAUTH_TOKEN; the group refuses version drift from the bound --version result.
- Never enable usage credits, billing overage, provider/model fallback, or max-budget API billing. For foreground owned children, a structured isUsingOverage=true terminates the child immediately and fails the gate. Visible-background has no per-turn structured overage stream on this CLI: its only credit controls are the unchanged account toggle confirmed immediately by the user, the exact group timeout, and stop-on-structured terminal failure. A terminal quota/credits-required result records QUOTA_PAUSED and stops all later model groups without retry.
- An allowed or allowed_warning advisory is not terminal; only the final structured result closes a foreground turn. Background failures come only from structured roster and sanitized StopFailure hooks.
- Requested model, effort, setting sources, tools, strict MCP, and auto-compaction are exact per-run policy. Missing effective model/effort/compaction attestation is CAPABILITY_MISSING.
- The user policy requests an exact per-run auto_compaction_trigger_tokens=274000. Claude's --autocompact 274000 requests a 274000 calculation window, not an exact 274000 trigger; window, trigger percentage, and trigger tokens are recorded separately. A propagated flag/environment value is not effective-trigger attestation. No global Codex or Claude configuration may change.
- Default live context is declared-native: user/project/local setting sources and normal Claude Code harness behavior; --bare and --safe-mode are forbidden. Strict MCP and explicit recursion denies remain mandatory.
- No raw provider stream, assistant/tool text, roster row, session/request/run ID, native path, credential, account identity, or transcript content is persisted. Raw values may exist only in bounded process memory. Ignored state stores only locally required opaque IDs/paths with mode 0600-equivalent and is never committed/model-facing.
- Pre-existing or user-owned Claude/Codex transcripts and all AgentBridge state are immutable to Subagent MCP. Subagent MCP never edits native files directly. The only possible transcript deletion is an official claude rm call for a row created by this exact plan in its disposable repository, and only after the user approves that explicit narrow carve-out in Group G; without that approval, retain the row and keep worktree_remove_hook BLOCKED.
- Worktree creation, proof-file removal, stop/respawn, concurrency, and row/worktree removal are limited to a freshly created no-remote disposable repository under the approved .phase0a/live root.
- Worktree removal is a separate destructive approval after a clean/no-extra-commit/common-dir/process audit. Never manually remove a worktree when claude rm refuses unless the user separately approves the exact path and recovery action.
- No provider-capable live group starts while an unexpected background row, matching standalone process, task-owned Python process, dirty disposable repository, stale approval receipt, or extra Subagent MCP worktree exists. The mandatory read-only residual inventory below runs first. If it finds residuals, implement only the no-model code needed for Group G, obtain a fresh cleanup/recovery approval, and reconcile them before any provider-capable group; do not deadlock the plan behind its later cleanup task.
- Node is not installed. If the resolved declared-native context contains a trusted Node-dependent hook/plugin/command, context_attestation becomes CAPABILITY_MISSING and the plan stops before the model call; no silent context downgrade.
- All subprocesses use argv arrays with shell=False. Child termination uses the owned process handle; no PID-only kill.
- Every live call is capped by its approval scope. Unused allowance does not carry into another group.
- Every deterministic runner, settings builder, hook, prompt template, plugin fixture, and evidence schema is committed and the tracked tree is clean before preview. Ignored materialized settings/MCP/hook/prompt files must be byte-for-byte outputs of those committed builders/templates, and their exact hashes are bound in the executable manifest. The scope binds HEAD plus that complete manifest; dirty or changed content invalidates approval.
- The approval receipt is an accidental-execution/audit guard, not a cryptographic proof of human intent. Task workers may implement preview/execute code but may never mint receipts or execute live groups. The root controller displays the digest, receives the direct user approval, writes the receipt, and invokes execute.
- Do not run any real_git_worktree pytest case in this plan without a new exact approval. Historical local evidence is supporting context only because the committed spec/report does not publish a fresh replayable result. Real Claude WorktreeCreate/Remove has its own explicit gates below and cannot borrow that test evidence.
- A failed required gate leaves Phase 0b blocked. The plan does not weaken an assertion to obtain PASS.
- If Git author identity is absent, derive the current HEAD author and use it only through per-command git -c user.name/user.email. Never run git config.
- A live canary that reveals a production-spike defect stops at BLOCKED/RECOVERY_REQUIRED. Do not edit the probed implementation mid-run; add a reviewed plan amendment/fix task, re-run safe verification, then request a fresh live approval.
- Every task that changes a hardened Phase 0a module must keep all pre-existing committed fixture replays green. Existing evidence-index hashes may change only when the task explicitly declares and tests a sanitized fixture migration.

## 2026-08-20 Live Defect Amendment: Host Capability Syntax

The approved no-model host lane found `tools_empty_documented=false` and `prompt_suggestions_false_documented=false`; no bound identity was written and the lane exited nonzero. The failure was a brittle prose-dependent `--tools` matcher and a prompt-suggestions matcher limited to `<boolean>`, not semantic/effective evidence from help. Task 2 now recognizes only anchored installed syntax lines `--tools <tools...>` and `--prompt-suggestions [value]`, while retaining `<boolean>` compatibility. Task 3 Group A exact no-model init includes `--tools ""` and `--prompt-suggestions false` in both strict and control argv so parser acceptance precedes every model Group B call; strict/control still differ only by the strict flag. Re-run the host lane and every Group A preview/execute only with fresh exact user approval. Help recognition remains parser-capability evidence only and never claims semantic/effective PASS.

## 2026-08-20 Live Defect Amendment: Group A Init Hook Contract

The approved Group A no-model execute returned BLOCKED with marker cleanup confirmed and only `InstructionsLoaded` observed. `--init-only` runs setup/session initialization, while `PreToolUse` can fire only before a tool call; Group A deliberately supplies no tools, so requiring `PreToolUse` would make its PASS impossible. Group A now registers `Setup`, `SessionStart`, and `InstructionsLoaded`; it requires delivery only of `Setup` and `InstructionsLoaded`, and records `SessionStart` when present. `SessionStart` is not inferred from Group A and remains mandatory only for its separate Task 5 `session_start_hook` background lifecycle gate. If a strict arm remains otherwise safe but lacks a required init hook, run the non-strict control once to collect the strict-MCP differential, then keep Group A BLOCKED unless both arms satisfy the required hooks and marker differential. Re-run Group A only with a fresh exact user approval.

## 2026-08-20 Live Defect Amendment: Group A Windows Init/Cleanup

The next approved Group A run again observed only `InstructionsLoaded`; its non-strict marker process was already absent, but the adjudicator returned `RECOVERY_REQUIRED` because Claude terminated the stdio child during its atomic exit-acknowledgement write. Group A now invokes the documented standalone `claude --init-only` form without print-mode `-p`; `-p` remains required only for the separate `--init`/`--maintenance` forms. Marker cleanup accepts a missing canonical exit acknowledgement only after the ownership record is valid and the OS-level process check confirms the exact owned process is gone; a corrupt/mismatched canonical exit record, identity mismatch, access failure, or surviving process still requires recovery. Make one fresh approval-bound rerun. If the installed Windows CLI still omits Setup/SessionStart, record that provider capability gap and stop retrying rather than opening another fix loop.

## Live Approval Budget

| Group | Purpose | Max provider-capable session launches | Worktree create | Stop/respawn | Removal |
|---|---:|---:|---:|---:|---:|
| A | no-model init/strict-MCP/observer | 0 | no | no | no |
| B | declared-native control + deny context | 2 Sonnet 5 low | no | no | no |
| C | write/lifecycle plus active stop/respawn | 2 Sonnet 5 low | yes, one | yes | no |
| D | needs-input/blocked row | 1 Sonnet 5 low | at most one | stop only | no |
| E | offline StopFailure taxonomy/unknown normalization only | 0 | no | no | no |
| F | two-row concurrency | 2 Sonnet 5 low | no | exactly two stops | no |
| G | exact disposable row/worktree release | 0 | no new | no | yes |
| H | final different-harness review | 1 Opus 5 xhigh | no | no | no |
| H2 | optional Claude scoped re-review after a fix wave | 1 Opus 5 xhigh | no | no | no |

The initial A–H pass has at most eight approved top-level provider-capable session launches. A Claude-side fix re-review is never borrowed from H: it requires a new H2 digest/approval and raises the absolute worst case to nine. This is not a hard provider-turn/API-request cap: visible-background sessions may perform multiple native model/tool loops, and Agent View may issue end-of-turn or periodic Haiku-class summary requests. Read-only control invocations such as version/auth/agents queries are separately bounded by command-specific poll counts and monotonic deadlines, not counted as provider sessions. Groups C, D, and F require approval that explicitly acknowledges supervisor-owned internal requests until the group's timeout/stop condition. Approval of this plan is not approval of Groups B–H2.

| Group | Pending scope | Approved receipt |
|---|---|---|
| A | .phase0a/live/init/pending-scope.json | .phase0a/live/approvals/approved-A.json |
| B | .phase0a/live/context/pending-scope.json | .phase0a/live/approvals/approved-B.json |
| C | .phase0a/live/background-main/pending-scope.json | .phase0a/live/approvals/approved-C.json |
| D | .phase0a/live/background-needs-input/pending-scope.json | .phase0a/live/approvals/approved-D.json |
| F | .phase0a/live/background-concurrency/pending-scope.json | .phase0a/live/approvals/approved-F.json |
| G | .phase0a/live/background-cleanup/pending-scope.json | .phase0a/live/approvals/approved-G.json |
| H | .phase0a/live/final-review/pending-scope.json | .phase0a/live/approvals/approved-H.json |
| H2 | .phase0a/live/final-rereview/pending-scope.json | .phase0a/live/approvals/approved-H2.json |

## Known Likely Blockers This Plan Must Surface, Not Hide

- The real Task 8 Claude review accepted --autocompact 274000, which current official semantics define as a calculation window. system/init exposed neither an effective window nor an exact trigger percentage/token. Unless official structured evidence attests an exact trigger at 274000, context_attestation remains BLOCKED; request/propagation of a 274000 window is recorded but cannot satisfy the user trigger policy.
- The same init envelope attested the model but not effective effort. Requested xhigh/low is not equivalent to effective effort attestation.
- Several declared-native fields (auto-memory mode, cleanup period, complete instruction/skill/agent/hook sources, nested-agent cap/depth) may not be observable before Phase 0b policy hooks exist. The live plan records the gap; it does not synthesize values or claim full context parity.
- The user's inherited plugin/hook set may contain a trusted Node-dependent executable. Node is currently absent; that makes the affected declared-native policy CAPABILITY_MISSING until a separate install decision.
- The installed CLI may not support disabling a --plugin-dir positive-control plugin through per-run settings. Failure leaves plugin_disable_effective BLOCKED.
- No documented official per-session Agent View accounting split is currently known. agent_view_overhead is expected to remain UNKNOWN unless a current official surface is found and separately approved.
- Foreground isUsingOverage=false cannot enforce a later supervisor-owned background call. overageStatus and overageDisabledReason are recorded when present but are not a proxy for the account toggle. Groups C, D, and F require a fresh foreground isUsingOverage=false result plus immediate user confirmation that usage credits remain off; their approval text must state that the CLI exposes no per-turn overage enforcement for supervisor-owned background rows. If that confirmation or foreground boolean is absent, those groups remain unavailable; Group G recovery and Group H read-only review remain reachable.
- Full declared-native context attestation is expected to stay BLOCKED, but it is not a prerequisite for independent section 19.1 lifecycle probes. Every C/D/F fixture must record declared_native_attestation=incomplete, requested_auto_compaction_window=274000, requested_auto_compaction_trigger=274000, effective trigger fields as observed/unknown, the exact missing-field list, and the intentional tool/permission delta from Group B. No lifecycle PASS may be presented as context parity or as permission to start Phase 0b.

The purpose of the plan is an honest gate decision. It is valid for the result to keep Phase 0b blocked.

## Gate-to-Task Map

| Gate | Owning task | PASS rule |
|---|---|---|
| standalone_cli | 2 | same bound handle identity/version/hash before and after every preflight |
| subscription_auth | 2 | logged_in=true, auth_method=claude.ai, api_provider=firstParty |
| credential_precedence | 2 | local override preflight is replayable, but the gate stays BLOCKED/UNKNOWN until a real CLI override-rejection path is safely proved; absence alone never PASSes |
| observer_visibility | 3 | parent and Claude hook observers attest the same standalone identity; wrapper remains rejected by ownership |
| init_only_capability | 3 | installed CLI accepts no-model init and produces expected hook evidence |
| strict_mcp_pre_spawn | 3 | strict marker absent and non-strict positive control marker present |
| project_manifest | 2 | current canonical manifest sanitized and every executable item trusted/blocked deterministically |
| windows_handle_release | 2 | real Windows success/timeout/cancel/hook-failure/start-failure paths all release |
| context_init_subset | 4 | required init fields validate from bounded stream |
| context_attestation | 4 | every design section 8 field is attested, including effective window, exact trigger percentage, and exact effective trigger_tokens=274000; a 274000 window alone cannot PASS |
| plugin_disable_effective | 4 | the same harmless --plugin-dir plugin is supplied to both arms and disappears only when the deny arm's per-run settings disable it; unsupported disable stays BLOCKED |
| agents_json_schema | 5–6 | fresh sanitized rows cover working, needs-input/blocked, done, failed, stopped |
| lifecycle_commands | 5–6 | documented lifecycle commands and all required states replay |
| session_start_hook | 5 | sanitized background SessionStart observed |
| worktree_create_hook | 5 | the dedicated WorktreeCreate handler's stdout path, lease, event, roster path, and common-dir agree before first write |
| stop_hook | 5 | successful background Stop observed |
| stop_failure_hook | 6 | stays BLOCKED unless a documented StopFailure is naturally observed; offline fixtures prove only the official category set and unknown normalization |
| daemon_stop_race | 5 | active stop stable twice, respawn one turn, final stop stable twice |
| background_concurrency | 6 | exactly two approved rows simultaneously active proves observed_floor=2; provider_ceiling and provider limit remain UNKNOWN |
| agent_view_overhead | 6 | official per-session surface only; otherwise UNKNOWN, never estimated |
| worktree_remove_hook | 7 | approved claude rm emits matching hook and exact clean disposable path disappears |

## File Responsibility Map

| File | Responsibility |
|---|---|
| spikes/phase0a/live_common.py | approval scopes/receipts, CLI identity binding, bounded subprocess/stream utilities, rate circuit |
| spikes/phase0a/live_host.py | sanitized host/auth/manifest/Windows-handle live gates |
| spikes/phase0a/live_init.py | no-model init observer and strict/non-strict MCP differential |
| spikes/phase0a/live_context.py | foreground stream context/plugin/274000 canary |
| spikes/phase0a/live_background.py | background lifecycle, state matrix, concurrency, race, local opaque state |
| spikes/phase0a/contracts.py and tests/fixtures/phase0a/current/stop-failure-contract.json | official StopFailure taxonomy and unknown-value normalization; no forced live failure |
| spikes/phase0a/worktree_hook.py | existing dedicated replacing WorktreeCreate handler; Task 5 binds and invokes it unchanged unless a reviewed defect requires a separate fix |
| spikes/phase0a/live_review_guard.py | Group H/H2 export-root-only PreToolUse path guard for Read/Glob/Grep |
| spikes/phase0a/live_evidence.py | live fixture envelopes, evidence adjudicators, report generation, review export, and final write-set guard; no raw values |
| tests/phase0a/test_live_*.py | deterministic fake-CLI/process tests; no real model |
| tests/fixtures/phase0a/current/live-*.json | sanitized accepted live derivatives only |
| docs/phase0a/phase0a-live-runbook.md | exact preview/approval/execute/recovery commands |
| docs/phase0a/phase0a-report.md | generated gate block plus reviewed decision |

The implementation embeds and tests this exact planned write set; no directory/prefix wildcard is allowed:

~~~text
spikes/phase0a/live_common.py
spikes/phase0a/live_host.py
spikes/phase0a/live_init.py
spikes/phase0a/live_context.py
spikes/phase0a/live_background.py
spikes/phase0a/live_evidence.py
spikes/phase0a/live_review_guard.py
spikes/phase0a/host_probe.py
spikes/phase0a/strict_probe.py
spikes/phase0a/hook_sink.py
spikes/phase0a/background_probe.py
spikes/phase0a/contracts.py
spikes/phase0a/fixtures.py
spikes/phase0a/report.py
tests/phase0a/test_live_common.py
tests/phase0a/test_live_host.py
tests/phase0a/test_live_init.py
tests/phase0a/test_live_context.py
tests/phase0a/test_live_background.py
tests/phase0a/test_live_evidence.py
tests/phase0a/test_live_review_guard.py
tests/phase0a/test_contracts.py
tests/phase0a/test_report.py
tests/fixtures/phase0a/control-plugin/.claude-plugin/plugin.json
tests/fixtures/phase0a/control-plugin/skills/subagent-harness-mcp-control/SKILL.md
tests/fixtures/phase0a/current/live-host.json
tests/fixtures/phase0a/current/live-windows-handles.json
tests/fixtures/phase0a/current/live-init-strict-mcp.json
tests/fixtures/phase0a/current/live-context.json
tests/fixtures/phase0a/current/live-background-lifecycle.json
tests/fixtures/phase0a/current/live-background-matrix.json
tests/fixtures/phase0a/current/live-worktree-remove.json
tests/fixtures/phase0a/current/context-attestation.json
tests/fixtures/phase0a/current/stop-failure-contract.json
tests/fixtures/phase0a/current/evidence-index.json
docs/phase0a/phase0a-live-runbook.md
docs/phase0a/phase0a-report.md
~~~

If a verified finding needs any other path, stop and amend/review the plan before editing. Existing/user/concurrent files under an otherwise familiar directory are never adopted.

---

### Pre-Live Gate 0: Mandatory Read-Only Residual Inventory

Implement this command in Task 2, then run it immediately after Task 2's safe implementation and before previewing any live group:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_host inventory --root .phase0a\live\inventory --cli "$env:USERPROFILE\.local\bin\claude.exe" --project-root .
~~~

The command performs only execute identity/version, auth status, agents --json --all, process identity, and git worktree --porcelain reads. It writes ignored local opaque details plus a sanitized classification: expected_clean, plan_owned_residual, user_or_unknown_residual, or recovery_required. It never stops/removes anything. If anything other than expected_clean is found, no provider-capable group may start. Implement Tasks 1, 2, 5, and 7 code without executing their live groups, then use a separately approved Group G preview only for plan-owned disposable targets. User/unknown targets are never auto-adopted or removed.

---

## Exact Invocation Capability Matrix

Task 2 captures one bounded top-level `claude --help` output for documented syntax and runs only the explicitly help-safe subcommands below. It never appends `--help` to a background/worktree invocation. Unit tests build and compare the complete argv array for each group; semantic evidence comes only from that group's init/hooks/roster/result, not from help text.

| Owner | Exact required argv surface (paths/names are typed manifest substitutions) | Class | Required evidence |
|---|---|---|---|
| 2 | `<cli> --version`; `<cli> auth status`; `<cli> agents --json --all` | help-safe/read-only | bound identity/version, subscription auth, sanitized roster counts |
| 2 | `<cli> stop --help`; `respawn --help`; `attach --help`; `rm --help` | help-safe/read-only | command recognized only; no lifecycle PASS |
| A | `<cli> --init-only --no-session-persistence --setting-sources user,project,local --settings <A-settings> --strict-mcp-config --mcp-config <empty> --disallowedTools mcp__codex__* mcp__agent_bridge__* mcp__subagent_harness_mcp__*` | approval-bound no-model init | zero model/rate events, required Setup + InstructionsLoaded observer, optional recorded SessionStart, exact settings/MCP identity |
| B | `<cli> -p --output-format stream-json --verbose --include-hook-events --model claude-sonnet-5 --effort low --autocompact 274000 --setting-sources user,project,local --settings <B-settings> --tools "" --disallowedTools mcp__codex__* mcp__agent_bridge__* mcp__subagent_harness_mcp__* --permission-mode dontAsk --prompt-suggestions false --strict-mcp-config --mcp-config <empty> --no-session-persistence <prompt>` | approval-bound foreground provider | system/init tools empty; model/rate/result; requested window and trigger recorded separately; hook evidence |
| C | `<cli> --bg --name {group_name} --worktree {worktree_name} --model claude-sonnet-5 --effort low --autocompact 274000 --setting-sources user,project,local --settings <C-settings> --tools Read,Write,Bash --disallowedTools mcp__codex__* mcp__agent_bridge__* mcp__subagent_harness_mcp__* --permission-mode acceptEdits --strict-mcp-config --mcp-config <empty> <prompt>` | approval-only; mutates Claude row and one worktree | WorktreeCreate stdout/lease/event/roster/cwd agreement before PreToolUse permits write; settings contain `worktree.baseRef="head"` |
| D | same fixed background policy as C, with unique `{group_name}`/`{worktree_name}`, `<D-settings>`, `--tools Read,Write`, the same three `--disallowedTools` patterns, and `--permission-mode manual` | approval-only; mutates Claude row and one worktree | blocked/needs-input before attach; worktree/lease/cwd agreement; no write |
| F | `<cli> --bg --name {group_name} --model claude-sonnet-5 --effort low --autocompact 274000 --setting-sources user,project,local --settings <F-settings> --tools Bash --disallowedTools mcp__codex__* mcp__agent_bridge__* mcp__subagent_harness_mcp__* --permission-mode dontAsk --strict-mcp-config --mcp-config <empty> <prompt>` twice | approval-only; mutates two Claude rows, zero worktrees | exact `sleep 20` hook allow, two simultaneous rows, two bounded stops |
| H/H2 | `<cli> -p --output-format stream-json --verbose --include-hook-events --model claude-opus-5 --effort xhigh --autocompact 274000 --setting-sources project --settings <review-settings> --tools Read,Glob,Grep --disallowedTools mcp__codex__* mcp__agent_bridge__* mcp__subagent_harness_mcp__* --permission-mode dontAsk --prompt-suggestions false --strict-mcp-config --mcp-config <empty> <packet>` | approval-bound foreground provider, persistent session | export cwd/guard, tool schemas, model/rate/result, no overage/path escape |
| C/D/F/G | `<cli> stop|respawn|attach|rm {short_id}` | approval-only dynamic argv template | ID belongs to the same group state, anchored regex, consumed ledger, exact counter |

`--bg`, `--worktree`, `--name`, and their combinations are never parser-probed: installed 2.1.224 `--bg --help` attempted to create a real job. `--worktree` is likewise treated as mutating. Missing or rejected approval-only semantics BLOCK only the affected gate; the runner never rewrites an argv, falls back, or performs a second trial. The H/H2 Read/Glob/Grep input schema is pinned to the official Agent SDK TypeScript tool schema retrieved 2026-08-20 and canaried on the installed CLI; an unknown schema quarantines review instead of widening access.

---

### Task 1: One-Shot Approval and Bounded Live-Process Boundary

**Files:**
- Create: spikes/phase0a/live_common.py
- Create: tests/phase0a/test_live_common.py

**Interfaces:**
- Produces RuntimeBinding, SideEffectSpec, ApprovalScope, approval_digest, require_one_shot_approval, consume_side_effect, BoundCliIdentity, BoundExecutableManifest, run_json_command, run_stream_command, and LiveCircuitResult.
- All later live tasks consume these functions; none may spawn the CLI directly.

- [ ] **Step 1: Write failing approval-scope tests**

Create tests proving exact canonical JSON hashing, HEAD/CLI identity/counts/executable manifest and the human-readable side-effect specs are digest-bound, expired or consumed receipts fail before spawn, and a mismatched digest never invokes the fake runner. A runtime-discovered ID is allowed only through a declared placeholder bound to this group's own state key and regex:

~~~python
def test_one_shot_receipt_binds_every_side_effect(tmp_path):
    attach = SideEffectSpec(
        kind="attach",
        argv_template=("<bound-cli>", "attach", "{short_id}"),
        bindings=(RuntimeBinding(
            token="{short_id}",
            state_key="group.short_id",
            pattern=r"^[A-Za-z0-9_-]{1,64}$",
            require_group_owned=True,
        ),),
        max_uses=1,
        exact_targets=(),
    )
    scope = ApprovalScope(
        schema_version=1,
        git_head="a" * 40,
        cli_sha256="b" * 64,
        gate_ids=("context_attestation",),
        side_effects=(attach,),
        max_provider_session_launches=2,
        max_worktree_creates=0,
        max_stop_respawn_actions=0,
        max_attach_actions=0,
        max_file_deletes=0,
        max_removals=0,
        background_internal_requests_acknowledged=False,
        executable_manifest_sha256="e" * 64,
        trust_revision=1,
    )
    receipt = write_test_receipt(tmp_path, scope, approved=True, consumed=False)
    assert require_one_shot_approval(scope, receipt, now=FIXED_NOW) == scope
    mark_receipt_consumed(receipt, scope)
    with pytest.raises(PermissionError, match="consumed"):
        require_one_shot_approval(scope, receipt, now=FIXED_NOW)
~~~

- [ ] **Step 2: Run approval tests and verify RED**

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= tests\phase0a\test_live_common.py -v
~~~

Expected: import failure because live_common does not exist.

- [ ] **Step 3: Implement immutable approval scopes**

Use a frozen dataclass and canonical JSON. Receipt files live only below .phase0a/live/approvals, contain scope_sha256, approved_at, expires_at, consumed_at, and no user/account identity. require_one_shot_approval verifies:

~~~python
@dataclass(frozen=True)
class RuntimeBinding:
    token: str
    state_key: str
    pattern: str
    require_group_owned: bool

@dataclass(frozen=True)
class SideEffectSpec:
    kind: str
    argv_template: tuple[str, ...]
    bindings: tuple[RuntimeBinding, ...]
    max_uses: int
    exact_targets: tuple[str, ...]

@dataclass(frozen=True)
class ApprovalScope:
    schema_version: int
    git_head: str
    cli_sha256: str
    gate_ids: tuple[str, ...]
    side_effects: tuple[SideEffectSpec, ...]
    max_provider_session_launches: int
    max_worktree_creates: int
    max_stop_respawn_actions: int
    max_attach_actions: int
    max_file_deletes: int
    max_removals: int
    background_internal_requests_acknowledged: bool
    executable_manifest_sha256: str
    trust_revision: int

def approval_digest(scope: ApprovalScope) -> str:
    body = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
~~~

Reject negative counts, duplicate/empty gate IDs, unknown side-effect kinds, duplicate placeholders, non-anchored binding regexes, a binding outside this group's state, max_uses above the matching top-level counter, expiry more than two hours after approval, any dirty tracked tree, a HEAD/manifest/trust-revision mismatch, and any receipt outside the ignored approval root. The preview renders the exact canonical ApprovalScope, including fixed argv arrays and runtime-binding templates, then hashes that same object; execute re-renders and re-hashes it before the first side effect. consume_side_effect validates substitutions against the group-owned state and regex, atomically appends the concrete argv/target to a consumed ledger before invocation, and rejects overuse. Write consumed_at atomically before the first side effect. A crash after consumption requires a new user approval; it never reuses the receipt.

Implement a local CLI subcommand used only by the controller after a direct user approval. Group A uses:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\init\pending-scope.json --output .phase0a\live\approvals\approved-A.json --expires-minutes 120
~~~

After each separate direct approval, the root controller uses exactly one matching command:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\context\pending-scope.json --output .phase0a\live\approvals\approved-B.json --expires-minutes 120
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\background-main\pending-scope.json --output .phase0a\live\approvals\approved-C.json --expires-minutes 120
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\background-needs-input\pending-scope.json --output .phase0a\live\approvals\approved-D.json --expires-minutes 120
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\background-concurrency\pending-scope.json --output .phase0a\live\approvals\approved-F.json --expires-minutes 120
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\background-cleanup\pending-scope.json --output .phase0a\live\approvals\approved-G.json --expires-minutes 120
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\final-review\pending-scope.json --output .phase0a\live\approvals\approved-H.json --expires-minutes 120
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_common approve-scope --scope .phase0a\live\final-rereview\pending-scope.json --output .phase0a\live\approvals\approved-H2.json --expires-minutes 120
~~~

Never run multiple lines from this block after one approval. approve-scope never contacts Claude and refuses an already approved or consumed output. The subcommand is controller-only by workflow; task implementers stop after preview and return the digest.

- [ ] **Step 4: Add failing bounded-stream/circuit tests**

Use a fake Python child that emits system/init, rate_limit_event, assistant/tool noise, and result lines. Tests require:

~~~python
def test_allowed_warning_waits_for_final_result(fake_cli):
    result = run_stream_command(fake_cli.allowed_warning_then_success())
    assert result.classification == "success"
    assert result.is_using_overage is False

def test_overage_true_terminates_owned_child(fake_cli):
    result = run_stream_command(fake_cli.overage_true_then_hang())
    assert result.classification == "usage_credits_forbidden"
    assert fake_cli.child_was_terminated_by_handle()

def test_terminal_quota_pauses_without_retry(fake_cli):
    result = run_stream_command(fake_cli.terminal_quota())
    assert result.classification == "quota_paused"
    assert fake_cli.spawn_count == 1
~~~

Also cover 8 MiB line, 64 MiB cumulative stream, malformed JSON, missing/duplicate init/result, model mismatch, timeout, stderr cap, UTF-8 output, assistant/thinking non-persistence, incremental SHA-256 provenance, and sanitized final-text output.

Add manifest tests proving every generated settings/MCP/plugin/hook/prompt/runner file and transitive executable target is represented by canonical repository identity, canonical path, SHA-256, trust revision, and file identity. A changed, replaced, newly added, missing, or untrusted item after preview must fail before spawn. The runtime holds validated executable files through initialization acknowledgement using the platform-specific handle/identity contract; finally releases every handle on success, timeout, cancellation, process-start failure, and hook failure.

- [ ] **Step 5: Implement bounded process runners**

run_json_command captures at most 8 MiB and validates the expected JSON type. run_stream_command uses reader threads/queue so the monotonic timeout can fire even while no bytes arrive. It retains only:

~~~python
@dataclass(frozen=True)
class LiveCircuitResult:
    classification: str
    exit_code: int | None
    model: str | None
    effort: str | None
    effective_auto_compaction_window: int | None
    effective_auto_compaction_trigger_percent: float | None
    effective_auto_compaction_trigger_tokens: int | None
    tools: tuple[str, ...]
    mcp_server_count: int
    plugin_count: int
    is_using_overage: bool | None
    rate_statuses: tuple[str, ...]
    source_sha256: str
    stream_bytes: int
    final_marker_matched: bool
    sanitized_final_text: str | None
~~~

run_stream_command accepts final_policy equal to exact_marker, sanitized_text, or discard. Context/background probes use exact_marker and store only final_marker_matched. The independent review uses sanitized_text, applies the existing key-aware credential/PII redactor plus a 256 KiB UTF-8 cap, returns it once to the orchestrator, and never writes it to raw local evidence. Direct isUsingOverage=false proves that turn did not use overage; absence of the optional disabled-reason field must not suppress a successful final result or invent account-level billing-setting attestation.

- [ ] **Step 6: Prove live local state is already ignored and commit**

Do not edit .gitignore: its existing .phase0a/ rule already covers the live root. Add a test that git check-ignore -q .phase0a/live/sentinel succeeds and that no file below .phase0a can enter an explicit checkpoint whitelist. Run the focused tests, complete safe suite, git diff --check, then commit:

~~~powershell
git add spikes\phase0a\live_common.py tests\phase0a\test_live_common.py
git commit -m "test: add approval-gated live process boundary"
~~~

---

### Task 2: No-Model Host, Auth, Manifest, and Windows Handle Gates

**Files:**
- Create: spikes/phase0a/live_host.py
- Create: spikes/phase0a/live_evidence.py
- Create: tests/phase0a/test_live_host.py
- Create: tests/phase0a/test_live_evidence.py
- Create: tests/fixtures/phase0a/current/live-host.json
- Create: tests/fixtures/phase0a/current/live-windows-handles.json
- Modify: tests/fixtures/phase0a/current/evidence-index.json
- Modify: spikes/phase0a/host_probe.py
- Create: docs/phase0a/phase0a-live-runbook.md

**Interfaces:**
- Consumes BoundCliIdentity and run_json_command.
- Produces collect_host_evidence(root, cli, env), run_windows_handle_matrix(root), live_fixture, and rebuild_live_evidence_index. Tasks 3–8 use the evidence helpers.

- [ ] **Step 1: Add failing sanitized host-evidence tests**

Tests must prove INSTALL_REQUIRED and AUTH_REQUIRED perform no later call, override presence blocks auth/model, first-party auth and identity bind to one open handle, and output contains no canonical path/email/org/session/roster values:

~~~python
def test_override_presence_stops_before_auth(fake_cli, tmp_path):
    result = collect_host_evidence(
        tmp_path, fake_cli.path, {"ANTHROPIC_API_KEY": "present"}
    )
    assert result["status"] == "credential_override"
    assert fake_cli.calls == ["version"]

def test_public_host_evidence_has_no_path_or_identity_value(host_evidence):
    serialized = json.dumps(host_evidence)
    assert "canonical_path" not in serialized
    assert "email" not in serialized
    assert "org" not in serialized.casefold()
    assert "session" not in serialized.casefold()
~~~

- [ ] **Step 2: Implement no-model preflight**

Call only --version, auth status, and agents --json --all through exact argv. Recheck the open executable handle identity before and after. Public evidence retains version, executable SHA-256/size, identity-stable boolean, auth categories, override booleans, roster counts/state categories, and wrapper rejection categories; no native path, device/inode, path fingerprint, or roster row.

Read the Desktop wrapper script and selected cache executable only through bounded read-only handles. Prove locally that:

- the wrapper and selected target are distinct from the accepted standalone identity;
- the target is below the Desktop-owned versioned cache root and the wrapper selection depends on mutable version-folder state;
- the accepted runtime path is the standalone installation root and never resolves through the wrapper/cache;
- parent and Task 3 hook observers agree on the standalone identity even if both can see the wrapper/cache.

Public evidence stores only booleans/categories and content digests, not paths. Wrapper execution success/failure is irrelevant; ownership and mutable cache lifecycle cause rejection. If ownership/root classification cannot be proved without a private path claim, standalone identity remains PASS-capable but observer_visibility/wrapper rejection remains UNKNOWN.

Missing/logged-out states return the exact user-facing codes from the spec and stop. Do not launch claude auth login.

Implement the Exact Invocation Capability Matrix above. Capture one bounded top-level help document and the four help-safe lifecycle subcommand pages; do not construct synthetic flag/value `--help` calls. Exact arrays are fixture-tested per group, while init/hooks/roster/result provide semantic evidence inside the matching approved scope. Never run `--bg --help`, `--worktree --help`, or any name/worktree combination outside C/D approval. Record recognized booleans only. A missing pair blocks its owning group; never rewrite argv or silently choose an alias. Never call logs as a control/evidence surface. Help recognition is necessary but never makes lifecycle_commands PASS without Tasks 5–7.

Task 2 records only the documented hook names/schema from the single bounded help/reference capture; it performs no init. The actual Setup, SessionStart, and InstructionsLoaded registration/delivery probe belongs to Task 3's approval-bound Group A no-model init; only Setup and InstructionsLoaded are required there. InstructionsLoaded may attest instruction source categories/hashes, but it does not prove the effective setting_sources list. If the CLI exposes no official structured setting-source field, keep that field missing and context_attestation BLOCKED.

- [ ] **Step 3: Add failing current-manifest tests**

Use scan_project on the actual checkout through an injected root, but project only aggregate counts/hashes:

~~~python
def test_current_manifest_public_projection_omits_paths(tmp_path, trusted_manifest):
    evidence = project_manifest_evidence(trusted_manifest)
    assert set(evidence) == {
        "repository_kind",
        "instruction_count",
        "hook_target_count",
        "external_count",
        "blocked_count",
        "manifest_digest",
    }
~~~

Any blocked executable item makes project_manifest BLOCKED; the live runner never creates trust decisions. collect_host_evidence also implements the Pre-Live Gate 0 inventory classification. A pre-existing/user row or unknown worktree is never converted into plan-owned state.

Add live_fixture with the same versioned envelope/provenance/privacy contract as existing fixtures. It accepts only sanitized payloads, an incremental source SHA-256, explicit observed/missing coverage, CLI identity digest/version, and gate ID. rebuild_live_evidence_index validates all committed current fixtures and excludes itself.

- [ ] **Step 4: Add failing real Windows handle-matrix tests**

Inject the child launcher so unit tests cover success, timeout, cancellation, child failure, and process-start failure without sleeping. Each branch must prove an atomic replace succeeds after finally cleanup.

- [ ] **Step 5: Implement the Windows handle canary**

On Windows only, open a disposable project settings file with the production-intended read/no-write-delete sharing semantics using hold_file.ps1. For each branch:

1. wait for a sanitized ready event;
2. prove both an in-place writer and a sibling-temp editor-style atomic replace are denied while held;
3. exit/terminate through the owned process handle;
4. in finally, retry atomic replace until a bounded 5-second deadline;
5. prove both save forms succeed after release and record latency, branch, and booleans only.

This is a real subprocess save/replace race, not a GUI-editor claim. If the selected release editor itself is not exercised under a separately approved platform gate, report editor_application_canary=not_run and do not use windows_handle_release as proof for that editor. Non-Windows records not_applicable and cannot support Windows release.

- [ ] **Step 6: Run the safe lane**

This task consumes no Claude model quota and creates no Claude row/worktree. Plan approval covers these read-only/local disposable checks.

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_host --root .phase0a\live\host --cli "$env:USERPROFILE\.local\bin\claude.exe" --project-root .
~~~

Stop on any identity/auth/override/manifest/handle failure. Do not repair the machine.

- [ ] **Step 7: Verify and commit**

Run focused tests, every pre-existing fixture replay, complete safe suite, public projection scans, git diff --check, then commit:

~~~powershell
git add spikes\phase0a\host_probe.py spikes\phase0a\live_host.py spikes\phase0a\live_evidence.py tests\phase0a\test_live_host.py tests\phase0a\test_live_evidence.py tests\fixtures\phase0a\current\live-host.json tests\fixtures\phase0a\current\live-windows-handles.json tests\fixtures\phase0a\current\evidence-index.json docs\phase0a\phase0a-live-runbook.md
git commit -m "test: add no-model Phase 0a host gates"
~~~

---

### Task 3: No-Model Init Observer and Strict MCP Differential

**Files:**
- Create: spikes/phase0a/live_init.py
- Create: tests/phase0a/test_live_init.py
- Create: tests/fixtures/phase0a/current/live-init-strict-mcp.json
- Modify: tests/fixtures/phase0a/current/evidence-index.json
- Modify: spikes/phase0a/strict_probe.py
- Modify: spikes/phase0a/hook_sink.py

**Interfaces:**
- Consumes Group A approval scope and the bound standalone identity.
- Produces sanitized init_only, observer_visibility, and strict_mcp_pre_spawn gate evidence.

- [ ] **Step 1: Add failing argv and observer tests**

Require exact standalone path, --init-only without print-mode -p, --no-session-persistence, user/project/local setting sources, strict empty MCP, all three recursion denies, no --bare/--safe-mode/model/prompt, and Setup/SessionStart/InstructionsLoaded observer hooks. Group A requires Setup and InstructionsLoaded delivery, records SessionStart if delivered, and does not register PreToolUse because no tool call occurs. SessionStart remains mandatory only for Task 5's separate background `session_start_hook` lifecycle gate and is never inferred from Group A. Group A capability-tests registration/delivery for those init hook surfaces; the observer writes only the standalone identity digest, hook-name booleans, instruction source categories/hashes, and environment-category booleans. No Task 2 command substitutes for this approval-bound init.

Every init-only argv also uses --no-session-persistence. Any model/rate event is a Group A failure; no invalid-model request is made merely to force a StopFailure.

- [ ] **Step 2: Implement preview mode**

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_init --preview --root .phase0a\live\init --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

The command first materializes every disposable settings/MCP/hook/marker file, validates the executable manifest, and prints the canonical ApprovalScope itself: gate IDs, clean HEAD, CLI hash, fixed no-model argv arrays, executable-manifest hash, max_provider_session_launches=0, the exact owned marker-process spec, and zero worktree/remove actions. It exits without invoking Claude. Do not print any claim that is absent from the hashed scope.

- [ ] **Step 3: Verify and commit deterministic Group A code before preview**

Run focused tests and the complete safe suite, then commit only code/tests. Regenerate the preview after this commit so pending-scope.json binds the new clean HEAD:

~~~powershell
git add spikes\phase0a\live_init.py spikes\phase0a\strict_probe.py spikes\phase0a\hook_sink.py tests\phase0a\test_live_init.py
git commit -m "test: add no-model init live canary"
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_init --preview --root .phase0a\live\init --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

- [ ] **Step 4: Stop for immediate Group A approval**

Show the exact preview digest and explain that the strict control starts one harmless local Python marker MCP and writes only below .phase0a/live/init. Do not create the receipt until the user approves that digest.

- [ ] **Step 5: Execute strict and control paths**

After approval, run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_init --execute --approval .phase0a\live\approvals\approved-A.json --root .phase0a\live\init --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Strict argv uses --strict-mcp-config with an empty declared config while the disposable repo contains a marker .mcp.json. Control omits strict mode. Capture stdout/stderr only in bounded memory; retain exit/timed-out/hook-error booleans and marker presence. The marker MCP is an owned child with a 30-second deadline; finally terminates/waits by process handle and verifies no marker process remains. Failure records RECOVERY_REQUIRED and prevents later live groups.

PASS requires:

- installed CLI accepts --init-only;
- Setup and InstructionsLoaded observer identities equal Task 2 identity; SessionStart is recorded when present but is not a Group A PASS condition;
- strict marker absent;
- control marker present;
- no forbidden MCP/tool/plugin or hook error;
- zero model/rate events.

If the strict arm has exit success, marker cleanup, zero model/rate/hook errors, and matching observer identity but lacks Setup or InstructionsLoaded, run the control arm once to collect the strict-MCP differential. The overall Group A result remains BLOCKED unless both arms satisfy the required init hooks and their marker expectations.

If --init-only is rejected, init_only_capability and observer_visibility are BLOCKED. Task 4 may still run only after its own approval; it does not retroactively make init-only PASS.

- [ ] **Step 6: Write sanitized live fixtures and commit**

Write versioned fixtures through live_evidence helpers; no raw files. Rebuild evidence index, run replay tests/scans, and commit:

~~~powershell
git add tests\fixtures\phase0a\current\live-init-strict-mcp.json tests\fixtures\phase0a\current\evidence-index.json
git commit -m "test: record no-model init and strict MCP evidence"
~~~

---

### Task 4: Declared-Native Context, Plugin Positive Control, and 274000 Trigger/Window Gate

**Files:**
- Create: spikes/phase0a/live_context.py
- Create: tests/phase0a/test_live_context.py
- Create: tests/fixtures/phase0a/control-plugin/.claude-plugin/plugin.json
- Create: tests/fixtures/phase0a/control-plugin/skills/subagent-harness-mcp-control/SKILL.md
- Create: tests/fixtures/phase0a/current/live-context.json
- Modify: tests/fixtures/phase0a/current/evidence-index.json
- Modify: spikes/phase0a/contracts.py
- Modify: spikes/phase0a/fixtures.py

**Interfaces:**
- Consumes run_stream_command and Group B approval.
- Produces control and deny context projections plus explicit requested/effective fields.

- [ ] **Step 1: Add failing exact context-argv tests**

The context run must execute from an explicit freshly created disposable Git cwd and contain:

~~~python
assert argv_contains_pairs(argv, {
    "--setting-sources": "user,project,local",
    "--model": "claude-sonnet-5",
    "--effort": "low",
    "--autocompact": "274000",
    "--permission-mode": "dontAsk",
    "--prompt-suggestions": "false",
})
assert "--strict-mcp-config" in argv
assert "--bare" not in argv
assert "--safe-mode" not in argv
assert "--fallback-model" not in argv
assert "--no-session-persistence" in argv
~~~

Use --tools "" and an exact no-tool response marker. PASS requires the bounded system/init envelope to report tools == (); the model-written marker is secondary evidence only. Installed CLI 2.1.224 help explicitly documents that --tools "" disables every built-in tool and accepts --prompt-suggestions false; Task 2 still parser-tests both exact pairs and blocks rather than rewriting argv if either is rejected. Record git status --porcelain for the real checkout immediately before and after each arm and require it to stay empty.

- [ ] **Step 2: Add failing attestation/circuit tests**

Tests require:

- requested_auto_compaction_trigger_tokens=274000 always;
- requested_auto_compaction_window_tokens=274000 records the exact --autocompact argv separately;
- effective_auto_compaction_window remains None unless an official structured surface attests it;
- effective_auto_compaction_trigger_percent and effective_auto_compaction_trigger_tokens remain None unless official exact values/formula attest them; a child hook observing a flag/environment value proves propagation only;
- effective effort remains None unless structured;
- missing effective values return CAPABILITY_MISSING, never PASS;
- when a documented non-mutating per-run plugin-disable mechanism exists, the same harmless plugin argv/source appears in both arms, appears in control init, and is absent only in deny init;
- a structured setting-source mismatch fails when that official field exists; otherwise requested setting sources are recorded and effective setting_sources remains missing;
- Node-dependent hook error stops without retry/install;
- allowed_warning plus success and isUsingOverage=false succeeds;
- isUsingOverage=true, quota rejection, malformed schema, or model mismatch stops the group.
- an injected InstructionsLoaded hook records only source categories, count, content hashes, and load reason; no path, path fingerprint, or content is persisted. Because the hook is asynchronous, wait only to a bounded post-result deadline and record missing delivery rather than inferring a source.
- background_eligible is true only when the foreground result has isUsingOverage=false and the user immediately confirms the account usage-credit toggle remains off. overageStatus/overageDisabledReason are informational fields and never stand in for that confirmation.

- [ ] **Step 3: Implement a harmless local plugin control**

The plugin contains only a static skill description and no hooks, MCP, executables, or Node. Its name is subagent-harness-mcp-phase0a-control. Never claim a positive control by passing --plugin-dir only to the control arm and omitting it from deny. If Task 2 finds a documented, non-mutating per-run disable mechanism, both arms load the same committed --plugin-dir plugin and differ only by that deny setting; control must report relative_plugin_delta=1 and deny delta=0. If no such official mechanism exists on the installed CLI, skip the extra control launch, keep plugin_disable_effective BLOCKED, and do not substitute a private user plugin or mutate any plugin registry/cache.

- [ ] **Step 4: Verify and commit deterministic Group B code**

Run focused and safe tests, then commit the runner, parser/writer changes, and harmless plugin before preview:

~~~powershell
git add spikes\phase0a\live_context.py spikes\phase0a\contracts.py spikes\phase0a\fixtures.py tests\phase0a\test_live_context.py tests\fixtures\phase0a\control-plugin\.claude-plugin\plugin.json tests\fixtures\phase0a\control-plugin\skills\subagent-harness-mcp-control\SKILL.md
git commit -m "test: add declared-native context live canary"
~~~

- [ ] **Step 5: Preview Group B and stop for approval**

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_context --preview --root .phase0a\live\context --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

The scope declares one required context launch plus at most one capability-dependent plugin-control launch, no worktree/stop/file-delete/remove, and contains the exact cwd, argv, marker, generated-file, and executable-manifest specs. Show the canonical scope and digest; only the root controller may create approved-B.json after direct user approval.

- [ ] **Step 6: Execute after approval**

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_context --execute --approval .phase0a\live\approvals\approved-B.json --root .phase0a\live\context --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

If the official plugin-disable control is available, run control first and the context/deny arm only if control succeeds and rate status allows; otherwise run only the required context arm. Raw stream is never written. A later call is skipped after any quota/circuit/schema/hook failure.

Full context_attestation PASS additionally requires every section 8 field, including effective effort, effective compaction window, exact effective trigger percentage/formula, effective trigger_tokens=274000, cleanup period, auto-memory, and setting/CLAUDE/rule/skill/agent/hook sources/hashes. Use the per-run InstructionsLoaded hook for instruction source categories/hashes where officially available; it does not prove effective setting_sources or compaction behavior. If the CLI does not expose any required effective field through an official structured surface, preserve the observed init subset but leave context_attestation BLOCKED. The fixture must say attested_configuration=foreground_no_tools and production_equivalent_attestation=outstanding; a tool-less canary cannot prove the context used by a write-capable delegated task.

Groups C, D, and F do not require full context_attestation. They require the validated Task 4 init subset, exact requested model/effort, requested window=274000, requested trigger=274000, strict empty MCP, recursion denies, explicit tool/permission delta, foreground isUsingOverage=false, and immediate user confirmation that usage credits remain off. Their evidence always carries declared_native_attestation=incomplete plus the exact Task 4 missing list and may not claim the trigger was honored. Group G recovery/cleanup remains independently available; Group H uses a separately declared project-only review context and makes no declared-native PASS claim.

- [ ] **Step 7: Verify and commit sanitized Group B evidence**

Commit only sanitized derivative and updated index:

~~~powershell
git add tests\fixtures\phase0a\current\live-context.json tests\fixtures\phase0a\current\evidence-index.json
git commit -m "test: record declared-native context live evidence"
~~~

---

### Task 5: One Background Worktree, Lifecycle, Stop, and Active Respawn Race

**Files:**
- Create: spikes/phase0a/live_background.py
- Create: tests/phase0a/test_live_background.py
- Create: tests/fixtures/phase0a/current/live-background-lifecycle.json
- Modify: tests/fixtures/phase0a/current/evidence-index.json
- Modify: spikes/phase0a/background_probe.py
- Use unchanged and bind in manifest: spikes/phase0a/worktree_hook.py

**Interfaces:**
- Consumes Group C approval and Task 4's same-context proxy result.
- Produces one local opaque row/worktree state record and sanitized lifecycle/worktree/race evidence.

Group C preview requires Task 4's validated init subset, exact requested model/effort, requested window=274000, requested trigger=274000, strict empty MCP, recursion denies, explicit background tool/permission delta, foreground isUsingOverage=false, and immediate user confirmation that usage credits remain off. It does not require full declared-native attestation or claim the trigger is effective. Task 6 Groups D/F enforce the same prerequisites and label their context incomplete. Group G cleanup remains independently callable.

- [ ] **Step 1: Add fake-roster state-machine tests**

Tests cover:

~~~python
def test_worktree_event_and_lease_precede_first_write(fake_background):
    result = run_write_race_canary(fake_background)
    assert result.event_order[:3] == ("lease", "WorktreeCreate", "handler_stdout")
    assert result.first_write_after_handoff is True

def test_active_stop_requires_two_stable_observations(fake_background):
    fake_background.states("working", "stopped", "working")
    with pytest.raises(LiveGateError, match="stop not stable"):
        run_write_race_canary(fake_background)
~~~

Also cover missing/duplicate --worktree, duplicate name/row, unknown schema, timeout, dirty/remote repo, mismatched event/lease/roster/hook-cwd path, proof content mismatch, unexpected file/commit, missing Stop, respawn identity mismatch, and quota pause.

- [ ] **Step 2: Implement the Group C runner and disposable builder**

prepare_background must record no remote, base commit, common-dir identity, unique session/worktree names, approval digest, and root containment. build_background_argv includes exact `--worktree {worktree_name}` and the per-run settings contain `{"worktree":{"baseRef":"head"}}`; neither surface is probed before Group C approval. It installs the existing dedicated spikes/phase0a/worktree_hook.py as the replacing WorktreeCreate command hook with fixed Python/repo/root/event/lease/lock/execution argv, includes that source and every target in the executable manifest, and rejects any generic-event-sink substitution. PASS requires the handler's last non-empty stdout path to equal its durable lease acknowledgement, sanitized WorktreeCreate event, roster worktree, PreToolUse hook cwd, and Git common-dir before the first write. The write guard denies until the permanent lease is committed and then permits only the exact proof path under that same cwd. Local state stores opaque short/session IDs and canonical worktree path only below the ignored run root. Public evidence stores counts, presence booleans, category enums, and equality/order booleans only; no identifier/path fingerprint.

- [ ] **Step 3: Verify and commit deterministic Group C code**

Run fake-roster, background-probe, and complete safe tests, then commit code/tests before creating the preview:

~~~powershell
git add spikes\phase0a\live_background.py spikes\phase0a\background_probe.py tests\phase0a\test_live_background.py
git commit -m "test: add background lifecycle live canary"
~~~

- [ ] **Step 4: Build, preview Group C, and stop for approval**

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background main --preview --root .phase0a\live\background-main --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Explain exactly:

- maximum two top-level Sonnet 5 low launches: initial plus one respawn; native internal model/tool and Agent View summary requests are not hard-counted;
- one Claude-owned disposable worktree;
- one exact phase0a-proof.txt create/read/remove;
- at most three stop/respawn actions: initial stop, one respawn, and one conditional timeout stop;
- no rm/removal in this group.

The scope binds max_provider_session_launches=2, max_worktree_creates=1, max_stop_respawn_actions=3, max_file_deletes=1, max_removals=0, the exact --worktree name, baseRef=head settings, relative proof path, SHA-256 of ready plus one newline, prompt/settings/hook manifests, and background_internal_requests_acknowledged=true. Its side-effect specs contain every fixed launch plus stop/respawn templates whose short_id binding must come from Group C state. The displayed approval states that no per-turn overage stream exists for this supervisor-owned row and that the unchanged account toggle plus timeout are the only credit controls. Only the root controller creates approved-C.json after immediate approval.

- [ ] **Step 5: Launch the bounded task**

Exact prompt:

~~~text
In this disposable worktree only, use the file editing tool to create phase0a-proof.txt containing exactly ready and one newline. Confirm it exists, then run a local 30-second wait without changing any other file. Do not commit, add a remote, push, merge, or modify anything outside this worktree.
~~~

Execute:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background main --execute --approval .phase0a\live\approvals\approved-C.json --root .phase0a\live\background-main --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Use acceptEdits, exactly Read/Write/Bash, strict empty MCP, the same model/effort/autocompact/setting-source/recursion-deny fields as the approved Task 4 context run, and no fallback. A committed PreToolUse policy permits only the exact relative proof-file Read/Write inputs and the exact Bash command `sleep 30`; every other Bash/path input is denied. Record the necessary delta explicitly: tools changes from none to Read/Write/Bash and permission mode from dontAsk to acceptEdits; no other claimed context field may drift. Resolve the row only by unique name in agents --json --all. The fixture records declared_native_attestation=incomplete and Task 4's missing-field list.

- [ ] **Step 6: Verify handoff then stop while active**

Before accepting any write:

1. SessionStart exists;
2. lease and WorktreeCreate agree and are under the disposable root;
3. event precedes proof-file observation;
4. common-dir matches;
5. no remote/extra commit/unexpected file.

Verify the exact proof content and that it is the only worktree change, then stop the still-working row while leaving the proof file in place. Observe stopped twice with a 750 ms stabilization interval.

- [ ] **Step 7: Respawn once and finish**

Respawn the same short ID once through consume_side_effect. Require the same session/worktree/model/context fingerprints, one new working state, eventual done state, and a sanitized Stop event. Do not issue stop after a correctly completed row. After done, verify the proof file is still the only change, remove it exactly once under the approved file-delete effect, and require a clean worktree. Only on bounded timeout may the runner use the third approved action to stop and retain the proof file plus recovery state; do not spend the delete merely to hide an incomplete run. Do not call rm.

- [ ] **Step 8: Commit sanitized evidence**

Commit live lifecycle fixture/index only. Keep IDs/paths/launch output ignored:

~~~powershell
git add tests\fixtures\phase0a\current\live-background-lifecycle.json tests\fixtures\phase0a\current\evidence-index.json
git commit -m "test: record background lifecycle live evidence"
~~~

---

### Task 6: Needs-Input, Failed/StopFailure, Concurrency, and Agent View Limits

**Files:**
- Modify: spikes/phase0a/live_background.py
- Modify: tests/phase0a/test_live_background.py
- Modify: spikes/phase0a/contracts.py
- Modify: tests/phase0a/test_contracts.py
- Modify: tests/fixtures/phase0a/current/stop-failure-contract.json
- Create: tests/fixtures/phase0a/current/live-background-matrix.json
- Modify: tests/fixtures/phase0a/current/evidence-index.json

**Interfaces:**
- Consumes Groups D and F as independent receipts; Group E is offline-only and has no receipt.
- Produces required roster-state and concurrency evidence; no cleanup.

- [ ] **Step 1: Add tests for independent receipts and stop-on-failure**

Prove a Group D receipt cannot authorize F, an unused launch cannot carry over, and any quota/schema/worktree anomaly prevents later model-group execution while Group G stop/recovery/cleanup remains available.

- [ ] **Step 2: Implement Groups D/F plus offline StopFailure correction and commit before preview**

Add exact prompts, state-machine policies, timeouts, attachment handling, per-effect consumed ledgers, the corrected StopFailure fixture, and fixture projection. Rebuild the evidence index after the taxonomy migration. Every stop/attach action goes through consume_side_effect; there is no direct controller shell escape. Run focused plus complete safe tests, then commit:

~~~powershell
git add spikes\phase0a\live_background.py tests\phase0a\test_live_background.py spikes\phase0a\contracts.py tests\phase0a\test_contracts.py tests\fixtures\phase0a\current\stop-failure-contract.json tests\fixtures\phase0a\current\evidence-index.json
git commit -m "test: add background matrix live canaries"
~~~

- [ ] **Step 3: Group D needs-input preview and execution**

Preview:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background needs-input --preview --include-attach --root .phase0a\live\background-needs-input --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

The scope binds max_provider_session_launches=1, max_worktree_creates=1, max_stop_respawn_actions=1, max_attach_actions=1, no file delete/removal, exact `--worktree {worktree_name}`, baseRef=head settings, prompt/settings/executable manifest, background internal-request acknowledgement, and attach/stop argv templates whose ID must be read from this group's own state and match ^[A-Za-z0-9_-]{1,64}$. It states the background credit limitation and context-incomplete label. After direct approval the controller creates approved-D.json and executes in a visible TTY:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background needs-input --execute --attach --approval .phase0a\live\approvals\approved-D.json --root .phase0a\live\background-needs-input --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Use one Sonnet 5 low background turn with exact --worktree in a fresh disposable repo, manual permission, Read/Write only, and a prompt requiring creation of one unapproved file. The same dedicated WorktreeCreate handler must establish lease/event/roster/hook-cwd equality before the attempted Write. The background agent must be unable to edit and must expose the documented needs-input/blocked category. Before attach, require the row to be needs-input/blocked, group-owned, and unchanged. If the installed schema uses an undocumented state, record agents_json_schema BLOCKED rather than mapping by guess.

live_background resolves the ID only after the checks above, writes the concrete attach argv to the consumed ledger, and launches claude attach through the bound CLI with stdin/stdout/stderr inherited from the controller TTY. Send no prompt and press Ctrl+Z, the exact detach control documented by installed claude attach --help. After the child exits, require the same blocked row, no worktree change, and no new working transition; only then consume the approved stop action and observe stopped twice. If attach unexpectedly resumes work, stop the same owned row within the existing allowance, mark the gate BLOCKED, and retain recovery evidence. Control evidence comes from attach exit plus the injected sanitized lifecycle hook's same-session equality boolean; never parse TUI text. If no TTY or attach approval exists, do not invoke it and keep lifecycle_commands BLOCKED.

- [ ] **Step 4: Correct StopFailure taxonomy offline; make no Group E live call**

Update contracts.py, its tests, and stop-failure-contract.json to the official 2026-08-20 hook set: rate_limit, authentication_failed, oauth_org_not_allowed, billing_error, invalid_request, server_error, max_output_tokens, unknown. Preserve any future/unrecognized installed value only as unknown plus bounded provenance. Tests must prove model_not_found and overloaded normalize to unknown on the StopFailure hook path while remaining valid normalized circuit conditions when a different official transport reports them. No deterministic safe documented background request can force StopFailure on this account without risking quota/billing/auth state, so Group E has no preview, receipt, provider launch, row, or worktree. Keep stop_failure_hook BLOCKED unless C/D/F naturally emits a documented category; do not induce one.

- [ ] **Step 5: Group F concurrency preview and execution**

Preview only:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background concurrency --preview --root .phase0a\live\background-concurrency --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Stop, display the digest, and wait. After direct approval and controller-created approved-F.json, execute:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background concurrency --execute --approval .phase0a\live\approvals\approved-F.json --root .phase0a\live\background-concurrency --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Group F's scope declares max_provider_session_launches=2, max_worktree_creates=0, max_stop_respawn_actions=2, max_attach_actions=0, and two exact stop templates bound to its two group-owned short IDs. After separate approval, launch exactly two uniquely named Sonnet 5 low background tasks with Bash as the only tool and a committed PreToolUse policy allowing only the exact command `sleep 20`; omit --worktree. Their prompt requires that wait and an exact marker response. Require both structured rows simultaneously working/blocked at least once. Stop both through group-owned short-ID templates and consumed ledgers. Record observed_floor=2 and provider_ceiling=UNKNOWN; launching exactly two cannot prove a provider limit. Subagent MCP's future cap of two is a policy, not an observed provider ceiling.

- [ ] **Step 6: Agent View overhead adjudication**

Do not make an extra live query. Cite https://code.claude.com/docs/en/agent-view with retrieval date 2026-08-20 for the documented end-of-turn/periodic Haiku-class summary requests. No official per-session accounting split was found on that date, so keep agent_view_overhead UNKNOWN; never read private usage-history files or infer cost from total_cost_usd.

- [ ] **Step 7: Verify the lifecycle matrix**

The committed sanitized fixture must cover working, needs-input/blocked, done, failed, and stopped without IDs/paths/status text. Missing any state keeps agents_json_schema and lifecycle_commands BLOCKED.

- [ ] **Step 8: Commit sanitized matrix evidence**

Commit fixture/index only. Do not call rm or delete any worktree/transcript:

~~~powershell
git add tests\fixtures\phase0a\current\live-background-matrix.json tests\fixtures\phase0a\current\evidence-index.json
git commit -m "test: record background state and concurrency evidence"
~~~

---

### Task 7: Approval-Gated Disposable Row and Worktree Release

**Files:**
- Modify: spikes/phase0a/live_background.py
- Modify: tests/phase0a/test_live_background.py
- Modify: docs/phase0a/phase0a-live-runbook.md
- Create: tests/fixtures/phase0a/current/live-worktree-remove.json
- Modify: tests/fixtures/phase0a/current/evidence-index.json

**Interfaces:**
- Consumes only Group G receipts listing exact local row fingerprints and canonical worktree targets.
- Produces sanitized WorktreeRemove evidence or a RECOVERY_REQUIRED residual manifest.

This task may be implemented and executed before Tasks 3–6 live groups when Pre-Live Gate 0 finds plan-owned residuals. Its code dependencies are Tasks 1, 2, and 5 implementation only; it never depends on a successful model gate.

- [ ] **Step 1: Add cleanup-audit tests**

For every row/worktree require:

- path under approved disposable root;
- common-dir equals its disposable repo;
- zero git status lines;
- zero commits above base;
- no remote;
- row is stopped/done/failed, never working;
- no matching standalone child process;
- lease/event path agreement.
- creation provenance proves the row/worktree was created by this exact plan and approval lineage; a pre-existing, user, or unknown row is ineligible.

A failed check returns RECOVERY_REQUIRED and performs zero rm/remove/unlink calls.

- [ ] **Step 2: Implement and commit deterministic cleanup code**

Run cleanup-audit tests and the complete safe suite, then commit code/tests/runbook before any preview:

~~~powershell
git add spikes\phase0a\live_background.py tests\phase0a\test_live_background.py docs\phase0a\phase0a-live-runbook.md
git commit -m "test: add approval-gated background cleanup canary"
~~~

- [ ] **Step 3: Produce a human-readable removal preview**

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background cleanup --preview --root .phase0a\live\background-cleanup --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Render the exact canonical ApprovalScope that will be hashed: local target paths/short IDs, ordered claude rm argv specs, creation-approval lineage, count, and audit results. Do not place them in committed or model-facing files. Execute re-renders and re-hashes the same object immediately before the first removal; any drift aborts all removals. max_removals equals the exact target count.

- [ ] **Step 4: Stop for immediate destructive approval**

Explain plainly that claude rm is the official Claude-owned lifecycle command and deletes the newly created disposable native session state and worktree. This is the only proposed transcript-deletion carve-out: it never applies to a pre-existing/user/unknown transcript, and Subagent MCP never edits a native file directly. If the user does not explicitly approve this deletion for the displayed targets, retain them and keep worktree_remove_hook BLOCKED. No approval from an earlier group applies.

- [ ] **Step 5: Execute exact approved removals**

After the controller creates approved-G.json from the approved digest, run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_background cleanup --execute --approval .phase0a\live\approvals\approved-G.json --root .phase0a\live\background-cleanup --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Call claude rm once per approved short ID only through consume_side_effect after appending the concrete argv to the ledger. After each call require:

- WorktreeRemove event matches the local approved row/path; public evidence stores only match=true;
- target path absent;
- git worktree porcelain no longer registers it;
- no unrelated row/worktree changed.

If rm refuses, stop. Do not fall back to git worktree remove or filesystem deletion.

- [ ] **Step 6: Record residuals and commit**

Commit only sanitized removal booleans/counts and updated index/report input. Keep any refused target in ignored recovery state and report it to the user:

~~~powershell
git add tests\fixtures\phase0a\current\live-worktree-remove.json tests\fixtures\phase0a\current\evidence-index.json
git commit -m "test: record approved disposable worktree release evidence"
~~~

---

### Task 8: Deterministic Gate Report, Independent Reviews, and Phase Decision

**Files:**
- Modify: spikes/phase0a/live_evidence.py
- Modify: tests/phase0a/test_live_evidence.py
- Create: spikes/phase0a/live_review_guard.py
- Create: tests/phase0a/test_live_review_guard.py
- Modify: spikes/phase0a/report.py
- Modify: tests/phase0a/test_report.py
- Modify: docs/phase0a/phase0a-report.md
- Modify: tests/fixtures/phase0a/current/context-attestation.json
- Modify: tests/fixtures/phase0a/current/evidence-index.json

**Interfaces:**
- Consumes only the committed evidence index, validated sanitized derivatives, and current safe-test/cleanup audit.
- Produces adjudicate_gate_set, an exact gate set, section 19.1 decision, path-confined common review export, final-write-set verification, and no Phase 0b action.

- [ ] **Step 1: Add failing evidence-to-gate tests**

Each gate may become PASS only from its named fixture and dependency set. Tests include:

~~~python
def test_context_gate_cannot_pass_from_requested_274000_only():
    evidence = context_evidence(
        requested_trigger=274000,
        requested_window=274000,
        effective_window=None,
        effective_trigger_percent=None,
        effective_trigger_tokens=None,
    )
    assert adjudicate_context(evidence) == "BLOCKED"

def test_lifecycle_gate_requires_all_five_states():
    evidence = background_evidence(states={"working", "done", "stopped", "failed"})
    assert adjudicate_lifecycle(evidence) == "BLOCKED"

def test_ignored_hand_edited_gate_file_cannot_change_report(tmp_path):
    (tmp_path / "gates.json").write_text(
        '{"context_attestation":{"status":"PASS","evidence":"typed"}}',
        encoding="utf-8",
    )
    first = adjudicate_gate_set(COMMITTED_INDEX, COMMITTED_FIXTURE_ROOT)
    second = adjudicate_gate_set(COMMITTED_INDEX, COMMITTED_FIXTURE_ROOT)
    assert first == second
    assert first["context_attestation"]["status"] == "BLOCKED"
~~~

Unknown/missing fixture, index/hash mismatch, CLI identity drift, unremoved unexpected worktree/process, or failed publication scan prevents PASS. There is no public or CLI function that accepts externally supplied gate statuses. Every PASS comes from one named adjudicator and dependency set over index-validated committed fixtures.

- [ ] **Step 2: Build deterministic sanitized live fixtures/index and report code**

Every fixture uses schema version, observed CLI version/executable content digest, incremental raw-source digest, sanitized bounded top-level system/rate/result envelopes sufficient to replay the parser, observed/missing coverage, and payload. Do not commit native path, device/inode, repository/path fingerprint, account/prompt/result text, roster ID, or machine-specific local identity.

Migrate the existing context fixture/public projections in the same tested checkpoint: remove total_cost_usd and exact token counts; replace absolute plugin_count with forbidden-surface booleans plus relative control/deny deltas. Cost-field presence may be recorded only as a boolean. Existing fixture replay must remain green after the evidence-index hashes are deliberately regenerated.

- [ ] **Step 3: Regenerate report twice**

Use one reviewed RFC3339 timestamp. Regenerate directly from the committed evidence index with the literal command:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence regenerate-report --evidence-index tests\fixtures\phase0a\current\evidence-index.json --fixture-root tests\fixtures\phase0a\current --generated-at 2026-08-20T00:00:00+07:00 --output docs\phase0a\phase0a-report.md
~~~

The subcommand calls adjudicate_gate_set and report rendering internally; it has no --gate-input/status override. Run it twice; require identical full SHA-256 and unchanged outer narrative. Update section 19.1 rows and decision conservatively. If any required gate remains BLOCKED/UNKNOWN, state Phase 0b must not begin and name the exact missing capability.

- [ ] **Step 4: Run fresh verification**

Run:

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -o addopts= -q -m "not real_git_worktree"
git diff --check
git status --short --branch
~~~

Also require exact expected Git worktree set, zero task-owned Python/standalone processes, no unexpected active rows, and credential/PII/raw-identifier scans.

- [ ] **Step 5: Commit the clean review checkpoint**

Commit deterministic adjudication code/tests/report/index before either reviewer sees it:

~~~powershell
git add spikes\phase0a\live_evidence.py spikes\phase0a\live_review_guard.py tests\phase0a\test_live_evidence.py tests\phase0a\test_live_review_guard.py spikes\phase0a\report.py tests\phase0a\test_report.py docs\phase0a\phase0a-report.md tests\fixtures\phase0a\current\context-attestation.json tests\fixtures\phase0a\current\evidence-index.json
git commit -m "docs: checkpoint Phase 0a live gate report"
~~~

Regenerate the common packet after this commit; it binds the clean HEAD and sanitized fixture/report hashes.

- [ ] **Step 6: Dispatch native Codex independent review**

Create one bounded model-agnostic packet containing relative spec/plan paths, base/head, sanitized fixtures/report, exact safe-test evidence, and no prior review conclusions/raw local evidence. Dispatch a fresh Sol-high reviewer read-only against the committed checkout. Adjudicate it before previewing H. Any plausible or verified finding about export confinement, approval binding, executable trust, quota/usage credits, transcript ownership, destructive scope, or CLI/config mutation fails fast: fix the checkpoint, run fresh verification, commit, and obtain a clean scoped native review first. Only non-safety findings may remain for dual-model comparison on the same checkpoint.

- [ ] **Step 7: Preview Group H different-harness review**

Create a fresh export root by archiving only clean HEAD, validating every archive member as a regular file/directory with no absolute path, .. component, symlink, hardlink, or reparse point, and extracting without .git, ignored state, or worktrees. Add the relative common packet plus only committed sanitized artifacts. Group H's cwd is this export root, never the real checkout.

Use that export through standalone Claude Code with:

- claude-opus-5;
- xhigh requested;
- exact trigger target 274000 plus --autocompact window 274000 requested and reported separately;
- project setting source for the independent review;
- strict empty MCP;
- Read/Glob/Grep only;
- persistent resumable session;
- no fallback/usage credits.
- a committed PreToolUse command hook pinned to the official 2026-08-20 schemas: Read requires string file_path and permits only optional nonnegative integer offset/limit plus a bounded pages range string; Glob requires string pattern and permits only optional string path; Grep requires string pattern and permits only path/glob/type strings, output_mode in content/files_with_matches/count, boolean -i/-n/multiline, and nonnegative integer -B/-A/-C/context/head_limit/offset. Read.file_path and optional Glob/Grep.path must canonicalize inside the export root. Glob.pattern and Grep.glob must be relative and contain no drive/UNC root or .. segment. Any other key/type/value, symlink/reparse resolution, malformed path, or escape exits 2 and quarantines Group H rather than widening the schema.

Run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence final-review --preview --root .phase0a\live\final-review --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Before preview, require Task 2's official PreToolUse capability probe plus a no-model init of the exact export/settings to attest zero hook errors and the expected project settings identity. The scope binds that no-model preflight plus one top-level CLI launch, zero worktree/stop/attach/delete/remove actions, the export manifest/hash, common packet hash, clean HEAD, exact export cwd, strict empty MCP/read-only argv, path-guard settings/executable, and persistent-session=true. Show the canonical scope and digest, disclose the complete exported file manifest and that Claude Code will retain a durable native review transcript containing only this export, and stop for approval. This review does not substitute for context_attestation because project-only settings are intentionally narrower.

- [ ] **Step 8: Execute Group H and adjudicate**

After the controller creates approved-H.json, run:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence final-review --execute --approval .phase0a\live\approvals\approved-H.json --root .phase0a\live\final-review --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Stream through live_common, return one UTF-8 sanitized final report without extra retrieval launches, and retain only sanitized metadata. Require the path guard's allow/deny audit counts with zero escape; never persist requested paths. If quota rejects, overage appears, or path confinement fails, stop without retry. H is reachable only after the preceding native safety review is clean. Then verify every Claude finding against source/tests; one consolidated fix wave and fresh verification are required for blocking findings. A native Codex scoped re-review consumes no H allowance. Any Claude scoped re-review requires a fresh exported snapshot, Group H2 preview/digest, direct approval, and at most one new Opus launch.

- [ ] **Step 9: Optional Group H2 scoped Claude re-review**

Only if verified blocking Claude findings caused a fix wave, preview a new clean-HEAD export:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence final-review --preview --rereview --root .phase0a\live\final-rereview --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

Stop for a new digest-specific approval. After the controller creates approved-H2.json, execute exactly:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence final-review --execute --rereview --approval .phase0a\live\approvals\approved-H2.json --root .phase0a\live\final-rereview --cli "$env:USERPROFILE\.local\bin\claude.exe"
~~~

H2 has the same export/path/quota restrictions as H and receives only the original verified findings plus the scoped diff/tests. No H/H2 allowance carries over.

- [ ] **Step 10: User report-acceptance gate**

Present:

- every gate and evidence;
- all remaining BLOCKED/UNKNOWN items;
- exact approved provider-capable session launches, structured rate states, and isUsingOverage status; background internal requests are reported as unbounded/unknown;
- created/removed/residual rows/worktrees;
- all rulings and review findings;
- recommendation: accept Phase 0a, remove affected v1 capability, or keep Phase 0b blocked.

Do not install SDK/Node/MCP dependencies or start Phase 0b until the user explicitly approves the report.

- [ ] **Step 11: Commit only after review fixes**

Run final safe tests/scans and commit:

~~~powershell
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence verify-final-write-set --planned-only --output .phase0a\live\report\final-write-set.json
if($LASTEXITCODE -ne 0){ throw 'final write-set verification failed' }
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence stage-final-write-set --manifest .phase0a\live\report\final-write-set.json
if($LASTEXITCODE -ne 0){ throw 'exact final write-set staging failed' }
& .\.venv\Scripts\python.exe -B -m spikes.phase0a.live_evidence verify-index-only
if($LASTEXITCODE -ne 0){ throw 'staged write-set verification failed' }
git commit -m "docs: record approved Phase 0a live gates"
~~~

verify-final-write-set parses git status --porcelain=v1 -z --untracked-files=all as bytes, rejects rename/copy/anything outside the embedded exact planned write set, sees untracked files, and writes an ignored manifest of exact relative paths plus content hashes. stage-final-write-set revalidates every path/hash and invokes git add -- with only that argv list; it never stages a prefix/directory. verify-index-only requires every staged fixture hash to match the staged evidence index and rejects any staged ignored/raw path.

## Plan Self-Review Checklist

- [x] Every current report gate maps to one task and an exact PASS rule.
- [x] No task uses the Desktop wrapper, SDK-bundled binary, raw transcript, TUI/log parser, or private usage history.
- [x] No task installs Node/SDK/MCP, changes auth/config/billing, enables usage credits, or registers a client.
- [x] Every provider-capable session launch belongs to exactly one user-approved digest and no group exceeds its maximum.
- [x] Requested/effective model and effort are distinct; trigger target, provider window, trigger percentage, and effective trigger tokens are separate, and unattested values stay blocked.
- [x] Background control comes only from agents JSON plus sanitized hook/lease events.
- [x] Worktree removal has a separate exact destructive approval and no fallback deletion.
- [x] All raw values remain in bounded memory or ignored local opaque state; public fixtures are replayable/sanitized.
- [x] The final report can honestly remain BLOCKED; the plan contains no force-PASS path.
- [x] Phase 0b dependencies and production MCP/UI remain out of scope.

## Execution Handoff

After this draft is reviewed, integrated into the chosen branch, committed, and explicitly approved:

1. Compact or start a fresh execution context.
2. Use superpowers:subagent-driven-development.
3. Execute Tasks 1–2 safe implementation first.
4. Stop at every Group A–H approval boundary; plan approval never substitutes for immediate live/destructive approval.

## Amendment: controller-only approval-storage bootstrap (2026-08-20)

The first real Group A approval attempt exposed a live defect before receipt creation or
execution: the host/preview-created `.phase0a/live` inherited a Windows DACL and
`approve-scope` correctly refused it as non-private. Group A did not execute.

Only the root controller, after direct user approval for this local ACL/mode action,
may run `prepare-approval-storage` against the exact cwd-relative
`.phase0a/live` root. The command creates only missing `.phase0a`, `live`, and
direct `approvals` directories; new `live`/`approvals` are owner-only. Existing
insecure storage remains fail-closed unless the controller explicitly requests
repair. Repair first proves direct containment and current-user ownership, refuses
nonempty insecure `approvals`, and changes only the `live` and `approvals`
directory ACL/mode; it never recurses or changes child contents. `approve-scope`
and every execute path remain unchanged and never invoke this bootstrap.

The existing live root requires `--repair-existing`; a fresh root requires no
repair flag. Any code commit changes the bound HEAD, so the old Group A pending
digest/approval is invalid. Regenerate a fresh Group A preview and obtain a new
direct digest-specific approval before creating a receipt or executing Group A.

## Amendment: owner-only runtime group roots (2026-08-20)

The first Group B preview exposed a separate pre-provider defect: Windows sandbox
directory creation gave a new runtime group root a multi-principal ACL, so the
approval ledger correctly failed closed before launch. Group B, the shared C/D/F
background materializer, and Group G cleanup now use one common helper that creates
only the missing leaf root with an owner-only ACL/mode. An existing root is accepted
only when it is direct, empty, and already owner-only; indirect, nonempty, or insecure
roots fail without repair. Group A and approval-storage bootstrap behavior are unchanged.

## Amendment: Group B post-init completion watchdog (2026-08-20)

The first provider-capable Group B run at the private-root checkpoint parsed a valid
`system/init`, delivered the bounded InstructionsLoaded observations, and reported no
stderr, rate, overage, result, or final marker before the runner ended almost exactly
30 seconds after launch. That proves the local all-in-one watchdog fired; it does not
prove a provider, quota, authentication, or model terminal error. Group B now retains
30 seconds as the pre-init/startup deadline and resets the deadline exactly once to a
separate 120-second post-init completion budget after a valid first init envelope.
Sanitized evidence records the terminal classification, process exit code, init/result
envelope booleans, and timeout phase without retaining provider text or identifiers.
After a clean deterministic fix and fresh digest-specific approval, this defect permits
at most one rerun; another timeout is recorded as a capability result rather than
opening an unchanged retry loop.
