from __future__ import annotations

import http.client
import hmac
import json
from importlib import resources
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest

from subagent_harness_mcp import cli
from subagent_harness_mcp import ui
from subagent_harness_mcp.ui import LoopbackUiServer, UiError
from subagent_harness_mcp.ui_process import (
    CONTROL_CHALLENGE_HEADER,
    CONTROL_HEADER,
    CONTROL_OPEN_PATH,
    CONTROL_PROOF_HEADER,
    CONTROL_STOP_PATH,
)


def _server(
    *,
    patch_calls: list[tuple[dict[str, object], int]] | None = None,
    refresh_calls: list[None] | None = None,
    control_token: str | None = None,
):
    calls = [] if patch_calls is None else patch_calls
    refreshes = [] if refresh_calls is None else refresh_calls

    def snapshot():
        return {
            "version": "0.1.0a14",
            "revision": 7,
            "health": {"state": "ready", "messages": []},
            "runtimes": [],
            "activity": [
                {
                    "id": "execution-1",
                    "title": "Bounded task",
                    "state": "succeeded",
                    "prompt": "must never leave the backend",
                    "events": [{"raw": "must never leave the backend"}],
                }
            ],
            "prompt": "must never leave the backend",
        }

    def patch_config(patch: dict[str, object], revision: int):
        calls.append((patch, revision))
        return {"revision": revision + 1}

    def refresh_provider():
        refreshes.append(None)
        value = snapshot()
        value["quota"] = {
            "state": "available",
            "overage_blocked": True,
            "raw": {"account": "must never leave the backend"},
        }
        return value

    return LoopbackUiServer(
        snapshot,
        patch_config,
        provider_refresher=refresh_provider,
        control_token=control_token,
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


def _bootstrap_token(server: LoopbackUiServer) -> str:
    fragment = urlsplit(server.bootstrap_url).fragment
    return parse_qs(fragment)["token"][0]


def _open_session(server: LoopbackUiServer) -> tuple[str, str]:
    status, headers, body = _request(
        server,
        "POST",
        "/api/v1/session",
        headers={
            "Origin": server.origin,
            "X-Subagent-MCP-Token": _bootstrap_token(server),
        },
    )
    assert status == 200
    cookie = SimpleCookie()
    cookie.load(headers["set-cookie"])
    morsel = cookie["smcp_session"]
    assert morsel["httponly"] is True
    assert morsel["samesite"].casefold() == "strict"
    csrf = json.loads(body)["csrf_token"]
    return f"smcp_session={morsel.value}", csrf


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "localhost"])
def test_non_literal_loopback_bind_is_rejected(host: str) -> None:
    with pytest.raises(UiError) as caught:
        LoopbackUiServer(lambda: {}, lambda _patch, revision: {"revision": revision}, host=host)

    assert caught.value.code == "LOOPBACK_REQUIRED"


def test_peer_validation_accepts_only_ip_loopback() -> None:
    assert ui._is_loopback_peer("127.0.0.1") is True
    assert ui._is_loopback_peer("::1") is True
    assert ui._is_loopback_peer("203.0.113.10") is False
    assert ui._is_loopback_peer("not-an-address") is False


def test_cli_routes_ui_without_importing_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports: list[int] = []

    def run_ui(*, port: int, open_browser: bool = True, control_file=None) -> int:
        assert open_browser is True
        assert control_file is None
        ports.append(port)
        return 23

    monkeypatch.setattr(ui, "run_ui", run_ui)

    assert cli.main(["ui"]) == 23
    assert ports == [8765]


def test_cli_accepts_an_explicit_ui_port(monkeypatch: pytest.MonkeyPatch) -> None:
    ports: list[int] = []

    def run_ui(*, port: int, open_browser: bool = True, control_file=None) -> int:
        assert open_browser is True
        assert control_file is None
        ports.append(port)
        return 0

    monkeypatch.setattr(ui, "run_ui", run_ui)

    assert cli.main(["ui", "--port", "9123"]) == 0
    assert ports == [9123]


def test_cli_can_run_the_foreground_ui_without_opening_a_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, bool, object]] = []

    def run_ui(*, port: int, open_browser: bool, control_file=None) -> int:
        calls.append((port, open_browser, control_file))
        return 0

    monkeypatch.setattr(ui, "run_ui", run_ui)

    assert cli.main(["ui", "--no-open"]) == 0
    assert calls == [(8765, False, None)]


