from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import write_live_context_fixture
from .core import read_fd_bounded, run_argv, write_json_atomic
from .hook_sink import build_hook_settings
from .live_common import (
    ApprovalScope,
    BoundCliIdentity,
    BoundExecutableFile,
    BoundExecutableManifest,
    ExecutionObservations,
    LiveCircuitResult,
    SideEffectSpec,
    approval_digest,
    claim_execution_authorization,
    consume_side_effect,
    prepare_private_runtime_group_root,
    run_stream_command,
)
from .live_host import load_bound_host_capabilities, load_bound_host_identity
from .live_init import (
    _expected_python_process_image,
    _git_checkpoint,
    assert_no_credential_overrides,
)


CONTEXT_FINAL_MARKER = "SUBAGENT_HARNESS_MCP_PHASE0A_CONTEXT_OK"
CONTEXT_PROMPT = f"Reply with exactly {CONTEXT_FINAL_MARKER} and nothing else."

_EXPECTED_MODEL = "claude-sonnet-5"
_EXPECTED_EFFORT = "low"
_REQUESTED_WINDOW_TOKENS = 274000
_REQUESTED_TRIGGER_TOKENS = 274000
_SETTING_SOURCES = "user,project,local"
_DENIED_TOOLS = (
    "mcp__codex__*",
    "mcp__agent_bridge__*",
    "mcp__subagent_harness_mcp__*",
)
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_EVENTS = 128
_STARTUP_TIMEOUT_SECONDS = 30
_POST_INIT_TIMEOUT_SECONDS = 120
_SAFE_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_DECLARED_NATIVE_FIELDS = (
    "effective_setting_sources",
    "effective_effort",
    "effective_auto_compaction_window_tokens",
    "effective_auto_compaction_trigger_percent",
    "effective_auto_compaction_trigger_formula",
    "effective_auto_compaction_trigger_tokens",
    "auto_memory_mode",
    "effective_cleanup_period",
    "claude_md_sources",
    "rule_sources",
    "skill_sources",
    "agent_sources",
    "extension_sources_attested",
    "inherited_hook_sources",
    "subagent_mcp_hook_sources",
    "declared_mcp_servers",
    "tool_allow_rules",
    "tool_deny_rules",
    "nested_agent_cap",
    "nested_agent_depth",
    "additional_directories",
    "system_preset_attested",
    "system_append_attested",
    "content_hashes",
    "attestation_sources",
)


@dataclass(frozen=True)
class ContextPaths:
    cwd: Path
    settings: Path
    empty_mcp: Path
    event_log: Path


@dataclass(frozen=True)
class MaterializedContext:
    paths: ContextPaths
    context_argv: tuple[str, ...]
    control_argv: tuple[str, ...] | None
    cli_sha256: str
    cli_version: str


def build_context_argv(cli: str | Path, paths: ContextPaths) -> tuple[str, ...]:
    return (
        str(Path(cli).resolve()),
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-hook-events",
        "--model", _EXPECTED_MODEL,
        "--effort", _EXPECTED_EFFORT,
        "--autocompact", str(_REQUESTED_WINDOW_TOKENS),
        "--setting-sources", _SETTING_SOURCES,
        "--settings", str(paths.settings.resolve()),
        "--tools", "",
        "--disallowedTools", *_DENIED_TOOLS,
        "--permission-mode", "dontAsk",
        "--prompt-suggestions", "false",
        "--strict-mcp-config",
        "--mcp-config", str(paths.empty_mcp.resolve()),
        "--no-session-persistence",
        CONTEXT_PROMPT,
    )


