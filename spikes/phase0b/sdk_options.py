from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import distribution
import math
import os
from pathlib import Path
from typing import Any, get_args
import unicodedata

from claude_agent_sdk import ClaudeAgentOptions, EffortLevel

from spikes.phase0a.live_common import BoundCliIdentity


CREDENTIAL_OVERRIDE_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

RECURSION_DENIES = (
    "mcp__codex__*",
    "mcp__agent_bridge__*",
    "mcp__subagent_harness_mcp__*",
)

_REVIEWED_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
if get_args(EffortLevel) != _REVIEWED_CLAUDE_EFFORTS:
    raise RuntimeError("Claude SDK effort schema differs from the reviewed adapter pair")
CLAUDE_EFFORTS = frozenset(_REVIEWED_CLAUDE_EFFORTS)


@dataclass(frozen=True)
class ManagedSpec:
    cli_path: Path
    cwd: Path
    settings: Path
    model: str
    effort: str
    tools: tuple[str, ...]
    max_turns: int
    max_budget_usd: float


def _validate_opaque_provider_value(label: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid provider-native {label}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"invalid provider-native {label}") from error
    if len(encoded) > 256 or any(
        unicodedata.category(character) == "Cc" for character in value
    ):
        raise ValueError(f"invalid provider-native {label}")
    return value


def _existing_file(path: Path, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as error:
        raise ValueError(f"managed {label} must be an existing file") from error
    if not resolved.is_file():
        raise ValueError(f"managed {label} must be an existing file")
    return resolved


def _existing_directory(path: Path, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as error:
        raise ValueError(f"managed {label} must be an existing directory") from error
    if not resolved.is_dir():
        raise ValueError(f"managed {label} must be an existing directory")
    return resolved


def _sdk_owned_cli_paths() -> tuple[Path, ...]:
    sdk_distribution = distribution("claude-agent-sdk")
    paths: list[Path] = []
    for item in sdk_distribution.files or ():
        if Path(str(item)).name.casefold() not in {"claude", "claude.exe"}:
            continue
        try:
            candidate = Path(sdk_distribution.locate_file(item)).resolve(strict=True)
        except OSError:
            continue
        if candidate.is_file():
            paths.append(candidate)
    return tuple(paths)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _assert_no_credential_overrides(environment: Mapping[str, str]) -> None:
    override_names = set(CREDENTIAL_OVERRIDE_NAMES)
    for source in (os.environ, environment):
        if any(
            isinstance(name, str)
            and name.upper() in override_names
            and bool(value)
            for name, value in source.items()
        ):
            raise PermissionError("credential override blocks managed SDK options")


def build_managed_options(
    spec: ManagedSpec,
    bound_identity: BoundCliIdentity,
    *,
    can_use_tool: Any = None,
    hooks: Any = None,
    environment: Mapping[str, str] | None = None,
    mcp_servers: Mapping[str, object] | None = None,
    disallowed_tools: Sequence[str] = RECURSION_DENIES,
    fallback_model: str | None = None,
) -> ClaudeAgentOptions:
    model = _validate_opaque_provider_value("model", spec.model)
    if not isinstance(spec.effort, str) or spec.effort not in CLAUDE_EFFORTS:
        raise ValueError("invalid Claude effort for the reviewed SDK schema")
    effort = spec.effort
    if (
        isinstance(spec.max_turns, bool)
        or not isinstance(spec.max_turns, int)
        or spec.max_turns < 1
    ):
        raise ValueError("max_turns must be a positive integer cap")
    if (
        isinstance(spec.max_budget_usd, bool)
        or not isinstance(spec.max_budget_usd, (int, float))
        or not math.isfinite(float(spec.max_budget_usd))
        or spec.max_budget_usd <= 0
    ):
        raise ValueError("max_budget_usd must be a positive finite cap")

    cli_path = _existing_file(spec.cli_path, "cli_path")
    settings = _existing_file(spec.settings, "settings")
    cwd = _existing_directory(spec.cwd, "cwd")
    if any(_same_path(cli_path, bundled) for bundled in _sdk_owned_cli_paths()):
        raise PermissionError("SDK-bundled CLI cannot satisfy standalone binding")
    if not isinstance(bound_identity.version, str) or not bound_identity.version.strip():
        raise PermissionError("bound CLI version is missing")
    if not bound_identity.matches(cli_path):
        raise PermissionError("bound CLI identity mismatch")

    explicit_environment = {} if environment is None else dict(environment)
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in explicit_environment.items()
    ):
        raise ValueError("managed environment must contain string keys and values")
    _assert_no_credential_overrides(explicit_environment)
    if mcp_servers:
        raise PermissionError("strict empty MCP policy rejects undeclared servers")
    if tuple(disallowed_tools) != RECURSION_DENIES:
        raise PermissionError("recursion deny policy drifted")
    if fallback_model is not None:
        raise PermissionError("fallback model must remain disabled")

    return ClaudeAgentOptions(
        cli_path=cli_path,
        cwd=cwd,
        settings=str(settings),
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user", "project", "local"],
        strict_mcp_config=True,
        mcp_servers={},
        tools=list(spec.tools),
        disallowed_tools=list(RECURSION_DENIES),
        permission_mode="dontAsk",
        model=model,
        effort=effort,
        thinking={"type": "adaptive", "display": "omitted"},
        fallback_model=None,
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        can_use_tool=can_use_tool,
        hooks=hooks,
        include_hook_events=True,
        env=explicit_environment,
        extra_args={
            "autocompact": "274000",
            "prompt-suggestions": "false",
        },
    )
