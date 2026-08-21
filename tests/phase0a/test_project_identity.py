from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _join(*parts: str) -> str:
    return "".join(parts)


OLD_IDENTITY_TOKENS = (
    _join("harness", "bridge"),
    _join("harness", "-bridge"),
    _join("harness", "_bridge"),
    _join("harness", "-meta"),
    _join("hb", "_phase0a"),
    _join("hb", "-phase0a"),
    _join("hb", "-control"),
    _join("x", "-hb-token"),
    _join("mcp__", "harness", "_bridge", "__"),
)


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            f"-c",
            f"safe.directory={REPOSITORY_ROOT.as_posix()}",
            "ls-files",
            "-z",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / Path(path.decode("utf-8"))
        for path in result.stdout.split(b"\0")
        if path
    ]


def _project_name(pyproject_text: str) -> str | None:
    in_project = False
    for raw_line in pyproject_text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project and line.startswith("name"):
            _, value = line.split("=", maxsplit=1)
            return value.strip().strip('"')
    return None


def test_tracked_files_use_only_the_subagent_mcp_identity() -> None:
    violations: list[str] = []
    for path in _tracked_files():
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        lowered_path = relative_path.lower()
        path_matches = [token for token in OLD_IDENTITY_TOKENS if token in lowered_path]
        if path_matches:
            violations.append(f"path {relative_path}: {path_matches}")

        content = path.read_bytes()
        if b"\0" in content:
            continue
        lowered_text = content.decode("utf-8", errors="replace").lower()
        text_matches = [token for token in OLD_IDENTITY_TOKENS if token in lowered_text]
        if text_matches:
            violations.append(f"text {relative_path}: {text_matches}")

    assert not violations, "\n".join(violations)


def test_canonical_project_identity_metadata_and_documents() -> None:
    pyproject_text = (REPOSITORY_ROOT / "pyproject.toml").read_text("utf-8")
    assert _project_name(pyproject_text) == "subagent-harness-mcp"

    design = REPOSITORY_ROOT / "docs/superpowers/specs/2026-08-17-subagent-mcp-design.md"
    live_plan = REPOSITORY_ROOT / "docs/superpowers/plans/2026-08-20-subagent-mcp-phase-0a-live-gates.md"
    hardening_plan = REPOSITORY_ROOT / "docs/superpowers/plans/2026-08-18-subagent-mcp-phase-0a-hardening.md"
    expected_paths = (
        design,
        REPOSITORY_ROOT / "docs/superpowers/plans/2026-08-17-subagent-mcp-phase-0a.md",
        REPOSITORY_ROOT / "docs/superpowers/plans/2026-08-18-subagent-mcp-phase-0a-correction.md",
        hardening_plan,
        live_plan,
    )
    assert all(path.exists() for path in expected_paths)

    old_design_name = _join("2026-08-17-", "harness", "bridge-design.md")
    old_live_plan_name = _join("2026-08-20-", "harness", "bridge-phase-0a-live-gates.md")
    assert not (REPOSITORY_ROOT / "docs/superpowers/specs" / old_design_name).exists()
    assert not (REPOSITORY_ROOT / "docs/superpowers/plans" / old_live_plan_name).exists()

    assert (REPOSITORY_ROOT / "CLAUDE.md").read_text("utf-8").startswith("# Subagent MCP")
    design_text = design.read_text("utf-8")
    assert "SUBAGENT_MCP_HOME" in design_text
    assert "SubagentMCP" in design_text
    assert "subagent_harness_mcp.adapters" in design_text
    assert "X-Subagent-MCP-Token" in design_text
    assert "SubagentMcpService" in design_text
    assert 'M["Subagent MCP MCP"]' not in design_text
    assert "| Project structure | New Subagent MCP, separate from AgentBridge |" in design_text

    live_plan_text = live_plan.read_text("utf-8")
    assert "mcp__subagent_harness_mcp__*" in live_plan_text
    assert _join("mcp__", "harness", "_bridge", "__") not in live_plan_text
    assert "docs/superpowers/plans/2026-08-20-subagent-mcp-phase-0a-live-gates.md" in hardening_plan.read_text("utf-8")