def materialize_context(
    root: str | Path,
    *,
    cli: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedContext:
    if bound_identity.version == "unverified" or not bound_identity.matches(cli):
        raise PermissionError("Task 2 bound CLI identity is required")
    target = prepare_private_runtime_group_root(root)
    repo = target / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Phase 0a context canary\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(
        "# Phase 0a context canary\n\nFollow the explicit user request exactly.\n",
        encoding="utf-8",
    )
    for name, argv in (
        ("git-init", ["git", "-C", str(repo), "init", "-b", "main"]),
        ("git-add", ["git", "-C", str(repo), "add", "README.md", "CLAUDE.md"]),
        (
            "git-commit",
            [
                "git", "-C", str(repo),
                "-c", "user.name=Subagent MCP Phase0a",
                "-c", "user.email=phase0a@example.invalid",
                "commit", "-m", "chore: initialize disposable context canary",
            ],
        ),
    ):
        probe = run_argv(name, argv, timeout_seconds=30)
        if probe.exit_code != 0 or probe.timed_out:
            raise RuntimeError(f"{name} failed")

    paths = ContextPaths(
        cwd=repo,
        settings=target / "settings.json",
        empty_mcp=target / "declared-empty.json",
        event_log=target / "events.jsonl",
    )
    settings = build_hook_settings(
        Path(python_exe),
        Path(hook_sink),
        paths.event_log,
        events=("InstructionsLoaded",),
        extra_args=(
            "--observer-cli", bound_identity.canonical_path,
            "--observer-cli-sha256", bound_identity.sha256,
        ),
    )
    write_json_atomic(paths.settings, settings)
    write_json_atomic(paths.empty_mcp, {"mcpServers": {}})
    return MaterializedContext(
        paths=paths,
        context_argv=build_context_argv(cli, paths),
        control_argv=None,
        cli_sha256=bound_identity.sha256,
        cli_version=bound_identity.version,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _execution_contract_digest(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_context_execution_manifest(
    materialized: MaterializedContext,
    *,
    python_exe: str | Path,
    hook_sink: str | Path,
) -> tuple[str, BoundExecutableManifest, dict[str, Any]]:
    generated = {
        "CLAUDE.md": materialized.paths.cwd / "CLAUDE.md",
        "declared-empty.json": materialized.paths.empty_mcp,
        "settings.json": materialized.paths.settings,
    }
    paths = {
        Path(materialized.context_argv[0]),
        Path(python_exe),
        Path(hook_sink),
        Path(__file__),
        Path(__file__).with_name("core.py"),
        Path(__file__).with_name("hook_sink.py"),
        Path(__file__).with_name("live_common.py"),
        Path(__file__).with_name("live_host.py"),
        Path(__file__).with_name("live_init.py"),
        Path(__file__).with_name("contracts.py"),
        Path(__file__).with_name("locking.py"),
        _expected_python_process_image(Path(python_exe)),
        *generated.values(),
    }
    entries = []
    for path in sorted((path.resolve(strict=True) for path in paths), key=str):
        identity = BoundCliIdentity.capture(path, version="unverified")
        entries.append(BoundExecutableFile(
            canonical_path=identity.canonical_path,
            sha256=identity.sha256,
            file_identity=identity.file_identity,
        ))
    manifest = BoundExecutableManifest(
        repository_id="group-b-generated",
        trust_revision=1,
        entries=tuple(entries),
    )
    output_paths = {
        "consumed-side-effects.json": materialized.paths.event_log.parent
        / "consumed-side-effects.json",
        "events.jsonl": materialized.paths.event_log,
        "live-context-candidate.json": materialized.paths.event_log.parent
        / "live-context-candidate.json",
    }
    contract = {
        "schema_version": 1,
        "file_manifest_sha256": manifest.sha256,
        "context_argv": list(materialized.context_argv),
        "control_argv": None,
        "cwd": str(materialized.paths.cwd.resolve(strict=True)),
        "observed_cli_version": materialized.cli_version,
        "final_marker": CONTEXT_FINAL_MARKER,
        "startup_timeout_seconds": _STARTUP_TIMEOUT_SECONDS,
        "post_init_timeout_seconds": _POST_INIT_TIMEOUT_SECONDS,
        "generated_file_sha256": {
            name: _sha256_file(path) for name, path in sorted(generated.items())
        },
        "mutable_outputs": {
            name: str(path.resolve()) for name, path in sorted(output_paths.items())
        },
    }
    return _execution_contract_digest(contract), manifest, contract


def _scope_payload(scope: ApprovalScope) -> dict[str, Any]:
    return json.loads(json.dumps(scope.to_dict()))


def _task2_identity_path(root: str | Path) -> Path:
    return Path(root).absolute().parent / "host" / "bound-identity.json"


def _context_exact_targets(materialized: MaterializedContext) -> tuple[str, ...]:
    root = materialized.paths.event_log.parent
    return (
        str(materialized.paths.cwd.resolve(strict=True)),
        str(materialized.paths.settings.resolve(strict=True)),
        str(materialized.paths.empty_mcp.resolve(strict=True)),
        str(materialized.paths.event_log.resolve()),
        str((root / "consumed-side-effects.json").resolve()),
        str((root / "live-context-candidate.json").resolve()),
    )


def preview_context(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
) -> dict[str, Any]:
    git_head, dirty = _git_checkpoint(project_root)
    if dirty:
        raise PermissionError("tracked checkout must be clean before preview")
    identity_path = _task2_identity_path(root)
    bound_identity = load_bound_host_identity(identity_path, cli)
    capabilities = load_bound_host_capabilities(identity_path)
    if not (
        capabilities["tools_empty_documented"]
        and capabilities["prompt_suggestions_false_documented"]
    ):
        raise PermissionError("Task 2 did not attest the exact context flags")
    materialized = materialize_context(
        root,
        cli=cli,
        python_exe=python_exe,
        hook_sink=hook_sink,
        bound_identity=bound_identity,
    )
    manifest_sha256, _manifest, contract = build_context_execution_manifest(
        materialized,
        python_exe=python_exe,
        hook_sink=hook_sink,
    )
    exact_targets = _context_exact_targets(materialized)
    scope = build_context_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        context_argv=materialized.context_argv,
        control_argv=None,
        exact_targets=exact_targets,
    )
    payload = _scope_payload(scope)
    display = {
        "scope": payload,
        "scope_sha256": approval_digest(scope),
        "execution_contract": contract,
        "plugin_control": "unsupported",
    }
    write_json_atomic(Path(root) / "pending-scope.json", payload)
    return display


def load_context(
    root: str | Path,
    *,
    cli: str | Path,
    bound_identity: BoundCliIdentity,
) -> MaterializedContext:
    target = Path(root).resolve(strict=True)
    if bound_identity.version == "unverified" or not bound_identity.matches(cli):
        raise PermissionError("Task 2 bound CLI identity is required")
    paths = ContextPaths(
        cwd=(target / "repo").resolve(strict=True),
        settings=(target / "settings.json").resolve(strict=True),
        empty_mcp=(target / "declared-empty.json").resolve(strict=True),
        event_log=target / "events.jsonl",
    )
    (paths.cwd / "CLAUDE.md").resolve(strict=True)
    return MaterializedContext(
        paths=paths,
        context_argv=build_context_argv(cli, paths),
        control_argv=None,
        cli_sha256=bound_identity.sha256,
        cli_version=bound_identity.version,
    )


def _read_events_once(path: Path) -> bytes:
    if not path.exists():
        return b""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return read_fd_bounded(fd, _MAX_EVENT_BYTES)
    finally:
        os.close(fd)


def _read_events_until(path: Path, *, timeout_seconds: float = 1.0) -> bytes:
    if timeout_seconds < 0 or timeout_seconds > 5:
        raise ValueError("InstructionsLoaded wait must be between zero and five seconds")
    deadline = time.monotonic() + timeout_seconds
    observed = b""
    while True:
        observed = _read_events_once(path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return observed
        time.sleep(min(0.01, remaining))


def _write_context_candidate(
    target: Path,
    projection: dict[str, Any],
    result: LiveCircuitResult,
    observed_cli_version: str,
    cli_sha256: str,
) -> dict[str, Any]:
    write_live_context_fixture(
        {"cli_content_sha256": cli_sha256, **projection},
        target / "live-context-candidate.json",
        observed_cli_version,
        source_sha256=result.source_sha256,
    )
    return projection


def execute_context(
    root: str | Path,
    *,
    cli: str | Path,
    project_root: str | Path,
    approval: str | Path,
    python_exe: str | Path,
    hook_sink: str | Path,
    env: Mapping[str, str] | None = None,
    confirm_usage_credits_off: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    execution_env = os.environ if env is None else env
    assert_no_credential_overrides(execution_env)
    target = Path(root).resolve(strict=True)
    identity_path = _task2_identity_path(target)
    bound_identity = load_bound_host_identity(identity_path, cli)
    capabilities = load_bound_host_capabilities(identity_path)
    if not (
        capabilities["tools_empty_documented"]
        and capabilities["prompt_suggestions_false_documented"]
    ):
        raise PermissionError("Task 2 did not attest the exact context flags")
    materialized = load_context(target, cli=cli, bound_identity=bound_identity)
    manifest_sha256, file_manifest, _contract = build_context_execution_manifest(
        materialized,
        python_exe=python_exe,
        hook_sink=hook_sink,
    )
    git_head, dirty = _git_checkpoint(project_root)
    exact_targets = _context_exact_targets(materialized)
    scope = build_context_scope(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        context_argv=materialized.context_argv,
        control_argv=None,
        exact_targets=exact_targets,
    )
    pending = json.loads((target / "pending-scope.json").read_text(encoding="utf-8"))
    if not isinstance(pending, dict) or pending != _scope_payload(scope):
        raise PermissionError("Group B preview drifted")
    observations = ExecutionObservations(
        git_head=git_head,
        cli_sha256=materialized.cli_sha256,
        executable_manifest_sha256=manifest_sha256,
        trust_revision=1,
        dirty_tracked=dirty,
    )
    repo_head, repo_dirty = _git_checkpoint(materialized.paths.cwd)
    if repo_dirty:
        raise PermissionError("Group B checkout must be clean before execution")
    with file_manifest.lease() as lease:
        authorization = claim_execution_authorization(
            scope,
            approval,
            approval_root=target.parent / "approvals",
            observations=observations,
            execution_id=f"group-b-{secrets.token_hex(8)}",
        )
        lease.verify_init_ack()
        materialized.paths.event_log.write_bytes(b"")

        def invoke_provider(argv: tuple[str, ...]) -> LiveCircuitResult:
            result = run_stream_command(
                argv,
                timeout_seconds=_STARTUP_TIMEOUT_SECONDS,
                post_init_timeout_seconds=_POST_INIT_TIMEOUT_SECONDS,
                cwd=materialized.paths.cwd,
                env=execution_env,
                expected_model=_EXPECTED_MODEL,
                requested_auto_compaction_window=_REQUESTED_WINDOW_TOKENS,
                requested_auto_compaction_trigger_tokens=_REQUESTED_TRIGGER_TOKENS,
                final_policy="exact_marker",
                final_marker=CONTEXT_FINAL_MARKER,
            )
            lease.verify_init_ack()
            return result

        def invoke_scoped(argv: tuple[str, ...]) -> LiveCircuitResult:
            kind = (
                "provider_control_launch"
                if materialized.control_argv is not None
                and argv == materialized.control_argv
                else "provider_launch"
            )
            return consume_side_effect(
                authorization,
                kind,
                {},
                target / "consumed-side-effects.json",
                invoke=invoke_provider,
            )

        def checkout_is_clean() -> bool:
            return _git_checkpoint(materialized.paths.cwd) == (repo_head, False)

        arms = run_context_arms(
            materialized.context_argv,
            control_argv=materialized.control_argv,
            invoke=invoke_scoped,
            checkout_is_clean=checkout_is_clean,
        )

    context_result = arms["context"]
    if context_result is None:
        control = arms["control"]
        if control is None:
            raise RuntimeError("Group B produced no circuit result")
        projection = project_context_result(
            control,
            instruction_observation=instruction_observation(b""),
            checkout_clean=checkout_is_clean(),
            usage_credits_off_confirmed=False,
            plugin_control={"plugin_disable_effective": "BLOCKED"},
        )
        return _write_context_candidate(
            target, projection, control, materialized.cli_version,
            materialized.cli_sha256,
        )
    checkout_clean = checkout_is_clean()
    preliminary = project_context_result(
        context_result,
        instruction_observation=instruction_observation(b""),
        checkout_clean=checkout_clean,
        usage_credits_off_confirmed=False,
    )
    confirmed = (
        preliminary["init_subset_status"] == "PASS"
        and confirm_usage_credits_off is not None
        and confirm_usage_credits_off() is True
    )
    events = instruction_observation(_read_events_until(materialized.paths.event_log))
    projection = project_context_result(
        context_result,
        instruction_observation=events,
        checkout_clean=checkout_is_clean(),
        usage_credits_off_confirmed=confirmed,
    )
    return _write_context_candidate(
        target, projection, context_result, materialized.cli_version,
        materialized.cli_sha256,
    )


def build_context_scope(
    *,
    git_head: str,
    cli_sha256: str,
    executable_manifest_sha256: str,
    context_argv: Sequence[str],
    control_argv: Sequence[str] | None,
    trust_revision: int = 1,
    exact_targets: Sequence[str] = (),
) -> ApprovalScope:
    effects: list[SideEffectSpec] = []
    if control_argv is not None:
        effects.append(SideEffectSpec(
            kind="provider_control_launch",
            argv_template=tuple(control_argv),
            bindings=(),
            max_uses=1,
            exact_targets=tuple(exact_targets),
        ))
    effects.append(SideEffectSpec(
        kind="provider_launch",
        argv_template=tuple(context_argv),
        bindings=(),
        max_uses=1,
        exact_targets=tuple(exact_targets),
    ))
    return ApprovalScope(
        schema_version=1,
        git_head=git_head,
        cli_sha256=cli_sha256,
        gate_ids=(
            "context_attestation",
            "plugin_disable_effective",
            "context_init_subset",
        ),
        side_effects=tuple(effects),
        max_provider_session_launches=len(effects),
        max_worktree_creates=0,
        max_stop_respawn_actions=0,
        max_attach_actions=0,
        max_file_deletes=0,
        max_removals=0,
        background_internal_requests_acknowledged=False,
        executable_manifest_sha256=executable_manifest_sha256,
        trust_revision=trust_revision,
    )


def instruction_observation(event_bytes: bytes) -> dict[str, Any]:
    if not isinstance(event_bytes, bytes) or len(event_bytes) > _MAX_EVENT_BYTES:
        raise ValueError("InstructionsLoaded event log exceeds its byte bound")
    try:
        text = event_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("InstructionsLoaded event log must be UTF-8") from exc
    lines = text.splitlines()
    if len(lines) > _MAX_EVENTS:
        raise ValueError("InstructionsLoaded event log exceeds its event bound")
    categories: set[str] = set()
    hashes: set[str] = set()
    reasons: set[str] = set()
    count = 0
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("InstructionsLoaded event log is malformed") from exc
        if not isinstance(item, dict):
            raise ValueError("InstructionsLoaded event must be an object")
        if item.get("hook_event_name") != "InstructionsLoaded":
            continue
        loaded = item.get("instructions_loaded")
        if not isinstance(loaded, dict):
            raise ValueError("InstructionsLoaded payload is malformed")
        count += 1
        category = loaded.get("source_category")
        content_hash = loaded.get("content_sha256")
        reason = loaded.get("load_reason")
        if not isinstance(category, str) or _SAFE_LABEL.fullmatch(category) is None:
            raise ValueError("InstructionsLoaded source category is unsafe")
        if not isinstance(reason, str) or _SAFE_LABEL.fullmatch(reason) is None:
            raise ValueError("InstructionsLoaded load reason is unsafe")
        if content_hash is not None and (
            not isinstance(content_hash, str) or _SHA256.fullmatch(content_hash) is None
        ):
            raise ValueError("InstructionsLoaded content digest is invalid")
        categories.add(category)
        reasons.add(reason)
        if content_hash is not None:
            hashes.add(content_hash)
    return {
        "delivery_observed": count > 0,
        "instruction_event_count": count,
        "source_categories": sorted(categories),
        "content_hashes": sorted(hashes),
        "load_reasons": sorted(reasons),
    }


def _structured_value(
    structured: Mapping[str, Any],
    key: str,
    observed: Any = None,
) -> tuple[Any, bool]:
    if key not in structured:
        return observed, False
    value = structured[key]
    return value, observed is not None and value != observed


def project_context_result(
    result: LiveCircuitResult,
    *,
    instruction_observation: Mapping[str, Any],
    checkout_clean: bool,
    usage_credits_off_confirmed: bool,
    structured_context: Mapping[str, Any] | None = None,
    plugin_control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    structured = {} if structured_context is None else dict(structured_context)
    projected: dict[str, Any] = {
        "terminal_classification": result.classification,
        "process_exit_code": result.exit_code,
        "init_envelope_observed": result.init_envelope_observed,
        "result_envelope_observed": result.result_envelope_observed,
        "timeout_phase": result.timeout_phase,
        "requested_model": _EXPECTED_MODEL,
        "requested_effort": _EXPECTED_EFFORT,
        "requested_setting_sources": _SETTING_SOURCES,
        "requested_auto_compaction_window_tokens": _REQUESTED_WINDOW_TOKENS,
        "requested_auto_compaction_trigger_percent": None,
        "requested_auto_compaction_trigger_tokens": _REQUESTED_TRIGGER_TOKENS,
        "effective_model": result.model,
        "tool_count": len(result.tools),
        "mcp_server_count": result.mcp_server_count,
        "is_using_overage": result.is_using_overage,
        "rate_statuses": list(result.rate_statuses),
        "final_marker_matched": result.final_marker_matched,
        "checkout_clean": checkout_clean,
        "instructions_loaded": {
            "delivery_observed": instruction_observation.get("delivery_observed") is True,
            "instruction_event_count": instruction_observation.get("instruction_event_count", 0),
            "source_categories": list(instruction_observation.get("source_categories", [])),
            "content_hashes": list(instruction_observation.get("content_hashes", [])),
            "load_reasons": list(instruction_observation.get("load_reasons", [])),
        },
        "attested_configuration": "foreground_no_tools",
        "production_equivalent_attestation": "outstanding",
        "usage_credits_off_confirmed": usage_credits_off_confirmed is True,
        "hook_error_observed": result.stderr_bytes > 0,
    }
    drift = False
    values = {
        "effective_setting_sources": None,
        "effective_effort": result.effort,
        "effective_auto_compaction_window_tokens": result.effective_auto_compaction_window,
        "effective_auto_compaction_trigger_percent": result.effective_auto_compaction_trigger_percent,
        "effective_auto_compaction_trigger_formula": None,
        "effective_auto_compaction_trigger_tokens": result.effective_auto_compaction_trigger_tokens,
        "auto_memory_mode": None,
        "effective_cleanup_period": None,
        "claude_md_sources": None,
        "rule_sources": None,
        "skill_sources": None,
        "agent_sources": None,
        "inherited_hook_sources": None,
        "subagent_mcp_hook_sources": None,
        "declared_mcp_servers": None,
        "tool_allow_rules": None,
        "tool_deny_rules": None,
        "nested_agent_cap": None,
        "nested_agent_depth": None,
        "additional_directories": None,
        "content_hashes": None,
        "attestation_sources": None,
    }
    for key, observed in tuple(values.items()):
        values[key], disagrees = _structured_value(structured, key, observed)
        drift = drift or disagrees
    projected.update(values)
    projected["extension_sources_attested"] = (
        "plugin_sources" in structured
        and structured.get("plugin_sources") is not None
    )
    projected["system_preset_attested"] = (
        "system_prompt_preset" in structured
        and structured.get("system_prompt_preset") is not None
    )
    projected["system_append_attested"] = (
        "system_prompt_append" in structured
        and structured.get("system_prompt_append") is not None
    )

    setting_sources = projected["effective_setting_sources"]
    normalized_setting_sources = (
        tuple(setting_sources) if isinstance(setting_sources, (list, tuple))
        else setting_sources
    )
    if normalized_setting_sources is not None and normalized_setting_sources not in {
        _SETTING_SOURCES, ("user", "project", "local"),
    }:
        drift = True
    expected_requested = (
        result.requested_auto_compaction_window == _REQUESTED_WINDOW_TOKENS
        and result.requested_auto_compaction_trigger_tokens == _REQUESTED_TRIGGER_TOKENS
        and result.requested_auto_compaction_trigger_percent is None
    )
    direct_safe = (
        result.classification == "success"
        and result.exit_code == 0
        and result.model == _EXPECTED_MODEL
        and result.effort in {None, _EXPECTED_EFFORT}
        and expected_requested
        and result.tools == ()
        and result.mcp_server_count == 0
        and result.is_using_overage is False
        and all(status in {"allowed", "allowed_warning"} for status in result.rate_statuses)
        and result.final_marker_matched
        and result.stderr_bytes == 0
        and checkout_clean is True
        and not drift
    )

    plugin_status = "BLOCKED"
    relative_plugin_delta = None
    if plugin_control is not None:
        plugin_status = plugin_control.get("plugin_disable_effective", "BLOCKED")
        relative_plugin_delta = plugin_control.get("relative_plugin_delta")
        if plugin_status != "PASS" or relative_plugin_delta != 1:
            direct_safe = False
    projected["plugin_disable_effective"] = plugin_status
    projected["relative_plugin_delta"] = relative_plugin_delta

    missing = [
        key for key in _DECLARED_NATIVE_FIELDS
        if projected[key] is None
    ]
    if projected["system_preset_attested"] is not True:
        missing.append("system_prompt_preset")
    if projected["system_append_attested"] is not True:
        missing.append("system_prompt_append")
    if projected["extension_sources_attested"] is not True:
        missing.append("plugin_sources")
    if projected["instructions_loaded"]["delivery_observed"] is not True:
        missing.append("instructions_loaded_delivery")
    if plugin_status != "PASS":
        missing.append("plugin_disable_effective")
    projected["missing_fields"] = sorted(set(missing))
    projected["init_subset_status"] = "PASS" if direct_safe else "BLOCKED"
    projected["status"] = (
        "BLOCKED" if not direct_safe
        else "CAPABILITY_MISSING" if projected["missing_fields"]
        else "PASS"
    )
    projected["declared_native_attestation"] = (
        "complete" if projected["status"] == "PASS" else "incomplete"
    )
    projected["background_eligible"] = (
        direct_safe and usage_credits_off_confirmed is True
    )
    return projected


def _arm_allows_next(result: LiveCircuitResult) -> bool:
    return (
        result.classification == "success"
        and result.exit_code == 0
        and result.model == _EXPECTED_MODEL
        and result.is_using_overage is False
        and all(status in {"allowed", "allowed_warning"} for status in result.rate_statuses)
        and result.stderr_bytes == 0
    )


def run_context_arms(
    context_argv: Sequence[str],
    *,
    control_argv: Sequence[str] | None,
    invoke: Callable[[tuple[str, ...]], LiveCircuitResult],
    checkout_is_clean: Callable[[], bool],
) -> dict[str, LiveCircuitResult | None]:
    def run(argv: Sequence[str]) -> LiveCircuitResult:
        if checkout_is_clean() is not True:
            raise PermissionError("checkout must be clean before Group B arm")
        value = invoke(tuple(argv))
        if checkout_is_clean() is not True:
            raise PermissionError("checkout drifted during Group B arm")
        return value

    control = None
    if control_argv is not None:
        control = run(control_argv)
        if not _arm_allows_next(control):
            return {"control": control, "context": None}
    return {"control": control, "context": run(context_argv)}


def _prompt_usage_credits_off_confirmation() -> bool:
    print(json.dumps({
        "confirmation_required": "usage_credits_remain_off",
        "enter_exactly": "CONFIRM_USAGE_CREDITS_OFF",
    }, sort_keys=True), flush=True)
    try:
        return input().strip() == "CONFIRM_USAGE_CREDITS_OFF"
    except EOFError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args(argv)
    python_exe = Path(sys.executable)
    hook_sink = Path(__file__).with_name("hook_sink.py")
    if args.preview:
        if args.approval is not None:
            parser.error("--approval is valid only with --execute")
        result = preview_context(
            args.root,
            cli=args.cli,
            project_root=args.project_root,
            python_exe=python_exe,
            hook_sink=hook_sink,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.approval is None:
        parser.error("--approval is required with --execute")
    result = execute_context(
        args.root,
        cli=args.cli,
        project_root=args.project_root,
        approval=args.approval,
        python_exe=python_exe,
        hook_sink=hook_sink,
        env=os.environ,
        confirm_usage_credits_off=_prompt_usage_credits_off_confirmation,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("init_subset_status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
