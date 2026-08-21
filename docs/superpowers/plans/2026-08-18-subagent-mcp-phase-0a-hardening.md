# Subagent MCP Phase 0a Contract Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase 0a probe code fail closed, privacy-safe, recoverable, reproducible, and honest enough to support a later separately approved live-gate plan.

**Architecture:** Keep all changes inside the existing Python Phase 0a spike. Harden shared parsing/redaction/locking primitives first, then manifest, hook/worktree, host, fixture, and report consumers. Code/evidence tasks run no model; the final different-harness review may run only after its own explicit approval. This plan deliberately ends with unproved live capabilities marked `BLOCKED`/`UNKNOWN`; it does not install the SDK, register MCP, or begin Phase 0b.

**Tech Stack:** Python 3.10 stdlib, pytest 8, Git, Windows PowerShell 5.1, existing standalone Claude Code 2.1.224 for no-model probes and an optional separately approved final review.

**Spec:** `docs/superpowers/specs/2026-08-17-subagent-mcp-design.md` at approved commit `af9b25514abfa95ef1134bfb4a9357b7a85144f0`.

## Global Constraints

- Read `AGENTS.md`, the approved spec, this plan, and the exact current checkpoint before every task.
- Work from a compact/fresh execution context. Do not execute this plan from the brainstorming transcript.
- Do not install Node, the Claude Agent SDK, the MCP SDK, or any other dependency.
- Do not register/unregister MCP, edit Codex/Claude configuration, enable usage credits, alter billing, or change authentication.
- Do not invoke a Claude model before Task 8's separately approved independent review. Do not create/remove a Claude background row or run a live hook canary anywhere in this plan.
- Existing Claude/Codex transcripts and AgentBridge state are immutable and out of scope.
- Use argv arrays with `shell=False`; never interpolate model/repository content into a shell command.
- Keep raw `.phase0a/` evidence local and ignored. Never copy account identity, native session IDs, request IDs, raw assistant text, or raw provider bodies into committed files/model-facing summaries.
- Tests run with bytecode and pytest cache disabled. The three real-Git worktree tests require a separate explicit approval immediately before execution; mocked/unit worktree tests do not.
- Before every commit run the task tests, `git diff --check`, and `git status --short --branch`.
- Use the repository/user Git identity already configured. If author identity is missing, stop and ask; do not modify global or repository Git config.
- A failed test, missing fixture source, unexpected CLI schema, dirty disposable repository, or uncertain cleanup state stops the task. Record the blocker rather than weakening an assertion.

## Plan Scope and Follow-on Plans

This plan implements spec section 19.1.1 items 1–8 and the non-model portion of item 9. It produces a corrected but conservative Phase 0a report. A later `phase-0a-live-gates` plan must be written and separately approved for positive plugin control, background lifecycle, WorktreeCreate/Remove, StopFailure, active stop/respawn, and any other quota-consuming or native-session mutation. Phase 0b planning remains forbidden until both plans and independent reviews pass.

## File Responsibility Map

| File | Responsibility after this plan |
|---|---|
| `spikes/phase0a/core.py` | Atomic JSON writes, argv execution, bounded stdin, key-aware redaction, hashing helpers |
| `spikes/phase0a/contracts.py` | Order-independent top-level envelope dispatch and strict normalized CLI contracts |
| `spikes/phase0a/manifest.py` | Complete project/local content manifest and canonical path+hash trust keys |
| `spikes/phase0a/locking.py` | Bounded cross-platform advisory file locks for event and creation serialization |
| `spikes/phase0a/hook_sink.py` | Event-specific, content-minimal hook normalization and append-only writes |
| `spikes/phase0a/worktree_hook.py` | Recoverable WorktreeCreate transaction including stdout hand-off |
| `spikes/phase0a/background_probe.py` | Unique execution IDs and exact handler argv/config generation |
| `spikes/phase0a/host_probe.py` | Normalized executable/auth/roster/observer evidence without raw command payloads |
| `spikes/phase0a/fixtures.py` | Versioned, provenance-carrying committed fixture envelopes and replay validation |
| `spikes/phase0a/report.py` | Exact gate validation and marker-bounded deterministic report updates |
| `tests/phase0a/*` | Focused unit/integration checks for every corrected failure branch |
| `tests/fixtures/phase0a/current/*` | Sanitized, versioned, replay-tested evidence only |
| `docs/phase0a/phase0a-report.md` | Reviewed narrative with generated gate block and conservative adjudication |

## Spec 19.1.1 Coverage Map

| Spec item | Plan coverage |
|---|---|
| 1. Strict order-independent envelopes/types/error codes | Task 1 |
| 2. Canonical path+hash+repository+revision trust and transitive manifest | Task 2 |
| 3. Minimal hook fields, normalized auth/roster, redaction, local-only raw evidence | Tasks 3, 5, and 6 |
| 4. Worktree transaction, partial add, durable recovery, bounded stdin/stdout hand-off | Task 4 |
| 5. Executable identity and non-vacuous observer/wrapper evidence | Task 5 |
| 6. Init subset versus declared-native, proxy caveat, plugin positive-control blocker | Tasks 6 and 7 |
| 7. Fixture replay/version/provenance and sanitized summaries | Task 6 |
| 8. Exact gates and non-destructive deterministic report generation | Task 7 |
| 9. Safe tests, approval-gated real-Git tests, and independent dual-harness review | Task 8 |

---

### Task 1: Fail-Closed, Key-Order-Independent CLI Contracts

**Files:**
- Modify: `spikes/phase0a/contracts.py`
- Modify: `tests/phase0a/test_contracts.py`

**Interfaces:**
- Consumes: UTF-8/UTF-8-BOM Claude stream JSONL, auth JSON object, agents JSON array.
- Produces: `peek_top_level_type(line: str) -> str | None`, strict `normalize_auth`, `normalize_agents`, `normalize_stream_json`, and `classify_turn` results.
- Classification values after this task: `success`, `incomplete`, `terminal_quota`, `terminal_credits_required`, `terminal_error`.

- [ ] **Step 1: Add failing auth/result/type tests**

Add these imports and tests to `tests/phase0a/test_contracts.py`:

```python
import json


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_normalize_auth_rejects_non_boolean_logged_in(value):
    with pytest.raises(ValueError, match="loggedIn must be a boolean"):
        normalize_auth({
            "loggedIn": value,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
        })


@pytest.mark.parametrize(
    "result",
    [
        {"subtype": "success", "is_error": False, "type": "result"},
        {"subtype": "success", "type": "result", "is_error": False},
        {"type": "result", "subtype": "success", "is_error": False},
    ],
)
def test_normalize_stream_json_accepts_any_top_level_result_key_order(tmp_path, result):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps({"model": "claude-sonnet-5", "tools": [], "subtype": "init", "type": "system"})
        + "\n"
        + json.dumps(result)
        + "\n",
        encoding="utf-8",
    )
    assert normalize_stream_json(stream)["result"]["is_error"] is False


def test_normalize_stream_json_rejects_missing_result_is_error(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        '{"type":"system","subtype":"init","model":"sonnet","tools":[]}\n'
        '{"subtype":"success","type":"result"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="result.is_error must be a boolean"):
        normalize_stream_json(stream)


@pytest.mark.parametrize("tools", [None, {}, "Read", ["Read", 1]])
def test_normalize_stream_json_rejects_invalid_tools(tmp_path, tools):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init", "model": "sonnet", "tools": tools})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "is_error": False})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="system.tools"):
        normalize_stream_json(stream)


@pytest.mark.parametrize("field,value", [("id", 7), ("sessionId", []), ("cwd", {})])
def test_normalize_agents_rejects_wrong_present_field_types(field, value):
    with pytest.raises(ValueError, match=field):
        normalize_agents([{"kind": "background", field: value}])
```

