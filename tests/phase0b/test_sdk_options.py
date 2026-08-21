from __future__ import annotations

from dataclasses import replace
from importlib.metadata import distribution
from pathlib import Path

import pytest

from spikes.phase0a.live_common import BoundCliIdentity
from spikes.phase0b.sdk_options import (
    CLAUDE_EFFORTS,
    CREDENTIAL_OVERRIDE_NAMES,
    RECURSION_DENIES,
    ManagedSpec,
    build_managed_options,
)


@pytest.fixture(autouse=True)
def _clear_credential_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_OVERRIDE_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def bound_spec(tmp_path: Path) -> tuple[ManagedSpec, BoundCliIdentity]:
    cli = tmp_path / "claude.exe"
    settings = tmp_path / "settings.json"
    cwd = tmp_path / "repo"
    cli.write_bytes(b"bound standalone")
    settings.write_text("{}", encoding="utf-8")
    cwd.mkdir()
    spec = ManagedSpec(
        cli_path=cli,
        cwd=cwd,
        settings=settings,
        model="provider-current-model",
        effort="low",
        tools=("Read", "Glob", "Grep"),
        max_turns=2,
        max_budget_usd=1.0,
    )
    return spec, BoundCliIdentity.capture(cli, version="provider-cli-current")


def test_options_map_the_exact_managed_policy(bound_spec) -> None:
    spec, identity = bound_spec

    async def can_use_tool(*_args):
        return None

    hooks = {"PreToolUse": []}
    options = build_managed_options(
        spec,
        identity,
        can_use_tool=can_use_tool,
        hooks=hooks,
        environment={"SUBAGENT_MCP_TEST_SENTINEL": "present"},
    )

    assert options.cli_path == spec.cli_path.resolve(strict=True)
    assert options.cwd == spec.cwd.resolve(strict=True)
    assert options.settings == str(spec.settings.resolve(strict=True))
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.setting_sources == ["user", "project", "local"]
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {}
    assert options.tools == list(spec.tools)
    assert options.allowed_tools == []
    assert options.disallowed_tools == list(RECURSION_DENIES)
    assert options.permission_mode == "dontAsk"
    assert options.model == spec.model
    assert options.effort == spec.effort
    assert options.thinking == {"type": "adaptive", "display": "omitted"}
    assert options.fallback_model is None
    assert options.max_turns == spec.max_turns
    assert options.max_budget_usd == spec.max_budget_usd
    assert options.can_use_tool is can_use_tool
    assert options.hooks is hooks
    assert options.include_hook_events is True
    assert options.env == {"SUBAGENT_MCP_TEST_SENTINEL": "present"}
    assert options.extra_args == {
        "autocompact": "274000",
        "prompt-suggestions": "false",
    }


def test_options_preserve_a_future_provider_model(bound_spec) -> None:
    spec, identity = bound_spec
    future = replace(
        spec,
        model="provider-future-model-2030",
        effort="xhigh",
    )

    options = build_managed_options(future, identity)

    assert options.model == future.model
    assert options.effort == future.effort
    assert options.fallback_model is None


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_options_accept_the_exact_public_claude_effort_schema(
    bound_spec,
    effort: str,
) -> None:
    spec, identity = bound_spec

    options = build_managed_options(replace(spec, effort=effort), identity)

    assert CLAUDE_EFFORTS == frozenset({"low", "medium", "high", "xhigh", "max"})
    assert options.effort == effort


def test_opaque_value_accepts_exactly_256_utf8_bytes(bound_spec) -> None:
    spec, identity = bound_spec
    boundary = replace(spec, model="é" * 128, effort="max")

    options = build_managed_options(boundary, identity)

    assert options.model == boundary.model
    assert options.effort == boundary.effort


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "   ",
        "contains\x00control",
        "contains\ncontrol",
        "a" * 257,
        "é" * 129,
    ],
)
def test_provider_native_model_fails_closed(bound_spec, invalid: str) -> None:
    spec, identity = bound_spec

    with pytest.raises(ValueError, match="provider-native model"):
        build_managed_options(replace(spec, model=invalid), identity)


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "LOW",
        "low ",
        "contains\ncontrol",
        "provider-future-effort-v2",
        "a" * 257,
    ],
)
def test_unknown_claude_effort_is_rejected(bound_spec, invalid: str) -> None:
    spec, identity = bound_spec

    with pytest.raises(ValueError, match="Claude effort"):
        build_managed_options(replace(spec, effort=invalid), identity)


