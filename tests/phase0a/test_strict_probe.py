import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from spikes.phase0a.strict_probe import prepare_probe


def test_prepare_probe_creates_repo_marker_and_empty_declared_config(tmp_path: Path):
    marker_script = tmp_path / "marker_mcp.py"
    marker_script.write_text("print('unused')", encoding="utf-8")
    layout = prepare_probe(tmp_path / "run", Path(sys.executable), marker_script)
    repo_config = json.loads(Path(layout["repo_mcp"]).read_text(encoding="utf-8"))
    declared = json.loads(Path(layout["declared_config"]).read_text(encoding="utf-8"))
    settings = json.loads(Path(layout["settings"]).read_text(encoding="utf-8"))
    assert "subagent_harness_mcp_phase0a_repo_marker" in repo_config["mcpServers"]
    marker_env = repo_config["mcpServers"]["subagent_harness_mcp_phase0a_repo_marker"]["env"]
    assert set(marker_env) == {
        "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER",
        "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT",
        "SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN",
    }
    assert marker_env["SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN"] == layout["marker_token"]
    assert len(layout["marker_token"]) == 32
    assert declared == {"mcpServers": {}}
    assert settings["enabledPlugins"]["codex@openai-codex"] is False
    assert settings["permissions"]["deny"] == [
        "mcp__codex__*",
        "mcp__agent_bridge__*",
        "mcp__subagent_harness_mcp__*",
    ]
    assert Path(layout["repo"], ".git").is_dir()


def test_prepare_probe_refuses_to_reuse_nonempty_live_root(tmp_path: Path) -> None:
    root = tmp_path / "live"
    root.mkdir()
    sentinel = root / "user-state.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    with pytest.raises(FileExistsError, match="fresh empty root"):
        prepare_probe(root, Path(sys.executable), Path(__file__))

    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_marker_mcp_writes_start_and_exit_acknowledgements(tmp_path: Path) -> None:
    started = tmp_path / "started.txt"
    exited = tmp_path / "exited.txt"
    marker_script = Path(__file__).parents[2] / "spikes" / "phase0a" / "marker_mcp.py"
    env = dict(os.environ)
    env["SUBAGENT_HARNESS_MCP_PHASE0A_MARKER"] = str(started)
    env["SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT"] = str(exited)
    env["SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN"] = "a" * 32

    result = subprocess.run(
        [sys.executable, str(marker_script)],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=5,
        check=False,
        shell=False,
    )

    assert result.returncode == 0
    started_record = json.loads(started.read_text(encoding="utf-8"))
    exited_record = json.loads(exited.read_text(encoding="utf-8"))
    assert started_record == exited_record
    assert started_record["ownership_token"] == "a" * 32
    assert started_record["pid"] > 0
