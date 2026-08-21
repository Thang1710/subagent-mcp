# Subagent MCP Phase 0a Contract Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Keep execution inline; do not delegate the live Claude canary to another worker.

**Goal:** Correct Phase 0a's Claude stream classification and WorktreeCreate semantics, then rerun the real background canary without enabling usage credits.

**Architecture:** Parse only top-level structured envelopes and decide success from the final `result`, not from the presence of a rate-limit advisory. Replace the generic WorktreeCreate event sink with a dedicated handler that creates a bounded Git worktree, writes a simulated lease acknowledgement and structured event, and only then returns the path to Claude Code. Keep all production adapter/SQLite work in Phase 0b.

**Tech Stack:** Python 3.10, pytest 8, uv, Git worktrees, Claude Code 2.1.224 structured JSON/hooks, Windows PowerShell.

---

## Current checkpoint

- Authority: design commit `0f1922a`.
- The stopped pre-correction background row is `20160e8c`; do not resume or delete it without a later cleanup approval.
- The base disposable repository remained clean and had one main worktree after that row was stopped.
- Successful raw calls already exist for `claude-opus-5`, `claude-opus-4-8`, and `claude-sonnet-5`. Their plan status was `allowed`/`allowed_warning`, final `result.is_error` was false, and usage credits remained disabled.
- The working tree contains uncommitted Task 1 parser changes and a generated context-attestation fixture. Preserve and verify them; do not discard them.

### Task 1: Normalize rate advisories and final results without parsing assistant payloads

**Files:**
- Modify: `spikes/phase0a/contracts.py`
- Modify: `tests/phase0a/test_contracts.py`
- Update: `tests/fixtures/phase0a/current/context-attestation.json`

- [ ] **Step 1: Add the failing advisory-classification tests**

Add `classify_turn` to the imports and add these tests:

```python
def test_allowed_plan_with_disabled_overage_is_success(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            '{"type":"system","subtype":"init","model":"claude-sonnet-5","tools":[]}',
            '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",'
            '"rateLimitType":"five_hour","overageStatus":"rejected",'
            '"overageDisabledReason":"out_of_credits","isUsingOverage":false}}',
            '{"is_error":false,"stop_reason":"end_turn","total_cost_usd":0.1,'
            '"type":"result","subtype":"success"}',
        ]) + "\n",
        encoding="utf-8",
    )
    normalized = normalize_stream_json(stream)
    assert normalized["rate_limits"] == [{
        "status": "allowed",
        "rate_limit_type": "five_hour",
        "resets_at": None,
        "utilization": None,
        "overage_status": "rejected",
        "overage_disabled_reason": "out_of_credits",
        "is_using_overage": False,
    }]
    assert classify_turn(normalized) == "success"


def test_rejected_plan_with_error_result_is_terminal_quota(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            '{"type":"system","subtype":"init","model":"claude-fable-5","tools":[]}',
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
            '"overageDisabledReason":"out_of_credits","isUsingOverage":false}}',
            '{"is_error":true,"stop_reason":"stop_sequence","total_cost_usd":0,'
            '"type":"result","subtype":"success"}',
        ]) + "\n",
        encoding="utf-8",
    )
    assert classify_turn(normalize_stream_json(stream)) == "terminal_quota"
```

Also update `test_normalize_stream_json_accepts_cli_result_key_order` so its exact result expectation includes `"stop_reason": "end_turn"` between `is_error` and `total_cost_usd`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
uv run pytest tests/phase0a/test_contracts.py -v
```

Expected: the existing result-order test passes, while imports/assertions for `classify_turn` and `rate_limits` fail.

- [ ] **Step 3: Replace the stream normalizer with the order-tolerant envelope implementation**

Keep the existing auth/agent/StopFailure functions. Use these constants and functions for stream handling:

```python
_TOP_LEVEL_TYPE = re.compile(r'^\s*\{\s*"type"\s*:\s*"([^"]+)"')
_INIT_SUBTYPE = re.compile(r'"subtype"\s*:\s*"init"')
_RESULT_PREFIX = re.compile(r'^\s*\{\s*"is_error"\s*:')
_RESULT_TYPE = re.compile(r'"type"\s*:\s*"result"')