def test_cli_rejects_an_invalid_ui_port_without_a_value_dump(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.main(["ui", "--port", "70000"])

    error = capsys.readouterr().err
    assert "PORT must be 0 through 65535" in error
    assert len(error) < 500


def test_loopback_ui_server_binds_an_explicit_port() -> None:
    ephemeral = LoopbackUiServer(
        lambda: {},
        lambda _patch, revision: {"revision": revision},
        port=0,
    )
    port = ephemeral.bound_port
    ephemeral.close()

    fixed = LoopbackUiServer(
        lambda: {},
        lambda _patch, revision: {"revision": revision},
        port=port,
    )
    try:
        assert fixed.bound_port == port
    finally:
        fixed.close()


def test_background_stop_requires_both_exact_origin_and_control_token() -> None:
    token = "control-token-with-enough-entropy-for-security"
    server = LoopbackUiServer(
        lambda: {},
        lambda _patch, revision: {"revision": revision},
        control_token=token,
    )
    thread = server.start()
    try:
        wrong_token, _, _ = _request(
            server,
            "POST",
            CONTROL_STOP_PATH,
            headers={"Origin": server.origin, CONTROL_HEADER: "x" * 40},
        )
        wrong_origin, _, _ = _request(
            server,
            "POST",
            CONTROL_STOP_PATH,
            headers={"Origin": "https://attacker.invalid", CONTROL_HEADER: token},
        )

        assert wrong_token == 401
        assert wrong_origin == 403
        assert thread.is_alive()
    finally:
        server.close()


def test_background_open_rotates_one_control_authenticated_bootstrap() -> None:
    control_token = "control-token-with-enough-entropy-for-open"
    challenge = "challenge-with-enough-entropy-for-open-proof"
    request_proof = hmac.digest(
        control_token.encode(),
        b"open-request\0" + challenge.encode(),
        "sha256",
    ).hex()
    server = LoopbackUiServer(
        lambda: {},
        lambda _patch, revision: {"revision": revision},
        control_token=control_token,
    )
    original_bootstrap = _bootstrap_token(server)
    thread = server.start()
    try:
        wrong_token, _, _ = _request(
            server,
            "POST",
            CONTROL_OPEN_PATH,
            headers={
                "Origin": server.origin,
                CONTROL_CHALLENGE_HEADER: challenge,
                CONTROL_PROOF_HEADER: "0" * 64,
            },
        )
        opened, _, body = _request(
            server,
            "POST",
            CONTROL_OPEN_PATH,
            headers={
                "Origin": server.origin,
                CONTROL_CHALLENGE_HEADER: challenge,
                CONTROL_PROOF_HEADER: request_proof,
            },
        )
        open_response = json.loads(body)
        bootstrap_url = open_response["bootstrap_url"]
        parsed = urlsplit(bootstrap_url)
        rotated_bootstrap = parse_qs(parsed.fragment)["token"][0]

        assert wrong_token == 401
        assert opened == 200
        assert parsed.scheme == "http"
        assert parsed.hostname == server.bound_host
        assert parsed.port == server.bound_port
        assert parsed.path == "/"
        assert not parsed.query
        assert rotated_bootstrap not in {original_bootstrap, control_token}
        assert hmac.compare_digest(
            open_response["proof"],
            hmac.digest(
                control_token.encode(),
                b"open-response\0"
                + challenge.encode()
                + b"\0"
                + bootstrap_url.encode(),
                "sha256",
            ).hex(),
        )

        stale, _, _ = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={
                "Origin": server.origin,
                "X-Subagent-MCP-Token": original_bootstrap,
            },
        )
        fresh, _, _ = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={
                "Origin": server.origin,
                "X-Subagent-MCP-Token": rotated_bootstrap,
            },
        )
        replay, _, _ = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={
                "Origin": server.origin,
                "X-Subagent-MCP-Token": rotated_bootstrap,
            },
        )
        assert stale == 401
        assert fresh == 200
        assert replay == 401
        assert thread.is_alive()
    finally:
        server.close()


def test_static_assets_have_restrictive_headers_and_no_cors() -> None:
    server = _server()
    thread = server.start()
    try:
        status, headers, body = _request(server, "GET", "/")
    finally:
        server.close()

    assert status == 200
    assert b"Subagent MCP" in body
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "style-src-elem 'self'" in headers["content-security-policy"]
    assert "style-src-attr 'unsafe-inline'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in headers
    assert not thread.is_alive()


