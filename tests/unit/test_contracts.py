from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import dataclasses

from subagent_harness_mcp.contracts import (
    ADAPTER_API_VERSION,
    AdapterManifest,
    AgentDescriptor,
    AgentEvent,
    AgentStatus,
    ArtifactReference,
    ContractError,
    ExecutionState,
    PROMPT_MAX_BYTES,
    ResultReadRequest,
    ROUGH_TOKEN_ESTIMATE_BASIS,
    SendRequest,
    ServiceError,
    SpawnRequest,
    TaskInput,
    TaskPacket,
    WaitRequest,
    WaitTarget,
    require_execution_transition,
    result_artifact_metadata,
    rough_token_estimate,
    slice_transfer_metrics,
    validate_model_id,
)


def _manifest() -> AdapterManifest:
    return AdapterManifest(
        adapter_api_version=ADAPTER_API_VERSION,
        runtime_id="future-harness",
        provider_id="future-provider",
        harness_id="future-native-harness",
        display_name="Future sub-agent",
        adapter_version="1.2.3",
        supported_platforms=("win32",),
        supported_transports=("managed-sdk",),
        capabilities=frozenset({"session", "resume", "interrupt"}),
        semantic_permissions=frozenset({"repo_read", "workspace_write"}),
        reasoning_schema={"type": "object"},
        model_schema={
            "anyOf": [
                {"const": "vendor/model", "title": "Vendor model"},
                {"type": "string", "minLength": 1, "title": "Custom model"},
            ]
        },
    )


def test_model_id_is_opaque_exact_and_bounded() -> None:
    model = "vendor/future-model:preview-01"

    assert validate_model_id(model) == model
    with pytest.raises(ContractError) as control:
        validate_model_id("vendor/model\nunsafe")
    with pytest.raises(ContractError) as oversized:
        validate_model_id("m" * 257)

    assert control.value.code == "REQUEST_INVALID"
    assert oversized.value.code == "REQUEST_INVALID"


def test_execution_transition_rejects_terminal_revival() -> None:
    require_execution_transition(ExecutionState.RUNNING, ExecutionState.SUCCEEDED)

    with pytest.raises(ContractError) as captured:
        require_execution_transition(ExecutionState.SUCCEEDED, ExecutionState.RUNNING)

    assert captured.value.code == "STATE_CONFLICT"


def test_spawn_write_set_is_relative_bounded_and_requires_workspace_write() -> None:
    task = TaskPacket("Implement", "Change one lane.", ("Done",), "writer")
    valid = SpawnRequest(
        "request-1",
        "future-harness",
        "future",
        task,
        ".",
        "implement",
        permissions=("repo_read", "workspace_write"),
        write_set=("src/context", "docs/status.md"),
    )

    assert valid.write_set == ("src/context", "docs/status.md")
    for invalid in (
        r"C:\outside",
        "../outside",
        "/outside",
        "src/../../outside",
        "src/file.txt:stream",
        "src/CON",
        "src/trailing.",
        "src/trailing ",
    ):
        with pytest.raises(ContractError, match="write_set"):
            SpawnRequest(
                "request-2",
                "future-harness",
                "future",
                task,
                ".",
                "implement",
                permissions=("repo_read", "workspace_write"),
                write_set=(invalid,),
            )
    with pytest.raises(ContractError, match="workspace_write"):
        SpawnRequest(
            "request-3",
            "future-harness",
            "future",
            task,
            ".",
            "review",
            permissions=("repo_read",),
            write_set=("src",),
        )


