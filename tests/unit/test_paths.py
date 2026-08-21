from pathlib import Path

import pytest

from subagent_harness_mcp.paths import PathResolutionError, resolve_paths


def test_override_resolves_exact_product_children_without_touching_disk(
    tmp_path: Path,
) -> None:
    home = tmp_path / "portable-home"

    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(home)},
        os_name="nt",
    )

    assert paths.config_dir == home / "config"
    assert paths.state_dir == home / "state"
    assert paths.data_dir == home / "data"
    assert paths.config_file == home / "config" / "config.json"
    assert paths.database_file == home / "state" / "state.db"
    assert not home.exists()


def test_windows_defaults_use_only_explicit_appdata_roots(tmp_path: Path) -> None:
    roaming = tmp_path / "roaming"
    local = tmp_path / "local"

    paths = resolve_paths(
        {"APPDATA": str(roaming), "LOCALAPPDATA": str(local)},
        os_name="nt",
    )

    assert paths.config_dir == roaming / "SubagentMCP"
    assert paths.state_dir == local / "SubagentMCP"
    assert paths.data_dir == local / "SubagentMCP"
    assert not roaming.exists()
    assert not local.exists()


@pytest.mark.parametrize(
    "env",
    [
        {"SUBAGENT_MCP_HOME": ""},
        {"SUBAGENT_MCP_HOME": "relative-home"},
        {},
        {"APPDATA": "relative", "LOCALAPPDATA": "relative"},
    ],
)
def test_ambiguous_windows_roots_fail_closed_without_home_fallback(
    env: dict[str, str],
) -> None:
    with pytest.raises(PathResolutionError) as captured:
        resolve_paths(env, os_name="nt")

    assert captured.value.code == "PATH_UNRESOLVED"


def test_drive_or_filesystem_root_override_is_rejected(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)

    with pytest.raises(PathResolutionError) as captured:
        resolve_paths({"SUBAGENT_MCP_HOME": str(root)}, os_name="nt")

    assert captured.value.code == "PATH_UNSAFE"


def test_preview_without_override_rejects_non_windows_platform() -> None:
    with pytest.raises(PathResolutionError) as captured:
        resolve_paths({}, os_name="posix")

    assert captured.value.code == "PLATFORM_UNSUPPORTED"

