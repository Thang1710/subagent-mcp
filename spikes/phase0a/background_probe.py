from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .core import run_argv, write_json_atomic
from .hook_sink import build_hook_settings as build_event_hook_settings
from .live_common import prepare_private_runtime_group_root


BACKGROUND_PROMPT = (
    "In this disposable worktree only, use the file editing tool to create "
    "phase0a-proof.txt containing exactly ready and one newline. Confirm it exists, "
    "then run a local 30-second wait without changing any other file. Do not commit, "
    "add a remote, push, merge, or modify anything outside this worktree."
)

_AUTOCOMPACT_TOKENS = 274000
_SETTING_SOURCES = "user,project,local"
_BACKGROUND_TOOLS = "Read,Write,Bash"
_PERMISSION_MODE = "acceptEdits"
_DENIED_TOOLS = (
    "mcp__codex__*",
    "mcp__agent_bridge__*",
    "mcp__subagent_harness_mcp__*",
)


def build_background_argv(
    cli: Path,
    settings: Path,
    mcp_config: Path,
    name: str,
    worktree_name: str,
    model: str,
    effort: str,
    prompt: str,
) -> list[str]:
    return [
        str(cli.resolve()),
        "--bg",
        "--name",
        name,
        "--worktree",
        worktree_name,
        "--model",
        model,
        "--effort",
        effort,
        "--autocompact",
        str(_AUTOCOMPACT_TOKENS),
        "--setting-sources",
        _SETTING_SOURCES,
        "--settings",
        str(settings.resolve()),
        "--tools",
        _BACKGROUND_TOOLS,
        "--disallowedTools",
        *_DENIED_TOOLS,
        "--permission-mode",
        _PERMISSION_MODE,
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config.resolve()),
        prompt,
    ]


def build_background_hook_settings(
    python_exe: Path,
    hook_sink: Path,
    worktree_hook: Path,
    guard_script: Path,
    event_log: Path,
    repo: Path,
    worktree_root: Path,
    lease_ack: Path,
    creation_lock: Path,
    guard_ack: Path,
    execution_id: str,
    proof_relative: str = "phase0a-proof.txt",
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
                "--repo",
                str(repo.resolve()),
                "--worktree-root",
                str(worktree_root.resolve()),
                "--event-log",
                str(event_log.resolve()),
                "--lease-ack",
                str(lease_ack.resolve()),
                "--creation-lock",
                str(creation_lock.resolve()),
                "--execution-id",
                execution_id,
            ],
            "timeout": 180,
        }]
    }]
    settings["hooks"]["PreToolUse"] = [{
        "hooks": [{
            "type": "command",
            "command": str(python_exe.resolve()),
            "args": [
                str(guard_script.resolve()),
                "pretool-guard",
                "--lease-ack",
                str(lease_ack.resolve()),
                "--event-log",
                str(event_log.resolve()),
                "--guard-ack",
                str(guard_ack.resolve()),
                "--worktree-root",
                str(worktree_root.resolve()),
                "--execution-id",
                execution_id,
                "--proof-relative",
                proof_relative,
            ],
            "timeout": 30,
        }]
    }]
    settings["worktree"] = {"baseRef": "head"}
    return settings


def _git_output(name: str, argv: list[str]) -> str:
    result = run_argv(name, argv, timeout_seconds=30)
    if result.exit_code != 0 or getattr(result, "timed_out", False):
        raise RuntimeError(f"{name} failed")
    return result.stdout.strip()


def _is_contained(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def prepare_background(
    root: Path,
    python_exe: Path,
    hook_sink: Path,
    worktree_hook: Path,
    guard_script: Path | None = None,
) -> dict[str, Any]:
    target = prepare_private_runtime_group_root(root)
    repo = target / "repo"
    repo.mkdir()
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
    base_commit = _git_output(
        "git-base-commit", ["git", "-C", str(repo), "rev-parse", "HEAD"],
    )
    remotes = _git_output(
        "git-remotes", ["git", "-C", str(repo), "remote"],
    ).splitlines()
    if remotes:
        raise RuntimeError("disposable background repository must have no remote")
    raw_common = _git_output(
        "git-common-dir", ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
    )
    common = Path(raw_common)
    if not common.is_absolute():
        common = repo / common
    repository_common_dir = common.resolve(strict=True)
    events = target / "events.jsonl"
    settings = target / "settings.json"
    declared = target / "declared-empty.json"
    prompt = target / "prompt.txt"
    worktree_root = target / "worktrees"
    worktree_root.mkdir()
    lease_ack = target / "worktree-lease.json"
    creation_lock = target / "repository-create.lock"
    guard_ack = target / "pretool-guard.jsonl"
    execution_id = uuid.uuid4().hex
    group_name = "subagent-harness-mcp-phase0a-c-" + execution_id[:16]
    worktree_name = "phase0a-c-" + execution_id[:16]
    selected_guard = (
        Path(__file__).with_name("live_background.py")
        if guard_script is None else guard_script
    )
    write_json_atomic(
        settings,
        build_background_hook_settings(
            python_exe,
            hook_sink,
            worktree_hook,
            selected_guard,
            events,
            repo,
            worktree_root,
            lease_ack,
            creation_lock,
            guard_ack,
            execution_id,
        ),
    )
    write_json_atomic(declared, {"mcpServers": {}})
    prompt.write_text(BACKGROUND_PROMPT + "\n", encoding="utf-8", newline="\n")
    layout = {
        "root": str(target),
        "repo": str(repo),
        "events": str(events),
        "settings": str(settings),
        "declared_config": str(declared),
        "worktree_root": str(worktree_root),
        "lease_ack": str(lease_ack),
        "creation_lock": str(creation_lock),
        "guard_ack": str(guard_ack),
        "prompt": str(prompt),
        "execution_id": execution_id,
        "name": group_name,
        "worktree_name": worktree_name,
        "base_commit": base_commit,
        "repository_common_dir": str(repository_common_dir),
        "remote_count": 0,
        "root_contained": all(
            _is_contained(path.resolve(), target)
            for path in (repo, settings, declared, prompt, worktree_root)
        ),
        "approval_scope_sha256": None,
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
        Path(__file__).with_name("live_background.py"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
