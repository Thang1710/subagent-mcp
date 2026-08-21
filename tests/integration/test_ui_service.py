from __future__ import annotations

import http.client
import json
import os
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from subagent_harness_mcp.adapters import claude_code as claude_code_module
from subagent_harness_mcp.adapters.fake import FakeAdapter
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigError, ConfigStore
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore
from subagent_harness_mcp.ui import (
    LocalUiBackend,
    LoopbackUiServer,
    create_local_backend,
)


def _request(
    server: LoopbackUiServer,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(
        server.bound_host,
        server.bound_port,
        timeout=3,
    )
    request_headers = {"Host": server.host_header}
    if headers:
        request_headers.update(headers)
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    result_headers = {key.casefold(): value for key, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, result_headers, payload


def _session(server: LoopbackUiServer) -> tuple[str, str]:
    token = parse_qs(urlsplit(server.bootstrap_url).fragment)["token"][0]
    status, headers, body = _request(
        server,
        "POST",
        "/api/v1/session",
        headers={
            "Origin": server.origin,
            "X-Subagent-MCP-Token": token,
        },
    )
    assert status == 200
    parsed = SimpleCookie()
    parsed.load(headers["set-cookie"])
    cookie = f"smcp_session={parsed['smcp_session'].value}"
    return cookie, json.loads(body)["csrf_token"]


def _configured_backend(home: Path) -> tuple[LocalUiBackend, ConfigStore]:
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(home.resolve())},
        os_name="nt",
    )
    config = ConfigStore(paths)
    config.save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "fake": {
                    "enabled": True,
                    "selection_mode": "fixed",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "configured",
                            "model": "provider/model-alpha",
                            "reasoning": {"mode": "provider-native"},
                        }
                    ],
                }
            },
            "trust": [
                {
                    "path": str((home / "workspace").resolve()),
                    "hash": "sha256:abc123",
                    "trusted": False,
                }
            ],
        },
        expected_revision=0,
    )
    service = SubagentMcpService(
        config=config,
        store=StateStore.open(paths),
        registry=AdapterRegistry(builtin_factories=(FakeAdapter,)),
    )
    return LocalUiBackend(config=config, service=service), config


def test_real_loopback_ui_reads_and_cas_updates_shared_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    backend, config = _configured_backend(home)
    server = LoopbackUiServer(backend.snapshot, backend.patch_config)
    thread = server.start()
    try:
        cookie, csrf = _session(server)
        status, _, body = _request(
            server,
            "GET",
            "/api/v1/snapshot",
            headers={"Cookie": cookie},
        )
        snapshot = json.loads(body)
        runtime = snapshot["runtimes"][0]
        fields = {
            field["id"]: field
            for group in runtime["groups"]
            for field in group["fields"]
        }

        patch = {
            "revision": snapshot["revision"],
            "runtimes": {
                "fake": {
                    "enabled": False,
                    "options": {
                        "variant.0.model": "provider/model-beta",
                        "variant.0.reasoning": '{"mode":"adapter-deep"}',
                    },
                }
            },
            "trust": [
                {
                    "path": str((home / "workspace").resolve()),
                    "hash": "sha256:abc123",
                    "trusted": True,
                }
            ],
        }
        stale_patch = dict(patch)
        stale_patch["revision"] = snapshot["revision"] - 1
        stale, _, _ = _request(
            server,
            "PATCH",
            "/api/v1/config",
            headers={
                "Cookie": cookie,
                "Origin": server.origin,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
            },
            body=json.dumps(stale_patch).encode("utf-8"),
        )
        patched, _, patched_body = _request(
            server,
            "PATCH",
            "/api/v1/config",
            headers={
                "Cookie": cookie,
                "Origin": server.origin,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
            },
            body=json.dumps(patch).encode("utf-8"),
        )
    finally:
        server.close()

    assert status == 200
    assert runtime["id"] == "fake"
    assert runtime["enabled"] is True
    assert fields["variant.0.model"]["value"] == "provider/model-alpha"
    assert fields["variant.0.reasoning"]["value"] == '{"mode":"provider-native"}'
    assert snapshot["trust"][0]["trusted"] is False
    assert stale == 409
    assert patched == 200
    assert json.loads(patched_body) == {"revision": 2}
    saved = config.load()
    assert saved["revision"] == 2
    assert saved["runtimes"]["fake"]["enabled"] is False
    assert saved["runtimes"]["fake"]["variants"][0]["model"] == "provider/model-beta"
    assert saved["runtimes"]["fake"]["variants"][0]["reasoning"] == {
        "mode": "adapter-deep"
    }
    assert saved["trust"][0]["trusted"] is True
    assert not thread.is_alive()
    assert os.environ.get("SUBAGENT_MCP_HOME") != str(home.resolve())