@pytest.mark.parametrize("max_turns", [0, -1, True, 1.5, "2"])
def test_invalid_turn_limit_is_rejected(bound_spec, max_turns) -> None:
    spec, identity = bound_spec

    with pytest.raises(ValueError, match="max_turns"):
        build_managed_options(replace(spec, max_turns=max_turns), identity)


@pytest.mark.parametrize(
    "max_budget_usd",
    [0, -1, True, float("nan"), float("inf"), "1.0"],
)
def test_invalid_budget_cap_is_rejected(bound_spec, max_budget_usd) -> None:
    spec, identity = bound_spec

    with pytest.raises(ValueError, match="max_budget_usd"):
        build_managed_options(replace(spec, max_budget_usd=max_budget_usd), identity)


def test_missing_cli_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec
    spec.cli_path.unlink()

    with pytest.raises(ValueError, match="cli_path"):
        build_managed_options(spec, identity)


def test_missing_settings_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec
    spec.settings.unlink()

    with pytest.raises(ValueError, match="settings"):
        build_managed_options(spec, identity)


def test_missing_cwd_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec
    spec.cwd.rmdir()

    with pytest.raises(ValueError, match="cwd"):
        build_managed_options(spec, identity)


def test_changed_cli_hash_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec
    spec.cli_path.write_bytes(b"changed after binding")

    with pytest.raises(PermissionError, match="bound CLI identity"):
        build_managed_options(spec, identity)


def test_different_cli_path_is_rejected(bound_spec, tmp_path: Path) -> None:
    spec, identity = bound_spec
    substitute = tmp_path / "substitute-claude.exe"
    substitute.write_bytes(b"bound standalone")

    with pytest.raises(PermissionError, match="bound CLI identity"):
        build_managed_options(replace(spec, cli_path=substitute), identity)


def test_empty_bound_cli_version_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec

    with pytest.raises(PermissionError, match="version"):
        build_managed_options(spec, replace(identity, version=""))


def test_sdk_bundled_cli_is_rejected_even_if_presented_as_bound(bound_spec) -> None:
    spec, _identity = bound_spec
    sdk_distribution = distribution("claude-agent-sdk")
    bundled = next(
        Path(sdk_distribution.locate_file(item)).resolve(strict=True)
        for item in sdk_distribution.files or ()
        if Path(str(item)).name.casefold() in {"claude", "claude.exe"}
    )
    claimed_identity = BoundCliIdentity(
        canonical_path=str(bundled),
        sha256="0" * 64,
        file_identity=(0,),
        version="provider-cli-current",
    )

    with pytest.raises(PermissionError, match="SDK-bundled CLI"):
        build_managed_options(replace(spec, cli_path=bundled), claimed_identity)


@pytest.mark.parametrize("name", CREDENTIAL_OVERRIDE_NAMES)
def test_inherited_credential_override_is_rejected(
    bound_spec,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    spec, identity = bound_spec
    monkeypatch.setenv(name, "forbidden")

    with pytest.raises(PermissionError, match="credential override"):
        build_managed_options(spec, identity)


@pytest.mark.parametrize("name", CREDENTIAL_OVERRIDE_NAMES)
def test_caller_credential_override_is_rejected(bound_spec, name: str) -> None:
    spec, identity = bound_spec

    with pytest.raises(PermissionError, match="credential override"):
        build_managed_options(spec, identity, environment={name: "forbidden"})


def test_undeclared_mcp_server_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec

    with pytest.raises(PermissionError, match="strict empty MCP"):
        build_managed_options(
            spec,
            identity,
            mcp_servers={"unexpected": {"command": "never-run"}},
        )


def test_recursion_deny_drift_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec

    with pytest.raises(PermissionError, match="recursion deny"):
        build_managed_options(spec, identity, disallowed_tools=RECURSION_DENIES[:-1])


def test_fallback_model_is_rejected(bound_spec) -> None:
    spec, identity = bound_spec

    with pytest.raises(PermissionError, match="fallback model"):
        build_managed_options(spec, identity, fallback_model="provider-fallback")
