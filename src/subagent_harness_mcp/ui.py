"""Temporary loopback-only settings and activity UI."""

from __future__ import annotations

import asyncio
import copy
import hmac
import ipaddress
import json
import secrets
import socket
import threading
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from . import __version__
from .config import ConfigError, ConfigStore
from .contracts import ServiceError


_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_SESSION_COOKIE = "smcp_session"
_MAX_BODY_BYTES = 256 * 1024
_MAX_HEADER_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PUBLIC_DEPTH = 10
_PUBLIC_TOP_LEVEL = frozenset(
    {
        "activity",
        "banners",
        "channel",
        "circuits",
        "config",
        "distribution",
        "health",
        "quota",
        "revision",
        "runtimes",
        "trust",
        "update",
        "version",
    }
)
_ACTIVITY_FIELDS = frozenset(
    {
        "adapter",
        "agentId",
        "completedAt",
        "createdAt",
        "durationMs",
        "duration_ms",
        "elapsedMs",
        "endedAt",
        "finishedAt",
        "finished_at",
        "id",
        "label",
        "name",
        "phase",
        "runtime",
        "runtimeName",
        "startedAt",
        "started_at",
        "state",
        "status",
        "taskId",
        "title",
    }
)
_PRIVATE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "chain_of_thought",
        "cookie",
        "credentials",
        "events",
        "hidden_thinking",
        "password",
        "prompt",
        "raw",
        "raw_events",
        "raw_output",
        "raw_provider_output",
        "secret",
        "system_prompt",
        "thinking",
        "thinking_content",
        "token",
        "transcript",
    }
)
_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src-elem 'self'; "
    "style-src-attr 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'none'; "
    "font-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "manifest-src 'none'; "
    "worker-src 'none'"
)

SnapshotProvider = Callable[[], Mapping[str, Any]]
ConfigPatcher = Callable[[dict[str, Any], int], Mapping[str, Any]]
ProviderRefresher = Callable[[], Mapping[str, Any]]
BrowserOpener = Callable[[str], object]


class UiError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocalUiBackend:
    """Whitelisted UI projection over the existing service and config store."""

    def __init__(
        self,
        *,
        config: ConfigStore,
        service: object,
        store: object | None = None,
    ) -> None:
        self._config = config
        self._service = service
        self._store = store
        self._provider_quota: dict[str, Any] | None = None

    def snapshot(self) -> Mapping[str, Any]:
        document = self._config.load()
        configured_runtime_ids = _configured_runtime_ids(document)
        records = _runtime_records(self._service)
        runtimes = _runtime_cards(document, records)
        circuits, activity = _read_ui_state(self._store)
        enabled_states = [item["status"]["state"] for item in runtimes if item["enabled"]]
        if any(state == "auto_paused" for state in enabled_states):
            health_state = "unavailable"
        elif any(state in {"quarantined", "incompatible", "unhealthy"} for state in enabled_states):
            health_state = "degraded"
        elif any(state == "needs_canary" for state in enabled_states):
            health_state = "needs_canary"
        elif any(state == "available" for state in enabled_states):
            health_state = "available"
        elif not enabled_states and any(
            item["status"]["state"] == "not_configured" for item in runtimes
        ):
            health_state = "setup_required"
        else:
            health_state = "ready"
        return {
            "distribution": "subagent-harness-mcp",
            "channel": "Windows Managed Preview",
            "version": __version__,
            "revision": document["revision"],
            "health": {
                "state": health_state,
                "label": _human_state(health_state),
                "messages": [
                    {
                        "label": item["name"],
                        "state": item["status"]["state"],
                        "detail": item["status"].get("detail", ""),
                    }
                    for item in runtimes
                ],
                "circuits": circuits,
            },
            "circuits": circuits,
            "quota": self._provider_quota
            or _quota_presentation(
                "check_required" if configured_runtime_ids else "configure_first"
            ),
            "update": {
                "state": "not_checked",
                "label": "Not checked",
            },
            "runtimes": runtimes,
            "trust": _trust_entries(document),
            "activity": activity,
        }

    def patch_config(
        self,
        patch: dict[str, Any],
        expected_revision: int,
    ) -> Mapping[str, Any]:
        document = self._config.load()
        if document["revision"] != expected_revision:
            raise ConfigError("REVISION_CONFLICT", "config changed since it was read")
        candidate = copy.deepcopy(document)
        manifests = _runtime_manifests(_runtime_records(self._service))
        _apply_runtime_patch(
            candidate,
            patch.get("runtimes"),
            manifests=manifests,
        )
        _apply_trust_patch(candidate, patch.get("trust"))
        saved = self._config.save(candidate, expected_revision=expected_revision)
        self._provider_quota = None
        return {"revision": saved["revision"]}

    def refresh_provider(self) -> Mapping[str, Any]:
        runtime_ids = _configured_runtime_ids(self._config.load())
        states: list[str] = []
        error_code: str | None = None
        overage_blocked = True
        for runtime_id in runtime_ids:
            try:
                checked = _run_service_method(
                    self._service,
                    "runtime_check",
                    runtime_id,
                    True,
                )
            except ServiceError:
                states.append("unknown")
                overage_blocked = False
                continue
            quota = checked.get("quota") if isinstance(checked, Mapping) else None
            state = quota.get("state") if isinstance(quota, Mapping) else "unknown"
            variants = quota.get("variants") if isinstance(quota, Mapping) else ()
            runtime_error_code: str | None = None
            if isinstance(variants, Sequence) and not isinstance(
                variants, (str, bytes, bytearray)
            ):
                if any(
                    isinstance(item, Mapping)
                    and item.get("error_code") == "CAPABILITY_MISSING"
                    for item in variants
                ):
                    runtime_error_code = "CAPABILITY_MISSING"
                    error_code = runtime_error_code
            if state == "quota_paused" and runtime_error_code == "CAPABILITY_MISSING":
                state = "unknown"
            states.append(state if isinstance(state, str) else "unknown")
            overage_blocked = bool(
                overage_blocked
                and isinstance(quota, Mapping)
                and quota.get("overage_blocked") is True
            )
        if not states:
            state = "configure_first"
        elif all(item == "available" for item in states):
            state = "available"
        elif "quota_paused" in states:
            state = "quota_paused"
        else:
            state = "unknown"
        self._provider_quota = _quota_presentation(
            state,
            overage_blocked=overage_blocked,
            error_code=error_code,
        )
        return self.snapshot()


