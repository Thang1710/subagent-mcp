# Subagent MCP Artifact Relay Implementation Plan

> **For agentic workers:** Use test-driven development and one bounded writer.
> Do not commit, publish, invoke a provider from tests, or touch user/global
> configuration.

**Goal:** Let Codex pass one complete persisted agent report directly to another
external agent by hash while keeping the full text out of Codex context and
public request state.

**Architecture:** Add one optional artifact reference to the existing
`agent_send` contract. Resolve and validate the source in the service, require
the same workspace, expand only in memory for `AdapterSendRequest`, and keep
the persisted request reference-only. Add exact byte counts plus explicitly
rough content-only token comparisons to existing artifact/read metadata.

**Release target:** `0.1.0a20`.

---

### Task 1: Contract and transfer metrics

**Files:**
- Modify: `src/subagent_harness_mcp/contracts.py`
- Modify: `tests/unit/test_contracts.py`
- Modify: `tests/unit/test_service.py`

- [x] Write RED tests for a validated immutable artifact reference, compact
  artifact exact byte metrics, labelled rough token comparison, and slice
  metrics.
- [x] Add the smallest frozen reference contract and pure metric helpers.
- [x] Keep compact encoded status within the existing 2,048-byte bound.
- [x] Run focused contract/service tests GREEN.

### Task 2: Same-workspace in-memory relay

**Files:**
- Modify: `src/subagent_harness_mcp/service.py`
- Modify: `tests/unit/test_service.py`

- [x] Write RED tests proving exact full text reaches the adapter while durable
  request/event/state JSON contains only the artifact reference.
- [x] Write RED tests for wrong conversation/hash, source not succeeded, same
  source/target conversation, cross-workspace identity, and expanded prompt
  overflow. Assert each rejects before adapter open/send/provider work.
- [x] Implement one source resolver plus one deterministic untrusted-data prompt
  wrapper. Reuse the existing send lifecycle and idempotency path.
- [x] Run service tests GREEN.

### Task 3: MCP surface, documentation, and a20 version

**Files:**
- Modify: `src/subagent_harness_mcp/server.py`
- Modify: `schemas/tools-v1.json`
- Modify: `tests/unit/test_server.py`
- Modify: `tests/integration/test_stdio_fake.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/threat-model.md`
- Modify: `CHANGELOG.md`
- Modify: package/version contract files and `uv.lock`

- [x] Parse an optional `artifact` object on `agent_send`; keep the MCP tool
  count unchanged and all old calls backward compatible.
- [x] Add stdio proof that one fake-agent result is relayed to another target
  without calling `agent_result_read` from the controller.
- [x] Document same-workspace/hash/security behavior and honest metric meaning.
- [x] Bump package, adapters, server manifest, README install commands, and
  package tests to `0.1.0a20`; regenerate the lock.
- [x] Run focused tests only and report exact files/counts. Do not commit.

### Task 4: Root verification and release

- [x] Main agent independently inspects the diff and runs the full safe suite,
  artifact acceptance, privacy scan, and one bounded DeepSeek Critical-only
  attempt with one native fallback if the provider fails.
- [x] Fix at most one bounded wave of verified Critical findings; defer all
  non-Critical polish.
- [ ] Commit with the owner's exact author/committer identity, mirror to public,
  repeat artifact acceptance, publish, reinstall from PyPI, and run one real
  same-workspace relay proof only if the provider remains authorized and healthy;
  otherwise record the provider error and do not retry.