def test_task_input_requires_safe_relative_path_and_lowercase_sha256() -> None:
    item = TaskInput("docs/specs/review.md", "a" * 64)

    assert item.to_dict() == {
        "path": "docs/specs/review.md",
        "expected_sha256": "a" * 64,
    }
    for invalid_path in (
        "/absolute.md",
        r"C:\absolute.md",
        "../escape.md",
        "docs/../escape.md",
        "docs//empty.md",
    ):
        with pytest.raises(ContractError, match="repository-relative"):
            TaskInput(invalid_path, "a" * 64)
    for invalid_hash in ("A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(ContractError, match="lowercase SHA-256"):
            TaskInput("docs/specs/review.md", invalid_hash)


def test_task_packet_rejects_duplicate_or_excess_input_paths() -> None:
    item = TaskInput("docs/specs/review.md", "a" * 64)
    packet = TaskPacket(
        "Review", "Review it.", ("Decide.",), "reviewer", inputs=(item,)
    )

    assert packet.inputs == (item,)
    with pytest.raises(ContractError, match="unique"):
        dataclasses.replace(packet, inputs=(item, item))
    with pytest.raises(ContractError, match="at most"):
        dataclasses.replace(
            packet,
            inputs=tuple(
                TaskInput(f"docs/specs/{index}.md", "a" * 64)
                for index in range(17)
            ),
        )

    with pytest.raises(ContractError, match="unique"):
        SendRequest(
            "send-duplicate-input",
            "conversation-1",
            "Review again.",
            inputs=(item, item),
        )


def test_descriptor_shape_is_provider_neutral_and_model_agnostic() -> None:
    descriptor = AgentDescriptor.from_manifest(
        _manifest(),
        model="vendor/future-model:preview-01",
        transport="managed-sdk",
        capability_gaps=("needs_input",),
    )

    payload = descriptor.to_dict()

    assert payload["runtime_id"] == "future-harness"
    assert payload["provider_id"] == "future-provider"
    assert payload["harness_id"] == "future-native-harness"
    assert payload["model_display_name"] == "vendor/future-model:preview-01"
    assert payload["capability_gaps"] == ["needs_input"]
    assert payload["icon"]["kind"] == "monogram"
    assert payload["ui_surfaces"]["native_host_panel"] == "unsupported"


def test_descriptor_icon_has_deterministic_monogram_and_tone() -> None:
    first = AgentDescriptor.from_manifest(
        _manifest(), model="vendor/model", transport="managed-sdk"
    ).to_dict()["icon"]
    second = AgentDescriptor.from_manifest(
        _manifest(), model="vendor/model", transport="managed-sdk"
    ).to_dict()["icon"]

    assert first == second
    assert first["kind"] == "monogram"
    assert first["text"] == "F"
    assert first["tone"] in {"blue", "green", "purple", "teal"}


def test_adapter_manifest_serializes_optional_model_schema() -> None:
    payload = _manifest().to_dict()

    assert payload["model_schema"]["anyOf"][0] == {
        "const": "vendor/model",
        "title": "Vendor model",
    }


def test_manifest_exposes_bounded_write_root_limit_with_safe_default() -> None:
    manifest = _manifest()

    assert manifest.max_write_roots_per_session == 1
    assert manifest.write_root_mode == "path-prefix"
    payload = manifest.to_dict()
    assert payload["max_write_roots_per_session"] == 1
    assert payload["write_root_mode"] == "path-prefix"

    widest = dataclasses.replace(manifest, max_write_roots_per_session=32)
    assert widest.max_write_roots_per_session == 32
    assert widest.to_dict()["max_write_roots_per_session"] == 32

    for invalid in (0, -1, 33, True, "2", 2.0, None):
        with pytest.raises(ContractError):
            dataclasses.replace(manifest, max_write_roots_per_session=invalid)

    directory_only = dataclasses.replace(
        manifest, write_root_mode="existing-directory"
    )
    assert directory_only.to_dict()["write_root_mode"] == "existing-directory"
    for invalid in ("directory", "file", "", None, True, [], {}):
        with pytest.raises(ContractError):
            dataclasses.replace(manifest, write_root_mode=invalid)


def test_built_in_fake_adapter_advertises_full_multi_root_support() -> None:
    from subagent_harness_mcp.adapters.fake import FakeAdapter

    assert FakeAdapter().manifest.max_write_roots_per_session == 32


def test_service_error_recovery_directive_is_fixed_omitted_or_exact() -> None:
    plain = ServiceError("CAPABILITY_MISSING", "no recovery path")

    assert plain.recovery is None
    assert "recovery" not in plain.to_dict()

    directive = {
        "action": "repair",
        "reason": "decompose_write_set",
        "max_attempts": 3,
        "max_write_roots_per_session": 1,
    }
    bounded = ServiceError(
        "CAPABILITY_MISSING",
        "too many write roots",
        category="capability",
        retryable=False,
        recovery=dict(directive),
    )

    assert bounded.recovery == directive
    assert bounded.retryable is False
    assert bounded.to_dict()["recovery"] == directive

    retry_directive = {
        "action": "retry",
        "reason": "transient_pre_provider",
        "max_attempts": 3,
    }
    retryable = ServiceError(
        "WRITE_SET_BUSY",
        "writer scope is temporarily busy",
        category="state",
        retryable=True,
        recovery=retry_directive,
    )
    assert retryable.to_dict()["recovery"] == retry_directive

    root_mode_directive = {
        "action": "repair",
        "reason": "select_supported_write_root",
        "max_attempts": 3,
        "max_write_roots_per_session": 1,
        "write_root_mode": "existing-directory",
    }
    root_mode = ServiceError(
        "CAPABILITY_MISSING",
        "runtime requires an existing directory write root",
        category="capability",
        retryable=False,
        recovery=root_mode_directive,
    )
    assert root_mode.to_dict()["recovery"] == root_mode_directive

    for broken in (
        {"action": "refresh", "reason": "decompose_write_set", "max_attempts": 3},
        {"action": "retry", "reason": "decompose_write_set", "max_attempts": 3},
        {"action": "repair", "reason": "transient_pre_provider", "max_attempts": 3},
        {"action": "repair", "reason": "unknown_reason", "max_attempts": 3},
        {"action": "repair", "reason": "decompose_write_set", "max_attempts": 4},
        {"action": "repair", "reason": "decompose_write_set", "max_attempts": 0},
        {"action": "repair", "reason": "decompose_write_set", "max_attempts": True},
        {"action": "repair", "reason": "decompose_write_set"},
        {
            "action": "repair",
            "reason": "decompose_write_set",
            "max_attempts": 3,
            "max_write_roots_per_session": 33,
        },
        {
            "action": "repair",
            "reason": "decompose_write_set",
            "max_attempts": 3,
            "max_write_roots_per_session": 0,
        },
        {
            "action": "repair",
            "reason": "decompose_write_set",
            "max_attempts": 3,
            "arbitrary_field": "must-not-passthrough",
        },
        "repair",
    ):
        with pytest.raises(ContractError):
            ServiceError("CAPABILITY_MISSING", "bad directive", recovery=broken)


def test_agent_status_compact_projection_uses_result_artifact_metadata() -> None:
    text = "native progress CAPSULE: bounded answer\nDETAILS:\ncomplete provider evidence"
    result = {"text": text, "model": "vendor/model"}
    status = AgentStatus(
        conversation_id="conversation-1",
        execution_id="execution-1",
        external_session_id="native-session-1",
        workspace_path=r"C:\private\workspace",
        conversation_state="idle",
        execution_state="succeeded",
        state_revision=4,
        descriptor=AgentDescriptor.from_manifest(
            _manifest(),
            model="vendor/model",
            transport="managed-sdk",
        ),
        result=result,
        needs_input=(),
        events=(
            AgentEvent(
                cursor=7,
                kind="completed",
                payload={"result": result},
            ),
        ),
        next_event_cursor=7,
    )

    payload = status.to_compact_dict()

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert payload == {
        "conversation_id": "conversation-1",
        "execution_id": "execution-1",
        "external_session_id": "native-session-1",
        "conversation_state": "idle",
        "execution_state": "succeeded",
        "status": "succeeded",
        "state_revision": 4,
        "next_event_cursor": 7,
        "result": {
            "artifact": {
                "artifact_id": f"result:execution-1:{digest}",
                "execution_id": "execution-1",
                "sha256": digest,
                "char_count": len(text),
                "capsule": "bounded answer",
                "transfer_metrics": {
                    "basis": ROUGH_TOKEN_ESTIMATE_BASIS,
                    "full_utf8_bytes": len(text.encode("utf-8")),
                    "compact_utf8_bytes": len("bounded answer".encode("utf-8")),
                    "rough_tokens_full": rough_token_estimate(
                        len(text.encode("utf-8"))
                    ),
                    "rough_tokens_compact": rough_token_estimate(
                        len("bounded answer".encode("utf-8"))
                    ),
                    "rough_tokens_saved": (
                        rough_token_estimate(len(text.encode("utf-8")))
                        - rough_token_estimate(len("bounded answer".encode("utf-8")))
                    ),
                },
            }
        },
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    assert "complete provider evidence" not in encoded
    assert len(encoded.encode()) <= 2048


def _running_status() -> AgentStatus:
    return AgentStatus(
        conversation_id="conversation-running",
        execution_id="execution-running",
        external_session_id="native-running",
        workspace_path="workspace",
        conversation_state="active",
        execution_state="running",
        state_revision=1,
        descriptor=AgentDescriptor.from_manifest(
            _manifest(), model="vendor/model", transport="managed-sdk"
        ),
        result=None,
        needs_input=(),
        events=(),
        next_event_cursor=1,
    )


def test_running_status_tells_fresh_controllers_to_continue_waiting() -> None:
    status = _running_status()

    assert status.to_compact_dict()["wait_policy"] == "continue_while_running"
    assert status.to_dict()["wait_policy"] == "continue_while_running"


@pytest.mark.parametrize(
    ("execution_state", "conversation_state"),
    (
        ("queued", "open"),
        ("starting", "active"),
        ("needs_input", "needs_input"),
        ("succeeded", "idle"),
        ("failed", "idle"),
        ("cancelled", "idle"),
        ("interrupted", "idle"),
    ),
)
def test_non_running_status_omits_wait_policy(
    execution_state: str, conversation_state: str
) -> None:
    status = dataclasses.replace(
        _running_status(),
        execution_state=execution_state,
        conversation_state=conversation_state,
    )

    assert "wait_policy" not in status.to_compact_dict()
    assert "wait_policy" not in status.to_dict()


def test_agent_status_compact_projection_keeps_terminal_error_direct() -> None:
    status = AgentStatus(
        conversation_id="conversation-error",
        execution_id="execution-error",
        external_session_id=None,
        workspace_path="workspace",
        conversation_state="idle",
        execution_state="failed",
        state_revision=1,
        descriptor=AgentDescriptor.from_manifest(
            _manifest(), model="vendor/model", transport="managed-sdk"
        ),
        result={"error": {"code": "PROVIDER_ERROR", "message": "turn failed"}},
        needs_input=(),
        events=(),
        next_event_cursor=1,
    )

    payload = status.to_compact_dict()
    assert payload["result"] == status.result
    assert payload["execution_id"] == "execution-error"
    assert "external_session_id" not in payload


def test_result_read_request_requires_exact_hash_and_bounded_character_slice() -> None:
    request = ResultReadRequest(
        "conversation-1", "execution-1", "a" * 64, offset=7, limit=8192
    )

    assert request.offset == 7
    assert request.limit == 8192
    with pytest.raises(ContractError, match="sha256"):
        ResultReadRequest("conversation-1", "execution-1", "not-a-hash")
    with pytest.raises(ContractError, match="sha256"):
        ResultReadRequest("conversation-1", "execution-1", None)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="limit"):
        ResultReadRequest("conversation-1", "execution-1", "a" * 64, limit=8193)


def test_agent_status_compact_projection_keeps_actionable_optional_state() -> None:
    status = AgentStatus(
        conversation_id="conversation-2",
        execution_id="execution-2",
        external_session_id=None,
        workspace_path="workspace",
        conversation_state="needs_input",
        execution_state="needs_input",
        state_revision=2,
        descriptor=AgentDescriptor.from_manifest(
            _manifest(),
            model="vendor/model",
            transport="managed-sdk",
        ),
        result=None,
        needs_input=({"id": "question-1", "prompt": "Choose one."},),
        events=(),
        next_event_cursor=3,
        recovery_required=True,
    )

    payload = status.to_compact_dict()

    assert payload["needs_input"] == [
        {"id": "question-1", "prompt": "Choose one."}
    ]
    assert payload["recovery_required"] is True
    assert "result" not in payload
    assert WaitRequest((WaitTarget("conversation-2"),)).timeout_seconds == 240.0


def test_agent_status_surfaces_input_and_reasoning_attestations() -> None:
    input_attestation = {
        "path": "docs/specs/review.md",
        "sha256": "a" * 64,
        "byte_count": 42,
        "source": "subagent-mcp-read-only-sha256",
    }
    reasoning_attestation = {
        "effective": {"effort": "max"},
        "source": "claude-code-managed-sdk",
        "binding": ["ClaudeAgentOptions.effort", "CLAUDE_CODE_EFFORT_LEVEL"],
        "provider_reported": False,
        "context_hash": "b" * 64,
    }
    status = AgentStatus(
        conversation_id="conversation-attested",
        execution_id="execution-attested",
        external_session_id="native-session-attested",
        workspace_path="workspace",
        conversation_state="idle",
        execution_state="succeeded",
        state_revision=2,
        descriptor=AgentDescriptor.from_manifest(
            _manifest(), model="vendor/model", transport="managed-sdk"
        ),
        result={"text": "CAPSULE: approved"},
        needs_input=(),
        events=(),
        next_event_cursor=2,
        input_attestations=(input_attestation,),
        reasoning_attestation=reasoning_attestation,
    )

    for payload in (status.to_compact_dict(), status.to_dict()):
        assert payload["input_attestations"] == [input_attestation]
        assert payload["reasoning_attestation"] == reasoning_attestation


def test_public_schemas_cover_adapter_and_normalized_descriptor() -> None:
    root = Path(__file__).resolve().parents[2]
    adapter = json.loads((root / "schemas" / "adapter-v1.json").read_text("utf-8"))
    descriptor = json.loads(
        (root / "schemas" / "agent-descriptor-v1.json").read_text("utf-8")
    )

    assert adapter["properties"]["adapter_api_version"]["const"] == ADAPTER_API_VERSION
    assert {
        "runtime_id",
        "provider_id",
        "harness_id",
        "supported_transports",
        "reasoning_schema",
    } <= set(adapter["required"])
    assert adapter["properties"]["model_schema"]["type"] == "object"
    assert "native-acp" in adapter["properties"]["supported_transports"]["items"]["enum"]
    assert descriptor["properties"]["schema_version"]["const"] == 1
    assert descriptor["properties"]["model_display_name"]["type"] == "string"
    assert "native-acp" in descriptor["properties"]["transport"]["enum"]
    assert descriptor["additionalProperties"] is True


def test_spawn_contract_accepts_native_acp_transport() -> None:
    request = SpawnRequest(
        request_id="spawn-1",
        runtime_id="deepseek-harness",
        variant_id="ox-alpha",
        task=TaskPacket("Review", "Review it.", ("Report",), "reviewer"),
        cwd="workspace",
        mode="review",
        transport="native-acp",
    )

    assert request.transport == "native-acp"


def test_artifact_reference_is_frozen_and_exactly_hash_bound() -> None:
    reference = ArtifactReference(
        "conversation-source", "execution-source", "a" * 64
    )

    assert reference.conversation_id == "conversation-source"
    assert reference.execution_id == "execution-source"
    assert reference.expected_sha256 == "a" * 64
    assert reference.to_dict() == {
        "conversation_id": "conversation-source",
        "execution_id": "execution-source",
        "expected_sha256": "a" * 64,
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        reference.expected_sha256 = "b" * 64  # type: ignore[misc]
    for bad_hash in ("A" * 64, "a" * 63, "g" * 64, 123):
        with pytest.raises(ContractError, match="sha256"):
            ArtifactReference("conversation-source", "execution-source", bad_hash)
    with pytest.raises(ContractError, match="conversation_id"):
        ArtifactReference("", "execution-source", "a" * 64)
    with pytest.raises(ContractError, match="execution_id"):
        ArtifactReference("conversation-source", "execution\nid", "a" * 64)


def test_send_request_artifact_is_optional_and_backward_compatible() -> None:
    legacy = SendRequest(
        request_id="send-1",
        conversation_id="conversation-1",
        prompt="Continue.",
    )

    assert legacy.artifact is None
    assert PROMPT_MAX_BYTES == 128 * 1024

    relay = SendRequest(
        request_id="send-2",
        conversation_id="conversation-target",
        prompt="Summarize the attached report.",
        artifact=ArtifactReference(
            "conversation-source", "execution-source", "a" * 64
        ),
    )

    assert relay.artifact is not None
    assert relay.artifact.execution_id == "execution-source"
    with pytest.raises(ContractError, match="artifact"):
        SendRequest(
            request_id="send-3",
            conversation_id="conversation-1",
            prompt="Continue.",
            artifact={"execution_id": "not-a-reference"},  # type: ignore[arg-type]
        )


def test_prompts_accept_multiline_formatting_but_reject_unsafe_controls() -> None:
    prompt = "Read AGENTS.md.\n\nImplement the bounded lane:\n\t- preserve data"
    task = TaskPacket("Implement", prompt, ("Done",), "writer")
    send = SendRequest("send-multiline", "conversation-1", prompt)

    assert task.prompt == prompt
    assert send.prompt == prompt

    for unsafe in ("\x00", "\x1b"):
        with pytest.raises(ContractError, match="control character"):
            TaskPacket("Implement", f"Before{unsafe}after", ("Done",), "writer")
        with pytest.raises(ContractError, match="control character"):
            SendRequest("send-unsafe", "conversation-1", f"Before{unsafe}after")

    with pytest.raises(ContractError, match="control character"):
        TaskPacket("Bad\ntitle", "Prompt.", ("Done",), "writer")


def test_rough_token_estimate_is_labelled_content_only_bytes_over_three() -> None:
    assert "ceil(utf8_bytes / 3)" in ROUGH_TOKEN_ESTIMATE_BASIS
    assert "not provider billing" in ROUGH_TOKEN_ESTIMATE_BASIS
    assert rough_token_estimate(0) == 0
    assert rough_token_estimate(1) == 1
    assert rough_token_estimate(3) == 1
    assert rough_token_estimate(4) == 2
    assert rough_token_estimate(65_535) == 21_845


def test_result_artifact_metadata_reports_exact_utf8_bytes_and_rough_tokens() -> None:
    text = "Rapport détaillé ✓\nsecond line of evidence"

    metadata = result_artifact_metadata("execution-mb", {"text": text})

    assert metadata is not None
    assert metadata["char_count"] == len(text)
    preview = " ".join(text.split())[:512]
    full_bytes = len(text.encode("utf-8"))
    compact_bytes = len(preview.encode("utf-8"))
    metrics = metadata["transfer_metrics"]
    assert metrics == {
        "basis": ROUGH_TOKEN_ESTIMATE_BASIS,
        "full_utf8_bytes": full_bytes,
        "compact_utf8_bytes": compact_bytes,
        "rough_tokens_full": (full_bytes + 2) // 3,
        "rough_tokens_compact": (compact_bytes + 2) // 3,
        "rough_tokens_saved": (full_bytes + 2) // 3 - (compact_bytes + 2) // 3,
    }
    assert metrics["full_utf8_bytes"] != metadata["char_count"]


@pytest.mark.parametrize("text", ["capsule:", "report done\ncapsule:"])
def test_result_artifact_metadata_handles_empty_capsule_suffix(text: str) -> None:
    metadata = result_artifact_metadata("execution-empty-capsule", {"text": text})

    assert metadata is not None
    assert metadata["preview"] == " ".join(text.split())
    assert "capsule" not in metadata


def test_slice_transfer_metrics_reports_exact_chars_bytes_and_rough_tokens() -> None:
    metrics = slice_transfer_metrics("héllo ✓")

    assert metrics == {
        "basis": ROUGH_TOKEN_ESTIMATE_BASIS,
        "chars": 7,
        "utf8_bytes": 10,
        "rough_tokens": 4,
    }
