# Concurrent writer scopes implementation plan

> **Writer rule:** root is the sole implementation writer. Use TDD, no live
> provider/model call, and one Critical-only review wave.

**Goal:** Remove the whole-workspace mutex while retaining deterministic overlap
protection and native write-boundary attestation.

### Task 1: Contract and store RED/GREEN

- Add failing contract tests for normalization, traversal rejection, and the
  whole-workspace compatibility default.
- Add failing store tests for atomic disjoint acquisition, equal/ancestor overlap,
  idempotency, and legacy-row compatibility.
- Implement scoped lease resource keys and `WRITE_SET_BUSY` without a destructive
  database migration.

### Task 2: Service and adapter RED/GREEN

- Add `write_set` to the MCP surface, spawn digest, requested metadata, context
  request, and attestation.
- Prove disjoint service spawns both launch while overlap stops before launch.
- Add Claude deterministic hook tests for in-scope allow and out-of-scope deny.
- Add DeepSeek deterministic tests proving one native scope becomes `session/new`
  cwd and an unsupported multi-root request fails before model work.

### Task 3: Critical verification and release

- Run focused contract/store/service/Claude/DeepSeek suites.
- Run integration and full safe suites, excluding separately gated real-worktree
  tests.
- Perform one fresh Critical-only review; accept or reject each finding once.
- Update public docs/changelog/version and build artifacts only after green tests.
- Provider live proof remains a separate explicit gate and never enables usage
  credits or overage.