Update `test_normalize_stream_json_extracts_init_and_result` so its result envelope explicitly contains `"is_error":false`.

- [ ] **Step 2: Add failing assistant-skip and quota-detail tests**

Append:

```python
def test_assistant_with_type_after_nested_content_is_never_json_decoded(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        '{"type":"system","subtype":"init","model":"sonnet","tools":[]}\n'
        '{"message":THIS_MUST_NOT_BE_PARSED,"type":"assistant"}\n'
        '{"type":"result","subtype":"success","is_error":false}\n',
        encoding="utf-8",
    )
    assert normalize_stream_json(stream)["result"]["is_error"] is False


def test_credits_required_is_distinct_from_resettable_plan_quota(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        '{"type":"system","subtype":"init","model":"fable","tools":[]}\n'
        '{"rate_limit_info":{"status":"rejected","errorCode":"credits_required",'
        '"futureScalar":"kept-by-name","isUsingOverage":false},"type":"rate_limit_event"}\n'
        '{"subtype":"error","is_error":true,"type":"result"}\n',
        encoding="utf-8",
    )
    normalized = normalize_stream_json(stream)
    assert normalized["rate_limits"][0]["error_code"] == "credits_required"
    assert normalized["rate_limits"][0]["unknown_keys"] == ["futureScalar"]
    assert classify_turn(normalized) == "terminal_credits_required"
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_contracts.py -v
```

Expected: failures for non-boolean auth, arbitrary result key order, missing `is_error`, invalid collections, and missing rate-limit fields. The malformed assistant test must fail only because dispatch is incomplete; it must not raise `JSONDecodeError` from parsing assistant content.

- [ ] **Step 4: Replace regex dispatch with a bounded top-level lexical scanner**

Remove `_TOP_LEVEL_TYPE`, `_INIT_SUBTYPE`, `_RESULT_PREFIX`, and `_RESULT_TYPE`. Add these helpers to `contracts.py`:

```python
_MAX_STREAM_LINE_BYTES = 8 * 1024 * 1024
_JSON_DECODER = json.JSONDecoder()


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _skip_string(text: str, index: int) -> int:
    if index >= len(text) or text[index] != '"':
        raise ValueError("expected JSON string")
    index += 1
    escaped = False
    while index < len(text):
        char = text[index]
        index += 1
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index
    raise ValueError("unterminated JSON string")


def _skip_value(text: str, index: int) -> int:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise ValueError("missing JSON value")
    if text[index] == '"':
        return _skip_string(text, index)
    if text[index] not in "[{":
        while index < len(text) and text[index] not in ",}":
            index += 1
        return index
    stack = ["]" if text[index] == "[" else "}"]
    index += 1
    while index < len(text) and stack:
        char = text[index]
        if char == '"':
            index = _skip_string(text, index)
            continue
        if char == "[":
            stack.append("]")
        elif char == "{":
            stack.append("}")
        elif char in "]}":
            if char != stack.pop():
                raise ValueError("mismatched JSON container")
        index += 1
    if stack:
        raise ValueError("unterminated JSON container")
    return index


def peek_top_level_type(line: str) -> str | None:
    if len(line.encode("utf-8")) > _MAX_STREAM_LINE_BYTES:
        raise ValueError("stream line exceeds 8 MiB")
    index = _skip_ws(line, 0)
    if index >= len(line) or line[index] != "{":
        return None
    index += 1
    while True:
        index = _skip_ws(line, index)
        if index >= len(line) or line[index] == "}":
            return None
        key, end = _JSON_DECODER.raw_decode(line, index)
        if not isinstance(key, str):
            raise ValueError("top-level JSON key must be a string")
        index = _skip_ws(line, end)
        if index >= len(line) or line[index] != ":":
            raise ValueError("missing JSON colon")
        index = _skip_ws(line, index + 1)
        if key == "type":
            value, _ = _JSON_DECODER.raw_decode(line, index)
            if not isinstance(value, str):
                raise ValueError("top-level type must be a string")
            return value
        index = _skip_ws(line, _skip_value(line, index))
        if index < len(line) and line[index] == ",":
            index += 1
            continue
        if index < len(line) and line[index] == "}":
            return None
        raise ValueError("malformed top-level JSON object")
```

Dispatch with `peek_top_level_type(line)`. Return/continue immediately for every type outside `system`, `result`, and `rate_limit_event`; call `json.loads` only after that decision. This preserves the malformed-assistant invariant.

- [ ] **Step 5: Add strict field helpers and normalization**

Add `_require_bool`, `_optional_bool`, `_require_string_list`, and `_optional_string`. Make `normalize_auth` require a real boolean. Make system/init require `tools` as `list[str]`; validate `mcp_servers`, `plugins`, and `capabilities` as arrays before iterating. Make every result require `is_error: bool`. Normalize rate-limit `errorCode` as `error_code` and record only sorted unknown key names, never unknown values:

```python
_RATE_LIMIT_KEYS = {
    "status", "rateLimitType", "resetsAt", "utilization", "overageStatus",
    "overageDisabledReason", "isUsingOverage", "errorCode",
}


def _require_bool(payload: dict[str, Any], key: str, label: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _require_string_list(payload: dict[str, Any], key: str, label: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings")
    return value
```

`normalize_agents` treats absent optional string fields as absent, but any present `id`, `sessionId`, `name`, `cwd`, `kind`, `state`, or `status` must have its documented string/null type; it never converts arbitrary objects to presence booleans. `classify_turn` must read the already validated boolean without `bool(...)`; classify `error_code == "credits_required"` before generic rejected quota. Update existing expected rate-limit dictionaries to include `"error_code": None` and `"unknown_keys": []`.

- [ ] **Step 6: Run Task 1 tests and the non-worktree regression suite**

Run:

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_contracts.py -v
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q --ignore=tests/phase0a/test_worktree_hook.py
git diff --check
```

Expected: all focused tests pass; the non-worktree suite remains green; no assistant/thinking payload is parsed.

- [ ] **Step 7: Commit Task 1**

```powershell
git add spikes\phase0a\contracts.py tests\phase0a\test_contracts.py
git commit -m "fix: fail closed on Claude contract drift"
```

---

### Task 2: Canonical Path + Hash Project Trust Manifest

**Files:**
- Modify: `spikes/phase0a/manifest.py`
- Modify: `tests/phase0a/test_manifest.py`

**Interfaces:**
- Consumes: canonical Git repository path and project/local Claude instruction/settings tree.
- Produces: `TrustKey(repository_id, canonical_path, sha256, trust_revision)` and a complete deterministic manifest.
- `blocked_items(manifest, trusted_items=set[TrustKey])` replaces hash-only trust.

- [ ] **Step 1: Add failing path-bound trust and transitive import tests**

Replace the hash-only success test and append:

```python
from spikes.phase0a.manifest import TrustKey


def _trust(item, repository_id="repo-1", revision=1):
    return TrustKey(
        repository_id=repository_id,
        canonical_path=item["path"],
        sha256=item["sha256"],
        trust_revision=revision,
    )


