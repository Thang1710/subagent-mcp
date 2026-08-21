from __future__ import annotations

import argparse
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

from .core import run_argv, write_json_atomic


def prepare_probe(root: Path, python_exe: Path, marker_script: Path) -> dict[str, Any]:
    supplied = root.absolute()
    if supplied.exists():
        attributes = getattr(supplied.stat(follow_symlinks=False), "st_file_attributes", 0)
        if supplied.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise PermissionError("probe root must not be a symlink or reparse point")
        if not supplied.is_dir() or any(supplied.iterdir()):
            raise FileExistsError("probe materialization requires a fresh empty root")
    target = supplied.resolve()
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
    marker_exit = target / "repo-mcp-exited.txt"
    marker_token = secrets.token_hex(16)
    repo_mcp = repo / ".mcp.json"
    declared = target / "declared-empty.json"
    settings = target / "settings.json"
    write_json_atomic(repo_mcp, {
        "mcpServers": {
            "subagent_harness_mcp_phase0a_repo_marker": {
                "type": "stdio",
                "command": str(python_exe.resolve()),
                "args": [str(marker_script.resolve())],
                "env": {
                    "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER": str(marker.resolve()),
                    "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT": str(marker_exit.resolve()),
                    "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN": marker_token,
                },
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
                "mcp__codex__*",
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
        "marker_exit": str(marker_exit),
        "marker_token": marker_token,
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
