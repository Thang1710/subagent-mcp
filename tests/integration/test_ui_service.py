from __future__ import annotations

import http.client
import json
import os
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

from subagent_harness_mcp.adapters import claude_code as claude_code_module
from subagent_harness_mcp.adapters import deepseek_harness as deepseek_harness_module
from subagent_harness_mcp.adapters.fake import FakeAdapter
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigError, ConfigStore
from subagent_harness_mcp.contracts import ServiceError
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
    status, headers, body = _request(
        server,
        "POST",
        "/api/v1/session",
        headers={"Origin": server.origin},
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


def test_ui_provider_refresh_is_explicit_and_uses_sanitized_quota_state(
    tmp_path: Path,
) -> None:
    _, config = _configured_backend(tmp_path / "home")
    manifest = FakeAdapter().manifest.to_dict()

    class RecordingService:
        def __init__(self) -> None:
            self.quota_calls: list[tuple[str, bool]] = []

        async def runtime_list(self):
            return (
                {
                    "runtime_id": "fake",
                    "state": "available",
                    "enabled": True,
                    "manifest": manifest,
                    "reason": None,
                    "circuits": [],
                },
            )

        async def runtime_check(self, runtime_id: str, refresh_quota: bool = False):
            self.quota_calls.append((runtime_id, refresh_quota))
            return {
                "runtime_id": runtime_id,
                "state": "ready",
                "quota": {
                    "state": "available",
                    "overage_blocked": True,
                    "raw": {"account": "must-not-escape"},
                },
            }

    service = RecordingService()
    backend = LocalUiBackend(config=config, service=service)

    local = backend.snapshot()
    refreshed = backend.refresh_provider()

    assert service.quota_calls == [("fake", True)]
    assert local["quota"]["state"] == "check_required"
    assert refreshed["quota"] == {
        "state": "available",
        "label": "Available · overage blocked",
        "detail": "Current provider evidence passed; usage credits stay blocked.",
        "overage_blocked": True,
    }
    assert "must-not-escape" not in json.dumps(refreshed)


def test_ui_snapshot_uses_paused_circuit_as_runtime_availability(
    tmp_path: Path,
) -> None:
    _, config = _configured_backend(tmp_path / "home")
    manifest = FakeAdapter().manifest.to_dict()

    class PausedService:
        async def runtime_list(self):
            return (
                {
                    "runtime_id": "fake",
                    "state": "available",
                    "enabled": True,
                    "manifest": manifest,
                    "reason": None,
                    "circuits": [
                        {
                            "variant_id": "configured",
                            "state": "auto_paused",
                            "revision": 1,
                            "pair_key": "sha256:test",
                        }
                    ],
                },
            )

    snapshot = LocalUiBackend(config=config, service=PausedService()).snapshot()

    assert snapshot["runtimes"][0]["status"] == {
        "state": "auto_paused",
        "label": "Auto paused",
        "detail": "",
    }
    assert snapshot["health"]["state"] == "unavailable"
    assert snapshot["health"]["label"] == "Unavailable"
    fields = {
        field["id"]: field
        for group in snapshot["runtimes"][0]["groups"]
        for field in group["fields"]
    }
    configured = fields["model_priority"]["options"][0]
    assert configured["state"] == "available"
    assert configured["available"] is True


def test_ui_provider_refresh_reports_unknown_without_backend_details(
    tmp_path: Path,
) -> None:
    _, config = _configured_backend(tmp_path / "home")
    manifest = FakeAdapter().manifest.to_dict()

    class FailingService:
        async def runtime_list(self):
            return (
                {
                    "runtime_id": "fake",
                    "state": "available",
                    "manifest": manifest,
                    "reason": None,
                    "circuits": [],
                },
            )

        async def runtime_check(self, _runtime_id: str, refresh_quota: bool = False):
            assert refresh_quota is True
            raise ServiceError(
                "RECOVERY_REQUIRED",
                "private provider session must-not-escape",
            )

    refreshed = LocalUiBackend(config=config, service=FailingService()).refresh_provider()

    assert refreshed["quota"] == {
        "state": "unknown",
        "label": "Unknown",
        "detail": "The provider did not return safe quota evidence.",
    }
    assert "must-not-escape" not in json.dumps(refreshed)


def test_ui_provider_refresh_explains_no_model_preflight_gap(tmp_path: Path) -> None:
    _, config = _configured_backend(tmp_path / "home")
    manifest = FakeAdapter().manifest.to_dict()

    class TimedOutService:
        async def runtime_list(self):
            return (
                {
                    "runtime_id": "fake",
                    "state": "needs_canary",
                    "manifest": manifest,
                    "reason": None,
                    "circuits": [],
                },
            )

        async def runtime_check(self, _runtime_id: str, refresh_quota: bool = False):
            assert refresh_quota is True
            return {
                "runtime_id": "fake",
                "state": "needs_canary",
                "quota": {
                    # Older backends could misclassify missing evidence as a
                    # quota pause. The UI must not tell users their quota is
                    # exhausted when the reason code says otherwise.
                    "state": "quota_paused",
                    "variants": [
                        {
                            "variant_id": "configured",
                            "state": "quota_paused",
                            "error_code": "CAPABILITY_MISSING",
                        }
                    ],
                },
            }

    refreshed = LocalUiBackend(config=config, service=TimedOutService()).refresh_provider()

    assert refreshed["quota"] == {
        "state": "unknown",
        "label": "Safety check unavailable",
        "detail": (
            "Your provider quota is not known to be exhausted. The native harness "
            "did not expose no-overage evidence, so Refresh started no provider task."
        ),
        "reason_code": "CAPABILITY_MISSING",
    }


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
                        "model_priority": ["provider/model-beta"],
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
    assert fields["model_priority"]["value"] == ["provider/model-alpha"]
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


def test_fresh_ui_lists_real_runtimes_and_creates_claude_policy_by_cas(
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
    real_deepseek_adapter = deepseek_harness_module.DeepSeekHarnessAdapter

    class CatalogDeepSeekAdapter(real_deepseek_adapter):
        async def model_catalog(self) -> tuple[dict[str, str], ...]:
            return (
                {
                    "value": "deepseek-official::deepseek-v4-flash",
                    "label": "DeepSeek-V4-Flash",
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-flash",
                },
                {
                    "value": "deepseek-official::deepseek-v4-pro",
                    "label": "DeepSeek-V4-Pro",
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-pro",
                },
                {
                    "value": "deepseek-official::deepseek-v4-flash-vision-exp",
                    "label": "DeepSeek-V4-Flash-Vision-Exp",
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-flash-vision-exp",
                },
                {
                    "value": "ox-provider::stealth/ox-alpha",
                    "label": "OX Alpha - OpenRouter",
                    "provider": "ox-provider",
                    "model": "stealth/ox-alpha",
                },
            )

    monkeypatch.setattr(
        deepseek_harness_module,
        "DeepSeekHarnessAdapter",
        CatalogDeepSeekAdapter,
    )

    backend = create_local_backend()
    fresh = backend.snapshot()
    by_id = {runtime["id"]: runtime for runtime in fresh["runtimes"]}

    assert fresh["revision"] == 0
    assert set(by_id) == {"claude-code", "deepseek-harness"}
    assert fresh["health"]["state"] == "setup_required"
    assert {
        item["label"]: item["state"] for item in fresh["health"]["messages"]
    } == {
        "Claude sub-agent": "not_configured",
        "DeepSeek Harness": "not_configured",
    }
    claude = by_id["claude-code"]
    assert claude["manifest"]["runtime_id"] == "claude-code"
    assert claude["manifest"]["harness_id"] == "claude-code"
    assert claude["status"]["state"] == "not_configured"
    assert claude["enabled"] is False
    assert claude["locked"] is False
    assert claude["subtitle"] == "Anthropic model · Claude Code native harness"
    assert claude["enabledLabel"] == "Available to Codex"
    assert "delegate" in claude["enabledHelp"].lower()

    deepseek = by_id["deepseek-harness"]
    assert deepseek["manifest"]["runtime_id"] == "deepseek-harness"
    assert deepseek["status"]["state"] == "not_configured"
    assert deepseek["enabled"] is False
    assert deepseek["locked"] is False
    assert deepseek["subtitle"] == "External provider model · DeepSeek Harness native harness"
    deepseek_fields = {
        field["id"]: field
        for group in deepseek["groups"]
        for field in group["fields"]
    }
    assert set(deepseek_fields) == {
        "delegation_priority",
        "model_priority",
    }
    assert deepseek_fields["delegation_priority"]["value"] == 0
    deepseek_priority = deepseek_fields["model_priority"]
    assert deepseek_priority["kind"] == "model-priority"
    # Native catalog rows are suggestions, not silently configured fallbacks.
    # Applying the popup may promote this suggested order into the persisted
    # value, but a read-only snapshot must reflect the empty fresh config.
    assert deepseek_priority["value"] == []
    assert [item["value"] for item in deepseek_priority["options"]] == [
        "deepseek-official::deepseek-v4-flash",
        "deepseek-official::deepseek-v4-pro",
        "deepseek-official::deepseek-v4-flash-vision-exp",
        "ox-provider::stealth/ox-alpha",
    ]
    assert [item["label"] for item in deepseek_priority["options"]] == [
        "DeepSeek-V4-Flash",
        "DeepSeek-V4-Pro",
        "DeepSeek-V4-Flash-Vision-Exp",
        "OX Alpha - OpenRouter",
    ]

    fields = {
        field["id"]: field
        for group in claude["groups"]
        for field in group["fields"]
    }
    assert set(fields) == {
        "delegation_priority",
        "model_priority",
        "variant.0.reasoning.effort",
    }
    model_field = fields["model_priority"]
    assert model_field["label"] == "Model priority"
    assert model_field["kind"] == "model-priority"
    assert [option["value"] for option in model_field["options"]] == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
    ]
    assert [option["label"] for option in model_field["options"]] == [
        "Opus 5",
        "Sonnet 5",
        "Fable 5",
    ]
    effort_field = fields["variant.0.reasoning.effort"]
    assert effort_field["label"] == "Reasoning effort"
    assert effort_field["kind"] == "select"
    assert [option["value"] for option in effort_field["options"]] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]

    model = "future/provider-model@2026.08"
    reasoning = {"effort": "xhigh"}
    patch = {
        "runtimes": {
            "claude-code": {
                "enabled": True,
                "options": {
                    "model_priority": [
                        model,
                        "future/provider-fallback-1@2026.08",
                        "future/provider-fallback-2@2026.08",
                    ],
                    "variant.0.reasoning.effort": reasoning["effort"],
                    "delegation_priority": 50,
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
    assert updated_fields["delegation_priority"]["value"] == 50
    assert updated_fields["model_priority"]["value"] == [
        model,
        "future/provider-fallback-1@2026.08",
        "future/provider-fallback-2@2026.08",
    ]
    assert updated_fields["variant.0.reasoning.effort"]["value"] == "xhigh"

    persisted = ConfigStore(
        resolve_paths(
            {"SUBAGENT_MCP_HOME": str(home.resolve())},
            os_name="nt",
        )
    ).load()
    persisted_policy = persisted["runtimes"]["claude-code"]
    assert persisted_policy["enabled"] is True
    assert persisted_policy["delegation_priority"] == 50
    assert persisted_policy["fallback"] is False
    assert persisted_policy["selection_mode"] == "lead-selects"
    assert persisted_policy["transport"] == "managed-sdk"
    assert [item["model"] for item in persisted_policy["variants"]][:3] == [
        model,
        "future/provider-fallback-1@2026.08",
        "future/provider-fallback-2@2026.08",
    ]
    assert all(
        item["reasoning"] == reasoning for item in persisted_policy["variants"]
    )
    assert persisted_policy | {"variants": []} == {
        "enabled": True,
        "delegation_priority": 50,
        "fallback": False,
        "selection_mode": "lead-selects",
        "transport": "managed-sdk",
        "variants": [],
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