def _normalize_rate_limit(item: dict[str, Any]) -> dict[str, Any]:
    info = item.get("rate_limit_info")
    if not isinstance(info, dict):
        info = {}
    return {
        "status": info.get("status"),
        "rate_limit_type": info.get("rateLimitType"),
        "resets_at": info.get("resetsAt"),
        "utilization": info.get("utilization"),
        "overage_status": info.get("overageStatus"),
        "overage_disabled_reason": info.get("overageDisabledReason"),
        "is_using_overage": bool(info.get("isUsingOverage")),
    }


def classify_turn(normalized: dict[str, Any]) -> str:
    result = normalized.get("result")
    if not isinstance(result, dict):
        return "incomplete"
    if not result.get("is_error"):
        return "success"
    rate_limits = normalized.get("rate_limits") or []
    if any(item.get("status") == "rejected" for item in rate_limits):
        return "terminal_quota"
    return "terminal_error"


def normalize_stream_json(path: str | Path) -> dict[str, Any]:
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    rate_limits: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        prefix = line[:512]
        if _RESULT_PREFIX.match(prefix):
            if _RESULT_TYPE.search(line) is None:
                continue
        else:
            type_match = _TOP_LEVEL_TYPE.match(prefix)
            if type_match is None:
                continue
            item_type = type_match.group(1)
            if item_type == "system" and _INIT_SUBTYPE.search(prefix) is None:
                continue
            if item_type not in {"system", "result", "rate_limit_event"}:
                continue
        item = json.loads(line)
        if item.get("type") == "system" and item.get("subtype") == "init":
            init = {
                "model": item.get("model"),
                "tools": sorted(item.get("tools") or []),
                "mcp_servers": sorted(
                    [
                        {"name": server.get("name"), "status": server.get("status")}
                        for server in (item.get("mcp_servers") or [])
                        if isinstance(server, dict)
                    ],
                    key=lambda server: str(server["name"]),
                ),
                "plugins": sorted(
                    [
                        plugin.get("name")
                        for plugin in (item.get("plugins") or [])
                        if isinstance(plugin, dict) and plugin.get("name")
                    ]
                ),
                "capabilities": sorted(item.get("capabilities") or []),
                "permission_mode": item.get("permissionMode"),
                "cwd_present": bool(item.get("cwd")),
            }
        elif item.get("type") == "rate_limit_event":
            rate_limits.append(_normalize_rate_limit(item))
        elif item.get("type") == "result":
            result = {
                "subtype": item.get("subtype"),
                "is_error": bool(item.get("is_error")),
                "stop_reason": item.get("stop_reason"),
                "total_cost_usd": item.get("total_cost_usd"),
                "usage": item.get("usage"),
            }
    if init is None:
        raise ValueError("stream has no system/init event")
    return {"init": init, "rate_limits": rate_limits, "result": result}
```

This code may inspect only `system/init`, `rate_limit_event`, and `result`. The malformed-assistant fixture must continue to prove that assistant/thinking payloads are skipped before `json.loads`.

- [ ] **Step 4: Regenerate the successful context fixture from the existing Sonnet 5 raw call**

Run:

```powershell
$root = (Get-Content -Raw '.phase0a\current-background-root.txt').Trim()
$env:HB_PROXY_RAW = Join-Path $root 'claude-sonnet-5.raw.jsonl'
$env:HB_PROXY_OUT = (Join-Path (Resolve-Path 'tests\fixtures\phase0a\current').Path 'context-attestation.json')
uv run python -c "import os; from spikes.phase0a.contracts import classify_turn,normalize_stream_json; from spikes.phase0a.core import write_json_atomic; n=normalize_stream_json(os.environ['HB_PROXY_RAW']); assert classify_turn(n)=='success'; write_json_atomic(os.environ['HB_PROXY_OUT'],n)"
Remove-Item Env:HB_PROXY_RAW,Env:HB_PROXY_OUT
```

Expected: model is `claude-sonnet-5`, final result is not an error, `is_using_overage` is false, and overage rejection does not change the success classification.

- [ ] **Step 5: Verify and commit the parser correction**

Run:

```powershell
uv run pytest tests/phase0a/test_contracts.py -v
git diff --check
git add spikes/phase0a/contracts.py tests/phase0a/test_contracts.py tests/fixtures/phase0a/current/context-attestation.json
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "fix: classify Claude plan quota from final result"
```

Expected: all contract tests pass and the fixture contains no raw assistant text, email, token, cwd, or transcript path.

### Task 2: Implement the dedicated WorktreeCreate handler

**Files:**
- Create: `spikes/phase0a/worktree_hook.py`
- Create: `tests/phase0a/test_worktree_hook.py`
- Modify: `spikes/phase0a/hook_sink.py`
- Modify: `tests/phase0a/test_hook_sink.py`

- [ ] **Step 1: Correct the generic event allowlist and default event set**

Add `name` and `execution_id` to `_ALLOWED`. Remove `WorktreeCreate` from `_DEFAULT_EVENTS`; leave these generic events:

```python
_DEFAULT_EVENTS = (
    "SessionStart",
    "WorktreeRemove",
    "Stop",
    "StopFailure",
)
```

Update the hook-sink test to assert that the generic settings do not contain `WorktreeCreate` and still contain `WorktreeRemove`.

- [ ] **Step 2: Write the failing real-Git handler tests**

Create `tests/phase0a/test_worktree_hook.py`:

```python
import json
from pathlib import Path