def test_static_javascript_invokes_its_iife_with_valid_syntax() -> None:
    source = (
        resources.files("subagent_harness_mcp")
        .joinpath("static/app.js")
        .read_text(encoding="utf-8")
        .rstrip()
    )

    assert source.endswith("})();")
    assert not source.endswith("}());")


def test_static_javascript_attempts_session_restore_without_a_fragment_token() -> None:
    source = (
        resources.files("subagent_harness_mcp")
        .joinpath("static/app.js")
        .read_text(encoding="utf-8")
    )
    boot = source.split("(async function boot() {", 1)[1]

    assert "if (!token)" not in boot
    assert "await openSession(token);" in boot


def test_static_css_preserves_the_hidden_attribute() -> None:
    source = (
        resources.files("subagent_harness_mcp")
        .joinpath("static/app.css")
        .read_text(encoding="utf-8")
    )

    assert "[hidden] { display: none !important; }" in source


def test_static_ui_exposes_only_plain_runtime_controls() -> None:
    package = resources.files("subagent_harness_mcp").joinpath("static")
    html = package.joinpath("index.html").read_text(encoding="utf-8")
    javascript = package.joinpath("app.js").read_text(encoding="utf-8")

    assert "Refresh status" in html
    assert "Available to Codex" in html
    assert 'type="checkbox" role="switch" data-enabled' in html
    assert 'class="switch-track" data-switch-track' in html
    assert "What this runtime supports" in html
    assert "Automatic safety stops" in html
    assert 'id="circuits-section" hidden' in html
    assert 'id="update-row" hidden' in html
    assert 'id="config-revision"' not in html
    assert "showModal()" in javascript
    assert "Model priority" in javascript
    assert "Advanced: add exact model ID" in javascript
    assert "draggable = true" in javascript
    assert "Move up" in javascript
    assert "Move down" in javascript
    assert "item.field.options.forEach" in javascript
    assert "if (draft.indexOf(option.value) === -1) draft.push(option.value);" in javascript
    assert "createElement('datalist')" not in javascript
    assert "setHidden(dom.circuitsSection, circuits.length === 0)" in javascript
    assert "setHidden(dom.updateRow" in javascript
    assert "'rev ' + revision" not in javascript
    assert "managed-sdk" not in html
    assert "managed-sdk" not in javascript
    assert "const API_REFRESH = '/api/v1/refresh';" in javascript
    assert "opts.provider ? API_REFRESH : API_SNAPSHOT" in javascript
    assert "dom.refresh.addEventListener('click', () => refresh({ provider: true }))" in javascript
    assert "await refresh();" in javascript


def test_host_origin_and_path_traversal_are_rejected() -> None:
    server = _server()
    server.start()
    try:
        bad_host, _, _ = _request(
            server,
            "GET",
            "/",
            headers={"Host": "attacker.invalid"},
        )
        bad_origin, _, _ = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={
                "Origin": "https://attacker.invalid",
                "X-Subagent-MCP-Token": _bootstrap_token(server),
            },
        )
        traversal, _, traversal_body = _request(
            server,
            "GET",
            "/%2e%2e/pyproject.toml",
        )
    finally:
        server.close()

    assert bad_host == 403
    assert bad_origin == 403
    assert traversal == 404
    assert b"build-system" not in traversal_body


def test_bootstrap_is_single_use_and_api_requires_cookie_and_csrf() -> None:
    patch_calls: list[tuple[dict[str, object], int]] = []
    server = _server(patch_calls=patch_calls)
    server.start()
    try:
        cookie, csrf = _open_session(server)
        replay, _, _ = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={
                "Origin": server.origin,
                "X-Subagent-MCP-Token": _bootstrap_token(server),
            },
        )
        anonymous, _, _ = _request(server, "GET", "/api/v1/snapshot")
        status, _, snapshot_body = _request(
            server,
            "GET",
            "/api/v1/snapshot",
            headers={"Cookie": cookie},
        )
        patch_body = json.dumps(
            {"revision": 7, "runtimes": {"fake": {"enabled": False}}}
        ).encode("utf-8")
        missing_csrf, _, _ = _request(
            server,
            "PATCH",
            "/api/v1/config",
            headers={
                "Cookie": cookie,
                "Origin": server.origin,
                "Content-Type": "application/json",
            },
            body=patch_body,
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
            body=patch_body,
        )
    finally:
        server.close()

    assert replay == 401
    assert anonymous == 401
    assert status == 200
    snapshot = json.loads(snapshot_body)
    assert "prompt" not in snapshot
    assert "prompt" not in snapshot["activity"][0]
    assert "events" not in snapshot["activity"][0]
    assert b"must never leave the backend" not in snapshot_body
    assert missing_csrf == 403
    assert patched == 200
    assert json.loads(patched_body) == {"revision": 8}
    assert patch_calls == [({"runtimes": {"fake": {"enabled": False}}}, 7)]


