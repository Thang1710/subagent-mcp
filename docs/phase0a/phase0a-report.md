# Subagent MCP Phase 0a Report

## Checkpoint summary

- This deterministic report uses only committed, sanitized Phase 0a fixtures and replayable repository tests as public evidence.
- Committed fixture replay covers structured result classification without parsing assistant/thinking payloads or depending on JSON key order; it does not establish current live model, quota, lifecycle, worktree, or cleanup state.
- Gates without committed sanitized, reproducible evidence remain `BLOCKED` or `UNKNOWN`; this non-live correction plan makes no acceptance claim from retained machine-local evidence.
- The production packaging invariant is now explicit: Subagent MCP must ship as a standalone open-source MCP server, independent of personal plugins or machine-specific state.

<!-- BEGIN GENERATED GATES -->
Generated: 2026-08-20T00:00:00+07:00

| Gate | Status | Evidence |
|---|---|---|
| agent_view_overhead | UNKNOWN | No indexed official per-session Agent View accounting surface is available. |
| agents_json_schema | BLOCKED | Required committed sanitized evidence is missing. |
| background_concurrency | BLOCKED | Required committed sanitized evidence is missing. |
| context_attestation | BLOCKED | Required committed sanitized evidence is missing. |
| context_init_subset | PASS | context-attestation.json validates the bounded init subset. |
| credential_precedence | BLOCKED | Required committed sanitized evidence is missing. |
| daemon_stop_race | BLOCKED | Required committed sanitized evidence is missing. |
| init_only_capability | BLOCKED | Required committed sanitized evidence is missing. |
| lifecycle_commands | BLOCKED | Required committed sanitized evidence is missing. |
| observer_visibility | UNKNOWN | No indexed live observer fixture proves equal standalone identity. |
| plugin_disable_effective | BLOCKED | Required committed sanitized evidence is missing. |
| project_manifest | BLOCKED | Required committed sanitized evidence is missing. |
| session_start_hook | BLOCKED | Required committed sanitized evidence is missing. |
| standalone_cli | UNKNOWN | No indexed live host fixture proves standalone identity and wrapper rejection. |
| stop_failure_hook | BLOCKED | Required committed sanitized evidence is missing. |
| stop_hook | BLOCKED | Required committed sanitized evidence is missing. |
| strict_mcp_pre_spawn | PASS | strict-mcp-control.json contains the indexed strict/control differential. |
| subscription_auth | PASS | Indexed subscription auth evidence is first-party claude.ai. |
| windows_handle_release | BLOCKED | Required committed sanitized evidence is missing. |
| worktree_create_hook | BLOCKED | Required committed sanitized evidence is missing. |
| worktree_remove_hook | BLOCKED | Required committed sanitized evidence is missing. |

UNKNOWN is not PASS. FAIL or BLOCKED prevents the dependent Phase 0b capability.
<!-- END GENERATED GATES -->

## Design section 19.1 coverage

<!-- BEGIN GENERATED SECTION 19.1 -->
| Requirement | Outcome | Report evidence |
|---|---|---|
| 1. Auth precedence | BLOCKED | subscription_auth=PASS, credential_precedence=BLOCKED |
| 2. Standalone identity and Desktop-wrapper rejection | UNKNOWN | standalone_cli=UNKNOWN, observer_visibility=UNKNOWN |
| 3. Lifecycle commands and all roster states | BLOCKED | lifecycle_commands=BLOCKED, agents_json_schema=BLOCKED, session_start_hook=BLOCKED |
| 4. Strict declared MCP before spawn | BLOCKED | strict_mcp_pre_spawn=PASS, init_only_capability=BLOCKED |
| 5. Project manifest and bounded handle cleanup | BLOCKED | project_manifest=BLOCKED, windows_handle_release=BLOCKED |
| 6. WorktreeCreate and WorktreeRemove | BLOCKED | worktree_create_hook=BLOCKED, worktree_remove_hook=BLOCKED |
| 7. Background Stop and StopFailure | BLOCKED | stop_hook=BLOCKED, stop_failure_hook=BLOCKED |
| 8. Active daemon stop/respawn race | BLOCKED | daemon_stop_race=BLOCKED |
| 9. Agent View overhead and concurrency | BLOCKED | agent_view_overhead=UNKNOWN, background_concurrency=BLOCKED |
| 10. Declared-native context and cost | BLOCKED | context_init_subset=PASS, context_attestation=BLOCKED, plugin_disable_effective=BLOCKED |
<!-- END GENERATED SECTION 19.1 -->

## Residual state and cleanup

- This report makes no claim about the current live roster, process, or worktree cleanup state.
- Ignored raw evidence remains under `.phase0a/` as retained local input; it is not public evidence and was not deleted by this plan.
- This non-live correction plan did not mutate native Claude/Codex transcripts or Claude/Codex configuration.

## Decision before Phase 0b

<!-- BEGIN GENERATED PHASE DECISION -->
Phase 0a evidence decision: **BLOCKED**. Phase 0b must not begin.

Non-PASS requirements:

- 1. Auth precedence
- 2. Standalone identity and Desktop-wrapper rejection
- 3. Lifecycle commands and all roster states
- 4. Strict declared MCP before spawn
- 5. Project manifest and bounded handle cleanup
- 6. WorktreeCreate and WorktreeRemove
- 7. Background Stop and StopFailure
- 8. Active daemon stop/respawn race
- 9. Agent View overhead and concurrency
- 10. Declared-native context and cost
<!-- END GENERATED PHASE DECISION -->