import pytest

from spikes.phase0a import worktree_hook
from spikes.phase0a.core import run_argv


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("probe\n", encoding="utf-8")
    commands = (
        ["git", "-C", str(repo), "init", "-b", "main"],
        ["git", "-C", str(repo), "add", "README.md"],
        ["git", "-C", str(repo), "-c", "user.name=Phase0a", "-c",
         "user.email=phase0a@example.invalid", "commit", "-m", "init"],
    )
    for index, argv in enumerate(commands):
        result = run_argv(f"git-{index}", argv)
        assert result.exit_code == 0, result.stderr
    return repo


def _payload(repo: Path, name: str = "probe-one") -> dict[str, str]:
    return {
        "session_id": "session",
        "cwd": str(repo),
        "hook_event_name": "WorktreeCreate",
        "name": name,
    }


def test_create_worktree_writes_lease_and_path_event(tmp_path: Path):
    repo = _repo(tmp_path)
    event_log = tmp_path / "events.jsonl"
    lease_ack = tmp_path / "lease.json"
    target = worktree_hook.create_worktree(
        repo, tmp_path / "worktrees", event_log, lease_ack, "execution-1", _payload(repo)
    )
    try:
        assert target.is_dir()
        assert json.loads(lease_ack.read_text(encoding="utf-8"))["worktree_path"] == str(target)
        event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[-1])
        assert event["name"] == "probe-one"
        assert event["worktree_path"] == str(target)
        assert event["execution_id"] == "execution-1"
    finally:
        run_argv("cleanup", ["git", "-C", str(repo), "worktree", "remove", str(target)])


def test_create_worktree_rejects_unsafe_name_without_side_effect(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="worktree name"):
        worktree_hook.create_worktree(
            repo, tmp_path / "worktrees", tmp_path / "events.jsonl",
            tmp_path / "lease.json", "execution-1", _payload(repo, "../escape")
        )
    assert not (tmp_path / "lease.json").exists()