def test_managed_ui_requires_bootstrap_for_its_first_browser_session() -> None:
    server = _server(control_token="managed-control-token-with-enough-entropy")
    server.start()
    try:
        status, _, _ = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={"Origin": server.origin},
        )
    finally:
        server.close()

    assert status == 401


def test_authorized_managed_tab_restores_its_existing_session() -> None:
    server = _server(control_token="managed-control-token-with-enough-entropy")
    server.start()
    try:
        cookie, csrf = _open_session(server)

        restored, restored_headers, restored_body = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={"Cookie": cookie, "Origin": server.origin},
        )
        snapshot, _, _ = _request(
            server,
            "GET",
            "/api/v1/snapshot",
            headers={"Cookie": cookie},
        )
    finally:
        server.close()

    assert restored == 200
    assert json.loads(restored_body) == {"csrf_token": csrf}
    assert "set-cookie" not in restored_headers
    assert snapshot == 200


def test_foreground_ui_still_requires_bootstrap_for_its_first_session() -> None:
    server = _server()
    server.start()
    try:
        status, _, _ = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={"Origin": server.origin},
        )
    finally:
        server.close()

    assert status == 401


def test_authorized_foreground_tab_restores_its_existing_session() -> None:
    server = _server()
    server.start()
    try:
        cookie, csrf = _open_session(server)
        status, headers, body = _request(
            server,
            "POST",
            "/api/v1/session",
            headers={"Cookie": cookie, "Origin": server.origin},
        )
    finally:
        server.close()

    assert status == 200
    assert json.loads(body) == {"csrf_token": csrf}
    assert "set-cookie" not in headers


def test_provider_refresh_requires_csrf_and_an_empty_body() -> None:
    refresh_calls: list[None] = []
    server = _server(refresh_calls=refresh_calls)
    server.start()
    try:
        cookie, csrf = _open_session(server)
        missing_csrf, _, _ = _request(
            server,
            "POST",
            "/api/v1/refresh",
            headers={"Cookie": cookie, "Origin": server.origin},
        )
        nonempty, _, _ = _request(
            server,
            "POST",
            "/api/v1/refresh",
            headers={
                "Cookie": cookie,
                "Origin": server.origin,
                "X-CSRF-Token": csrf,
            },
            body=b"{}",
        )
    finally:
        server.close()

    assert missing_csrf == 403
    assert nonempty == 400
    assert refresh_calls == []


def test_provider_refresh_returns_one_sanitized_snapshot() -> None:
    refresh_calls: list[None] = []
    server = _server(refresh_calls=refresh_calls)
    server.start()
    try:
        cookie, csrf = _open_session(server)
        status, _, body = _request(
            server,
            "POST",
            "/api/v1/refresh",
            headers={
                "Cookie": cookie,
                "Origin": server.origin,
                "X-CSRF-Token": csrf,
            },
        )
    finally:
        server.close()

    payload = json.loads(body)
    assert status == 200
    assert refresh_calls == [None]
    assert payload["quota"] == {"state": "available", "overage_blocked": True}
    assert b"must never leave the backend" not in body


def test_oversized_or_unexpected_config_payload_is_rejected_before_callback() -> None:
    patch_calls: list[tuple[dict[str, object], int]] = []
    server = _server(patch_calls=patch_calls)
    server.start()
    try:
        cookie, csrf = _open_session(server)
        common = {
            "Cookie": cookie,
            "Origin": server.origin,
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
        }
        unexpected, _, _ = _request(
            server,
            "PATCH",
            "/api/v1/config",
            headers=common,
            body=json.dumps({"revision": 7, "lifecycle": {"close": True}}).encode(),
        )
        oversized, _, _ = _request(
            server,
            "PATCH",
            "/api/v1/config",
            headers=common,
            body=(b" " * (256 * 1024 + 1)),
        )
    finally:
        server.close()

    assert unexpected == 400
    assert oversized == 413
    assert patch_calls == []
