# Subagent MCP Phase 0a Host Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Produce reproducible, redacted evidence for the installed Windows/Claude host contracts that Subagent MCP depends on, without writing a production adapter or registering MCP.

**Architecture:** Phase 0a is an isolated probe harness under spikes/phase0a. It uses only Python stdlib plus pytest for tests, invokes CLIs with argv arrays, writes raw evidence only under ignored .phase0a/, and commits only normalized fixtures and a report. Host installation, login, live model calls, and worktree cleanup remain separate approval gates.

**Tech Stack:** Python 3.10, uv, pytest 8, Windows PowerShell 5.1, Git, standalone Claude Code CLI.

---

## Scope boundary

This plan implements Phase 0a only. It must not:

- create the production MCP server;
- install the Python Claude Agent SDK;
- register a Codex plugin or MCP server;
- edit Codex, Claude, AgentBridge, or global Git configuration;
- install Node;
- write or modify a native Claude/Codex transcript;
- proceed to Phase 0b from the same execution context.

When this plan finishes, stop for user review. Phase 0b receives its own plan after the report is approved and the context is compacted or restarted.

## Execution mode

Execute this plan inline in one fresh context with the executing-plans skill. The evidence chain is sequential and carries approval state, run IDs, layouts, and raw-artifact ownership forward. Do not dispatch host-facing tasks to subagents. This execution-mode choice takes effect only after the user approves this revised plan and the context is compacted/restarted.

## File map

