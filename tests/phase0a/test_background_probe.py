import json
from pathlib import Path

from spikes.phase0a import live_common
from spikes.phase0a.background_probe import (
    build_background_argv,
    build_background_hook_settings,
    prepare_background,
)


def test_background_argv_never_combines_bg_and_print(tmp_path: Path):
    argv = build_background_argv(
        Path("claude.exe"),
        tmp_path / "settings.json",
        tmp_path / "declared-empty.json",
        "subagent-harness-mcp-phase0a-test",
        "phase0a-c-test",
        "claude-sonnet-5",
        "low",
        "finish the disposable task",
    )
    assert argv == [
        str(Path("claude.exe").resolve()),
        "--bg",
        "--name", "subagent-harness-mcp-phase0a-test",
        "--worktree", "phase0a-c-test",
        "--model", "claude-sonnet-5",
        "--effort", "low",
        "--autocompact", "274000",
        "--setting-sources", "user,project,local",
        "--settings", str((tmp_path / "settings.json").resolve()),
        "--tools", "Read,Write,Bash",
        "--disallowedTools",
        "mcp__codex__*", "mcp__agent_bridge__*", "mcp__subagent_harness_mcp__*",
        "--permission-mode", "acceptEdits",
        "--strict-mcp-config",
        "--mcp-config", str((tmp_path / "declared-empty.json").resolve()),
        "finish the disposable task",
    ]


def test_hook_settings_include_required_events(tmp_path: Path):
    settings = build_background_hook_settings(
        Path("python.exe"),
        Path("hook_sink.py"),
        Path("worktree_hook.py"),
        Path("live_background.py"),
        tmp_path / "events.jsonl",
        tmp_path / "repo",
        tmp_path / "worktrees",
        tmp_path / "lease.json",
        tmp_path / "create.lock",
        tmp_path / "guard.jsonl",
        "execution-1",
    )
    assert set(settings["hooks"]) == {
        "SessionStart",
        "WorktreeCreate",
        "WorktreeRemove",
        "PreToolUse",
        "Stop",
        "StopFailure",
    }
    assert settings["worktree"] == {"baseRef": "head"}
    assert settings["enabledPlugins"]["codex@openai-codex"] is False
    assert settings["enabledPlugins"]["bridge@agent-bridge"] is False
    handler = settings["hooks"]["WorktreeCreate"][0]["hooks"][0]
    assert handler == {
        "type": "command",
        "command": str(Path("python.exe").resolve()),
        "args": [
            str(Path("worktree_hook.py").resolve()),
            "--repo",
            str((tmp_path / "repo").resolve()),
            "--worktree-root",
            str((tmp_path / "worktrees").resolve()),
            "--event-log",
            str((tmp_path / "events.jsonl").resolve()),
            "--lease-ack",
            str((tmp_path / "lease.json").resolve()),
            "--creation-lock",
            str((tmp_path / "create.lock").resolve()),
            "--execution-id",
            "execution-1",
        ],
        "timeout": 180,
    }
    guard = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert guard == {
        "type": "command",
        "command": str(Path("python.exe").resolve()),
        "args": [
            str(Path("live_background.py").resolve()),
            "pretool-guard",
            "--lease-ack", str((tmp_path / "lease.json").resolve()),
            "--event-log", str((tmp_path / "events.jsonl").resolve()),
            "--guard-ack", str((tmp_path / "guard.jsonl").resolve()),
            "--worktree-root", str((tmp_path / "worktrees").resolve()),
            "--execution-id", "execution-1",
            "--proof-relative", "phase0a-proof.txt",
        ],
        "timeout": 30,
    }


def test_prepare_background_creates_disposable_repo(tmp_path: Path):
    sink = tmp_path / "hook_sink.py"
    sink.write_text("raise SystemExit(0)", encoding="utf-8")
    worktree = tmp_path / "worktree_hook.py"
    worktree.write_text("raise SystemExit(0)", encoding="utf-8")
    layout = prepare_background(
        tmp_path / "background",
        Path("python.exe"),
        sink,
        worktree,
        tmp_path / "live_background.py",
    )
    assert Path(layout["repo"], ".git").is_dir()
    assert Path(layout["settings"]).is_file()
    assert Path(layout["declared_config"]).is_file()
    assert Path(layout["worktree_root"]).is_dir()
    assert not Path(layout["lease_ack"]).exists()
    assert Path(layout["creation_lock"]).name == "repository-create.lock"
    assert Path(layout["guard_ack"]).name == "pretool-guard.jsonl"
    assert Path(layout["prompt"]).read_text(encoding="utf-8").endswith("Do not commit, add a remote, push, merge, or modify anything outside this worktree.\n")
    assert layout["remote_count"] == 0
    assert len(layout["base_commit"]) == 40
    assert Path(layout["repository_common_dir"]).resolve() == Path(layout["repo"], ".git").resolve()
    assert layout["root_contained"] is True
    assert layout["approval_scope_sha256"] is None
    assert len(layout["execution_id"]) == 32
    assert layout["execution_id"] != Path(layout["root"]).parent.name
    live_common._verify_private_path(Path(layout["root"]), directory=True)
    settings = json.loads(Path(layout["settings"]).read_text(encoding="utf-8"))
    assert settings["worktree"]["baseRef"] == "head"


def test_prepare_background_rejects_nonempty_root_without_mutating_it(tmp_path: Path):
    root = tmp_path / "existing"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    import pytest

    with pytest.raises(FileExistsError, match="fresh empty root"):
        prepare_background(
            root,
            Path("python.exe"),
            tmp_path / "hook_sink.py",
            tmp_path / "worktree_hook.py",
            tmp_path / "live_background.py",
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
