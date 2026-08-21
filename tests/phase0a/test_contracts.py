import json
from pathlib import Path

import pytest

from spikes.phase0a.contracts import (
    _forbidden_surface_presence,
    classify_turn,
    normalize_agents,
    normalize_auth,
    normalize_stop_failure,
    normalize_stream_json,
    normalize_transport_circuit_condition,
)


def _init_event(**changes):
    event = {
        "type": "system",
        "subtype": "init",
        "model": "sonnet",
        "tools": [],
        "mcp_servers": [],
        "plugins": [],
        "capabilities": [],
        "permissionMode": "default",
        "cwd": "C:\\repo",
    }
    event.update(changes)
    return event


def _write_stream(tmp_path, *events):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(event if isinstance(event, str) else json.dumps(event) for event in events)
        + "\n",
        encoding="utf-8",
    )
    return stream


def test_normalize_auth_keeps_only_contract_fields():
    result = normalize_auth({
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "futureField": {"keep_out": True},
    })
    assert result == {
        "logged_in": True,
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
    }


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_normalize_auth_rejects_non_boolean_logged_in(value):
    with pytest.raises(ValueError, match="loggedIn must be a boolean"):
        normalize_auth({
            "loggedIn": value,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
        })


def test_normalize_agents_accepts_unknown_fields_and_preserves_state():
    result = normalize_agents([{
        "id": "short",
        "sessionId": "uuid",
        "cwd": "C:\\repo",
        "kind": "background",
        "state": "working",
        "startedAt": 1,
        "futureField": 2,
    }])
    assert result[0]["session_id_present"] is True
    assert result[0]["state"] == "working"
    assert result[0]["cwd_present"] is True


def test_normalize_agents_rejects_non_array():
    with pytest.raises(ValueError):
        normalize_agents({"state": "working"})


def test_normalize_stream_json_extracts_init_and_result(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            '{"type":"system","subtype":"init","model":"fable","tools":["Read"],'
            '"mcp_servers":[{"name":"declared","status":"connected","future":1}],'
            '"plugins":[{"name":"ponytail","future":{"safe":true}}],'
            '"capabilities":["interrupt_v1"],'
            '"permissionMode":"default","cwd":"C:\\\\repo","future":1}',
            '{"type":"result","subtype":"success","is_error":false,"total_cost_usd":0.01,'
            '"usage":{"input_tokens":12,"output_tokens":3}}',
        ]) + "\n",
        encoding="utf-8",
    )
    result = normalize_stream_json(stream)
    assert result["init"]["model"] == "fable"
    assert result["init"]["tools"] == ["Read"]
    assert result["init"]["mcp_servers"] == [{"name": "declared", "status": "connected"}]
    assert result["init"]["cwd_present"] is True
    assert result["result"]["total_cost_usd"] == 0.01


def test_forbidden_surface_presence_detects_canonical_subagent_mcp_recursion_tool():
    presence = _forbidden_surface_presence(["mcp__subagent_harness_mcp__agent_run"])

    assert presence["subagent_mcp"] is True


def test_normalize_stream_json_never_parses_assistant_payload(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            json.dumps(_init_event()),
            '{"type":"assistant","message":THIS_MUST_NOT_BE_PARSED}',
            '{"type":"result","subtype":"success","is_error":false}',
        ]) + "\n",
        encoding="utf-8",
    )
    result = normalize_stream_json(stream)
    assert result["init"]["model"] == "sonnet"
    assert result["result"]["is_error"] is False


def test_normalize_stream_json_accepts_cli_result_key_order(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            json.dumps(_init_event(model="claude-sonnet-5")),
            '{"is_error":false,"stop_reason":"end_turn","total_cost_usd":0.1,'
            '"type":"result","subtype":"success"}',
        ]) + "\n",
        encoding="utf-8",
    )
    result = normalize_stream_json(stream)
    assert result["result"] == {
        "subtype": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "total_cost_usd": 0.1,
        "usage": None,
    }


@pytest.mark.parametrize(
    "result",
    [
        {"subtype": "success", "is_error": False, "type": "result"},
        {"subtype": "success", "type": "result", "is_error": False},
        {"type": "result", "subtype": "success", "is_error": False},
    ],
)
def test_normalize_stream_json_accepts_any_top_level_result_key_order(tmp_path, result):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(_init_event(model="claude-sonnet-5"))
        + "\n"
        + json.dumps(result)
        + "\n",
        encoding="utf-8",
    )
    assert normalize_stream_json(stream)["result"]["is_error"] is False


