from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from subagent_harness_mcp.contracts import (
    ADAPTER_API_VERSION,
    AdapterManifest,
    AgentDescriptor,
    AgentEvent,
    AgentStatus,
    ContractError,
    ExecutionState,
    ResultReadRequest,
    SpawnRequest,
    TaskPacket,
    WaitRequest,
    WaitTarget,
    require_execution_transition,
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


def test_adapter_manifest_serializes_optional_model_schema() -> None:
    payload = _manifest().to_dict()

    assert payload["model_schema"]["anyOf"][0] == {
        "const": "vendor/model",
        "title": "Vendor model",
    }


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
            }
        },
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    assert "complete provider evidence" not in encoded
    assert len(encoded.encode()) <= 2048


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

    assert status.to_compact_dict()["result"] == status.result


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
    assert WaitRequest((WaitTarget("conversation-2"),)).timeout_seconds == 300.0


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
