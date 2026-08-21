from __future__ import annotations

import json
from pathlib import Path

import pytest

from subagent_harness_mcp.contracts import (
    ADAPTER_API_VERSION,
    AdapterManifest,
    AgentDescriptor,
    ContractError,
    ExecutionState,
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
    assert descriptor["properties"]["schema_version"]["const"] == 1
    assert descriptor["properties"]["model_display_name"]["type"] == "string"
    assert descriptor["additionalProperties"] is True