def test_normalize_stream_json_rejects_missing_result_is_error(tmp_path):
    stream = _write_stream(tmp_path, _init_event(), {"subtype": "success", "type": "result"})
    with pytest.raises(ValueError, match="result.is_error must be a boolean"):
        normalize_stream_json(stream)


@pytest.mark.parametrize("tools", [None, {}, "Read", ["Read", 1]])
def test_normalize_stream_json_rejects_invalid_tools(tmp_path, tools):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(_init_event(tools=tools))
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "is_error": False})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="system.tools"):
        normalize_stream_json(stream)


@pytest.mark.parametrize("field,value", [("id", 7), ("sessionId", []), ("cwd", {})])
def test_normalize_agents_rejects_wrong_present_field_types(field, value):
    with pytest.raises(ValueError, match=field):
        normalize_agents([{"kind": "background", field: value}])


def test_allowed_plan_with_disabled_overage_is_success(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            json.dumps(_init_event(model="claude-sonnet-5")),
            '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",'
            '"rateLimitType":"five_hour","overageStatus":"rejected",'
            '"overageDisabledReason":"out_of_credits","isUsingOverage":false}}',
            '{"is_error":false,"stop_reason":"end_turn","total_cost_usd":0.1,'
            '"type":"result","subtype":"success"}',
        ]) + "\n",
        encoding="utf-8",
    )
    normalized = normalize_stream_json(stream)
    assert normalized["rate_limits"] == [{
        "status": "allowed",
        "rate_limit_type": "five_hour",
        "resets_at": None,
        "utilization": None,
        "overage_status": "rejected",
        "overage_disabled_reason": "out_of_credits",
        "is_using_overage": False,
        "error_code": None,
        "unknown_keys": [],
    }]
    assert classify_turn(normalized) == "success"


def test_rejected_plan_with_error_result_is_terminal_quota(tmp_path):
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join([
            json.dumps(_init_event(model="claude-fable-5")),
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
            '"overageDisabledReason":"out_of_credits","isUsingOverage":false}}',
            '{"is_error":true,"stop_reason":"stop_sequence","total_cost_usd":0,'
            '"type":"result","subtype":"success"}',
        ]) + "\n",
        encoding="utf-8",
    )
    assert classify_turn(normalize_stream_json(stream)) == "terminal_quota"


@pytest.mark.parametrize(
    "category",
    [
        "rate_limit",
        "authentication_failed",
        "oauth_org_not_allowed",
        "billing_error",
        "invalid_request",
        "server_error",
        "max_output_tokens",
        "unknown",
    ],
)
def test_normalize_stop_failure_accepts_documented_categories(category):
    assert normalize_stop_failure({"error": category}) == {
        "category": category,
        "raw_category": category,
        "retry_after": None,
    }


def test_normalize_stop_failure_maps_future_category_to_unknown():
    assert normalize_stop_failure({"error": "future_failure", "retry_after": 17}) == {
        "category": "unknown",
        "raw_category": "future_failure",
        "retry_after": 17,
    }


@pytest.mark.parametrize("category", ["model_not_found", "overloaded"])
def test_background_stop_failure_does_not_invent_non_hook_categories(category):
    assert normalize_stop_failure({"error": category}) == {
        "category": "unknown",
        "raw_category": category,
        "retry_after": None,
    }

    assert normalize_transport_circuit_condition(category) == category


@pytest.mark.parametrize("category", [None, "", "x" * 65, "unsafe value", 7])
def test_background_stop_failure_bounds_unknown_provenance(category):
    assert normalize_stop_failure({"error": category}) == {
        "category": "unknown",
        "raw_category": "invalid",
        "retry_after": None,
    }


def test_assistant_with_type_after_nested_content_is_never_json_decoded(tmp_path):
    stream = _write_stream(
        tmp_path,
        _init_event(),
        '{"message":THIS_MUST_NOT_BE_PARSED,"type":"assistant"}',
        {"type": "result", "subtype": "success", "is_error": False},
    )
    assert normalize_stream_json(stream)["result"]["is_error"] is False


def test_credits_required_is_distinct_from_resettable_plan_quota(tmp_path):
    stream = _write_stream(
        tmp_path,
        _init_event(model="fable"),
        '{"rate_limit_info":{"status":"rejected","errorCode":"credits_required",'
        '"futureScalar":"kept-by-name","isUsingOverage":false},"type":"rate_limit_event"}',
        {"subtype": "error", "is_error": True, "type": "result"},
    )
    normalized = normalize_stream_json(stream)
    assert normalized["rate_limits"][0]["error_code"] == "credits_required"
    assert normalized["rate_limits"][0]["unknown_keys"] == ["futureScalar"]
    assert classify_turn(normalized) == "terminal_credits_required"