- Create: .gitignore — ignores virtual environments and raw host evidence.
- Create: pyproject.toml — non-package uv/pytest configuration for spike tests.
- Create: uv.lock — uv-generated locked development dependency graph.
- Create: spikes/__init__.py — marks spike modules.
- Create: spikes/phase0a/__init__.py — marks Phase 0a modules.
- Create: tests/phase0a/__init__.py — marks the Phase 0a test package.
- Create: tests/phase0a/test_scaffold.py — proves the local pytest harness is runnable.
- Create: spikes/phase0a/core.py — safe argv execution, redaction, atomic JSON writes.
- Create: spikes/phase0a/host_probe.py — observer/path/CLI snapshot collector.
- Create: spikes/phase0a/hook_sink.py — bounded, redacted event sink plus shell-free exec-form hook settings.
- Create: spikes/phase0a/manifest.py — project executable-content manifest.
- Create: spikes/phase0a/hold_file.ps1 — Windows FileShare.Read contention probe.
- Create: spikes/phase0a/contracts.py — normalized auth/agents/context-stream contract parsers.
- Create: spikes/phase0a/marker_mcp.py — disposable marker MCP server for strict-mode tests.
- Create: spikes/phase0a/strict_probe.py — prepares the disposable strict-MCP repository/configuration.
- Create: spikes/phase0a/background_probe.py — builds background hook settings and argv.
- Create: spikes/phase0a/report.py — produces the committed Phase 0a report.
- Create: tests/phase0a/test_core.py
- Create: tests/phase0a/test_host_probe.py
- Create: tests/phase0a/test_hook_sink.py
- Create: tests/phase0a/test_manifest.py
- Create: tests/phase0a/test_contracts.py
- Create: tests/phase0a/test_strict_probe.py
- Create: tests/phase0a/test_background_probe.py
- Create: tests/phase0a/test_report.py
- Create after live probes: tests/fixtures/phase0a/current/*.json — normalized, secret-free fixtures whose payload records the observed CLI version.
- Create after the managed proxy: tests/fixtures/phase0a/current/context-attestation.json — normalized actual context/capability/cost evidence.
- Create after live probes: docs/phase0a/phase0a-report.md — pass/fail/unknown evidence and Phase 0b decisions.

## Spec coverage map

| Design requirement | Plan task |
|---|---|
| Clean-context entry and approval boundaries | Tasks 1–2 |
| Safe argv execution/redaction/atomic evidence | Task 4 |
| Cross-harness Desktop-cache observation | Task 5 |
| SessionStart/Stop/StopFailure event capture | Tasks 6 and 10 |
| Hook exec-form health and per-run plugin disable | Tasks 6 and 10 |
| Project hook/import manifest and bounded handle lifetime | Task 7 |
| Auth/agents/lifecycle JSON fixtures and unknown-field tolerance | Task 8 |
| Strict declared MCP pre-spawn exclusion | Task 9 |
| Documented-but-help-omitted init-only capability | Tasks 6 and 9 |
| WorktreeCreate/WorktreeRemove delivery and provisional-lease evidence | Task 10 |
| Daemon stop/respawn race and two-session concurrency | Task 10 |
| Managed proxy context/cost attestation | Task 10 |
| Agent View overhead recorded as measured or UNKNOWN | Task 10 |
| Pass/fail/unknown gate report and hard stop before Phase 0b | Tasks 11–12 |

Fresh Codex High/XHigh/Max/Ultra delegation remains a release acceptance test and is intentionally absent from Phase 0a.

## Authoritative live references

Re-open these immediately before an installation or live probe because CLI behavior is time-sensitive:

- Claude installation: https://code.claude.com/docs/en/installation
- Claude authentication: https://code.claude.com/docs/en/authentication
- Claude CLI reference: https://code.claude.com/docs/en/cli-usage
- Claude headless mode: https://code.claude.com/docs/en/headless
- Claude Agent View/background sessions: https://code.claude.com/docs/en/agent-view
- Claude hooks: https://code.claude.com/docs/en/hooks

## Execution-wide safety rules

- Use the exact standalone CLI path, not the claude wrapper on PATH.
- Never print credential values. Record only booleans and the auth method returned by auth status.
- Use a fresh .phase0a/runs/$runId directory for every live probe, where $runId is a newly generated GUID without separators.
- Do not delete a live-probe directory until its manifest, worktree state, and background session are reviewed.
- Do not call claude rm except for Task 10's separately approved WorktreeRemove probe after a clean/no-extra-commit audit. Stop/respawn are allowed only in the disposable probe repository.
- Every model-spending command requires a commentary update and explicit user approval immediately before it runs.
- Git commits use command-scoped identity; never write global Git config.

### Task 1: Enter a clean execution context and confirm authority

**Files:**
- Read: AGENTS.md
- Read: CLAUDE.md
- Read: docs/superpowers/specs/2026-08-17-subagent-mcp-design.md
- Read: docs/superpowers/plans/2026-08-17-subagent-mcp-phase-0a.md

- [ ] **Step 1: Confirm the execution context was compacted or restarted**

The active context must contain only the committed spec, this plan, applicable AGENTS.md files, current repository state, and the current checkpoint. If the brainstorming transcript is still the working context, stop and compact/restart before continuing.

- [ ] **Step 2: Verify repository authority and cleanliness**

Run:

~~~powershell
git status --short --branch
git log -3 --oneline --decorate
~~~

Expected: branch main, no changes, and the latest commits include the approved spec and this plan.

- [ ] **Step 3: Re-read the implementation gates**

Run:

~~~powershell
Get-Content -Raw AGENTS.md
Get-Content -Raw docs\superpowers\specs\2026-08-17-subagent-mcp-design.md
~~~

Expected: transcripts are immutable, host changes require approval, Phase 0a precedes adapters, and no completion claim relies only on static/config evidence.

- [ ] **Step 4: Capture read-only prerequisite state**

Run:

~~~powershell
$nativeCli = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
[pscustomobject]@{
  NativeClaudeExists = Test-Path -LiteralPath $nativeCli -PathType Leaf
  NodeExists = [bool](Get-Command node -ErrorAction SilentlyContinue)
  Python = (python --version 2>&1)
  Uv = (uv --version 2>&1)
} | Format-List
~~~

Expected before approval: NativeClaudeExists may be False; NodeExists may be False. Do not install anything in this step.

### Task 2: Approval-gated standalone Claude installation and login

**Files:**
- No repository files
- Host write after approval: %USERPROFILE%\.local\bin\claude.exe
- Host write after user login: %USERPROFILE%\.claude credential store

- [ ] **Step 1: Ask for exact installation approval**

Ask permission to install the official standalone Claude Code stable channel. Do not treat approval of this plan as installation approval.

- [ ] **Step 2: Install the official stable native CLI**

Only after approval, run from Windows PowerShell:

~~~powershell
& ([scriptblock]::Create((irm https://claude.ai/install.ps1))) stable
~~~

Expected: the installer reports success and creates %USERPROFILE%\.local\bin\claude.exe. Do not remove or edit %USERPROFILE%\bin\claude.cmd.

- [ ] **Step 3: Execute-validate the standalone path**

Run:

~~~powershell
$nativeCli = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
Get-Item -LiteralPath $nativeCli | Select-Object FullName,Length,LastWriteTime
& $nativeCli --version
~~~

Expected: file exists and --version exits 0. Record path/version, not assumptions about PATH ordering.

- [ ] **Step 4: Ask the user to complete subscription login**

Run interactively only after the user is ready:

~~~powershell
& $nativeCli auth login
~~~

Expected: browser/terminal OAuth completes under the user's Claude subscription.

- [ ] **Step 5: Verify auth without exposing credentials**

Run:

~~~powershell
& $nativeCli auth status
[pscustomobject]@{
  AnthropicApiKeySet = -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_API_KEY)
  AnthropicAuthTokenSet = -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_AUTH_TOKEN)
  ClaudeOauthTokenSet = -not [string]::IsNullOrWhiteSpace($env:CLAUDE_CODE_OAUTH_TOKEN)
} | ConvertTo-Json
~~~

Expected: auth status exits 0 with loggedIn true and subscription/Claude.ai auth. Higher-precedence API-key variables must be false or explicitly resolved before continuing. Never print their values.

### Task 3: Scaffold the isolated Phase 0a probe harness

**Files:**
- Create: .gitignore
- Create: pyproject.toml
- Create: spikes/__init__.py
- Create: spikes/phase0a/__init__.py
- Create: tests/phase0a/__init__.py

- [ ] **Step 1: Create the project metadata**

Create pyproject.toml:

~~~toml
[project]
name = "subagent-harness-mcp"
version = "0.0.0"
requires-python = ">=3.10"
dependencies = []

[dependency-groups]
dev = ["pytest>=8,<9"]

[tool.uv]
package = false

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
~~~

- [ ] **Step 2: Create the ignore rules**

Create .gitignore:

~~~gitignore
.venv/
.pytest_cache/
__pycache__/
*.py[cod]
.phase0a/
artifacts/phase0a/raw/
~~~

- [ ] **Step 3: Create empty package markers**

Create empty package files:

~~~text
spikes/__init__.py
spikes/phase0a/__init__.py
tests/phase0a/__init__.py
~~~

Create tests/phase0a/test_scaffold.py:

~~~python
def test_phase0a_scaffold_is_runnable():
    assert True
~~~

- [ ] **Step 4: Sync only repository-local development dependencies**

Run:

~~~powershell
uv sync --group dev
uv run pytest --collect-only
~~~

Expected: uv creates the local environment and pytest collects one scaffold test without an import error.

- [ ] **Step 5: Commit the scaffold**

Run:

~~~powershell
git add .gitignore pyproject.toml uv.lock spikes tests
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: scaffold Phase 0a probes"
~~~

Expected: one clean commit containing only repository-local test scaffolding.

### Task 4: Implement the safe probe result and argv runner

**Files:**
- Create: tests/phase0a/test_core.py
- Create: spikes/phase0a/core.py

- [ ] **Step 1: Write the failing tests**

Create tests/phase0a/test_core.py:

~~~python
import json
import sys
from pathlib import Path

from spikes.phase0a.core import redact_text, run_argv, write_json_atomic


def test_run_argv_captures_success_without_shell():
    result = run_argv(
        "echo",
        [sys.executable, "-c", "print('ok')"],
        timeout_seconds=5,
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.timed_out is False
    assert result.argv[0] == sys.executable


def test_run_argv_reports_timeout():
    result = run_argv(
        "timeout",
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout_seconds=0.05,
    )
    assert result.timed_out is True
    assert result.exit_code is None


def test_redact_text_masks_supported_credentials():
    value = "ANTHROPIC_API_KEY=sk-ant-secret Bearer abc.def.ghi"
    redacted = redact_text(value)
    assert "sk-ant-secret" not in redacted
    assert "abc.def.ghi" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_write_json_atomic_replaces_complete_document(tmp_path: Path):
    target = tmp_path / "result.json"
    write_json_atomic(target, {"value": 1})
    write_json_atomic(target, {"value": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}
    assert list(tmp_path.glob("*.tmp")) == []
~~~

- [ ] **Step 2: Run the tests and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_core.py -v
~~~

Expected: collection fails because spikes.phase0a.core does not exist.

- [ ] **Step 3: Implement the minimal safe runner**

Create spikes/phase0a/core.py:

~~~python
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


_SECRET_PATTERNS = (
    re.compile(r"(?i)(ANTHROPIC_API_KEY\s*=\s*)\S+"),
    re.compile(r"(?i)(CLAUDE_CODE_OAUTH_TOKEN\s*=\s*)\S+"),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]+\b"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda match: match.group(1) + "[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


@dataclass(frozen=True)
class ProbeResult:
    name: str
    argv: tuple[str, ...]
    cwd: str | None
    started_at: str
    duration_ms: int
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_argv(
    name: str,
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout_seconds: float = 30,
    env: Mapping[str, str] | None = None,
) -> ProbeResult:
    if not argv or any(not isinstance(part, str) for part in argv):
        raise ValueError("argv must be a non-empty sequence of strings")
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timed_out = True
    return ProbeResult(
        name=name,
        argv=tuple(argv),
        cwd=str(Path(cwd).resolve()) if cwd is not None else None,
        started_at=started_at,
        duration_ms=round((time.perf_counter() - started) * 1000),
        exit_code=exit_code,
        stdout=redact_text(stdout),
        stderr=redact_text(stderr),
        timed_out=timed_out,
    )


def write_json_atomic(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=target.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()
~~~

- [ ] **Step 4: Run tests and verify pass**

Run:

~~~powershell
uv run pytest tests/phase0a/test_core.py -v
~~~

Expected: four tests pass.

- [ ] **Step 5: Commit**

Run:

~~~powershell
git add spikes/phase0a/core.py tests/phase0a/test_core.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: add safe Phase 0a command runner"
~~~

### Task 5: Implement cross-harness host observation

**Files:**
- Create: tests/phase0a/test_host_probe.py
- Create: spikes/phase0a/host_probe.py

- [ ] **Step 1: Write the failing tests**

Create tests/phase0a/test_host_probe.py:

~~~python
from pathlib import Path

from spikes.phase0a.host_probe import compare_observers, path_record


def test_path_record_reports_file_and_missing(tmp_path: Path):
    present = tmp_path / "present.bin"
    present.write_bytes(b"abc")
    assert path_record(present)["exists"] is True
    assert path_record(present)["size"] == 3
    assert path_record(tmp_path / "missing")["exists"] is False


def test_compare_observers_reports_visibility_mismatch():
    left = {"paths": {"cache": {"exists": False}}}
    right = {"paths": {"cache": {"exists": True}}}
    assert compare_observers(left, right) == {
        "cache": {"left_exists": False, "right_exists": True}
    }
~~~

- [ ] **Step 2: Run tests and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_host_probe.py -v
~~~

Expected: import failure because host_probe.py does not exist.

- [ ] **Step 3: Implement the observer**

Create spikes/phase0a/host_probe.py:

~~~python
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
from typing import Any

from .core import run_argv, write_json_atomic


def path_record(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        stat = target.stat()
    except FileNotFoundError:
        return {"path": str(target), "exists": False}
    return {
        "path": str(target),
        "exists": True,
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "size": stat.st_size if target.is_file() else None,
        "mtime_ns": stat.st_mtime_ns,
    }


def compare_observers(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    mismatches: dict[str, Any] = {}
    for name in sorted(set(left.get("paths", {})) | set(right.get("paths", {}))):
        left_exists = bool(left.get("paths", {}).get(name, {}).get("exists"))
        right_exists = bool(right.get("paths", {}).get(name, {}).get("exists"))
        if left_exists != right_exists:
            mismatches[name] = {
                "left_exists": left_exists,
                "right_exists": right_exists,
            }
    return mismatches


def build_snapshot(observer: str, claude_path: Path) -> dict[str, Any]:
    cache_root = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"
    wrapper = Path.home() / "bin" / "claude.cmd"
    payload: dict[str, Any] = {
        "observer": observer,
        "username": getpass.getuser(),
        "appdata": os.environ.get("APPDATA"),
        "paths": {
            "desktop_cache_root": path_record(cache_root),
            "desktop_cache_2_1_229": path_record(cache_root / "2.1.229" / "claude.exe"),
            "standalone_cli": path_record(claude_path),
            "wrapper": path_record(wrapper),
        },
        "credential_env_present": {
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "ANTHROPIC_AUTH_TOKEN": bool(os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            "CLAUDE_CODE_OAUTH_TOKEN": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        },
        "commands": {},
    }
    if claude_path.is_file():
        for name, args in {
            "version": ["--version"],
            "auth_status": ["auth", "status"],
            "agents_json": ["agents", "--json", "--all"],
        }.items():
            payload["commands"][name] = run_argv(
                name,
                [str(claude_path), *args],
                timeout_seconds=30,
            ).to_dict()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", required=True)
    parser.add_argument("--claude-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_json_atomic(args.output, build_snapshot(args.observer, args.claude_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 4: Run tests and capture the Codex observer**

Run:

~~~powershell
uv run pytest tests/phase0a/test_host_probe.py -v
$runId = [guid]::NewGuid().ToString('N')
$raw = Join-Path '.phase0a\runs' $runId
$nativeCli = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
uv run python -m spikes.phase0a.host_probe --observer codex --claude-path $nativeCli --output "$raw\codex-host.json"
[IO.File]::WriteAllText(
  (Join-Path '.phase0a' 'current-observer-run.txt'),
  (Resolve-Path $raw).Path
)
~~~

Expected: two tests pass and codex-host.json exists under the ignored run directory.

- [ ] **Step 5: Capture the Claude observer without using Subagent MCP**

Open a normal standalone Claude Code terminal in this repository and explicitly ask it to run:

~~~powershell
$runPath = Get-Content -Raw '.phase0a\current-observer-run.txt'
uv run python -m spikes.phase0a.host_probe --observer claude --claude-path "$env:USERPROFILE\.local\bin\claude.exe" --output (Join-Path $runPath 'claude-host.json')
~~~

Expected: the file is written by the Claude harness. Compare only redacted/path metadata; do not import its conversation.

- [ ] **Step 6: Compare observers**

Run:

~~~powershell
uv run python -c "import json; from pathlib import Path; from spikes.phase0a.host_probe import compare_observers; p=Path(r'$raw'); print(json.dumps(compare_observers(json.loads((p/'codex-host.json').read_text()), json.loads((p/'claude-host.json').read_text())), indent=2))"
~~~

Expected: any cache visibility mismatch is recorded as observer-specific evidence, not converted into a global host fact.

- [ ] **Step 7: Commit**

Run:

~~~powershell
git add spikes/phase0a/host_probe.py tests/phase0a/test_host_probe.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: add cross-harness host observer"
~~~

### Task 6: Implement the structured hook event sink

**Files:**
- Create: tests/phase0a/test_hook_sink.py
- Create: spikes/phase0a/hook_sink.py

- [ ] **Step 1: Write the failing tests**

Create tests/phase0a/test_hook_sink.py:

~~~python
import json
from pathlib import Path

from spikes.phase0a.hook_sink import append_event, build_hook_settings, sanitize_event


def test_sanitize_event_keeps_contract_fields_and_drops_unknown():
    event = sanitize_event({
        "session_id": "abc",
        "hook_event_name": "StopFailure",
        "error": "rate_limit",
        "last_assistant_message": "Bearer secret",
        "unknown_private_field": "drop-me",
    })
    assert event["session_id"] == "abc"
    assert event["error"] == "rate_limit"
    assert "secret" not in event["last_assistant_message"]
    assert "unknown_private_field" not in event


def test_append_event_writes_one_json_line(tmp_path: Path):
    target = tmp_path / "events.jsonl"
    append_event(target, {"hook_event_name": "SessionStart"})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["hook_event_name"] == "SessionStart"


def test_build_hook_settings_uses_exec_form_not_shell_quoting(tmp_path: Path):
    python_exe = tmp_path / "python.exe"
    sink = tmp_path / "hook_sink.py"
    settings = build_hook_settings(python_exe, sink, tmp_path / "events.jsonl")
    handler = settings["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"] == str(python_exe.resolve())
    assert handler["args"] == [
        str(sink.resolve()),
        "--event-log",
        str((tmp_path / "events.jsonl").resolve()),
    ]
    assert "shell" not in handler
    assert settings["enabledPlugins"]["codex@openai-codex"] is False
    assert settings["enabledPlugins"]["bridge@agent-bridge"] is False
~~~

- [ ] **Step 2: Run tests and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_hook_sink.py -v
~~~

Expected: import failure because hook_sink.py does not exist.

- [ ] **Step 3: Implement the sink**

Create spikes/phase0a/hook_sink.py:

~~~python
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from .core import redact_text
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from spikes.phase0a.core import redact_text


_ALLOWED = {
    "session_id",
    "cwd",
    "hook_event_name",
    "source",
    "model",
    "agent_id",
    "agent_type",
    "transcript_path",
    "error",
    "error_details",
    "last_assistant_message",
    "tool_name",
    "tool_input",
    "worktree_path",
}

_DEFAULT_EVENTS = (
    "SessionStart",
    "WorktreeCreate",
    "WorktreeRemove",
    "Stop",
    "StopFailure",
)


def build_hook_settings(
    python_exe: Path,
    hook_sink: Path,
    event_log: Path,
    events: tuple[str, ...] = _DEFAULT_EVENTS,
) -> dict[str, Any]:
    handler = {
        "type": "command",
        "command": str(python_exe.resolve()),
        "args": [
            str(hook_sink.resolve()),
            "--event-log",
            str(event_log.resolve()),
        ],
        "timeout": 30,
    }
    return {
        "enabledPlugins": {
            "codex@openai-codex": False,
            "bridge@agent-bridge": False,
        },
        "permissions": {
            "deny": [
                "mcp__agent_bridge__*",
                "mcp__subagent_harness_mcp__*",
            ]
        },
        "hooks": {
            event: [{"hooks": [handler.copy()]}]
            for event in events
        },
    }


def sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in sorted(_ALLOWED):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            value = redact_text(value)
        clean[key] = value
    return clean


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if os.path.getsize(lock_path) == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_event(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize_event(payload), ensure_ascii=False, sort_keys=True)
    with _locked(target.with_suffix(target.suffix + ".lock")):
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-log", type=Path, required=True)
    args = parser.parse_args()
    raw = os.read(0, 1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("hook payload exceeds 1 MiB")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    append_event(args.event_log, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 4: Run tests and verify pass**

Run:

~~~powershell
uv run pytest tests/phase0a/test_hook_sink.py -v
~~~

Expected: three tests pass.

- [ ] **Step 5: Generate an actual no-model hook smoke configuration**

Run:

~~~powershell
$cli = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
$smokeRoot = Join-Path '.phase0a\runs' (Join-Path ([guid]::NewGuid().ToString('N')) 'hook-smoke')
New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null
$env:HB_HOOK_SMOKE_ROOT = (Resolve-Path $smokeRoot).Path
$env:HB_HOOK_SINK = (Resolve-Path 'spikes\phase0a\hook_sink.py').Path
uv run python -c "import os,sys; from pathlib import Path; from spikes.phase0a.hook_sink import build_hook_settings; from spikes.phase0a.core import write_json_atomic; r=Path(os.environ['HB_HOOK_SMOKE_ROOT']); write_json_atomic(r/'settings.json', build_hook_settings(Path(sys.executable), Path(os.environ['HB_HOOK_SINK']), r/'events.jsonl')); write_json_atomic(r/'declared-empty.json', {'mcpServers':{}})"
Remove-Item Env:HB_HOOK_SMOKE_ROOT,Env:HB_HOOK_SINK
~~~

Expected: settings.json uses exec form with command plus args, disables Codex/AgentBridge plugins, and points at the absolute sink/event paths.

- [ ] **Step 6: Capability-probe init-only and require event-file evidence**

The current official hooks documentation names --init-only, but the installed CLI may omit it from --help. Treat runtime acceptance as a capability probe:

~~~powershell
$smokeStdout = Join-Path $smokeRoot 'init-only.stdout.txt'
$smokeStderr = Join-Path $smokeRoot 'init-only.stderr.txt'
$initArgs = @(
  '--init-only',
  '--settings',(Join-Path $smokeRoot 'settings.json'),
  '--strict-mcp-config',
  '--mcp-config',(Join-Path $smokeRoot 'declared-empty.json')
)
& $cli @initArgs 1> $smokeStdout 2> $smokeStderr
$initOnlyExit = $LASTEXITCODE
$stderrText = Get-Content -Raw -ErrorAction SilentlyContinue $smokeStderr
$events = @()
if(Test-Path (Join-Path $smokeRoot 'events.jsonl')){
  $events = @(Get-Content (Join-Path $smokeRoot 'events.jsonl') | ForEach-Object { $_ | ConvertFrom-Json })
}
$sessionStartSeen = $null -ne ($events | Where-Object { $_.hook_event_name -eq 'SessionStart' } | Select-Object -First 1)
$hookErrorSeen = $stderrText -match '(?i)hook.*(failed|error)|node:\s*(command not found|not recognized)'
[pscustomobject]@{
  ExitCode = $initOnlyExit
  SessionStartSeen = $sessionStartSeen
  HookErrorSeen = $hookErrorSeen
} | Format-List
~~~

PASS requires SessionStartSeen=True and HookErrorSeen=False. ExitCode=0 alone is never evidence that hooks worked.

If the installed CLI rejects --init-only, record init_only_capability=UNKNOWN/BLOCKED and ask for a separate live -p fallback approval. Do not reinterpret the missing event as a hook-delivery failure.

- [ ] **Step 7: Commit**

Run:

~~~powershell
git add spikes/phase0a/hook_sink.py tests/phase0a/test_hook_sink.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: add structured Phase 0a hook sink"
~~~

### Task 7: Implement project manifest and bounded Windows handle probe

**Files:**
- Create: tests/phase0a/test_manifest.py
- Create: spikes/phase0a/manifest.py
- Create: spikes/phase0a/hold_file.ps1

- [ ] **Step 1: Write the failing manifest tests**

Create tests/phase0a/test_manifest.py:

~~~python
import json
from pathlib import Path

from spikes.phase0a.manifest import blocked_items, scan_project


def test_scan_project_finds_hooks_and_external_import(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo x"}]}]}}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.md"
    outside.write_text("external", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(f"@{outside}\n", encoding="utf-8")
    manifest = scan_project(repo)
    assert manifest["settings"][0]["hook_events"] == ["SessionStart"]
    assert manifest["external_imports"][0]["outside_repo"] is True
    assert len(manifest["settings"][0]["sha256"]) == 64
    blocked = blocked_items(manifest, trusted_sha256=set())
    assert {item["kind"] for item in blocked} == {"project_hooks", "external_import"}


def test_trusted_hashes_unblock_exact_manifest_items(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    manifest = scan_project(repo)
    trusted = {manifest["settings"][0]["sha256"]}
    assert blocked_items(manifest, trusted_sha256=trusted) == []
~~~

- [ ] **Step 2: Run test and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_manifest.py -v
~~~

Expected: import failure because manifest.py does not exist.

- [ ] **Step 3: Implement the manifest scanner**

Create spikes/phase0a/manifest.py:

~~~python
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


_IMPORT = re.compile(r"(?m)^\s*@([^\s]+)\s*$")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def scan_project(root: str | Path) -> dict[str, Any]:
    repo = Path(root).resolve(strict=True)
    settings: list[dict[str, Any]] = []
    for relative in (Path(".claude/settings.json"), Path(".claude/settings.local.json")):
        path = repo / relative
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        hooks = payload.get("hooks", {}) if isinstance(payload, dict) else {}
        settings.append({
            "path": str(path),
            "sha256": _hash(path),
            "hook_events": sorted(hooks) if isinstance(hooks, dict) else [],
        })

    imports: list[dict[str, Any]] = []
    for candidate in (repo / "CLAUDE.md", repo / ".claude" / "CLAUDE.md"):
        if not candidate.is_file():
            continue
        for match in _IMPORT.finditer(candidate.read_text(encoding="utf-8")):
            raw = match.group(1)
            target = (candidate.parent / raw).resolve()
            imports.append({
                "source": str(candidate),
                "raw": raw,
                "resolved": str(target),
                "outside_repo": not _inside(target, repo),
                "exists": target.is_file(),
                "sha256": _hash(target) if target.is_file() else None,
            })
    return {
        "repo": str(repo),
        "settings": settings,
        "external_imports": imports,
    }


def blocked_items(
    manifest: dict[str, Any],
    *,
    trusted_sha256: set[str],
) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    for item in manifest.get("settings", []):
        if item.get("hook_events") and item.get("sha256") not in trusted_sha256:
            blocked.append({
                "kind": "project_hooks",
                "path": item.get("path"),
                "sha256": item.get("sha256"),
            })
    for item in manifest.get("external_imports", []):
        if item.get("outside_repo") and item.get("sha256") not in trusted_sha256:
            blocked.append({
                "kind": "external_import",
                "path": item.get("resolved"),
                "sha256": item.get("sha256"),
            })
    return blocked
~~~

- [ ] **Step 4: Create the Windows sharing probe**

Create spikes/phase0a/hold_file.ps1 as UTF-8 with BOM:

~~~powershell
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$ReadyPath,
    [int]$HoldMilliseconds = 5000
)

$stream = $null
try {
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    [System.IO.File]::WriteAllText($ReadyPath, "ready")
    Start-Sleep -Milliseconds $HoldMilliseconds
}
finally {
    if ($null -ne $stream) {
        $stream.Dispose()
    }
}
~~~

- [ ] **Step 5: Force the committed PowerShell script to UTF-8 with BOM**

Run:

~~~powershell
$path = Resolve-Path 'spikes\phase0a\hold_file.ps1'
$content = Get-Content -Raw -LiteralPath $path
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($path, $content, $utf8Bom)
~~~

Expected: the first three bytes are EF BB BF and Windows PowerShell 5.1 can parse the script.

- [ ] **Step 6: Run manifest tests**

Run:

~~~powershell
uv run pytest tests/phase0a/test_manifest.py -v
~~~

Expected: two tests pass.

- [ ] **Step 7: Scan the current Subagent MCP project**

Run:

~~~powershell
$manifestRun = Join-Path '.phase0a\runs' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $manifestRun -Force | Out-Null
$env:HB_MANIFEST_OUT = (Join-Path (Resolve-Path $manifestRun).Path 'project-manifest.json')
uv run python -c "import os; from spikes.phase0a.manifest import blocked_items, scan_project; from spikes.phase0a.core import write_json_atomic; m=scan_project('.'); write_json_atomic(os.environ['HB_MANIFEST_OUT'], {'manifest':m,'blocked':blocked_items(m,trusted_sha256=set())})"
Remove-Item Env:HB_MANIFEST_OUT
~~~

Expected: the manifest records no executable project/local hooks and no untrusted external CLAUDE.md import in the new Subagent MCP repository. If it reports a blocked item, Phase 0a stops before any Claude run.

- [ ] **Step 8: Verify bounded sharing contention and release**

Run in a fresh .phase0a run directory:

~~~powershell
$run = Join-Path '.phase0a\runs' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $run -Force | Out-Null
$target = Join-Path $run 'settings.json'
$ready = Join-Path $run 'ready.txt'
[IO.File]::WriteAllText($target, '{}')
$proc = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @(
  '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
  (Resolve-Path 'spikes\phase0a\hold_file.ps1'),
  '-Path',$target,'-ReadyPath',$ready,'-HoldMilliseconds','3000'
)
while (-not (Test-Path $ready)) { Start-Sleep -Milliseconds 50 }
$blocked = $false
try { [IO.File]::WriteAllText($target, '{"changed":true}') } catch { $blocked = $true }
$proc.WaitForExit()
[IO.File]::WriteAllText($target, '{"after":true}')
[pscustomobject]@{BlockedWhileHeld=$blocked;WriteAfterRelease=(Test-Path $target)} | Format-List
~~~

Expected: BlockedWhileHeld=True and WriteAfterRelease=True. The process exits and no handle remains.

- [ ] **Step 9: Commit**

Run:

~~~powershell
git add spikes/phase0a/manifest.py spikes/phase0a/hold_file.ps1 tests/phase0a/test_manifest.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: add project-content stability probes"
~~~

### Task 8: Normalize CLI contract fixtures

**Files:**
- Create: tests/phase0a/test_contracts.py
- Create: spikes/phase0a/contracts.py
- Create after live capture: tests/fixtures/phase0a/current/auth-status.json
- Create after live capture: tests/fixtures/phase0a/current/agents-normalized.json

- [ ] **Step 1: Write parser tests with unknown fields**

Create tests/phase0a/test_contracts.py:

~~~python
import pytest

from spikes.phase0a.contracts import normalize_agents, normalize_auth, normalize_stream_json


def test_normalize_auth_keeps_only_contract_fields():
    result = normalize_auth({
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "futureField": {"keep_out": True},
    })
    assert result == {
        "logged_in": True,
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
    }


def test_normalize_agents_accepts_unknown_fields_and_preserves_state():
    result = normalize_agents([{
        "id": "short",
        "sessionId": "uuid",
        "cwd": "C:\\repo",
        "kind": "background",
        "state": "working",
        "startedAt": 1,
        "futureField": 2,
    }])
    assert result[0]["session_id_present"] is True
    assert result[0]["state"] == "working"
    assert result[0]["cwd_present"] is True


def test_normalize_agents_rejects_non_array():
    with pytest.raises(ValueError):
        normalize_agents({"state": "working"})


def test_normalize_stream_json_extracts_init_and_result(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            '{"type":"system","subtype":"init","model":"fable","tools":["Read"],'
            '"mcp_servers":[{"name":"declared","status":"connected"}],'
            '"plugins":[{"name":"ponytail"}],"capabilities":["interrupt_v1"],'
            '"cwd":"C:\\\\repo","future":1}',
            '{"type":"result","subtype":"success","total_cost_usd":0.01,'
            '"usage":{"input_tokens":12,"output_tokens":3}}',
        ]) + "\n",
        encoding="utf-8",
    )
    result = normalize_stream_json(stream)
    assert result["init"]["model"] == "fable"
    assert result["init"]["tools"] == ["Read"]
    assert result["init"]["mcp_servers"] == [{"name": "declared", "status": "connected"}]
    assert result["init"]["cwd_present"] is True
    assert result["result"]["total_cost_usd"] == 0.01
~~~

- [ ] **Step 2: Run tests and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_contracts.py -v
~~~

Expected: import failure because contracts.py does not exist.

- [ ] **Step 3: Implement contract normalization**

Create spikes/phase0a/contracts.py:

~~~python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core import write_json_atomic


def normalize_auth(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "logged_in": bool(payload.get("loggedIn")),
        "auth_method": payload.get("authMethod"),
        "api_provider": payload.get("apiProvider"),
    }


def normalize_agents(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("agents payload must be an array")
    normalized: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("agent entry must be an object")
        normalized.append({
            "id_present": bool(item.get("id")),
            "session_id_present": bool(item.get("sessionId")),
            "name_present": bool(item.get("name")),
            "cwd_present": bool(item.get("cwd")),
            "kind": item.get("kind"),
            "state": item.get("state"),
            "pid_present": item.get("pid") is not None,
            "status": item.get("status"),
            "waiting_for": item.get("waitingFor"),
            "started_at_present": item.get("startedAt") is not None,
        })
    return normalized


def normalize_stream_json(path: str | Path) -> dict[str, Any]:
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
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
        if item.get("type") == "result":
            result = {
                "subtype": item.get("subtype"),
                "is_error": bool(item.get("is_error")),
                "total_cost_usd": item.get("total_cost_usd"),
                "usage": item.get("usage"),
            }
    if init is None:
        raise ValueError("stream has no system/init event")
    return {"init": init, "result": result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--agents", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    version = args.version_file.read_text(encoding="utf-8-sig").strip()
    auth = normalize_auth(json.loads(args.auth.read_text(encoding="utf-8-sig")))
    agents = normalize_agents(json.loads(args.agents.read_text(encoding="utf-8-sig")))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(args.output_dir / "auth-status.json", {
        "observed_cli_version": version,
        "auth": auth,
    })
    write_json_atomic(args.output_dir / "agents-normalized.json", {
        "observed_cli_version": version,
        "agents": agents,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 4: Capture no-model CLI evidence**

Run with the explicit standalone path:

~~~powershell
$cli = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
$run = Join-Path '.phase0a\runs' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $run -Force | Out-Null
& $cli --version | Set-Content -Encoding utf8 (Join-Path $run 'version.txt')
& $cli auth status | Set-Content -Encoding utf8 (Join-Path $run 'auth-status.raw.json')
& $cli agents --json --all | Set-Content -Encoding utf8 (Join-Path $run 'agents.raw.json')
foreach($command in @('stop','logs','respawn','attach','rm')) {
  & $cli $command --help *> (Join-Path $run "$command-help.txt")
}
~~~

Expected: version/auth/agents exit 0. Each lifecycle command either exposes a supported help surface or is recorded as missing; do not infer support from docs alone.

- [ ] **Step 5: Normalize and commit secret-free fixtures**

Run:

~~~powershell
$normalizeArgs = @(
  '--auth', (Join-Path $run 'auth-status.raw.json'),
  '--agents', (Join-Path $run 'agents.raw.json'),
  '--version-file', (Join-Path $run 'version.txt'),
  '--output-dir', 'tests\fixtures\phase0a\current'
)
uv run python -m spikes.phase0a.contracts @normalizeArgs
uv run pytest tests/phase0a/test_contracts.py -v
git diff --check
~~~

Expected: four tests pass and normalized fixtures contain no tokens, emails, raw prompts, or absolute transcript paths.

- [ ] **Step 6: Commit**

Run:

~~~powershell
git add spikes/phase0a/contracts.py tests/phase0a/test_contracts.py tests/fixtures/phase0a
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: record Claude CLI contract fixtures"
~~~

### Task 9: Prove strict MCP blocks repository server spawn

**Files:**
- Create: spikes/phase0a/marker_mcp.py
- Create: spikes/phase0a/strict_probe.py
- Create: tests/phase0a/test_strict_probe.py
- Raw only: .phase0a/runs/$runId/strict-mcp/

- [ ] **Step 1: Write the failing layout test**

Create tests/phase0a/test_strict_probe.py:

~~~python
import json
import sys
from pathlib import Path

from spikes.phase0a.strict_probe import prepare_probe


def test_prepare_probe_creates_repo_marker_and_empty_declared_config(tmp_path: Path):
    marker_script = tmp_path / "marker_mcp.py"
    marker_script.write_text("print('unused')", encoding="utf-8")
    layout = prepare_probe(tmp_path / "run", Path(sys.executable), marker_script)
    repo_config = json.loads(Path(layout["repo_mcp"]).read_text(encoding="utf-8"))
    declared = json.loads(Path(layout["declared_config"]).read_text(encoding="utf-8"))
    settings = json.loads(Path(layout["settings"]).read_text(encoding="utf-8"))
    assert "subagent_harness_mcp_phase0a_repo_marker" in repo_config["mcpServers"]
    assert declared == {"mcpServers": {}}
    assert settings["enabledPlugins"]["codex@openai-codex"] is False
    assert Path(layout["repo"], ".git").is_dir()
~~~

- [ ] **Step 2: Run the test and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_strict_probe.py -v
~~~

Expected: import failure because strict_probe.py does not exist.

- [ ] **Step 3: Create the disposable marker server**

Create spikes/phase0a/marker_mcp.py:

~~~python
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


marker = os.environ.get("SUBAGENT_HARNESS_MCP_PHASE0A_MARKER")
if not marker:
    raise SystemExit("SUBAGENT_HARNESS_MCP_PHASE0A_MARKER is required")
Path(marker).write_text("spawned", encoding="utf-8")

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "subagent-harness-mcp-phase0a-marker", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": []}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
~~~

- [ ] **Step 4: Implement deterministic probe preparation**

Create spikes/phase0a/strict_probe.py:

~~~python
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .core import run_argv, write_json_atomic


def prepare_probe(root: Path, python_exe: Path, marker_script: Path) -> dict[str, Any]:
    target = root.resolve()
    repo = target / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# Phase 0a disposable repo\n", encoding="utf-8")
    for name, argv in (
        ("git-init", ["git", "-C", str(repo), "init", "-b", "main"]),
        ("git-add", ["git", "-C", str(repo), "add", "README.md"]),
        (
            "git-commit",
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Subagent MCP Phase0a",
                "-c",
                "user.email=phase0a@example.invalid",
                "commit",
                "-m",
                "chore: initialize disposable probe",
            ],
        ),
    ):
        result = run_argv(name, argv, timeout_seconds=30)
        if result.exit_code != 0:
            raise RuntimeError(f"{name} failed: {result.stderr}")

    marker = target / "repo-mcp-spawned.txt"
    repo_mcp = repo / ".mcp.json"
    declared = target / "declared-empty.json"
    settings = target / "settings.json"
    write_json_atomic(repo_mcp, {
        "mcpServers": {
            "subagent_harness_mcp_phase0a_repo_marker": {
                "type": "stdio",
                "command": str(python_exe.resolve()),
                "args": [str(marker_script.resolve())],
                "env": {"SUBAGENT_HARNESS_MCP_PHASE0A_MARKER": str(marker.resolve())},
            }
        }
    })
    write_json_atomic(declared, {"mcpServers": {}})
    write_json_atomic(settings, {
        "enabledPlugins": {
            "codex@openai-codex": False,
            "bridge@agent-bridge": False,
        },
        "permissions": {
            "deny": [
                "mcp__agent_bridge__*",
                "mcp__subagent_harness_mcp__*",
            ]
        },
    })
    layout = {
        "root": str(target),
        "repo": str(repo),
        "repo_mcp": str(repo_mcp),
        "declared_config": str(declared),
        "settings": str(settings),
        "marker": str(marker),
    }
    write_json_atomic(target / "layout.json", layout)
    return layout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    prepare_probe(args.root, Path(sys.executable), Path(__file__).with_name("marker_mcp.py"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 5: Run tests and prepare a disposable repository**

Run:

~~~powershell
uv run pytest tests/phase0a/test_strict_probe.py -v
$runId = [guid]::NewGuid().ToString('N')
$strictRoot = Join-Path '.phase0a\runs' (Join-Path $runId 'strict-mcp')
uv run python -m spikes.phase0a.strict_probe --root $strictRoot
$layout = Get-Content -Raw (Join-Path $strictRoot 'layout.json') | ConvertFrom-Json
~~~

Expected: one test passes; layout.json names the disposable repo, empty declared config, and marker path.

- [ ] **Step 6: Run the no-model strict probe**

The official hooks documentation names --init-only even when a local --help omits it. Probe the installed binary instead of assuming:

~~~powershell
$strictStdout = Join-Path $strictRoot 'strict-init.stdout.txt'
$strictStderr = Join-Path $strictRoot 'strict-init.stderr.txt'
Push-Location $layout.repo
try {
  & $cli --init-only --settings $layout.settings --strict-mcp-config --mcp-config $layout.declared_config 1> $strictStdout 2> $strictStderr
  $strictInitExit = $LASTEXITCODE
} finally {
  Pop-Location
}
$strictErrorText = Get-Content -Raw -ErrorAction SilentlyContinue $strictStderr
$initOnlyRejected = $strictErrorText -match '(?i)(unknown|invalid|unrecognized).*(init-only)'
$strictHookError = $strictErrorText -match '(?i)hook.*(failed|error)|node:\s*(command not found|not recognized)'
[pscustomobject]@{
  ExitCode=$strictInitExit
  InitOnlyRejected=$initOnlyRejected
  HookErrorSeen=$strictHookError
  MarkerSpawned=(Test-Path -LiteralPath $layout.marker)
} | Format-List
~~~

PASS for the no-model path requires exit 0, InitOnlyRejected=False, HookErrorSeen=False, and MarkerSpawned=False. If --init-only is rejected, record init_only_capability as BLOCKED and use the separately approved documented -p fallback; do not label the strict gate PASS from this step.

- [ ] **Step 7: Establish that the probe exercised MCP startup**

Run the disposable control without strict mode:

~~~powershell
$controlStdout = Join-Path $strictRoot 'control-init.stdout.txt'
$controlStderr = Join-Path $strictRoot 'control-init.stderr.txt'
Push-Location $layout.repo
try {
  & $cli --init-only --settings $layout.settings 1> $controlStdout 2> $controlStderr
  $controlExit = $LASTEXITCODE
} finally {
  Pop-Location
}
$controlErrorText = Get-Content -Raw -ErrorAction SilentlyContinue $controlStderr
$controlHookError = $controlErrorText -match '(?i)hook.*(failed|error)|node:\s*(command not found|not recognized)'
[pscustomobject]@{
  ExitCode=$controlExit
  HookErrorSeen=$controlHookError
  ControlMarkerSpawned=(Test-Path -LiteralPath $layout.marker)
} | Format-List
~~~

On this host, project approval is expected to keep the server pending, so ControlMarkerSpawned=False is likely. Record that control as inconclusive immediately; do not retry it or wait for a prompt. HookErrorSeen must still be False; exit 0 never overrides a hook error.

If no no-model control can prove startup, ask for a separate live-canary approval and run:

~~~powershell
$strictLiveStdout = Join-Path $strictRoot 'strict-live.stdout.jsonl'
$strictLiveStderr = Join-Path $strictRoot 'strict-live.stderr.txt'
Push-Location $layout.repo
try {
  & $cli -p "Reply exactly PHASE0A_READY and do not use tools." --model fable --effort high --output-format stream-json --verbose --settings $layout.settings --strict-mcp-config --mcp-config $layout.declared_config 1> $strictLiveStdout 2> $strictLiveStderr
  $strictLiveExit = $LASTEXITCODE
} finally {
  Pop-Location
}
$strictLiveErrorText = Get-Content -Raw -ErrorAction SilentlyContinue $strictLiveStderr
$strictLiveHookError = $strictLiveErrorText -match '(?i)hook.*(failed|error)|node:\s*(command not found|not recognized)'
if($strictLiveExit -ne 0){ throw "strict live fallback failed with exit $strictLiveExit" }
if($strictLiveHookError){ throw 'strict live fallback reported a hook/plugin failure' }
~~~

Expected: system/init contains no subagent_harness_mcp_phase0a_repo_marker entry and the marker file remains absent. This command consumes Claude quota and must not run without immediate approval.

- [ ] **Step 8: Record the result and commit deterministic sources**

Run:

~~~powershell
git add spikes/phase0a/marker_mcp.py spikes/phase0a/strict_probe.py tests/phase0a/test_strict_probe.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: add strict MCP spawn marker"
~~~

The report records PASS only when pre-spawn exclusion is demonstrated. Inconclusive is not PASS.

### Task 10: Probe background hooks, worktree reporting, and daemon lifecycle

**Files:**
- Create: tests/phase0a/test_background_probe.py
- Create: spikes/phase0a/background_probe.py
- Raw only: .phase0a/runs/$runId/background/

- [ ] **Step 1: Write the failing command/settings tests**

Create tests/phase0a/test_background_probe.py:

~~~python
from pathlib import Path

from spikes.phase0a.background_probe import (
    build_background_argv,
    build_hook_settings,
    prepare_background,
)


def test_background_argv_never_combines_bg_and_print(tmp_path: Path):
    argv = build_background_argv(
        Path("claude.exe"),
        tmp_path / "settings.json",
        tmp_path / "declared-empty.json",
        "subagent-harness-mcp-phase0a-test",
        "fable",
        "high",
        "finish the disposable task",
    )
    assert "--bg" in argv
    assert "-p" not in argv
    assert "--print" not in argv
    assert "--strict-mcp-config" in argv


def test_hook_settings_include_required_events(tmp_path: Path):
    settings = build_hook_settings(
        Path("python.exe"),
        Path("hook_sink.py"),
        tmp_path / "events.jsonl",
    )
    assert set(settings["hooks"]) == {
        "SessionStart",
        "WorktreeCreate",
        "WorktreeRemove",
        "Stop",
        "StopFailure",
    }
    assert settings["enabledPlugins"]["codex@openai-codex"] is False
    assert settings["enabledPlugins"]["bridge@agent-bridge"] is False


def test_prepare_background_creates_disposable_repo(tmp_path: Path):
    sink = tmp_path / "hook_sink.py"
    sink.write_text("raise SystemExit(0)", encoding="utf-8")
    layout = prepare_background(tmp_path / "run" / "background", Path("python.exe"), sink)
    assert Path(layout["repo"], ".git").is_dir()
    assert Path(layout["settings"]).is_file()
    assert Path(layout["declared_config"]).is_file()
~~~

- [ ] **Step 2: Run tests and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_background_probe.py -v
~~~

Expected: import failure because background_probe.py does not exist.

- [ ] **Step 3: Implement settings and argv construction**

Create spikes/phase0a/background_probe.py:

~~~python
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .core import run_argv, write_json_atomic
from .hook_sink import build_hook_settings


def build_background_argv(
    cli: Path,
    settings: Path,
    mcp_config: Path,
    name: str,
    model: str,
    effort: str,
    prompt: str,
) -> list[str]:
    return [
        str(cli.resolve()),
        "--bg",
        "--name",
        name,
        "--settings",
        str(settings.resolve()),
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config.resolve()),
        "--model",
        model,
        "--effort",
        effort,
        prompt,
    ]


def prepare_background(root: Path, python_exe: Path, hook_sink: Path) -> dict[str, Any]:
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
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Subagent MCP Phase0a",
                "-c",
                "user.email=phase0a@example.invalid",
                "commit",
                "-m",
                "chore: initialize disposable background probe",
            ],
        ),
    ):
        result = run_argv(name, argv, timeout_seconds=30)
        if result.exit_code != 0:
            raise RuntimeError(f"{name} failed: {result.stderr}")
    events = target / "events.jsonl"
    settings = target / "settings.json"
    declared = target / "declared-empty.json"
    write_json_atomic(settings, build_hook_settings(python_exe, hook_sink, events))
    write_json_atomic(declared, {"mcpServers": {}})
    layout = {
        "root": str(target),
        "repo": str(repo),
        "events": str(events),
        "settings": str(settings),
        "declared_config": str(declared),
        "name": "subagent-harness-mcp-phase0a-" + target.parent.name,
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 4: Run tests and verify pass**

Run:

~~~powershell
uv run pytest tests/phase0a/test_background_probe.py -v
~~~

Expected: three tests pass.

- [ ] **Step 5: Ask for live background canary approval**

Explain that the next command consumes Claude subscription automation quota, may create a Claude-owned worktree inside a disposable repository, and may trigger Agent View summary requests. Do not continue without immediate approval.

- [ ] **Step 6: Prepare the disposable no-remote repository and hook settings**

Run:

~~~powershell
$runId = [guid]::NewGuid().ToString('N')
$backgroundRoot = Join-Path '.phase0a\runs' (Join-Path $runId 'background')
uv run python -m spikes.phase0a.background_probe --root $backgroundRoot
$layout = Get-Content -Raw (Join-Path $backgroundRoot 'layout.json') | ConvertFrom-Json
$before = & $cli agents --json --all | ConvertFrom-Json
$before | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8 (Join-Path $backgroundRoot 'agents-before.json')
~~~

Expected: the disposable repository has no remote; settings.json contains bridge hooks, recursion denies, and disabled Codex/AgentBridge plugins.

- [ ] **Step 7: Capture the managed proxy attestation**

The live-canary approval from Step 5 covers this minimal managed call and the background call that follows. Run:

~~~powershell
$proxyRaw = Join-Path $backgroundRoot 'context-proxy.raw.jsonl'
$proxyStderr = Join-Path $backgroundRoot 'context-proxy.stderr.txt'
$proxyArgs = @(
  '-p','Reply exactly PHASE0A_CONTEXT_READY and do not use tools.',
  '--no-session-persistence',
  '--output-format','stream-json','--verbose',
  '--settings',$layout.settings,
  '--strict-mcp-config','--mcp-config',$layout.declared_config,
  '--model','fable','--effort','high'
)
Push-Location $layout.repo
try {
  & $cli @proxyArgs 1> $proxyRaw 2> $proxyStderr
  $proxyExit = $LASTEXITCODE
} finally {
  Pop-Location
}
$proxyErrorText = Get-Content -Raw -ErrorAction SilentlyContinue $proxyStderr
$proxyHookError = $proxyErrorText -match '(?i)hook.*(failed|error)|node:\s*(command not found|not recognized)'
if($proxyExit -ne 0){ throw "managed proxy failed with exit $proxyExit" }
if($proxyHookError){ throw 'managed proxy reported a hook/plugin failure on stderr' }
$env:HB_PROXY_RAW = (Resolve-Path $proxyRaw).Path
$env:HB_PROXY_OUT = (Join-Path (Resolve-Path 'tests\fixtures\phase0a\current').Path 'context-attestation.json')
uv run python -c "import os; from spikes.phase0a.contracts import normalize_stream_json; from spikes.phase0a.core import write_json_atomic; write_json_atomic(os.environ['HB_PROXY_OUT'], normalize_stream_json(os.environ['HB_PROXY_RAW']))"
$attestation = Get-Content -Raw $env:HB_PROXY_OUT | ConvertFrom-Json
$forbiddenPlugins = @($attestation.init.plugins | Where-Object { $_ -match '(?i)(codex|agent[-_]?bridge|harness[-_]?bridge)' })
$forbiddenMcp = @($attestation.init.mcp_servers | Where-Object { $_.name -match '(?i)(agent_bridge|subagent_mcp)' })
if($forbiddenPlugins.Count -gt 0){ throw "forbidden plugins still loaded: $($forbiddenPlugins -join ', ')" }
if($forbiddenMcp.Count -gt 0){ throw "forbidden MCP still loaded: $($forbiddenMcp.name -join ', ')" }
Remove-Item Env:HB_PROXY_RAW,Env:HB_PROXY_OUT
~~~

Expected: context-attestation.json records the actual model, tools, declared MCP list, loaded plugins, capabilities, permission mode, and usage/cost metadata without cwd or transcript paths. Codex and AgentBridge plugins/tools are absent, and stderr contains no hook failure or Node-not-found error. This is the live proof that the per-run plugin disable took effect.

- [ ] **Step 8: Launch without parsing launch text**

Run:

~~~powershell
$prompt = 'In this disposable repository only, create phase0a-proof.txt containing the word ready, confirm it exists, then delete it so git status is clean. Do not commit, add a remote, push, merge, or modify files outside the worktree. Then report the worktree path and stop.'
$bgArgs = @(
  '--bg','--name',$layout.name,
  '--settings',$layout.settings,
  '--strict-mcp-config','--mcp-config',$layout.declared_config,
  '--model','fable','--effort','high',
  $prompt
)
Push-Location $layout.repo
try {
  & $cli @bgArgs 1> (Join-Path $backgroundRoot 'launch-display-only.txt') 2> (Join-Path $backgroundRoot 'launch.stderr.txt')
  $launchExit = $LASTEXITCODE
} finally {
  Pop-Location
}
$launchErrorText = Get-Content -Raw -ErrorAction SilentlyContinue (Join-Path $backgroundRoot 'launch.stderr.txt')
if($launchExit -ne 0){ throw "background launch failed with exit $launchExit" }
if($launchErrorText -match '(?i)hook.*(failed|error)|node:\s*(command not found|not recognized)'){
  throw 'background launch reported a hook/plugin failure on stderr'
}
$deadline = [DateTime]::UtcNow.AddMinutes(3)
$entry = $null
do {
  $agents = & $cli agents --json --all | ConvertFrom-Json
  $entry = $agents | Where-Object { $_.name -eq $layout.name } | Select-Object -First 1
  if($null -eq $entry){ Start-Sleep -Milliseconds 500 }
} while($null -eq $entry -and [DateTime]::UtcNow -lt $deadline)
if($null -eq $entry){ throw 'background session did not appear in agents JSON' }
$shortId = $entry.id
~~~

The launch text is stored only for diagnostics and never parsed for control. The unique name is resolved through agents JSON.

Expected: a new background entry with a session ID and structured state.

- [ ] **Step 9: Verify worktree and lifecycle hook evidence**

Wait until the session is done, failed, or needs input. Inspect events.jsonl and require:

- SessionStart;
- WorktreeCreate with an actual path before any permanent writer lease would be assigned;
- Stop on success or StopFailure on API error.

If WorktreeCreate is missing or arrives after the first write evidence, auto-worktree mode fails its Phase 0a gate.

- [ ] **Step 10: Probe stop/respawn race**

Use only the short ID from agents JSON:

~~~powershell
& $cli stop $shortId
& $cli agents --json --all
Start-Sleep -Milliseconds 750
& $cli agents --json --all
& $cli respawn $shortId
& $cli agents --json --all
& $cli stop $shortId
~~~

Expected: stop remains stable across two observations, respawn is visible, and the final stop remains stable. Do not call claude rm.

- [ ] **Step 11: Approval-gated two-session concurrency probe**

Ask for immediate approval for two additional short Claude background turns. After approval, launch two uniquely named read-only sessions in the disposable repository:

~~~powershell
$concurrencyNames = @("$($layout.name)-c1", "$($layout.name)-c2")
foreach($name in $concurrencyNames) {
  $args = @(
    '--bg','--name',$name,
    '--settings',$layout.settings,
    '--strict-mcp-config','--mcp-config',$layout.declared_config,
    '--model','fable','--effort','high',
    'Run a local 20-second wait command without editing files, then reply exactly PHASE0A_CONCURRENCY_READY.'
  )
  Push-Location $layout.repo
  try { & $cli @args | Out-Null } finally { Pop-Location }
}
$deadline = [DateTime]::UtcNow.AddMinutes(2)
$entries = @()
do {
  $agents = & $cli agents --json --all | ConvertFrom-Json
  $entries = @($agents | Where-Object { $_.name -in $concurrencyNames })
  if($entries.Count -lt 2){ Start-Sleep -Milliseconds 500 }
} while($entries.Count -lt 2 -and [DateTime]::UtcNow -lt $deadline)
[pscustomobject]@{
  EntriesObserved = $entries.Count
  SimultaneouslyActive = @($entries | Where-Object { $_.state -in @('working','blocked') }).Count
} | Format-List
foreach($entry in $entries){ & $cli stop $entry.id | Out-Null }
~~~

Expected: both entries appear in structured agents JSON. Record the maximum simultaneously active count; do not infer a higher platform ceiling than the two sessions actually tested.

- [ ] **Step 12: Record Agent View overhead as observed or unknown**

Use only an official account/usage surface if one is available and approved. Do not parse plan-usage-history.json. If no official per-session accounting separates Agent View summaries, record the overhead as UNKNOWN with the commands/evidence attempted.

- [ ] **Step 13: Approval-gated WorktreeRemove probe**

Read the structured hook events and select the final WorktreeCreate path:

~~~powershell
$events = Get-Content -LiteralPath $layout.events | ForEach-Object { $_ | ConvertFrom-Json }
$worktreeEvent = $events | Where-Object { $_.hook_event_name -eq 'WorktreeCreate' } | Select-Object -Last 1
$worktreePath = $worktreeEvent.worktree_path
if([string]::IsNullOrWhiteSpace($worktreePath)){ throw 'WorktreeCreate did not report worktree_path' }
$baseCommit = git -C $layout.repo rev-parse HEAD
$dirty = @(git -C $worktreePath status --porcelain)
$extraCommits = [int](git -C $worktreePath rev-list --count "$baseCommit..HEAD")
[pscustomobject]@{Worktree=$worktreePath;DirtyLines=$dirty.Count;ExtraCommits=$extraCommits} | Format-List
~~~

Expected: DirtyLines=0 and ExtraCommits=0. If either is nonzero, do not remove anything; record WorktreeRemove as BLOCKED.

With a clean audit, ask for immediate approval to delete only this Claude-owned disposable background row/worktree. After approval:

~~~powershell
& $cli rm $shortId
$eventsAfterRemove = Get-Content -LiteralPath $layout.events | ForEach-Object { $_ | ConvertFrom-Json }
$removeEvent = $eventsAfterRemove | Where-Object { $_.hook_event_name -eq 'WorktreeRemove' } | Select-Object -Last 1
[pscustomobject]@{
  RemoveHookObserved = $null -ne $removeEvent
  WorktreeStillExists = Test-Path -LiteralPath $worktreePath
} | Format-List
~~~

Expected: RemoveHookObserved=True and WorktreeStillExists=False. The native transcript remains owned by Claude and is not deleted by Subagent MCP.

- [ ] **Step 14: Commit the deterministic helper**

Run:

~~~powershell
git add spikes/phase0a/background_probe.py tests/phase0a/test_background_probe.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "test: add background lifecycle probe builder"
~~~

### Task 11: Generate the Phase 0a gate report

**Files:**
- Create: tests/phase0a/test_report.py
- Create: spikes/phase0a/report.py
- Create: docs/phase0a/phase0a-report.md

- [ ] **Step 1: Write the failing report test**

Create tests/phase0a/test_report.py:

~~~python
import pytest

from spikes.phase0a.report import default_gates, init_gate_file, render_report


def test_render_report_keeps_unknown_distinct_from_pass():
    text = render_report({
        "auth": {"status": "PASS", "evidence": "auth-status.json"},
        "agent_view_overhead": {"status": "UNKNOWN", "evidence": "no official split"},
    })
    assert "| auth | PASS | auth-status.json |" in text
    assert "| agent_view_overhead | UNKNOWN | no official split |" in text
    assert "UNKNOWN is not PASS" in text


def test_default_gates_start_blocked():
    gates = default_gates()
    assert gates["strict_mcp_pre_spawn"]["status"] == "BLOCKED"
    assert gates["worktree_create_hook"]["status"] == "BLOCKED"


def test_init_gate_file_refuses_overwrite(tmp_path):
    target = tmp_path / "gates.json"
    init_gate_file(target)
    with pytest.raises(FileExistsError):
        init_gate_file(target)
~~~

- [ ] **Step 2: Run test and verify failure**

Run:

~~~powershell
uv run pytest tests/phase0a/test_report.py -v
~~~

Expected: import failure because report.py does not exist.

- [ ] **Step 3: Implement report rendering**

Create spikes/phase0a/report.py:

~~~python
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import write_json_atomic


_GATE_NAMES = (
    "standalone_cli",
    "subscription_auth",
    "credential_precedence",
    "observer_visibility",
    "lifecycle_commands",
    "agents_json_schema",
    "context_attestation",
    "init_only_capability",
    "plugin_disable_effective",
    "strict_mcp_pre_spawn",
    "project_manifest",
    "windows_handle_release",
    "session_start_hook",
    "worktree_create_hook",
    "worktree_remove_hook",
    "stop_hook",
    "stop_failure_hook",
    "daemon_stop_race",
    "agent_view_overhead",
    "background_concurrency",
)


def default_gates() -> dict[str, dict[str, str]]:
    return {
        name: {"status": "BLOCKED", "evidence": "not evaluated"}
        for name in _GATE_NAMES
    }


def init_gate_file(path: str | Path) -> None:
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing gate file: {target}")
    write_json_atomic(target, default_gates())


def render_report(gates: dict[str, dict[str, Any]]) -> str:
    lines = [
        "# Subagent MCP Phase 0a Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "| Gate | Status | Evidence |",
        "|---|---|---|",
    ]
    for name in sorted(gates):
        row = gates[name]
        status = row["status"]
        if status not in {"PASS", "FAIL", "UNKNOWN", "BLOCKED"}:
            raise ValueError(f"invalid gate status: {status}")
        evidence = str(row.get("evidence", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {status} | {evidence} |")
    lines.extend([
        "",
        "UNKNOWN is not PASS. FAIL or BLOCKED prevents the dependent Phase 0b capability.",
        "",
    ])
    return "\n".join(lines)


def write_report(path: str | Path, gates: dict[str, dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(gates), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-gates", type=Path)
    parser.add_argument("--gates", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.init_gates:
        if args.gates or args.output:
            parser.error("--init-gates cannot be combined with --gates/--output")
        init_gate_file(args.init_gates)
        return 0
    if not args.gates or not args.output:
        parser.error("--gates and --output are required")
    gates = json.loads(args.gates.read_text(encoding="utf-8"))
    write_report(args.output, gates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 4: Run all deterministic tests**

Run:

~~~powershell
uv run pytest -v
git diff --check
~~~

Expected: every Phase 0a unit test passes; no whitespace errors.

- [ ] **Step 5: Build the report from reviewed evidence**

Initialize every gate as BLOCKED:

~~~powershell
$gateFile = '.phase0a\phase0a-gates.json'
uv run python -m spikes.phase0a.report --init-gates $gateFile
~~~

Review the raw/normalized evidence. Edit phase0a-gates.json with apply_patch, changing one row at a time from BLOCKED to PASS, FAIL, or UNKNOWN and replacing "not evaluated" with an exact fixture/report path or a concise reason. A PASS without evidence is invalid.

Render the committed report:

~~~powershell
uv run python -m spikes.phase0a.report --gates $gateFile --output 'docs\phase0a\phase0a-report.md'
~~~

Expected: the report contains all 20 gate rows. Unsupported measurements are UNKNOWN, not omitted.

- [ ] **Step 6: Inspect for sensitive data**

Run:

~~~powershell
rg -n -i 'sk-ant-|bearer\s+|oauth.*token|api[_-]?key\s*[:=]|authorization:' docs/phase0a tests/fixtures/phase0a
~~~

Expected: no credential values. If a match is a field name or redaction marker, inspect it manually before commit.

- [ ] **Step 7: Commit report and normalized fixtures**

Run:

~~~powershell
git add docs/phase0a tests/fixtures/phase0a spikes/phase0a/report.py tests/phase0a/test_report.py
git -c user.name="Subagent MCP Contributor" -c user.email=subagent-harness-mcp-contributor@example.invalid commit -m "docs: record Subagent MCP Phase 0a evidence"
~~~

### Task 12: Stop at the Phase 0a review checkpoint

**Files:**
- Read: docs/phase0a/phase0a-report.md
- Read: docs/superpowers/specs/2026-08-17-subagent-mcp-design.md

- [ ] **Step 1: Verify final repository state**

Run:

~~~powershell
git status --short --branch
git log --oneline --decorate --max-count=12
uv run pytest -v
git diff --check
~~~

Expected: clean main branch and all tests pass.

- [ ] **Step 2: Compare report to spec gates**

For every Phase 0a requirement in spec section 19.1, point to one report row and its evidence. Add a BLOCKED/UNKNOWN row for anything without evidence; do not write a Phase 0b workaround in this step.

- [ ] **Step 3: Present results and stop**

Report:

- verified host facts;
- failed/unknown gates;
- CLI/model quota consumed;
- sessions/worktrees still present;
- exact safe cleanup options, without performing cleanup;
- decisions that Phase 0b must make.

- [ ] **Step 4: Require user review**

Do not invoke Phase 0b, install Node, install the Agent SDK, register MCP, or create production modules. Wait for the user to approve the report.

- [ ] **Step 5: Compact before the next plan**

After report approval, compact or start a fresh planning context containing only AGENTS.md, the design spec, the Phase 0a report, current repository state, and the Phase 0b planning checkpoint. Then invoke writing-plans again for Phase 0b.