def test_fresh_ui_lists_builtins_and_creates_claude_policy_by_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("SUBAGENT_MCP_HOME", str(home.resolve()))

    def provider_must_not_start(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("fresh UI snapshot must not start a Claude provider")

    real_adapter = claude_code_module.ClaudeCodeAdapter

    def guarded_adapter() -> object:
        return real_adapter(
            command_runner=provider_must_not_start,
            client_factory=provider_must_not_start,
        )

    monkeypatch.setattr(claude_code_module, "ClaudeCodeAdapter", guarded_adapter)

    backend = create_local_backend()
    fresh = backend.snapshot()
    by_id = {runtime["id"]: runtime for runtime in fresh["runtimes"]}

    assert fresh["revision"] == 0
    assert set(by_id) == {"claude-code", "fake"}
    assert fresh["health"]["state"] == "setup_required"
    assert {
        item["label"]: item["state"] for item in fresh["health"]["messages"]
    } == {
        "Claude sub-agent": "not_configured",
        "Fake sub-agent": "not_configured",
    }
    claude = by_id["claude-code"]
    assert claude["manifest"]["runtime_id"] == "claude-code"
    assert claude["manifest"]["harness_id"] == "claude-code"
    assert claude["status"]["state"] == "not_configured"
    assert claude["enabled"] is False
    assert claude["locked"] is False

    fields = {
        field["id"]: field
        for group in claude["groups"]
        for field in group["fields"]
    }
    assert fields["transport"]["value"] == "managed-sdk"
    assert fields["variant.0.model"]["value"] == ""
    assert fields["variant.0.reasoning"]["value"] == "{}"

    model = "future/provider-model@2026.08"
    reasoning = {"effort": "xhigh"}
    patch = {
        "runtimes": {
            "claude-code": {
                "enabled": True,
                "options": {
                    "selection_mode": "fixed",
                    "transport": "managed-sdk",
                    "variant.0.model": model,
                    "variant.0.reasoning": json.dumps(reasoning),
                },
            }
        }
    }
    saved = backend.patch_config(patch, expected_revision=0)

    assert saved == {"revision": 1}
    with pytest.raises(ConfigError) as stale:
        backend.patch_config(patch, expected_revision=0)
    assert stale.value.code == "REVISION_CONFLICT"

    updated = backend.snapshot()
    updated_claude = next(
        runtime for runtime in updated["runtimes"] if runtime["id"] == "claude-code"
    )
    updated_fields = {
        field["id"]: field
        for group in updated_claude["groups"]
        for field in group["fields"]
    }
    assert updated["revision"] == 1
    assert updated_claude["enabled"] is True
    assert updated_fields["transport"]["value"] == "managed-sdk"
    assert updated_fields["variant.0.model"]["value"] == model
    assert json.loads(updated_fields["variant.0.reasoning"]["value"]) == reasoning

    persisted = ConfigStore(
        resolve_paths(
            {"SUBAGENT_MCP_HOME": str(home.resolve())},
            os_name="nt",
        )
    ).load()
    assert persisted["runtimes"]["claude-code"] == {
        "enabled": True,
        "fallback": False,
        "selection_mode": "fixed",
        "transport": "managed-sdk",
        "variants": [
            {
                "id": "default",
                "model": model,
                "reasoning": reasoning,
            }
        ],
    }


def test_browser_callback_runs_only_after_successful_loopback_bind() -> None:
    server = LoopbackUiServer(lambda: {}, lambda _patch, revision: {"revision": revision})
    observations: list[tuple[str, int]] = []

    def opener(url: str) -> bool:
        connection = http.client.HTTPConnection(server.bound_host, server.bound_port, timeout=3)
        connection.request("GET", "/", headers={"Host": server.host_header})
        response = connection.getresponse()
        observations.append((url, response.status))
        response.read()
        connection.close()
        return True

    thread = server.start()
    try:
        assert server.open_browser(opener) is True
    finally:
        server.close()

    assert observations == [(server.bootstrap_url, 200)]
    assert not thread.is_alive()