def test_normalize_stream_json_reads_cumulative_large_stream_incrementally(tmp_path, monkeypatch):
    stream = tmp_path / "stream.jsonl"
    assistant_line = b'{"type":"assistant","message":"ignored"}\n'
    with stream.open("wb") as output:
        output.write(b"\xef\xbb\xbf")
        output.write(json.dumps(_init_event()).encode("utf-8") + b"\n")
        while output.tell() <= 8 * 1024 * 1024:
            output.write(assistant_line)
        output.write(b'{"type":"result","subtype":"success","is_error":false}\n')

    def fail_read_text(*_args, **_kwargs):
        raise AssertionError("normalize_stream_json must not read the whole stream")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    assert normalize_stream_json(stream)["result"]["is_error"] is False


@pytest.mark.parametrize(
    "second",
    [
        _init_event(),
        _init_event(model="different"),
    ],
    ids=["identical", "conflicting"],
)
def test_normalize_stream_json_rejects_second_init_envelope(tmp_path, second):
    stream = _write_stream(
        tmp_path,
        _init_event(),
        second,
        {"type": "result", "subtype": "success", "is_error": False},
    )
    with pytest.raises(ValueError, match="multiple system/init"):
        normalize_stream_json(stream)


@pytest.mark.parametrize(
    "first,second",
    [
        (
            {"type": "result", "subtype": "success", "is_error": False},
            {"type": "result", "subtype": "success", "is_error": False},
        ),
        (
            {"type": "result", "subtype": "error", "is_error": True},
            {"type": "result", "subtype": "success", "is_error": False},
        ),
    ],
    ids=["duplicate-result", "error-then-success"],
)
def test_normalize_stream_json_rejects_second_result_envelope(tmp_path, first, second):
    stream = _write_stream(tmp_path, _init_event(), first, second)
    with pytest.raises(ValueError, match="multiple result"):
        normalize_stream_json(stream)


def test_normalize_stream_json_rejects_conflicting_duplicate_type_keys(tmp_path):
    item = _init_event()
    encoded = json.dumps(item)
    conflicting = encoded[:-1] + ', "type": "assistant"}'
    stream = _write_stream(tmp_path, conflicting)
    with pytest.raises(ValueError, match="multiple top-level type keys"):
        normalize_stream_json(stream)


def test_normalize_stream_json_rejects_reverse_order_duplicate_type_keys(tmp_path):
    duplicate = (
        '{"type":"assistant","message":{"content":[{"type":"text"}]},'
        '"type":"system"}'
    )
    stream = _write_stream(
        tmp_path,
        _init_event(),
        duplicate,
        {"type": "result", "subtype": "success", "is_error": False},
    )
    with pytest.raises(ValueError, match="multiple top-level type keys"):
        normalize_stream_json(stream)


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "tools",
        "mcp_servers",
        "plugins",
        "capabilities",
        "permissionMode",
        "cwd",
    ],
)
def test_normalize_stream_json_rejects_absent_required_init_field(tmp_path, field):
    init = _init_event()
    init.pop(field)
    stream = _write_stream(tmp_path, init)
    with pytest.raises(ValueError, match=f"system\\.{field}"):
        normalize_stream_json(stream)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("model", "", "system.model"),
        ("model", 7, "system.model"),
        ("mcp_servers", ["bad"], "system.mcp_servers member"),
        ("mcp_servers", [{"name": 7, "status": "connected"}], "mcp_servers.name"),
        ("mcp_servers", [{"name": "declared", "status": 7}], "mcp_servers.status"),
        ("plugins", ["bad"], "system.plugins member"),
        ("plugins", [{"name": ""}], "system.plugins.name"),
        ("plugins", [{"name": 7}], "system.plugins.name"),
        ("capabilities", [7], "system.capabilities"),
        ("permissionMode", "", "system.permissionMode"),
        ("permissionMode", 7, "system.permissionMode"),
        ("cwd", "", "system.cwd"),
        ("cwd", 7, "system.cwd"),
    ],
)
def test_normalize_stream_json_rejects_malformed_required_init_values(
    tmp_path, field, value, match
):
    stream = _write_stream(tmp_path, _init_event(**{field: value}))
    with pytest.raises(ValueError, match=match):
        normalize_stream_json(stream)