def test_event_failure_rolls_back_new_worktree_and_ack(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    lease_ack = tmp_path / "lease.json"

    def fail_event(*_args, **_kwargs):
        raise OSError("event unavailable")

    monkeypatch.setattr(worktree_hook, "append_event", fail_event)
    with pytest.raises(OSError, match="event unavailable"):
        worktree_hook.create_worktree(
            repo, tmp_path / "worktrees", tmp_path / "events.jsonl",
            lease_ack, "execution-1", _payload(repo)
        )
    assert not (tmp_path / "worktrees" / "probe-one").exists()
    assert not lease_ack.exists()
    listing = run_argv("list", ["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    assert listing.stdout.count("worktree ") == 1
```

- [ ] **Step 3: Run the new tests and verify import failure**

Run:

```powershell
uv run pytest tests/phase0a/test_worktree_hook.py -v
```

Expected: collection fails because `worktree_hook.py` does not exist.

- [ ] **Step 4: Implement the bounded handler**

Create `spikes/phase0a/worktree_hook.py`:

```python
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from .core import run_argv
    from .hook_sink import append_event
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from spikes.phase0a.core import run_argv
    from spikes.phase0a.hook_sink import append_event


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _git(argv: list[str], name: str) -> str:
    result = run_argv(name, argv, timeout_seconds=30)
    if result.exit_code != 0:
        raise RuntimeError(f"{name} failed: {result.stderr}")
    return result.stdout.strip()


def _common_dir(repo: Path) -> Path:
    raw = _git(["git", "-C", str(repo), "rev-parse", "--git-common-dir"], "git-common-dir")
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve(strict=True)


def _write_ack_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def create_worktree(
    repo: Path,
    worktree_root: Path,
    event_log: Path,
    lease_ack: Path,
    execution_id: str,
    payload: dict[str, Any],
) -> Path:
    expected_repo = repo.resolve(strict=True)
    if payload.get("hook_event_name") != "WorktreeCreate":
        raise ValueError("unexpected hook event")
    name = payload.get("name")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError("invalid worktree name")
    cwd = Path(str(payload.get("cwd", ""))).resolve(strict=True)
    if cwd != expected_repo:
        raise ValueError("hook cwd does not match expected repository")

    worktree_root.mkdir(parents=True, exist_ok=True)
    root = worktree_root.resolve(strict=True)
    target = root / name
    if target.exists() or lease_ack.exists():
        raise FileExistsError("worktree target or lease acknowledgement already exists")

    common_before = _common_dir(expected_repo)
    created = False
    ack_written = False
    try:
        _git(
            ["git", "-C", str(expected_repo), "worktree", "add", "--detach", str(target), "HEAD"],
            "git-worktree-add",
        )
        created = True
        if _common_dir(target) != common_before:
            raise RuntimeError("created worktree common-dir mismatch")
        acknowledgement = {
            "execution_id": execution_id,
            "repository_common_dir": str(common_before),
            "worktree_path": str(target),
            "status": "leased",
        }
        _write_ack_exclusive(lease_ack, acknowledgement)
        ack_written = True
        event = dict(payload)
        event.update({"execution_id": execution_id, "worktree_path": str(target)})
        append_event(event_log, event)
        return target
    except BaseException as error:
        if ack_written:
            lease_ack.unlink(missing_ok=True)
        if created:
            cleanup = run_argv(
                "git-worktree-rollback",
                ["git", "-C", str(expected_repo), "worktree", "remove", str(target)],
                timeout_seconds=30,
            )
            if cleanup.exit_code != 0:
                raise RuntimeError(
                    f"RECOVERY_REQUIRED: rollback failed for {target}: {cleanup.stderr}"
                ) from error
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--worktree-root", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--lease-ack", type=Path, required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()
    raw = os.read(0, 1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("hook payload exceeds 1 MiB")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    path = create_worktree(
        args.repo, args.worktree_root, args.event_log,
        args.lease_ack, args.execution_id, payload,
    )
    sys.stdout.write(str(path) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests and commit the handler**

Run:

```powershell
uv run pytest tests/phase0a/test_hook_sink.py tests/phase0a/test_worktree_hook.py -v
git diff --check
git add spikes/phase0a/hook_sink.py spikes/phase0a/worktree_hook.py tests/phase0a/test_hook_sink.py tests/phase0a/test_worktree_hook.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: add bounded WorktreeCreate handler"
```

Expected: generic sink no longer replaces Claude's default creation by accident, and all handler tests pass with rollback evidence.

### Task 3: Wire the handler into background-only settings

**Files:**
- Modify: `spikes/phase0a/background_probe.py`
- Modify: `tests/phase0a/test_background_probe.py`

- [ ] **Step 1: Write the failing settings tests**

Replace the generic settings test with one that calls `build_background_hook_settings` and asserts:

```python
handler = settings["hooks"]["WorktreeCreate"][0]["hooks"][0]
assert handler["command"] == str(Path("python.exe").resolve())
assert handler["args"][0] == str(Path("worktree_hook.py").resolve())
assert "--repo" in handler["args"]
assert "--worktree-root" in handler["args"]
assert "--lease-ack" in handler["args"]
assert "--execution-id" in handler["args"]
assert set(settings["hooks"]) == {
    "SessionStart", "WorktreeCreate", "WorktreeRemove", "Stop", "StopFailure"
}
```

Update the prepare test to pass both dummy scripts and assert that layout contains existing `worktree_root`, absent `lease_ack`, and a non-empty `execution_id`.

- [ ] **Step 2: Run the background tests and verify failure**

Run:

```powershell
uv run pytest tests/phase0a/test_background_probe.py -v
```

Expected: import/signature failures because the background-only builder is not implemented.

- [ ] **Step 3: Add the background-only settings builder**

Rename the generic import and add:

```python
from .hook_sink import build_hook_settings as build_event_hook_settings


def build_background_hook_settings(
    python_exe: Path,
    hook_sink: Path,
    worktree_hook: Path,
    event_log: Path,
    repo: Path,
    worktree_root: Path,
    lease_ack: Path,
    execution_id: str,
) -> dict[str, Any]:
    settings = build_event_hook_settings(
        python_exe,
        hook_sink,
        event_log,
        events=("SessionStart", "WorktreeRemove", "Stop", "StopFailure"),
    )
    settings["hooks"]["WorktreeCreate"] = [{
        "hooks": [{
            "type": "command",
            "command": str(python_exe.resolve()),
            "args": [
                str(worktree_hook.resolve()),
                "--repo", str(repo.resolve()),
                "--worktree-root", str(worktree_root.resolve()),
                "--event-log", str(event_log.resolve()),
                "--lease-ack", str(lease_ack.resolve()),
                "--execution-id", execution_id,
            ],
            "timeout": 30,
        }]
    }]
    return settings
```

Before calling this function, `prepare_background` must create `worktree_root`. Change its signature to accept `worktree_hook`, create `execution_id = target.parent.name`, and add `worktree_root`, `lease_ack`, and `execution_id` to `layout.json`. Its `main` passes `Path(__file__).with_name("worktree_hook.py")`.

Replace `prepare_background` and its `main` call with:

```python
def prepare_background(
    root: Path,
    python_exe: Path,
    hook_sink: Path,
    worktree_hook: Path,
) -> dict[str, Any]:
    target = root.resolve()
    repo = target / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# Phase 0a background probe\n", encoding="utf-8")
    for name, argv in (
        ("git-init", ["git", "-C", str(repo), "init", "-b", "main"]),
        ("git-add", ["git", "-C", str(repo), "add", "README.md"]),
        (
            "git-commit",
            [
                "git", "-C", str(repo),
                "-c", "user.name=Subagent MCP Phase0a",
                "-c", "user.email=phase0a@example.invalid",
                "commit", "-m", "chore: initialize disposable background probe",
            ],
        ),
    ):
        result = run_argv(name, argv, timeout_seconds=30)
        if result.exit_code != 0:
            raise RuntimeError(f"{name} failed: {result.stderr}")

    events = target / "events.jsonl"
    settings = target / "settings.json"
    declared = target / "declared-empty.json"
    worktree_root = target / "worktrees"
    worktree_root.mkdir()
    lease_ack = target / "worktree-lease.json"
    execution_id = target.parent.name
    write_json_atomic(settings, build_background_hook_settings(
        python_exe, hook_sink, worktree_hook, events, repo,
        worktree_root, lease_ack, execution_id,
    ))
    write_json_atomic(declared, {"mcpServers": {}})
    layout = {
        "root": str(target),
        "repo": str(repo),
        "events": str(events),
        "settings": str(settings),
        "declared_config": str(declared),
        "worktree_root": str(worktree_root),
        "lease_ack": str(lease_ack),
        "execution_id": execution_id,
        "name": "subagent-harness-mcp-phase0a-" + execution_id,
    }
    write_json_atomic(target / "layout.json", layout)
    return layout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    prepare_background(
        args.root,
        Path(sys.executable),
        Path(__file__).with_name("hook_sink.py"),
        Path(__file__).with_name("worktree_hook.py"),
    )
    return 0
```

- [ ] **Step 4: Verify and commit wiring**

Run:

```powershell
uv run pytest tests/phase0a/test_background_probe.py -v
uv run pytest -v
git diff --check
git add spikes/phase0a/background_probe.py tests/phase0a/test_background_probe.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: wire WorktreeCreate handoff into background probe"
```

Expected: all tests pass and no generic WorktreeCreate handler remains.

### Task 4: Rerun the real background lifecycle and correct the report

**Files:**
- Raw only: `.phase0a/runs/<new-run-id>/background/`
- Modify: `docs/phase0a/phase0a-report.md`
- Modify if live evidence changes it: `tests/fixtures/phase0a/current/context-attestation.json`

- [ ] **Step 1: Prepare a fresh disposable repository**

Do not reuse the pre-correction stopped row or repository:

```powershell
$cli = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
$runId = [guid]::NewGuid().ToString('N')
$backgroundRoot = Join-Path '.phase0a\runs' (Join-Path $runId 'background')
uv run python -m spikes.phase0a.background_probe --root $backgroundRoot
$backgroundRoot = (Resolve-Path $backgroundRoot).Path
[IO.File]::WriteAllText((Join-Path '.phase0a' 'current-background-root.txt'), $backgroundRoot)
$layout = Get-Content -Raw (Join-Path $backgroundRoot 'layout.json') | ConvertFrom-Json
if(@(git -C $layout.repo remote).Count -ne 0){ throw 'disposable repo unexpectedly has a remote' }
```

- [ ] **Step 2: Launch one real Sonnet 5 task with bounded edit permission**

The existing user approval covers this rerun. Do not enable usage credits or overage. Run:

```powershell
$prompt = 'In this disposable worktree only, use the file editing tool to create phase0a-proof.txt containing exactly ready and a newline. Do not run shell commands, delete the file, commit, add a remote, push, merge, or modify anything else. Then report the worktree path and stop.'
$args = @(
  '--bg','--name',$layout.name,
  '--permission-mode','acceptEdits',
  '--settings',$layout.settings,
  '--strict-mcp-config','--mcp-config',$layout.declared_config,
  '--model','claude-sonnet-5','--effort','low',
  $prompt
)
Push-Location $layout.repo
try {
  & $cli @args 1> (Join-Path $backgroundRoot 'launch.txt') 2> (Join-Path $backgroundRoot 'launch.stderr.txt')
  $launchExit = $LASTEXITCODE
} finally { Pop-Location }
if($launchExit -ne 0){ throw 'background launch failed' }

$deadline = [DateTime]::UtcNow.AddMinutes(3)
$entry = $null
do {
  $agents = & $cli agents --json --all | ConvertFrom-Json
  $entry = $agents | Where-Object name -eq $layout.name | Select-Object -First 1
  if($null -eq $entry){ Start-Sleep -Milliseconds 500 }
} while($null -eq $entry -and [DateTime]::UtcNow -lt $deadline)
if($null -eq $entry){ throw 'background row did not appear in structured roster' }
$shortId = [string]$entry.id
$sessionId = [string]$entry.sessionId
[IO.File]::WriteAllText((Join-Path $backgroundRoot 'background-short-id.txt'), $shortId)

do {
  $entry = (& $cli agents --json --all | ConvertFrom-Json) |
    Where-Object id -eq $shortId | Select-Object -First 1
  if($entry.state -eq 'working'){ Start-Sleep -Milliseconds 500 }
} while($entry.state -eq 'working' -and [DateTime]::UtcNow -lt $deadline)
if($entry.state -ne 'done'){
  throw "background row ended in state=$($entry.state), waitingFor=$($entry.waitingFor)"
}
```

The unique row is resolved only through `agents --json --all`. Do not parse launch text, TUI, logs, or transcripts.

- [ ] **Step 3: Require the handoff evidence before accepting task success**

Read only the structured event file and lease acknowledgement:

```powershell
$events = @(Get-Content -LiteralPath $layout.events | ForEach-Object { $_ | ConvertFrom-Json })
$create = $events | Where-Object hook_event_name -eq 'WorktreeCreate' | Select-Object -Last 1
$lease = Get-Content -Raw -LiteralPath $layout.lease_ack | ConvertFrom-Json
if([string]::IsNullOrWhiteSpace($create.worktree_path)){ throw 'missing WorktreeCreate path' }
if($create.worktree_path -ne $lease.worktree_path){ throw 'event/lease path mismatch' }
$worktreePath = (Resolve-Path -LiteralPath $create.worktree_path).Path
$baseCommit = git -C $layout.repo rev-parse HEAD
$dirty = @(git -C $worktreePath status --porcelain)
$extraCommits = [int](git -C $worktreePath rev-list --count "$baseCommit..HEAD")
if($dirty.Count -ne 1 -or $dirty[0] -ne '?? phase0a-proof.txt'){
  throw "unexpected background diff: $($dirty -join '; ')"
}
$proof = Join-Path $worktreePath 'phase0a-proof.txt'
$proofText = Get-Content -Raw -LiteralPath $proof
if($proofText -notin @("ready`n", "ready`r`n")){
  throw 'proof file content mismatch'
}
if($extraCommits -ne 0){ throw 'background task created an unexpected commit' }
Remove-Item -LiteralPath $proof
if(@(git -C $worktreePath status --porcelain).Count -ne 0){
  throw 'worktree is not clean after removing the exact approved proof file'
}
$stopDeadline = [DateTime]::UtcNow.AddSeconds(10)
do {
  $events = @(Get-Content -LiteralPath $layout.events | ForEach-Object { $_ | ConvertFrom-Json })
  $stop = $events | Where-Object {
    $_.hook_event_name -eq 'Stop' -and $_.session_id -eq $sessionId
  } | Select-Object -Last 1
  if($null -eq $stop){ Start-Sleep -Milliseconds 250 }
} while($null -eq $stop -and [DateTime]::UtcNow -lt $stopDeadline)
if($null -eq $stop){ throw 'background Stop hook was not observed' }
```

The exact proof-file removal is already inside the approved canary scope; report that it was removed. PASS also requires a background `Stop` event after `WorktreeCreate`, a `done` roster state, and no hook/plugin error on stderr.

- [ ] **Step 4: Probe stable stop/respawn without deleting**

Use only the structured short ID:

```powershell
& $cli stop $shortId | Out-Null
$stopped1 = (& $cli agents --json --all | ConvertFrom-Json) |
  Where-Object id -eq $shortId | Select-Object -First 1
Start-Sleep -Milliseconds 750
$stopped2 = (& $cli agents --json --all | ConvertFrom-Json) |
  Where-Object id -eq $shortId | Select-Object -First 1
if($stopped1.state -ne 'stopped' -or $stopped2.state -ne 'stopped'){
  throw 'stop state was not stable'
}
& $cli respawn $shortId | Out-Null
$respawned = (& $cli agents --json --all | ConvertFrom-Json) |
  Where-Object id -eq $shortId | Select-Object -First 1
if($null -eq $respawned){ throw 'respawned row is missing' }
& $cli stop $shortId | Out-Null
$final1 = (& $cli agents --json --all | ConvertFrom-Json) |
  Where-Object id -eq $shortId | Select-Object -First 1
Start-Sleep -Milliseconds 750
$final2 = (& $cli agents --json --all | ConvertFrom-Json) |
  Where-Object id -eq $shortId | Select-Object -First 1
if($final1.state -ne 'stopped' -or $final2.state -ne 'stopped'){
  throw 'final stop state was not stable'
}
```

Do not call `claude rm` in this step.

- [ ] **Step 5: Ask separately before concurrency and removal**

Concurrency still requires approval for two additional short turns. Worktree removal still requires an audit plus exact approval naming the short ID and canonical worktree path. If approved, use official `claude rm <short-id>` and require `WorktreeRemove` with the same path plus `Test-Path=False`. Never delete the native transcript directly.

- [ ] **Step 6: Correct the Phase 0a report from reviewed evidence**

Remove the false statement that every Opus/Sonnet call was rate-limited. Record Fable as the actual usage-credit failure and Opus/Sonnet as successful plan-backed turns with disabled overage. Update each background/worktree/daemon/concurrency row only from fresh evidence; leave untested rows BLOCKED/UNKNOWN.

- [ ] **Step 7: Final verification and commit**

Run:

```powershell
uv run pytest -v
git diff --check
rg -n -i 'sk-ant-|bearer\s+|authorization:' docs/phase0a tests/fixtures/phase0a
git status --short --branch
```

Inspect any credential-field-name-only match manually. Commit only deterministic code, normalized fixtures, and the corrected report:

```powershell
git add docs/phase0a tests/fixtures/phase0a
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "docs: correct Phase 0a live lifecycle evidence"
```

Stop again at the Phase 0a report review gate. Do not install Node/SDK or register the MCP.