class _UiState:
    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        config_patcher: ConfigPatcher,
        provider_refresher: ProviderRefresher | None,
    ) -> None:
        self.snapshot_provider = snapshot_provider
        self.config_patcher = config_patcher
        self.provider_refresher = provider_refresher
        self.bootstrap_token: str | None = secrets.token_urlsafe(32)
        self.sessions: dict[str, str] = {}
        self.lock = threading.Lock()
        self.origin = ""
        self.host_header = ""

    def exchange_bootstrap(self, supplied: str) -> tuple[str, str] | None:
        with self.lock:
            expected = self.bootstrap_token
            if expected is None or not hmac.compare_digest(supplied, expected):
                return None
            self.bootstrap_token = None
            session_id = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self.sessions[session_id] = csrf
            return session_id, csrf

    def csrf_for(self, session_id: str) -> str | None:
        with self.lock:
            return self.sessions.get(session_id)

    def clear(self) -> None:
        with self.lock:
            self.bootstrap_token = None
            self.sessions.clear()


class _Ipv6ThreadingHttpServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True


class _Ipv4ThreadingHttpServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True


class LoopbackUiServer:
    """Bound loopback HTTP server with an in-memory, single-use bootstrap."""

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        config_patcher: ConfigPatcher,
        *,
        provider_refresher: ProviderRefresher | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if host not in {"127.0.0.1", "::1"}:
            raise UiError(
                "LOOPBACK_REQUIRED",
                "the settings UI binds only to literal loopback addresses",
            )
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise UiError("UI_PORT_INVALID", "the settings UI port must be 0 through 65535")
        if not callable(snapshot_provider) or not callable(config_patcher):
            raise UiError("UI_BACKEND_INVALID", "UI callbacks must be callable")
        if provider_refresher is not None and not callable(provider_refresher):
            raise UiError("UI_BACKEND_INVALID", "provider refresh callback must be callable")
        self._state = _UiState(snapshot_provider, config_patcher, provider_refresher)
        server_type = (
            _Ipv6ThreadingHttpServer if host == "::1" else _Ipv4ThreadingHttpServer
        )
        try:
            self._httpd = server_type(
                (host, port),
                _handler_type(self._state),
                bind_and_activate=True,
            )
        except OSError as exc:
            raise UiError("UI_BIND_FAILED", "the loopback UI could not bind") from exc
        self.bound_host = host
        self.bound_port = int(self._httpd.server_address[1])
        rendered_host = f"[{host}]" if host == "::1" else host
        self.host_header = f"{rendered_host}:{self.bound_port}"
        self.origin = f"http://{self.host_header}"
        self._state.origin = self.origin
        self._state.host_header = self.host_header
        self._bootstrap_url = (
            f"{self.origin}/#token={quote(str(self._state.bootstrap_token), safe='')}"
        )
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def bootstrap_url(self) -> str:
        return self._bootstrap_url

    def start(self) -> threading.Thread:
        if self._closed:
            raise UiError("UI_CLOSED", "the loopback UI is closed")
        if self._thread is not None:
            return self._thread
        thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="subagent-mcp-ui",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return thread

    def serve_forever(self) -> None:
        if self._closed:
            raise UiError("UI_CLOSED", "the loopback UI is closed")
        if self._thread is not None:
            raise UiError("UI_ALREADY_RUNNING", "the loopback UI already has a thread")
        self._httpd.serve_forever(poll_interval=0.05)

    def open_browser(self, opener: BrowserOpener = webbrowser.open) -> bool:
        if self._closed:
            raise UiError("UI_CLOSED", "the loopback UI is closed")
        try:
            return bool(opener(self._bootstrap_url))
        except Exception as exc:
            raise UiError("BROWSER_OPEN_FAILED", "the browser could not be opened") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._httpd.shutdown()
            thread.join(timeout=5)
        self._httpd.server_close()
        self._state.clear()

    def __enter__(self) -> "LoopbackUiServer":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def create_local_backend() -> LocalUiBackend:
    """Create the same config/state/service stack used by the stdio surface."""

    from .adapters.claude_code import ClaudeCodeAdapter
    from .adapters.deepseek_harness import DeepSeekHarnessAdapter
    from .adapters.registry import AdapterRegistry
    from .paths import resolve_paths
    from .service import SubagentMcpService
    from .store import StateStore

    paths = resolve_paths()
    config = ConfigStore(paths)
    store = StateStore.open(paths)
    registry = AdapterRegistry(
        builtin_factories=(ClaudeCodeAdapter, DeepSeekHarnessAdapter)
    )
    registry.discover()
    service = SubagentMcpService(config=config, store=store, registry=registry)
    return LocalUiBackend(config=config, service=service, store=store)


def run_ui(*, port: int = 8765, browser_opener: BrowserOpener = webbrowser.open) -> int:
    """Bind first, open the fragment URL, and run until interrupted."""

    backend = create_local_backend()
    server = LoopbackUiServer(
        backend.snapshot,
        backend.patch_config,
        provider_refresher=backend.refresh_provider,
        port=port,
    )
    try:
        if not server.open_browser(browser_opener):
            raise UiError("BROWSER_OPEN_FAILED", "the browser declined the local URL")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
        return 0
    finally:
        server.close()


