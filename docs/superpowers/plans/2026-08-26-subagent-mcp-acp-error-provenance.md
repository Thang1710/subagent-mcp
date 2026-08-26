# Subagent MCP Native ACP Error Provenance Implementation Plan

> **For agentic workers:** Use test-driven development and verification-before-completion. No live provider call is part of this plan.

**Goal:** Preserve safe native ACP error provenance so a fresh controller can
classify a failed OX turn without guessing quota or replaying terminal work.

**Architecture:** Introduce one private typed ACP response exception at the
stdio boundary, carry only bounded scalar diagnostics through the existing
adapter snapshot, and rely on the service's existing recursive redaction before
persistence/public output. Keep lifecycle, retry, and public error codes stable.

**Tech Stack:** Python 3.10+, asyncio, dataclasses, pytest, Hatch/uv packaging.

## Task 1: Lock the regression in RED

**Files:**
- Modify `tests/unit/test_deepseek_harness_adapter.py`

- [ ] Add a wire-boundary test for JSON-RPC code, nested native detail, and a
      stable provider code.
- [ ] Add an adapter-lifecycle test proving terminal `PROVIDER_ERROR`, exact
      safe detail, structured evidence, and read-only recovery guidance.
- [ ] Run only those tests and capture the expected failure before production
      edits.

## Task 2: Preserve bounded ACP diagnostics

**Files:**
- Modify `src/subagent_harness_mcp/adapters/deepseek_harness.py`
- Modify `tests/unit/test_deepseek_harness_adapter.py`

- [ ] Add a private `_AcpResponseError` carrying bounded scalar facts only.
- [ ] Raise it from `_handle_response` for JSON-RPC errors.
- [ ] Preserve explicit provider code fields or a known stable code found in
      native text; never infer quota from absence/unknown state.
- [ ] Publish redacted error text and additive `provider_error` evidence from
      the failed snapshot.
- [ ] Keep read-only/write retry semantics and the old failed execution state
      unchanged.
- [ ] Run the focused tests GREEN plus existing quota/429 regressions.

## Task 3: Prepare and verify the release

**Files:**
- Modify `CHANGELOG.md`, `README.md`
- Modify release version/provenance files for the next patch version.
- Update ignored SDD checkpoint/progress evidence after verification.

- [ ] Run adapter, service-redaction, and public-contract focused tests.
- [ ] Run the complete safe suite with live/provider/real-worktree gates
      excluded exactly as documented by the repository.
- [ ] Build wheel/sdist and prove installed behavior outside the checkout.
- [ ] Run artifact privacy and secret scans plus `git diff --check`.
- [ ] Commit with the repository owner's configured identity, publish the next
      patch release, and verify public CI/artifact metadata.
- [ ] Notify the reporting task in one sentence first, then provide version,
      commit, test/CI evidence, restart command, and exact new-conversation retry
      instructions. Do not call the provider from this repair task.
