from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class PathResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProductPaths:
    config_dir: Path
    state_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def database_file(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def ui_control_file(self) -> Path:
        return self.state_dir / "ui-control.json"


def resolve_paths(
    env: Mapping[str, str] | None = None,
    *,
    os_name: str | None = None,
) -> ProductPaths:
    """Resolve product-owned roots without creating or opening anything."""

    source = os.environ if env is None else env
    platform = os.name if os_name is None else os_name
    if "SUBAGENT_MCP_HOME" in source:
        home = _absolute_root(source["SUBAGENT_MCP_HOME"], "SUBAGENT_MCP_HOME")
        return ProductPaths(home / "config", home / "state", home / "data")

    if platform != "nt":
        raise PathResolutionError(
            "PLATFORM_UNSUPPORTED",
            "The Windows release requires SUBAGENT_MCP_HOME off Windows",
        )
    try:
        app_data = source["APPDATA"]
        local_app_data = source["LOCALAPPDATA"]
    except KeyError as exc:
        raise PathResolutionError(
            "PATH_UNRESOLVED",
            "APPDATA and LOCALAPPDATA are required",
        ) from exc
    config = _absolute_root(app_data, "APPDATA") / "SubagentMCP"
    local = _absolute_root(local_app_data, "LOCALAPPDATA") / "SubagentMCP"
    return ProductPaths(config, local, local)


def _absolute_root(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PathResolutionError("PATH_UNRESOLVED", f"{label} must be nonempty")
    raw = Path(value)
    if not raw.is_absolute():
        raise PathResolutionError("PATH_UNRESOLVED", f"{label} must be absolute")
    resolved = raw.resolve(strict=False)
    anchor = Path(resolved.anchor)
    if resolved == anchor:
        raise PathResolutionError("PATH_UNSAFE", f"{label} cannot be a filesystem root")
    return resolved