def _handler_type(state: _UiState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Subagent-MCP"
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            del args

        def parse_request(self) -> bool:
            if not super().parse_request():
                return False
            total = len(self.requestline.encode("utf-8", errors="replace"))
            for key, value in self.headers.items():
                total += len(key.encode("utf-8", errors="replace"))
                total += len(value.encode("utf-8", errors="replace"))
                total += 4
            if total > _MAX_HEADER_BYTES:
                self._json_error(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE, "HEADERS_TOO_LARGE")
                return False
            return True

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            del message, explain
            try:
                status = HTTPStatus(code)
            except ValueError:
                status = HTTPStatus.BAD_REQUEST
            self._json_error(status, "HTTP_ERROR")

        def handle_expect_100(self) -> bool:
            self._json_error(HTTPStatus.EXPECTATION_FAILED, "EXPECTATION_UNSUPPORTED")
            return False

        def do_GET(self) -> None:
            path = self._request_path(require_origin=False)
            if path is None:
                return
            if path == "/api/v1/snapshot":
                if self._session(require_csrf=False) is None:
                    return
                try:
                    snapshot = _public_snapshot(state.snapshot_provider())
                except (ConfigError, UiError) as exc:
                    self._backend_error(exc)
                    return
                except Exception:
                    self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "UI_BACKEND_FAILED")
                    return
                self._send_json(HTTPStatus.OK, snapshot)
                return
            asset = _ASSETS.get(path)
            if asset is None:
                self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return
            self._send_asset(*asset)

        def do_POST(self) -> None:
            path = self._request_path(require_origin=True)
            if path is None:
                return
            if path == "/api/v1/refresh":
                if self._session(require_csrf=True) is None:
                    self._discard_bounded_body()
                    return
                if not self._require_empty_body():
                    return
                if state.provider_refresher is None:
                    self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "REFRESH_UNAVAILABLE")
                    return
                try:
                    snapshot = _public_snapshot(state.provider_refresher())
                except (ConfigError, UiError) as exc:
                    self._backend_error(exc)
                    return
                except Exception:
                    self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "UI_BACKEND_FAILED")
                    return
                self._send_json(HTTPStatus.OK, snapshot)
                return
            if path != "/api/v1/session":
                self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return
            if not self._require_empty_body():
                return
            values = self.headers.get_all("X-Subagent-MCP-Token", [])
            if len(values) != 1 or not values[0] or len(values[0]) > 256:
                self._json_error(HTTPStatus.UNAUTHORIZED, "BOOTSTRAP_REJECTED")
                return
            exchanged = state.exchange_bootstrap(values[0])
            if exchanged is None:
                self._json_error(HTTPStatus.UNAUTHORIZED, "BOOTSTRAP_REJECTED")
                return
            session_id, csrf = exchanged
            cookie = (
                f"{_SESSION_COOKIE}={session_id}; Path=/; HttpOnly; "
                "SameSite=Strict"
            )
            self._send_json(
                HTTPStatus.OK,
                {"csrf_token": csrf},
                extra_headers={"Set-Cookie": cookie},
            )

        def do_PATCH(self) -> None:
            path = self._request_path(require_origin=True)
            if path is None:
                return
            if path != "/api/v1/config":
                self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return
            if self._session(require_csrf=True) is None:
                self._discard_bounded_body()
                return
            body = self._read_json_body()
            if body is None:
                return
            try:
                revision, patch = _config_patch_request(body)
                result = state.config_patcher(patch, revision)
                response = _config_response(result)
            except (ConfigError, UiError) as exc:
                self._backend_error(exc)
                return
            except Exception:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "UI_BACKEND_FAILED")
                return
            self._send_json(HTTPStatus.OK, response)

        def do_OPTIONS(self) -> None:
            self._json_error(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")

        def do_HEAD(self) -> None:
            self._json_error(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")

        def _request_path(self, *, require_origin: bool) -> str | None:
            peer = str(self.client_address[0])
            if not _is_loopback_peer(peer):
                self._json_error(HTTPStatus.FORBIDDEN, "NON_LOOPBACK_PEER")
                return None
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or not hmac.compare_digest(hosts[0], state.host_header):
                self._json_error(HTTPStatus.FORBIDDEN, "HOST_REJECTED")
                return None
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (origins and not hmac.compare_digest(origins[0], state.origin)):
                self._json_error(HTTPStatus.FORBIDDEN, "ORIGIN_REJECTED")
                return None
            if require_origin and len(origins) != 1:
                self._json_error(HTTPStatus.FORBIDDEN, "ORIGIN_REQUIRED")
                return None
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.query:
                self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return None
            try:
                path = unquote(target.path, errors="strict")
            except UnicodeError:
                self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return None
            segments = path.split("/")
            if (
                "\\" in path
                or "\x00" in path
                or any(segment in {".", ".."} for segment in segments)
            ):
                self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return None
            return path

        def _session(self, *, require_csrf: bool) -> str | None:
            cookie_headers = self.headers.get_all("Cookie", [])
            if len(cookie_headers) != 1:
                self._json_error(HTTPStatus.UNAUTHORIZED, "SESSION_REQUIRED")
                return None
            cookies = SimpleCookie()
            try:
                cookies.load(cookie_headers[0])
            except CookieError:
                self._json_error(HTTPStatus.UNAUTHORIZED, "SESSION_REQUIRED")
                return None
            morsel = cookies.get(_SESSION_COOKIE)
            if morsel is None or len(morsel.value) > 256:
                self._json_error(HTTPStatus.UNAUTHORIZED, "SESSION_REQUIRED")
                return None
            expected_csrf = state.csrf_for(morsel.value)
            if expected_csrf is None:
                self._json_error(HTTPStatus.UNAUTHORIZED, "SESSION_REQUIRED")
                return None
            if require_csrf:
                supplied = self.headers.get_all("X-CSRF-Token", [])
                if (
                    len(supplied) != 1
                    or len(supplied[0]) > 256
                    or not hmac.compare_digest(supplied[0], expected_csrf)
                ):
                    self._json_error(HTTPStatus.FORBIDDEN, "CSRF_REJECTED")
                    return None
            return morsel.value

        def _require_empty_body(self) -> bool:
            if self.headers.get("Transfer-Encoding") is not None:
                self._json_error(HTTPStatus.BAD_REQUEST, "TRANSFER_ENCODING_REJECTED")
                return False
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return True
            try:
                length = int(raw_length, 10)
            except ValueError:
                self._json_error(HTTPStatus.BAD_REQUEST, "BODY_INVALID")
                return False
            if length != 0:
                self._discard_bounded_body()
                self._json_error(HTTPStatus.BAD_REQUEST, "BODY_NOT_ALLOWED")
                return False
            return True

        def _read_json_body(self) -> Mapping[str, Any] | None:
            if self.headers.get("Transfer-Encoding") is not None:
                self._json_error(HTTPStatus.BAD_REQUEST, "TRANSFER_ENCODING_REJECTED")
                return None
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().casefold() != "application/json":
                self._json_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON_REQUIRED")
                return None
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length, 10) if raw_length is not None else -1
            except ValueError:
                length = -1
            if length < 0:
                self._json_error(HTTPStatus.LENGTH_REQUIRED, "CONTENT_LENGTH_REQUIRED")
                return None
            if length > _MAX_BODY_BYTES:
                self._discard_bounded_body(maximum_bytes=_MAX_BODY_BYTES + 1)
                self._json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "BODY_TOO_LARGE")
                return None
            try:
                raw = self.rfile.read(length)
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                self._json_error(HTTPStatus.BAD_REQUEST, "JSON_INVALID")
                return None
            if not isinstance(value, Mapping):
                self._json_error(HTTPStatus.BAD_REQUEST, "JSON_OBJECT_REQUIRED")
                return None
            return value

        def _discard_bounded_body(
            self,
            *,
            maximum_bytes: int = _MAX_BODY_BYTES,
        ) -> None:
            if self.headers.get("Transfer-Encoding") is not None:
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length, 10) if raw_length is not None else 0
            except ValueError:
                return
            if length <= 0 or length > maximum_bytes:
                return
            previous_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(0.25)
                self.rfile.read(length)
            except OSError:
                pass
            finally:
                self.connection.settimeout(previous_timeout)

        def _send_asset(self, filename: str, content_type: str) -> None:
            try:
                payload = (
                    resources.files("subagent_harness_mcp")
                    .joinpath("static", filename)
                    .read_bytes()
                )
            except (FileNotFoundError, OSError):
                self._json_error(HTTPStatus.NOT_FOUND, "ASSET_NOT_FOUND")
                return
            if len(payload) > _MAX_RESPONSE_BYTES:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "ASSET_TOO_LARGE")
                return
            self.send_response(HTTPStatus.OK)
            self._security_headers(content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(
            self,
            status: HTTPStatus,
            value: Mapping[str, Any],
            *,
            extra_headers: Mapping[str, str] | None = None,
        ) -> None:
            try:
                payload = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                payload = b'{"error":"RESPONSE_INVALID","message":"The local UI response was invalid."}'
            if len(payload) > _MAX_RESPONSE_BYTES:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                payload = b'{"error":"RESPONSE_TOO_LARGE","message":"The local UI response was too large."}'
            self.send_response(status)
            self._security_headers("application/json; charset=utf-8")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json_error(self, status: HTTPStatus, code: str) -> None:
            self._send_json(
                status,
                {
                    "error": code,
                    "message": _error_message(status),
                },
            )

        def _backend_error(self, error: BaseException) -> None:
            code = str(getattr(error, "code", "UI_BACKEND_FAILED"))
            if code == "REVISION_CONFLICT":
                status = HTTPStatus.CONFLICT
            elif code in {
                "CONFIG_INVALID",
                "CONFIG_VERSION_UNSUPPORTED",
                "UI_PATCH_INVALID",
            }:
                status = HTTPStatus.BAD_REQUEST
            elif code in {"CONFIG_LOCK_TIMEOUT"}:
                status = HTTPStatus.SERVICE_UNAVAILABLE
            else:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                code = "UI_BACKEND_FAILED"
            self._send_json(
                status,
                {"error": code, "message": _error_message(status)},
            )

        def _security_headers(self, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
            )
            self.send_header("Connection", "close")
            self.close_connection = True

    return Handler


def _is_loopback_peer(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback


def _public_snapshot(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise UiError("UI_BACKEND_INVALID", "snapshot must be an object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        rendered_key = str(key)
        if rendered_key not in _PUBLIC_TOP_LEVEL:
            continue
        if rendered_key == "activity":
            result[rendered_key] = _public_activity(item)
            continue
        result[rendered_key] = _bounded_public_value(item, depth=0)
    return result


def _public_activity(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:128]:
        if not isinstance(raw, Mapping):
            continue
        item = {
            str(key): _bounded_public_value(field, depth=1)
            for key, field in raw.items()
            if str(key) in _ACTIVITY_FIELDS
        }
        result.append(item)
    return result


def _bounded_public_value(value: object, *, depth: int) -> Any:
    if depth > _MAX_PUBLIC_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:16_384]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 256:
                break
            rendered_key = str(key)
            if rendered_key.casefold() in _PRIVATE_KEYS:
                continue
            result[rendered_key] = _bounded_public_value(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_public_value(item, depth=depth + 1)
            for item in value[:256]
        ]
    return str(value)[:4096]


def _config_patch_request(value: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    allowed = {"revision", "runtimes", "trust"}
    if any(str(key) not in allowed for key in value):
        raise UiError("UI_PATCH_INVALID", "config patch contains an unsupported field")
    revision = value.get("revision")
    if type(revision) is not int or revision < 0:
        raise UiError("UI_PATCH_INVALID", "config revision must be nonnegative")
    patch = {str(key): copy.deepcopy(item) for key, item in value.items() if key != "revision"}
    if not patch:
        raise UiError("UI_PATCH_INVALID", "config patch is empty")
    _require_json_shape(patch, depth=0)
    return revision, patch


def _require_json_shape(value: object, *, depth: int) -> None:
    if depth > _MAX_PUBLIC_DEPTH:
        raise UiError("UI_PATCH_INVALID", "config patch is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 16_384:
            raise UiError("UI_PATCH_INVALID", "config value is too long")
        return
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise UiError("UI_PATCH_INVALID", "config object has too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8")) > 512:
                raise UiError("UI_PATCH_INVALID", "config field name is invalid")
            _require_json_shape(item, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 256:
            raise UiError("UI_PATCH_INVALID", "config array has too many entries")
        for item in value:
            _require_json_shape(item, depth=depth + 1)
        return
    raise UiError("UI_PATCH_INVALID", "config patch is not JSON-safe")


def _config_response(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise UiError("UI_BACKEND_INVALID", "config callback returned an invalid value")
    revision = value.get("revision")
    if type(revision) is not int or revision < 0:
        raise UiError("UI_BACKEND_INVALID", "config callback omitted its revision")
    return {"revision": revision}


def _configured_runtime_ids(document: Mapping[str, Any]) -> list[str]:
    runtimes = document.get("runtimes", {})
    if not isinstance(runtimes, Mapping):
        return []
    result: list[str] = []
    for runtime_id, policy in runtimes.items():
        if not isinstance(runtime_id, str) or not isinstance(policy, Mapping):
            continue
        variants = policy.get("variants", ())
        if policy.get("enabled") is not True or not isinstance(variants, Sequence):
            continue
        if any(
            isinstance(item, Mapping)
            and isinstance(item.get("model"), str)
            and bool(item["model"])
            for item in variants
        ):
            result.append(runtime_id)
    return result


def _quota_presentation(
    state: str,
    *,
    overage_blocked: bool = False,
    error_code: str | None = None,
) -> dict[str, Any]:
    labels = {
        "available": "Available · overage blocked",
        "quota_paused": "Unavailable · quota paused",
        "check_required": "Check required",
        "configure_first": "Configure a runtime first",
        "unknown": "Unknown",
    }
    details = {
        "available": "Current provider evidence passed; usage credits stay blocked.",
        "quota_paused": "Included provider allowance is unavailable. Refresh after your plan or quota changes.",
        "check_required": "Refresh to ask the native harness for current provider evidence.",
        "configure_first": "Choose and enable a model before checking quota.",
        "unknown": "The provider did not return safe quota evidence.",
    }
    checked = state if state in labels else "unknown"
    result: dict[str, Any] = {
        "state": checked,
        "label": labels[checked],
        "detail": details[checked],
    }
    if checked == "unknown" and error_code == "CAPABILITY_MISSING":
        result["label"] = "Safety check unavailable"
        result["detail"] = (
            "Your provider quota is not known to be exhausted. The native harness "
            "did not expose no-overage evidence, so Refresh started no provider task."
        )
        result["reason_code"] = error_code
    if overage_blocked and checked in {"available", "quota_paused"}:
        result["overage_blocked"] = True
    return result


def _runtime_records(service: object) -> list[Mapping[str, Any]]:
    value = _run_service_method(service, "runtime_list")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise UiError("UI_BACKEND_INVALID", "runtime_list returned an invalid value")
    return [item for item in value if isinstance(item, Mapping)]


def _runtime_manifests(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    manifests: dict[str, Mapping[str, Any]] = {}
    for record in records:
        runtime_id = record.get("runtime_id")
        manifest = record.get("manifest")
        if (
            record.get("state") == "available"
            and isinstance(runtime_id, str)
            and isinstance(manifest, Mapping)
        ):
            manifests[runtime_id] = manifest
    return manifests


def _manifest_strings(manifest: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = manifest.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _schema_options(schema: object) -> list[dict[str, Any]]:
    if not isinstance(schema, Mapping):
        return []
    choices = schema.get("anyOf", ())
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        return []
    options: list[dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, Mapping) or not isinstance(choice.get("const"), str):
            continue
        value = choice["const"]
        title = choice.get("title")
        options.append(
            {
                "value": value,
                "label": title if isinstance(title, str) and title else value,
                "available": True,
            }
        )
    return options


def _runtime_subtitle(manifest: Mapping[str, Any]) -> str:
    proper_names = {
        "claude-code": "Claude Code",
        "deepseek-harness": "DeepSeek Harness",
        "multi-provider": "External provider",
    }
    provider_id = str(manifest.get("provider_id", ""))
    harness_id = str(manifest.get("harness_id", ""))
    provider = proper_names.get(provider_id, _human_state(provider_id))
    harness = proper_names.get(harness_id, _human_state(harness_id))
    if provider and harness:
        return f"{provider} model · {harness} native harness"
    return harness or provider


_CAPABILITY_UI = {
    "canary": (
        "Safety check",
        "Checks this runtime before Codex delegates managed work.",
    ),
    "session": (
        "Native session",
        "Keeps delegated work in the provider's native session.",
    ),
    "resume": (
        "Resume",
        "Continues the same native session in a later turn.",
    ),
    "workspace": (
        "Workspace",
        "Runs with the exact workspace selected by Codex.",
    ),
}


def _capability_card(capability: str) -> dict[str, Any]:
    label, detail = _CAPABILITY_UI.get(
        capability,
        (_human_state(capability), "Published by this runtime adapter."),
    )
    return {
        "id": capability,
        "label": label,
        "detail": detail,
        "available": True,
    }


def _simple_reasoning_fields(
    variant: Mapping[str, Any],
    manifest: Mapping[str, Any],
    index: int,
) -> list[dict[str, Any]] | None:
    schema = manifest.get("reasoning_schema")
    if not isinstance(schema, Mapping):
        return None
    if schema.get("additionalProperties") is False and schema.get("maxProperties") == 0:
        return []
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or len(properties) != 1:
        return None
    key, definition = next(iter(properties.items()))
    if not isinstance(key, str) or not isinstance(definition, Mapping):
        return None
    choices = definition.get("enum")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        return None
    values = [item for item in choices if isinstance(item, str) and item]
    if not values:
        return None
    reasoning = variant.get("reasoning", {})
    current = reasoning.get(key, "") if isinstance(reasoning, Mapping) else ""
    required = schema.get("required", ())
    return [
        {
            "id": f"variant.{index}.reasoning.{key}",
            "label": "Reasoning effort" if key == "effort" else _human_state(key),
            "kind": "select",
            "value": current,
            "options": [
                {"value": value, "label": value, "available": True}
                for value in values
            ],
            "required": isinstance(required, Sequence) and key in required,
            "help": "Higher effort can improve difficult work but uses more provider quota.",
        }
    ]


def _new_runtime_policy(manifest: Mapping[str, Any]) -> dict[str, Any]:
    transports = _manifest_strings(manifest, "supported_transports")
    return {
        "enabled": False,
        "delegation_priority": 0,
        "selection_mode": "fixed",
        "fallback": False,
        "transport": transports[0] if len(transports) == 1 else "",
        "variants": [
            {
                "id": "default",
                "model": "",
                "reasoning": {},
            }
        ],
    }


def _runtime_cards(
    document: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    policies = document.get("runtimes", {})
    if not isinstance(policies, Mapping):
        policies = {}
    by_id = {
        str(record.get("runtime_id")): record
        for record in records
        if isinstance(record.get("runtime_id"), str)
    }
    runtime_ids = sorted(
        set(by_id) | {str(key) for key in policies},
        key=lambda runtime_id: (
            -int(
                policies.get(runtime_id, {}).get("delegation_priority", 0)
                if isinstance(policies.get(runtime_id), Mapping)
                else 0
            ),
            runtime_id,
        ),
    )
    cards: list[dict[str, Any]] = []
    for runtime_id in runtime_ids:
        record = by_id.get(runtime_id, {})
        policy = policies.get(runtime_id, {})
        if not isinstance(policy, Mapping):
            policy = {}
        manifest = record.get("manifest")
        if not isinstance(manifest, Mapping):
            manifest = {}
        supported_transports = _manifest_strings(manifest, "supported_transports")
        capabilities = _manifest_strings(manifest, "capabilities")
        record_state = str(record.get("state", "unavailable"))
        if any(
            isinstance(circuit, Mapping) and circuit.get("state") == "auto_paused"
            for circuit in record.get("circuits", ())
        ):
            record_state = "auto_paused"
        configured = bool(policy)
        enabled = policy.get("enabled") is True
        if record_state == "available" and not configured:
            status_state = "not_configured"
        else:
            status_state = record_state
        reason = record.get("reason")
        if reason is None and status_state == "not_configured":
            reason = "Choose a model and reasoning effort, then save changes."
        configurable = configured or (
            record_state == "available" and bool(supported_transports)
        )
        cards.append(
            {
                "id": runtime_id,
                "name": str(manifest.get("display_name", runtime_id)),
                "manifest": dict(manifest),
                "subtitle": _runtime_subtitle(manifest),
                "status": {
                    "state": status_state,
                    "label": _human_state(status_state),
                    "detail": "" if reason is None else str(reason),
                },
                "needs_canary": status_state == "needs_canary",
                "enabled": enabled,
                "enabledLabel": "Available to Codex",
                "enabledHelp": (
                    "When enabled, Codex may delegate work after required safety checks pass."
                ),
                "configured": configured,
                "canEnable": configurable,
                "locked": not configurable,
                "capabilities": [_capability_card(str(item)) for item in capabilities],
                "groups": _runtime_groups(policy, manifest),
            }
        )
    return cards


def _runtime_groups(
    policy: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rendered_policy = policy if policy else _new_runtime_policy(manifest)
    variants = rendered_policy.get("variants", ())
    if not isinstance(variants, Sequence) or isinstance(variants, (str, bytes, bytearray)):
        variants = ()
    transports = _manifest_strings(manifest, "supported_transports")
    selected_transport = rendered_policy.get("transport", "")
    if selected_transport not in transports:
        selected_transport = transports[0] if len(transports) == 1 else ""
    groups: list[dict[str, Any]] = []
    policy_fields: list[dict[str, Any]] = []
    policy_fields.append(
        {
            "id": "delegation_priority",
            "label": "Delegation priority",
            "kind": "number",
            "value": rendered_policy.get("delegation_priority", 0),
            "min": 0,
            "max": 100,
            "step": 1,
            "required": True,
            "help": (
                "Higher values are preferred by the orchestrator. This orders "
                "external runtimes; the Codex host still controls native fallback."
            ),
        }
    )
    if len(variants) > 1:
        policy_fields.append(
            {
                    "id": "selection_mode",
                    "label": "Selection mode",
                    "kind": "select",
                    "value": rendered_policy.get("selection_mode", "fixed"),
                    "options": [
                        {
                            "value": "fixed",
                            "label": "Fixed",
                            "available": len(variants) == 1,
                        },
                        {
                            "value": "lead-selects",
                            "label": "Lead selects",
                            "available": True,
                        },
                    ],
                    "required": True,
            }
        )
    if len(transports) > 1:
        policy_fields.append(
            {
                    "id": "transport",
                    "label": "Transport",
                    "kind": "select",
                    "value": selected_transport,
                    "options": [
                        {
                            "value": transport,
                            "label": _human_state(transport),
                            "available": True,
                        }
                        for transport in transports
                    ],
                    "required": True,
                    "help": "Exact transport published by the selected adapter.",
            }
        )
    if policy_fields:
        groups.append({"id": "policy", "label": "Policy", "fields": policy_fields})
    for index, variant in enumerate(variants):
        if not isinstance(variant, Mapping):
            continue
        variant_id = str(variant.get("id", index))
        model_schema = manifest.get("model_schema", {})
        if not isinstance(model_schema, Mapping):
            model_schema = {}
        model_placeholder = model_schema.get(
            "placeholder", "Choose a model or enter an exact model ID"
        )
        model_help = model_schema.get(
            "description",
            "Choose a suggested model or enter another exact provider model ID.",
        )
        reasoning_fields = _simple_reasoning_fields(variant, manifest, index)
        if reasoning_fields is None:
            reasoning_fields = [
                {
                    "id": f"variant.{index}.reasoning",
                    "label": "Provider reasoning JSON",
                    "kind": "text",
                    "value": json.dumps(
                        variant.get("reasoning", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    "required": True,
                    "format": "json-object",
                    "help": "Validated by the selected adapter's provider-native schema.",
                }
            ]
        groups.append(
            {
                "id": f"variant-{index}",
                "label": (
                    ("Model & reasoning" if reasoning_fields else "Model")
                    if len(variants) == 1
                    else f"Variant {variant_id}"
                ),
                "fields": [
                    {
                        "id": f"variant.{index}.model",
                        "label": "Model",
                        "kind": "model",
                        "value": variant.get("model", ""),
                        "options": _schema_options(model_schema),
                        "required": True,
                        "placeholder": str(model_placeholder),
                        "help": str(model_help),
                    },
                    *reasoning_fields,
                ],
            }
        )
    context = rendered_policy.get("context")
    if isinstance(context, Mapping):
        fields: list[dict[str, Any]] = []
        for index, (key, value) in enumerate(context.items()):
            kind, rendered = _editable_value(value)
            fields.append(
                {
                    "id": f"context.{index}",
                    "label": _human_state(str(key)),
                    "kind": kind,
                    "value": rendered,
                    "help": f"Adapter-published context field: {key}",
                }
            )
        if fields:
            groups.append({"id": "context", "label": "Context", "fields": fields})
    return groups


def _apply_runtime_patch(
    document: dict[str, Any],
    raw: object,
    *,
    manifests: Mapping[str, Mapping[str, Any]],
) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping) or len(raw) > 128:
        raise UiError("UI_PATCH_INVALID", "runtimes patch must be an object")
    runtimes = document.get("runtimes")
    if not isinstance(runtimes, dict):
        raise ConfigError("CONFIG_INVALID", "runtimes must be an object")
    for runtime_id, change in raw.items():
        if (
            not isinstance(runtime_id, str)
            or not isinstance(change, Mapping)
        ):
            raise UiError("UI_PATCH_INVALID", "runtime patch is not recognized")
        if any(str(key) not in {"enabled", "options"} for key in change):
            raise UiError("UI_PATCH_INVALID", "runtime patch contains an unsupported field")
        creating = runtime_id not in runtimes
        if creating:
            if runtime_id not in manifests:
                raise UiError("UI_PATCH_INVALID", "runtime patch is not recognized")
            policy = _new_runtime_policy(manifests[runtime_id])
            runtimes[runtime_id] = policy
        else:
            policy = runtimes[runtime_id]
        if not isinstance(policy, dict):
            raise ConfigError("CONFIG_INVALID", "runtime policy must be an object")
        if "enabled" in change:
            if type(change["enabled"]) is not bool:
                raise UiError("UI_PATCH_INVALID", "runtime enabled must be boolean")
            policy["enabled"] = change["enabled"]
        options = change.get("options", {})
        if not isinstance(options, Mapping):
            raise UiError("UI_PATCH_INVALID", "runtime options must be an object")
        required_reasoning = manifests[runtime_id].get("reasoning_schema", {}).get(
            "required", ()
        ) if creating else ()
        has_required_reasoning = all(
            f"variant.0.reasoning.{key}" in options
            or "variant.0.reasoning" in options
            for key in required_reasoning
        )
        transports = _manifest_strings(manifests.get(runtime_id, {}), "supported_transports")
        has_transport = len(transports) == 1 or "transport" in options
        if creating and (
            "variant.0.model" not in options
            or not has_required_reasoning
            or not has_transport
        ):
            raise UiError(
                "UI_PATCH_INVALID",
                "new runtime policy needs model, reasoning, and a supported transport",
            )
        for field_id, value in options.items():
            _apply_runtime_option(
                policy,
                str(field_id),
                value,
                manifest=manifests.get(runtime_id, {}),
            )


def _apply_runtime_option(
    policy: dict[str, Any],
    field_id: str,
    value: object,
    *,
    manifest: Mapping[str, Any],
) -> None:
    if field_id == "delegation_priority":
        if type(value) is not int or not 0 <= value <= 100:
            raise UiError("UI_PATCH_INVALID", "delegation priority is invalid")
        policy["delegation_priority"] = value
        return
    if field_id == "selection_mode":
        if value not in {"fixed", "lead-selects"}:
            raise UiError("UI_PATCH_INVALID", "selection mode is invalid")
        policy["selection_mode"] = value
        return
    if field_id == "transport":
        if not isinstance(value, str) or value not in _manifest_strings(
            manifest, "supported_transports"
        ):
            raise UiError("UI_PATCH_INVALID", "transport is not supported")
        policy["transport"] = value
        return
    parts = field_id.split(".")
    if (
        len(parts) == 4
        and parts[0] == "variant"
        and parts[1].isdigit()
        and parts[2] == "reasoning"
    ):
        variants = policy.get("variants")
        index = int(parts[1])
        if (
            not isinstance(variants, list)
            or index >= len(variants)
            or not isinstance(variants[index], dict)
        ):
            raise UiError("UI_PATCH_INVALID", "variant option is not recognized")
        schema = manifest.get("reasoning_schema", {})
        properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
        definition = properties.get(parts[3]) if isinstance(properties, Mapping) else None
        choices = definition.get("enum", ()) if isinstance(definition, Mapping) else ()
        if value not in choices:
            raise UiError("UI_PATCH_INVALID", "reasoning value is not supported")
        reasoning = variants[index].get("reasoning")
        if not isinstance(reasoning, dict):
            reasoning = {}
            variants[index]["reasoning"] = reasoning
        reasoning[parts[3]] = value
        return
    if len(parts) == 3 and parts[0] == "variant" and parts[1].isdigit():
        variants = policy.get("variants")
        index = int(parts[1])
        if (
            not isinstance(variants, list)
            or index >= len(variants)
            or not isinstance(variants[index], dict)
        ):
            raise UiError("UI_PATCH_INVALID", "variant option is not recognized")
        if parts[2] == "model":
            if not isinstance(value, str):
                raise UiError("UI_PATCH_INVALID", "model must be text")
            variants[index]["model"] = value
            return
        if parts[2] == "reasoning":
            if not isinstance(value, str):
                raise UiError("UI_PATCH_INVALID", "reasoning must be JSON text")
            try:
                reasoning = json.loads(value)
            except json.JSONDecodeError as exc:
                raise UiError("UI_PATCH_INVALID", "reasoning JSON is malformed") from exc
            if not isinstance(reasoning, Mapping):
                raise UiError("UI_PATCH_INVALID", "reasoning must be a JSON object")
            variants[index]["reasoning"] = dict(reasoning)
            return
    if len(parts) == 2 and parts[0] == "context" and parts[1].isdigit():
        context = policy.get("context")
        index = int(parts[1])
        if not isinstance(context, dict) or index >= len(context):
            raise UiError("UI_PATCH_INVALID", "context option is not recognized")
        key = list(context)[index]
        context[key] = _coerce_editable_value(context[key], value)
        return
    raise UiError("UI_PATCH_INVALID", "runtime option is not recognized")


def _apply_trust_patch(document: dict[str, Any], raw: object) -> None:
    if raw is None:
        return
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise UiError("UI_PATCH_INVALID", "trust patch must be an array")
    entries = document.get("trust", [])
    if not isinstance(entries, list):
        raise ConfigError("CONFIG_INVALID", "trust state must be an array")
    for change in raw:
        if not isinstance(change, Mapping) or set(change) != {"path", "hash", "trusted"}:
            raise UiError("UI_PATCH_INVALID", "trust entry is invalid")
        if type(change["trusted"]) is not bool:
            raise UiError("UI_PATCH_INVALID", "trust state must be boolean")
        match = next(
            (
                item
                for item in entries
                if isinstance(item, dict)
                and item.get("path") == change["path"]
                and item.get("hash") == change["hash"]
            ),
            None,
        )
        if match is None:
            raise UiError("UI_PATCH_INVALID", "trust path and hash are not recognized")
        match["trusted"] = change["trusted"]


def _trust_entries(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("trust", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[:128]:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        digest = item.get("hash")
        if not isinstance(path, str) or not isinstance(digest, str):
            continue
        result.append(
            {
                "path": path,
                "hash": digest,
                "trusted": item.get("trusted") is True,
                "state": str(item.get("state", "recorded")),
            }
        )
    return result


def _read_ui_state(store: object | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transaction = getattr(store, "transaction", None)
    if not callable(transaction):
        return [], []
    try:
        with transaction() as database:
            circuit_rows = database.execute(
                """
                SELECT runtime_id, variant_id, state, category, retry_after_utc
                FROM circuits ORDER BY updated_at_utc DESC LIMIT 64
                """
            ).fetchall()
            activity_rows = database.execute(
                """
                SELECT e.execution_id, c.runtime_id, e.state,
                       e.created_at_utc, e.terminal_at_utc
                FROM executions AS e
                JOIN conversations AS c ON c.conversation_id = e.conversation_id
                ORDER BY e.rowid DESC LIMIT 50
                """
            ).fetchall()
    except Exception:
        return [], []
    circuits = [
        {
            "id": f"{row[0]}:{row[1]}",
            "name": str(row[0]),
            "state": str(row[2]),
            "detail": "" if row[3] is None else str(row[3]),
            "retryAt": row[4],
        }
        for row in circuit_rows
    ]
    activity = [
        {
            "id": str(row[0]),
            "title": "External-agent execution",
            "runtime": str(row[1]),
            "state": str(row[2]),
            "startedAt": row[3],
            "finishedAt": row[4],
        }
        for row in activity_rows
    ]
    return circuits, activity


def _run_service_method(
    service: object,
    name: str,
    *args: object,
    **kwargs: object,
) -> object:
    method = getattr(service, name, None)
    if not callable(method):
        raise UiError("UI_BACKEND_INVALID", f"service method {name} is unavailable")
    result = method(*args, **kwargs)
    if not hasattr(result, "__await__"):
        return result
    return asyncio.run(result)


def _editable_value(value: object) -> tuple[str, object]:
    if type(value) is bool:
        return "boolean", value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number", value
    if isinstance(value, str):
        return "text", value
    return (
        "text",
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )


def _coerce_editable_value(original: object, value: object) -> object:
    if type(original) is bool:
        if type(value) is not bool:
            raise UiError("UI_PATCH_INVALID", "context value must be boolean")
        return value
    if isinstance(original, (int, float)) and not isinstance(original, bool):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise UiError("UI_PATCH_INVALID", "context value must be numeric")
        return value
    if isinstance(original, str):
        if not isinstance(value, str):
            raise UiError("UI_PATCH_INVALID", "context value must be text")
        return value
    if not isinstance(value, str):
        raise UiError("UI_PATCH_INVALID", "context value must be JSON text")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise UiError("UI_PATCH_INVALID", "context JSON is malformed") from exc


def _human_state(value: str) -> str:
    words = value.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "Unknown"


def _error_message(status: HTTPStatus) -> str:
    if status == HTTPStatus.CONFLICT:
        return "Configuration changed elsewhere. Refresh and try again."
    if status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
        return "The local UI session was rejected."
    if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
        return "The request body is too large."
    if status == HTTPStatus.NOT_FOUND:
        return "The requested local UI resource does not exist."
    if status.value >= 500:
        return "The local UI could not complete this request."
    return "The local UI request was invalid."