def test_same_hash_at_another_path_does_not_inherit_trust(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    first = repo / ".claude" / "settings.json"
    second = repo / ".claude" / "settings.local.json"
    payload = json.dumps({"hooks": {"Stop": []}})
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")
    manifest = scan_project(repo)
    trust = {_trust(manifest["settings"][0], manifest["repository_id"])}
    blocked = blocked_items(manifest, trusted_items=trust, trust_revision=1)
    assert [item["path"] for item in blocked] == [str(second.resolve())]


def test_scan_project_follows_transitive_imports_once(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_a = tmp_path / "a.md"
    outside_b = tmp_path / "b.md"
    outside_a.write_text(f"@{outside_b}\n", encoding="utf-8")
    outside_b.write_text(f"@{outside_a}\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(f"@{outside_a}\n", encoding="utf-8")
    manifest = scan_project(repo)
    assert [item["path"] for item in manifest["external_imports"]] == [
        str(outside_a.resolve()),
        str(outside_b.resolve()),
    ]


def test_scan_project_includes_rules_skills_agents_and_commands(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = [
        repo / ".claude" / "rules" / "rule.md",
        repo / ".claude" / "skills" / "review" / "SKILL.md",
        repo / ".claude" / "agents" / "reviewer.md",
        repo / ".claude" / "commands" / "review.md",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe\n", encoding="utf-8")
    manifest = scan_project(repo)
    assert {item["path"] for item in manifest["instruction_files"]} == {
        str(path.resolve()) for path in paths
    }
```

- [ ] **Step 2: Add failing malformed settings tests**

```python
def test_scan_project_rejects_non_object_settings(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="settings must be an object"):
        scan_project(repo)


def test_scan_project_rejects_non_object_hooks(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text('{"hooks":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="hooks must be an object"):
        scan_project(repo)
```

Add `import pytest`.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_manifest.py -v
```

Expected: import failure for `TrustKey`, hash-only API mismatch, missing transitive/instruction files, and malformed settings accepted incorrectly.

- [ ] **Step 4: Implement deterministic instruction/import discovery**

Add:

```python
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class TrustKey:
    repository_id: str
    canonical_path: str
    sha256: str
    trust_revision: int


def _instruction_candidates(repo: Path) -> list[Path]:
    candidates = [repo / "CLAUDE.md", repo / ".claude" / "CLAUDE.md"]
    candidates.extend(sorted((repo / ".claude" / "rules").glob("**/*.md")))
    candidates.extend(sorted((repo / ".claude" / "skills").glob("**/SKILL.md")))
    candidates.extend(sorted((repo / ".claude" / "agents").glob("*.md")))
    candidates.extend(sorted((repo / ".claude" / "commands").glob("*.md")))
    return sorted({path.resolve() for path in candidates if path.is_file()}, key=str)
```

Derive `repository_id` from the resolved Git common-dir when available; for the synthetic no-Git unit fixtures use the canonical repository path prefixed with `path:`. Traverse imports breadth-first with a visited canonical-path set, preserve source/raw/path/outside/existence/hash, and sort every emitted list by canonical path. Missing imports are manifest entries and block execution; never hash a missing file.

- [ ] **Step 5: Replace hash-only trust**

Change `blocked_items` to:

```python
def blocked_items(
    manifest: dict[str, Any],
    *,
    trusted_items: set[TrustKey],
    trust_revision: int,
) -> list[dict[str, Any]]:
    repository_id = manifest["repository_id"]
    blocked: list[dict[str, Any]] = []
    candidates = [
        *(item for item in manifest["settings"] if item["hook_events"]),
        *(item for item in manifest["external_imports"] if item["outside_repo"]),
    ]
    for item in candidates:
        key = TrustKey(repository_id, item["path"], item["sha256"] or "", trust_revision)
        if not item.get("exists", True) or key not in trusted_items:
            blocked.append({
                "kind": item["kind"],
                "path": item["path"],
                "sha256": item.get("sha256"),
                "repository_id": repository_id,
                "trust_revision": trust_revision,
            })
    return sorted(blocked, key=lambda item: (item["kind"], item["path"]))
```

All settings/import items must expose `kind`, `path`, `exists`, and `sha256`. Never accept a trust decision containing only a digest.

- [ ] **Step 6: Run Task 2 tests and regression suite**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_manifest.py -v
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q --ignore=tests/phase0a/test_worktree_hook.py
git diff --check
```

Expected: path/repository/revision-bound trust, deterministic transitive imports, and strict malformed-settings behavior all pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add spikes\phase0a\manifest.py tests\phase0a\test_manifest.py
git commit -m "fix: bind project trust to path and revision"
```

---

### Task 3: Bounded I/O, Redaction, Locking, and Minimal Hook Events

**Files:**
- Modify: `spikes/phase0a/core.py`
- Create: `spikes/phase0a/locking.py`
- Modify: `spikes/phase0a/hook_sink.py`
- Modify: `tests/phase0a/test_core.py`
- Create: `tests/phase0a/test_locking.py`
- Modify: `tests/phase0a/test_hook_sink.py`

**Interfaces:**
- Produces: `read_fd_bounded(fd: int, limit: int) -> bytes`, `redact_data(value, key=None)`, `fingerprint(value)`, and `locked_file(path, timeout_seconds, poll_seconds)`.
- Hook files retain only event contract metadata. They never retain assistant text, tool input, transcript path, cwd, raw error bodies, emails, organization IDs, auth values, or raw native IDs.

- [ ] **Step 1: Add failing core redaction and bounded-read tests**

Append to `tests/phase0a/test_core.py` and update its imports:

```python
import os
import threading

from spikes.phase0a.core import fingerprint, read_fd_bounded, redact_data


def test_redact_text_masks_all_precedence_credentials():
    text = (
        "ANTHROPIC_API_KEY=one ANTHROPIC_AUTH_TOKEN=two "
        "CLAUDE_CODE_OAUTH_TOKEN=three Authorization: Bearer four "
        "person@example.com"
    )
    redacted = redact_text(text)
    assert all(secret not in redacted for secret in ("one", "two", "three", "four", "person@example.com"))


def test_redact_data_masks_sensitive_keys_and_pii():
    redacted = redact_data({
        "email": "person@example.com",
        "orgId": "org-private",
        "request_id": "req-private",
        "nested": {"password": "secret", "safe": "ok"},
    })
    assert redacted == {
        "email": "[REDACTED]",
        "orgId": "[REDACTED]",
        "request_id": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "safe": "ok"},
    }


def test_read_fd_bounded_reads_until_eof_across_chunks():
    read_fd, write_fd = os.pipe()
    payload = b"abc" * 100_000

    def write_all():
        try:
            view = memoryview(payload)
            while view:
                written = os.write(write_fd, view)
                view = view[written:]
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=write_all)
    writer.start()
    try:
        assert read_fd_bounded(read_fd, len(payload)) == payload
    finally:
        os.close(read_fd)
        writer.join(timeout=2)
    assert not writer.is_alive()


def test_read_fd_bounded_rejects_limit_plus_one():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"12345")
        os.close(write_fd)
        write_fd = -1
        with pytest.raises(ValueError, match="exceeds 4 bytes"):
            read_fd_bounded(read_fd, 4)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_fingerprint_is_stable_without_disclosing_input():
    value = fingerprint("native-session-id")
    assert value == fingerprint("native-session-id")
    assert "native-session-id" not in value
    assert len(value) == 64
```

Add `import pytest` if not already present.

- [ ] **Step 2: Add failing lock deadline tests**

Create `tests/phase0a/test_locking.py`:

```python
from pathlib import Path

import pytest

from spikes.phase0a import locking


def test_locked_file_retries_nonblocking_until_deadline(tmp_path: Path, monkeypatch):
    attempts = 0

    def always_busy(_fd):
        nonlocal attempts
        attempts += 1
        return False

    monkeypatch.setattr(locking, "_try_lock", always_busy)
    with pytest.raises(TimeoutError, match="lock timeout"):
        with locking.locked_file(tmp_path / "busy.lock", timeout_seconds=0.01, poll_seconds=0.001):
            pass
    assert attempts > 1


def test_locked_file_unlocks_after_body_failure(tmp_path: Path):
    target = tmp_path / "event.lock"
    with pytest.raises(RuntimeError, match="body failed"):
        with locking.locked_file(target, timeout_seconds=1):
            raise RuntimeError("body failed")
    with locking.locked_file(target, timeout_seconds=1):
        pass
```

- [ ] **Step 3: Replace content-heavy hook expectations with minimal events**

Replace the first two hook tests with:

```python
def test_sanitize_event_keeps_only_normalized_contract_fields():
    event = sanitize_event({
        "session_id": "native-session",
        "hook_event_name": "StopFailure",
        "error": "future_failure",
        "retry_after": 17,
        "execution_id": "execution-1",
        "name": "probe-one",
        "last_assistant_message": "private model output",
        "tool_input": {"password": "private"},
        "error_details": "raw provider body",
        "transcript_path": "C:/private/transcript.jsonl",
        "cwd": "C:/private/repo",
    })
    assert event == {
        "execution_id": "execution-1",
        "hook_event_name": "StopFailure",
        "name": "probe-one",
        "session_fingerprint": fingerprint("native-session"),
        "stop_failure": {
            "category": "unknown",
            "raw_category": "future_failure",
            "retry_after": 17,
        },
    }


def test_sanitize_event_never_keeps_content_heavy_fields():
    serialized = json.dumps(sanitize_event({
        "hook_event_name": "Stop",
        "last_assistant_message": "secret-output",
        "tool_input": {"authorization": "Bearer nested.secret.value"},
        "error_details": "request-private",
    }))
    assert "secret-output" not in serialized
    assert "nested.secret.value" not in serialized
    assert "request-private" not in serialized
```

Import `fingerprint` from `spikes.phase0a.core`.

- [ ] **Step 4: Run Task 3 tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_core.py tests\phase0a\test_locking.py tests\phase0a\test_hook_sink.py -v
```

Expected: missing helpers/module, blocking lock implementation, retained content fields, and missing auth-token redaction fail.

- [ ] **Step 5: Implement core helpers**

In `core.py` add the missing `ANTHROPIC_AUTH_TOKEN` pattern plus a case-insensitive email-value pattern, sensitive-key matching, recursive redaction, stable SHA-256 fingerprinting, and the bounded read loop:

```python
_SENSITIVE_KEY = re.compile(
    r"(?i)^(authorization|api[_-]?key|auth[_-]?token|oauth[_-]?token|"
    r"password|secret|cookie|email|org(?:anization)?_?id|orgId|request_?id)$"
)


def redact_data(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.fullmatch(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(item_key): redact_data(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    return value


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_fd_bounded(fd: int, limit: int) -> bytes:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(64 * 1024, limit + 1 - min(total, limit)))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError(f"input exceeds {limit} bytes")
        chunks.append(chunk)
```

Add `import hashlib`. Keep `run_argv` redacting stdout/stderr. Do not add a generic raw-output persistence helper.

- [ ] **Step 6: Implement bounded nonblocking locks**

Create `spikes/phase0a/locking.py` with `locked_file`. `_try_lock` uses `msvcrt.LK_NBLCK` on Windows and `fcntl.LOCK_EX | LOCK_NB` on POSIX, returning `False` only for the platform's busy-lock error. Retry with `time.monotonic()` until the supplied deadline. `_unlock` runs only after successful acquisition; file descriptors always close in `finally`.

The module must not expose a forever-blocking mode. `timeout_seconds <= 0` and `poll_seconds <= 0` raise `ValueError`.

- [ ] **Step 7: Implement event-specific sanitization and bounded stdin**

Delete `_ALLOWED`, `_sanitize_value`, and `_locked` from `hook_sink.py`. Import `fingerprint`, `read_fd_bounded`, `normalize_stop_failure`, and `locked_file`. Implement:

```python
_SCALAR_FIELDS = ("hook_event_name", "source", "model", "name", "agent_type", "execution_id", "worktree_path")


def sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {
        key: redact_data(payload[key], key)
        for key in _SCALAR_FIELDS
        if isinstance(payload.get(key), (str, int, float, bool))
    }
    if isinstance(payload.get("session_id"), str):
        clean["session_fingerprint"] = fingerprint(payload["session_id"])
    if isinstance(payload.get("agent_id"), str):
        clean["agent_fingerprint"] = fingerprint(payload["agent_id"])
    if payload.get("hook_event_name") == "StopFailure":
        clean["stop_failure"] = normalize_stop_failure(payload)
    return clean
```

Use `locked_file(..., timeout_seconds=10)` in `append_event`. In `main`, replace the single `os.read` with `read_fd_bounded(0, 1_048_576)`.

- [ ] **Step 8: Run Task 3 tests and regression suite**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_core.py tests\phase0a\test_locking.py tests\phase0a\test_hook_sink.py -v
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q --ignore=tests/phase0a/test_worktree_hook.py
git diff --check
```

Expected: all tests pass; no persisted event test expects assistant/tool/error body content.

- [ ] **Step 9: Commit Task 3**

```powershell
git add spikes\phase0a\core.py spikes\phase0a\locking.py spikes\phase0a\hook_sink.py tests\phase0a\test_core.py tests\phase0a\test_locking.py tests\phase0a\test_hook_sink.py
git commit -m "fix: bound and redact Phase 0a hook evidence"
```

---

### Task 4: Recoverable WorktreeCreate Transaction

**Files:**
- Modify: `spikes/phase0a/worktree_hook.py`
- Modify: `spikes/phase0a/background_probe.py`
- Modify: `tests/phase0a/test_worktree_hook.py`
- Modify: `tests/phase0a/test_background_probe.py`

**Interfaces:**
- `create_worktree(..., creation_lock: Path, handoff: Callable[[Path], None]) -> Path` owns creation through stdout-equivalent hand-off.
- Lease acknowledgement statuses: `leased` or `recovery_required`.
- `prepare_background` generates a UUID execution ID and one repository creation-lock path, then wires both through fixed argv.

- [ ] **Step 1: Add failing rollback/recovery/handoff tests without real Git**

Append to `test_worktree_hook.py`:

```python
from types import SimpleNamespace


def test_rollback_failure_retains_recovery_record(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    common = tmp_path / "common"
    common.mkdir()
    lease = tmp_path / "lease.json"
    target = tmp_path / "worktrees" / "probe-one"

    def fake_git(_argv, name):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path: common)
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("event failed")))
    monkeypatch.setattr(worktree_hook, "run_argv", lambda *_args, **_kwargs: SimpleNamespace(exit_code=1, stderr="busy"))

    with pytest.raises(RuntimeError, match="RECOVERY_REQUIRED"):
        worktree_hook.create_worktree(
            repo, tmp_path / "worktrees", tmp_path / "events.jsonl", lease,
            tmp_path / "create.lock", "execution-1", _payload(repo), lambda _path: None,
        )
    recovery = json.loads(lease.read_text(encoding="utf-8"))
    assert recovery["status"] == "recovery_required"
    assert recovery["worktree_path"] == str(target)


def test_handoff_failure_rolls_back_before_ack_removal(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    lease = tmp_path / "lease.json"
    target = tmp_path / "worktrees" / "probe-one"
    common = tmp_path / "common"
    common.mkdir()

    def fake_git(_argv, name):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
        return ""

    def cleanup(*_args, **_kwargs):
        target.rmdir()
        return SimpleNamespace(exit_code=0, stderr="")

    def fail_handoff(_path):
        raise BrokenPipeError("stdout closed")

    monkeypatch.setattr(worktree_hook, "_git", fake_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path: common)
    monkeypatch.setattr(worktree_hook, "_target_is_registered", lambda *_args: False)
    monkeypatch.setattr(worktree_hook, "append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worktree_hook, "run_argv", cleanup)

    with pytest.raises(BrokenPipeError, match="stdout closed"):
        worktree_hook.create_worktree(
            repo, tmp_path / "worktrees", tmp_path / "events.jsonl", lease,
            tmp_path / "create.lock", "execution-1", _payload(repo), fail_handoff,
        )
    assert not (tmp_path / "worktrees" / "probe-one").exists()
    assert not lease.exists()


def test_uncertain_add_reconciles_partial_target(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "worktrees" / "probe-one"
    common = tmp_path / "common"
    common.mkdir()

    def uncertain_git(_argv, name):
        if name == "git-worktree-add":
            target.mkdir(parents=True)
            raise TimeoutError("add outcome unknown")
        return ""

    monkeypatch.setattr(worktree_hook, "_git", uncertain_git)
    monkeypatch.setattr(worktree_hook, "_common_dir", lambda _path: common)
    monkeypatch.setattr(worktree_hook, "_target_is_registered", lambda *_args: True)
    monkeypatch.setattr(worktree_hook, "run_argv", lambda *_args, **_kwargs: SimpleNamespace(exit_code=0, stderr=""))
    with pytest.raises(TimeoutError, match="outcome unknown"):
        worktree_hook.create_worktree(
            repo, tmp_path / "worktrees", tmp_path / "events.jsonl", tmp_path / "lease.json",
            tmp_path / "create.lock", "execution-1", _payload(repo), lambda _path: None,
        )
```

The third test must also assert the cleanup command was called; capture its argv in a list rather than accepting the abbreviated lambda in the final test.

- [ ] **Step 2: Update existing worktree tests for the new transaction interface**

Pass `tmp_path / "create.lock"` and `lambda _path: None` to every existing `create_worktree` call. Keep the real-Git happy path, unsafe-name path, and event-failure rollback assertions unchanged.

- [ ] **Step 3: Add failing bounded-main and background identity tests**

In `test_background_probe.py`, assert:

```python
assert "--creation-lock" in handler["args"]
assert Path(layout["creation_lock"]).name == "repository-create.lock"
assert len(layout["execution_id"]) == 32
assert layout["execution_id"] != Path(layout["root"]).parent.name
```

Add a subprocess-level `worktree_hook.main` test that monkeypatches `read_fd_bounded` to return the payload, monkeypatches `create_worktree`, captures the handoff callable, invokes it, and asserts exactly one newline-terminated path is written. Add a limit test proving payloads over 1 MiB fail before JSON parsing.

- [ ] **Step 4: Run only mocked/non-worktree Task 4 tests and verify RED**

Mark the existing three real-Git tests with `@pytest.mark.real_git_worktree`. Register the marker in `pyproject.toml`. Run:

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_worktree_hook.py tests\phase0a\test_background_probe.py -m "not real_git_worktree" -v
```

Expected: new interface/recovery/UUID/creation-lock tests fail. No real worktree is created by this command.

- [ ] **Step 5: Implement one guarded transaction**

Refactor `create_worktree` so the repository creation lock surrounds: initial checks, `git worktree add`, uncertain-outcome reconciliation, common-dir verification, exclusive leased acknowledgement, event append, and `handoff(target)`.

Use this rollback order in one helper:

```python
def _rollback_or_record(
    repo: Path,
    target: Path,
    lease_ack: Path,
    acknowledgement: dict[str, Any],
    original: BaseException,
) -> None:
    cleanup = run_argv(
        "git-worktree-rollback",
        ["git", "-C", str(repo), "worktree", "remove", str(target)],
        timeout_seconds=30,
    )
    if cleanup.exit_code == 0 and not target.exists() and not _target_is_registered(repo, target):
        lease_ack.unlink(missing_ok=True)
        return
    recovery = dict(acknowledgement)
    recovery.update({
        "status": "recovery_required",
        "failure": "rollback_failed",
        "cleanup_exit_code": cleanup.exit_code,
    })
    write_json_atomic(lease_ack, recovery)
    raise RuntimeError(f"RECOVERY_REQUIRED: rollback failed for {target}") from original
```

Never place raw stderr in the recovery record. `_target_is_registered` parses only `git worktree list --porcelain` from an argv call and compares canonical paths. If add times out/raises/nonzero but either the target exists or is registered, rollback owns it because the target was verified absent before acquiring the creation lock. Append a normalized rollback event after a successful cleanup; leave the original exception as the raised cause.

`main` uses `read_fd_bounded(0, 1_048_576)` and passes a `_stdout_handoff` callable into `create_worktree`; no stdout write exists after the transaction returns.

- [ ] **Step 6: Generate unique execution and creation-lock layout**

In `background_probe.py`, use `uuid.uuid4().hex` inside `prepare_background`, independent of directory names. Create `creation_lock = target / "repository-create.lock"`, include it in layout, add `--creation-lock` to the handler argv, and update function signatures/tests. Keep all task text as one argv element.

- [ ] **Step 7: Run mocked Task 4 tests and non-worktree regression suite**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_worktree_hook.py tests\phase0a\test_background_probe.py -m "not real_git_worktree" -v
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q -m "not real_git_worktree"
git diff --check
```

Expected: all safe tests pass. Do not run the three marked tests yet.

- [ ] **Step 8: Pause for explicit real-Git worktree-test approval**

Report the exact command, that it creates only pytest-temporary Git repositories/worktrees, and that no Claude process/session/config is involved. Continue only after approval:

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_worktree_hook.py -m real_git_worktree -v
```

Expected after approval: three real-Git tests pass and pytest temporary cleanup leaves the Subagent MCP repository with exactly one worktree. Verify with `git worktree list --porcelain` and `git status --short --branch`.

- [ ] **Step 9: Commit Task 4**

```powershell
git add pyproject.toml spikes\phase0a\worktree_hook.py spikes\phase0a\background_probe.py tests\phase0a\test_worktree_hook.py tests\phase0a\test_background_probe.py
git commit -m "fix: retain recoverable WorktreeCreate state"
```

---

### Task 5: Executable Identity, Auth Precedence, and Observer Evidence

**Files:**
- Modify: `spikes/phase0a/host_probe.py`
- Modify: `tests/phase0a/test_host_probe.py`

**Interfaces:**
- Produces: `executable_identity(path)`, `credential_precedence_ok(env)`, normalized `build_snapshot`, and `compare_observers` with explicit evidence state.
- Snapshot command payloads contain normalized version/auth/agent contracts only; no raw stdout/stderr/argv, username, APPDATA value, email, organization ID, native session ID, pid value, or cwd value.

- [ ] **Step 1: Add failing executable and precedence tests**

Append to `test_host_probe.py`:

```python
import hashlib
from types import SimpleNamespace

import pytest

from spikes.phase0a.host_probe import (
    build_snapshot,
    credential_precedence_ok,
    executable_identity,
)


def test_executable_identity_records_canonical_hash_and_file_identity(tmp_path: Path):
    binary = tmp_path / "claude"
    binary.write_bytes(b"native-binary")
    identity = executable_identity(binary, observed_version="2.1.224 (Claude Code)")
    assert identity["canonical_path"] == str(binary.resolve())
    assert identity["sha256"] == hashlib.sha256(b"native-binary").hexdigest()
    assert identity["size"] == len(b"native-binary")
    assert identity["device"] is not None
    assert identity["inode"] is not None
    assert identity["observed_version"] == "2.1.224 (Claude Code)"


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"],
)
def test_credential_precedence_rejects_every_override(name):
    assert credential_precedence_ok({name: "present"}) is False


def test_credential_precedence_accepts_absent_or_empty_values():
    assert credential_precedence_ok({}) is True
    assert credential_precedence_ok({"ANTHROPIC_API_KEY": ""}) is True
```

- [ ] **Step 2: Add failing normalized snapshot tests**

```python
def test_build_snapshot_discards_raw_auth_and_roster_values(tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_bytes(b"binary")
    responses = {
        ("--version",): "2.1.224 (Claude Code)\n",
        ("auth", "status"): '{"loggedIn":true,"authMethod":"claude.ai",'
        '"apiProvider":"firstParty","email":"private@example.com","orgId":"private-org"}',
        ("agents", "--json", "--all"): '[{"id":"short","sessionId":"private-session",'
        '"cwd":"C:/private","kind":"background","state":"done","pid":123}]',
    }

    def fake_runner(_name, argv, **_kwargs):
        return SimpleNamespace(exit_code=0, timed_out=False, stdout=responses[tuple(argv[1:])], stderr="")

    snapshot = build_snapshot("test", cli, env={}, runner=fake_runner)
    serialized = json.dumps(snapshot)
    assert "private@example.com" not in serialized
    assert "private-org" not in serialized
    assert "private-session" not in serialized
    assert "C:/private" not in serialized
    assert snapshot["auth"] == {
        "logged_in": True,
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
    }
    assert snapshot["agents"][0]["session_id_present"] is True


def test_equal_absent_observations_are_not_positive_visibility_evidence():
    result = compare_observers(
        {"paths": {"desktop_cache_root": {"exists": False}}},
        {"paths": {"desktop_cache_root": {"exists": False}}},
    )
    assert result == {
        "status": "not_observed",
        "mismatches": {},
        "observed_present": [],
    }
```

Add `import json`.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_host_probe.py -v
```

Expected: missing identity/precedence helpers, raw snapshot persistence, and vacuous observer equality fail.

- [ ] **Step 4: Implement normalized host evidence**

Remove `getpass`, raw APPDATA values, the hard-coded `desktop_cache_2_1_229` record, and raw `commands`. Implement executable identity from `Path.resolve(strict=True)`, `stat().st_dev/st_ino/st_size`, streamed SHA-256, and execute-observed version. `credential_precedence_ok` checks the three exact variables.

Update the existing visibility-mismatch test to expect `status="mismatch"`, the same `mismatches` mapping, and the names positively observed by either side. `build_snapshot(observer, claude_path, *, env=None, runner=run_argv)` records:

```text
schema_version
observer
standalone_cli identity
wrapper/cache-root existence metadata (no versioned cache child)
wrapper accepted=false and ownership/lifecycle rejection reasons
credential_env_present booleans and precedence_ok
normalized auth contract
normalized agents contract
probe exit/timed_out booleans
```

Parse command JSON immediately and discard raw text. A nonzero/malformed auth or agents result is a structured probe failure, not an empty successful contract. `compare_observers` returns `mismatch`, `matched_present`, or `not_observed`; only `matched_present` is positive shared-visibility evidence, and it is still not wrapper acceptance.

- [ ] **Step 5: Run Task 5 tests and regression suite**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_host_probe.py -v
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q -m "not real_git_worktree"
git diff --check
```

Expected: no normalized snapshot contains raw identity/session/cwd data, precedence negative paths pass, and absent cache observations remain inconclusive.

- [ ] **Step 6: Commit Task 5**

```powershell
git add spikes\phase0a\host_probe.py tests\phase0a\test_host_probe.py
git commit -m "fix: normalize Claude host identity evidence"
```

---

### Task 6: Versioned Fixture Envelopes and Shareable Evidence

**Files:**
- Create: `spikes/phase0a/fixtures.py`
- Create: `tests/phase0a/test_fixtures.py`
- Modify: `spikes/phase0a/contracts.py`
- Modify: `tests/fixtures/phase0a/current/auth-status.json`
- Modify: `tests/fixtures/phase0a/current/agents-normalized.json`
- Modify: `tests/fixtures/phase0a/current/context-attestation.json`
- Modify: `tests/fixtures/phase0a/current/stop-failure-contract.json`
- Create: `tests/fixtures/phase0a/current/model-outcomes.json`
- Create: `tests/fixtures/phase0a/current/strict-mcp-control.json`
- Create: `tests/fixtures/phase0a/current/evidence-index.json`

**Interfaces:**
- Every committed fixture uses `fixture_schema_version=1`, `kind`, `observed_cli_version`, `source.kind`, `source.sha256`, `coverage.observed`, `coverage.missing`, and `payload`.
- Source paths, run IDs, account/native session identifiers, and raw output are forbidden in committed fixtures.

- [ ] **Step 1: Add failing fixture-schema and replay tests**

Create `tests/phase0a/test_fixtures.py`:

```python
import json
from pathlib import Path

import pytest

from spikes.phase0a.fixtures import fixture_envelope, validate_fixture


FIXTURE_ROOT = Path("tests/fixtures/phase0a/current")


@pytest.mark.parametrize("path", sorted(FIXTURE_ROOT.glob("*.json")), ids=lambda path: path.name)
def test_committed_fixture_replays_against_schema(path: Path):
    validate_fixture(json.loads(path.read_text(encoding="utf-8")))


def test_fixture_envelope_never_persists_source_path_or_run_id():
    envelope = fixture_envelope(
        kind="auth_status",
        observed_cli_version="2.1.224 (Claude Code)",
        source_kind="auth_status_json",
        source_sha256="a" * 64,
        payload={"auth": {"logged_in": True}},
        observed=["auth.logged_in"],
        missing=[],
    )
    serialized = json.dumps(envelope)
    assert "source_path" not in serialized
    assert "run_id" not in serialized


def test_context_fixture_explicitly_lists_unattested_declared_native_fields():
    fixture = json.loads((FIXTURE_ROOT / "context-attestation.json").read_text(encoding="utf-8"))
    assert "setting_sources" in fixture["coverage"]["missing"]
    assert "auto_compaction_window" in fixture["coverage"]["missing"]
    assert "nested_agent_cap" in fixture["coverage"]["missing"]
```

- [ ] **Step 2: Run fixture tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_fixtures.py -v
```

Expected: missing module/schema and all legacy fixture shapes fail.

- [ ] **Step 3: Implement fixture envelopes and validation**

Create `fixtures.py` with:

```python
FIXTURE_SCHEMA_VERSION = 1
_REQUIRED_TOP_LEVEL = {
    "fixture_schema_version", "kind", "observed_cli_version", "source", "coverage", "payload"
}


def fixture_envelope(
    *, kind: str, observed_cli_version: str, source_kind: str,
    source_sha256: str, payload: dict[str, Any], observed: list[str], missing: list[str],
) -> dict[str, Any]:
    envelope = {
        "fixture_schema_version": FIXTURE_SCHEMA_VERSION,
        "kind": kind,
        "observed_cli_version": observed_cli_version,
        "source": {"kind": source_kind, "sha256": source_sha256},
        "coverage": {"observed": sorted(set(observed)), "missing": sorted(set(missing))},
        "payload": redact_data(payload),
    }
    validate_fixture(envelope)
    return envelope
```

`validate_fixture` requires exactly the top-level keys, a 64-character lowercase hex source digest, nonempty kind/version, string-list coverage with no overlap, and an object payload. Its recursive scan rejects credential/PII values and exact raw keys such as `email`, `orgId`, `request_id`, `session_id`, `id`, `pid`, `cwd`, `transcript_path`, `stdout`, `stderr`, `raw_output`, `source_path`, and `run_id`; safe normalized keys such as `session_id_present`, `pid_present`, and `cwd_present` remain allowed. It rejects absolute home paths and accepts unknown non-sensitive payload fields so fixture consumers remain forward compatible.

- [ ] **Step 4: Add deterministic fixture writers**

Refactor `contracts.main` to call `fixture_envelope` for auth, agents, and StopFailure fixtures. Add a dedicated context writer function that wraps `normalize_stream_json` and declares only these observed fields:

```text
model, tools, mcp_servers, plugin_count, forbidden_plugin_presence, capabilities, permission_mode,
cwd_present, rate_limit advisory, final result, usage/cost metadata
```

Its missing list is the section 8 declared-native remainder:

```text
setting_sources, claude_rule_sources, skills, agents, inherited_hooks,
bridge_hooks, auto_memory_mode, auto_compaction_window, cleanup_period,
nested_agent_cap, nested_agent_depth, additional_directories,
system_prompt_preset, system_prompt_append, content_hashes
```

Do not commit the user's enabled plugin names: convert them to `plugin_count` plus booleans for forbidden Codex/AgentBridge/Subagent MCP presence. The context source kind is `managed_proxy`; coverage also records `background_environment_equivalence` as missing.

- [ ] **Step 5: Regenerate sanitized fixtures from retained evidence without a model call**

Use only existing ignored source files after checking their SHA-256 and permissions. Run the deterministic writers through `.venv\Scripts\python.exe`; do not print raw content. Generate `model-outcomes.json` by applying the corrected parser/classifier to retained model stream files and storing only requested/observed model, classification, final `is_error`, result subtype, rate status/error code, usage-credit-disabled booleans, and cost metadata. Generate `strict-mcp-control.json` from the retained strict/control marker results, containing only declared server count, marker-spawned booleans, exit success, observed CLI version, and source hashes. Do not store prompt/result text, plugin names, session IDs, request IDs, timestamps, paths, or account fields.

If an expected retained source is missing or cannot be mapped unambiguously, do not synthesize it: omit the claimed PASS evidence and record the report gate as `BLOCKED`/`UNKNOWN` in Task 7.

- [ ] **Step 6: Build and validate the evidence index**

`evidence-index.json` is itself a fixture envelope whose payload maps each committed fixture filename to its SHA-256 and kind. It excludes itself from the map. Add a test that recomputes every listed hash and fails on missing/unlisted JSON files.

- [ ] **Step 7: Run fixture, contract, and secret scans**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_fixtures.py tests\phase0a\test_contracts.py -v
rg -n -i 'sk-ant-|bearer\s+|authorization:|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}' tests\fixtures\phase0a\current
git diff --check
```

Expected: tests pass; ripgrep returns no credential/email values. Field names and `[REDACTED]` matches must be manually inspected, not waved through.

- [ ] **Step 8: Commit Task 6**

```powershell
git add spikes\phase0a\fixtures.py spikes\phase0a\contracts.py tests\phase0a\test_fixtures.py tests\fixtures\phase0a\current
git commit -m "test: version and sanitize Phase 0a evidence"
```

---

### Task 7: Exact Gates and Non-Destructive Report Generation

**Files:**
- Modify: `spikes/phase0a/report.py`
- Modify: `tests/phase0a/test_report.py`
- Modify: `docs/phase0a/phase0a-report.md`
- Raw/local modify: `.phase0a/phase0a-gates.json`

**Interfaces:**
- `validate_gates(gates)` requires exactly `_GATE_NAMES`, valid statuses, and nonempty evidence.
- `render_gate_block(gates, generated_at)` is deterministic for explicit inputs.
- `update_report(path, gate_block)` replaces exactly one marker-bounded generated section and preserves all reviewed narrative byte-for-byte outside it.

- [ ] **Step 1: Add failing exact-gate/evidence tests**

Replace the two-row renderer test and append:

```python
from spikes.phase0a.report import render_gate_block, update_report, validate_gates


def test_validate_gates_rejects_missing_and_unknown_names():
    gates = default_gates()
    gates.pop("standalone_cli")
    with pytest.raises(ValueError, match="missing gates: standalone_cli"):
        validate_gates(gates)
    gates = default_gates()
    gates["invented"] = {"status": "BLOCKED", "evidence": "not supported"}
    with pytest.raises(ValueError, match="unknown gates: invented"):
        validate_gates(gates)


def test_validate_gates_rejects_pass_without_evidence():
    gates = default_gates()
    gates["standalone_cli"] = {"status": "PASS", "evidence": "  "}
    with pytest.raises(ValueError, match="standalone_cli.*evidence"):
        validate_gates(gates)


def test_update_report_preserves_reviewed_narrative(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Report\n\nReviewed before\n\n"
        "<!-- BEGIN GENERATED GATES -->\nold\n<!-- END GENERATED GATES -->\n\n"
        "Reviewed after\n",
        encoding="utf-8",
    )
    update_report(report, "new table\n")
    assert report.read_text(encoding="utf-8") == (
        "# Report\n\nReviewed before\n\n"
        "<!-- BEGIN GENERATED GATES -->\nnew table\n<!-- END GENERATED GATES -->\n\n"
        "Reviewed after\n"
    )


@pytest.mark.parametrize("body", ["no markers", "<!-- BEGIN GENERATED GATES -->\nonly begin"])
def test_update_report_refuses_ambiguous_markers(tmp_path, body):
    report = tmp_path / "report.md"
    report.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="generated gate markers"):
        update_report(report, "table\n")
```

- [ ] **Step 2: Split partial/full context gate names**

Add `context_init_subset` to `_GATE_NAMES`. Keep `context_attestation` as the full declared-native gate, initially `BLOCKED`. Update tests to assert both names exist. Do not rename old evidence silently.

- [ ] **Step 3: Run report tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_report.py -v
```

Expected: subset render is no longer accepted, blank PASS evidence passes incorrectly, and report overwrite behavior fails.

- [ ] **Step 4: Implement exact validation and marker-bounded update**

Use constants:

```python
_BEGIN = "<!-- BEGIN GENERATED GATES -->"
_END = "<!-- END GENERATED GATES -->"
_STATUSES = {"PASS", "FAIL", "UNKNOWN", "BLOCKED"}
```

`validate_gates` compares exact key sets and validates every row object/status/evidence. `render_gate_block` accepts an explicit RFC3339 `generated_at`; it does not call the clock. `update_report` requires exactly one begin/end marker in order and writes through this local helper rather than `Path.write_text` in place:

```python
def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
```

Add `import os` and `import tempfile`.

Change the CLI to require `--generated-at`; `--init-gates` remains exclusive and refuses overwrite. Re-running the same inputs must produce identical bytes.

- [ ] **Step 5: Migrate the report and adjudicate gates conservatively**

Insert markers around the current gate table. Keep checkpoint summary, design coverage, residual state, and decision narrative outside the markers. Update false claims and local paths. At the end of this non-live plan, the expected minimum adjudication is:

| Gate | Required status/evidence rule |
|---|---|
| `standalone_cli` | PASS only from versioned sanitized executable-identity fixture |
| `subscription_auth` | PASS only from sanitized auth fixture |
| `credential_precedence` | PASS only if live absence plus three negative unit paths are both cited |
| `observer_visibility` | UNKNOWN unless both observers positively saw the same identity; never wrapper acceptance |
| `agents_json_schema` | BLOCKED if no replay-tested background lifecycle fixture exists |
| `context_init_subset` | PASS from managed-proxy fixture with explicit coverage |
| `context_attestation` | BLOCKED because full section 8 fields are missing |
| `plugin_disable_effective` | BLOCKED until a positive control runs |
| `project_manifest` | PASS from path+hash/transitive synthetic tests and current project scan |
| `worktree_create_hook` | BLOCKED until the changed handler passes a fresh approved live canary |
| `background_concurrency` | BLOCKED unless a sanitized, reproducible sampling artifact exists |
| existing unproved lifecycle/handle/remove/StopFailure/race gates | remain BLOCKED/UNKNOWN |

Strict MCP may remain PASS only if its committed sanitized differential marker evidence is replayable; otherwise downgrade it. Never cite `.phase0a/` as the sole evidence for a public/shareable PASS.

- [ ] **Step 6: Update the design-coverage table and decision**

Section 19.1 requirements are PASS only when every dependent gate passes. Explicitly state that this plan does not accept Phase 0a and that the next artifact is a separately approved live-gates plan. Remove any statement that Phase 0b may begin after static corrections alone.

- [ ] **Step 7: Run deterministic regeneration twice**

Use one reviewed RFC3339 timestamp stored in the gate input/evidence index. Run the documented command twice and compare SHA-256 of `phase0a-report.md`; hashes must match. Confirm reviewed narrative remains unchanged outside markers.

- [ ] **Step 8: Run report/fixture tests and public-evidence scans**

```powershell
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests\phase0a\test_report.py tests\phase0a\test_fixtures.py -v
rg -n -i 'C:/Users/|C:\\Users\\|sk-ant-|bearer\s+|authorization:|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}' docs\phase0a tests\fixtures\phase0a
git diff --check
```

Expected: tests pass; no personal path, credential, or email value exists in committed evidence/report.

- [ ] **Step 9: Commit Task 7**

```powershell
git add spikes\phase0a\report.py tests\phase0a\test_report.py docs\phase0a\phase0a-report.md
git commit -m "docs: make Phase 0a gates reproducible"
```

Do not add `.phase0a/phase0a-gates.json`; it is local raw state.

---

### Task 8: Static Completion Audit and Independent Review Gate

**Files:**
- No planned code files; findings may create a follow-up task before completion.
- Update only if verified review findings require it: files owned by Tasks 1–7.

**Interfaces:**
- Produces: clean repository, complete safe test evidence, exact remaining live blockers, and two independent review reports.

- [ ] **Step 1: Run the complete safe suite**

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q -m "not real_git_worktree"
git diff --check
git status --short --branch
```

Expected: every non-worktree test passes and the worktree contains only intended committed state.

- [ ] **Step 2: Reconcile the separately approved real-Git test evidence**

If Task 4 approval was granted, cite its passing command and verify one Subagent MCP worktree. If not granted, record the three tests as not run and keep every dependent gate BLOCKED. Never use older 42/42 output as fresh proof.

- [ ] **Step 3: Audit ignored/local residual state without deleting**

Read-only checks:

```powershell
$worktreeCount = @(git worktree list --porcelain | Where-Object { $_ -like 'worktree *' }).Count
[pscustomobject]@{ git_worktree_count = $worktreeCount } | Format-List
git status --short --branch
$rows = @(& "$env:USERPROFILE\.local\bin\claude.exe" agents --json --all | ConvertFrom-Json)
[pscustomobject]@{
  background_rows = @($rows | Where-Object kind -eq 'background').Count
  interactive_rows = @($rows | Where-Object kind -eq 'interactive').Count
  states = @($rows | Group-Object state | ForEach-Object { "$($_.Name):$($_.Count)" }) -join ','
} | Format-List
$standaloneProcesses = @(Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object {
  $_.Path -eq "$env:USERPROFILE\.local\bin\claude.exe"
})
[pscustomobject]@{ standalone_claude_process_count = $standaloneProcesses.Count } | Format-List
```

The raw roster flows only through the in-memory pipeline and is never printed or persisted. Do not print IDs/cwds/pids and do not remove rows, worktrees, transcripts, or `.phase0a/` evidence. Any unexpected active background row or extra worktree stops review and requires user direction.

- [ ] **Step 4: Request an independent native Codex review**

Dispatch a fresh read-only reviewer with a scoped task packet: approved spec/plan paths, base `af9b255`, current HEAD, Phase 0a-only scope, exact safe test output, and no prior review conclusions. Require file:line findings, severity, verification limitations, and a clear report-acceptance verdict. Do not preload raw evidence or full chat history.

- [ ] **Step 5: Request approval for a different-harness review**

Explain that a Claude Code review consumes Claude quota but does not enable usage credits. Continue only with explicit approval and a non-paused plan circuit. Use the registered future MCP only after it exists; for this Phase 0a pre-MCP gate, use the execute-validated standalone CLI with a persistent resumable session, strict empty MCP, read-only semantic tools, no recursive Codex/AgentBridge/Subagent MCP tools, and the same scoped packet as the native reviewer.

If quota returns `allowed_warning` then terminal rejection, stop immediately, retain the session ID, report `QUOTA_PAUSED`, and do not retry/fallback/enable overage.

- [ ] **Step 6: Adjudicate reviews with source evidence**

Use `receiving-code-review`. Verify every finding against current source/tests; do not vote by model or implement unverified suggestions. Blocking valid findings reopen the owning task and its tests. Record false positives with exact technical reasoning. Repeat review only after fixes and fresh verification.

- [ ] **Step 7: Final checkpoint**

The plan is complete only when:

- all approved safe/real-Git tests pass;
- repository and worktree audit are clean;
- committed evidence/report contain no PII/raw output;
- both independent reviews have no unresolved blocking static finding;
- the report still says Phase 0b is blocked pending the named live-gates plan.

Do not mark the overall Subagent MCP goal complete. Update the project plan so the next exact action is to write and obtain approval for `docs/superpowers/plans/2026-08-20-subagent-mcp-phase-0a-live-gates.md`.
