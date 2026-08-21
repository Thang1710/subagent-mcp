"""Small, explicit lifecycle for the optional localhost UI process."""

from __future__ import annotations

import hmac
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .paths import ProductPaths


CONTROL_HEADER = "X-Subagent-MCP-Control"
CONTROL_STOP_PATH = "/api/v1/control/stop"
_MAX_CONTROL_BYTES = 4096
_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WINDOWS_CREATE_NO_WINDOW = 0x08000000


class UiProcessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BackgroundUiResult:
    changed: bool
    running: bool
    managed: bool
    port: int
    pid: int | None


@dataclass(frozen=True, slots=True)
class _ControlRecord:
    pid: int
    port: int
    token: str
    payload: bytes


class _ChildProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


PopenFactory = Callable[..., _ChildProcess]
Probe = Callable[[int], bool]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
StopRequester = Callable[[_ControlRecord], None]


def probe_ui(port: int) -> bool:
    """Return true only for the expected Subagent MCP loopback page."""

    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        payload = response.read(32 * 1024 + 1)
        server = response.getheader("Server", "")
        return (
            response.status == 200
            and len(payload) <= 32 * 1024
            and server.startswith("Subagent-MCP")
            and b"Subagent MCP" in payload
        )
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def start_background_ui(
    paths: ProductPaths,
    *,
    port: int,
    open_browser: bool,
    executable: str | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
    probe: Probe = probe_ui,
    sleeper: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    timeout_seconds: float = 10.0,
    platform: str | None = None,
) -> BackgroundUiResult:
    """Start one detached UI child and wait for the product health marker."""

    _require_paths(paths)
    if port == 0:
        raise UiProcessError(
            "UI_BACKGROUND_PORT_REQUIRED",
            "background UI requires a fixed loopback port",
        )
    _require_port(port)
    if probe(port):
        current = status_background_ui(paths, port=port, probe=probe)
        return BackgroundUiResult(False, True, current.managed, port, current.pid)
    if timeout_seconds <= 0:
        raise UiProcessError("UI_START_INVALID", "background startup timeout is invalid")

    argv = [
        sys.executable if executable is None else executable,
        "-I",
        "-B",
        "-m",
        "subagent_harness_mcp.cli",
        "ui",
        "--port",
        str(port),
        "--background-child",
    ]
    if not open_browser:
        argv.append("--no-open")
    active_platform = os.name if platform is None else platform
    kwargs: dict[str, object] = {
        "close_fds": True,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if active_platform == "nt":
        kwargs["creationflags"] = (
            _WINDOWS_CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        child = popen_factory(argv, **kwargs)
    except OSError as exc:
        raise UiProcessError("UI_START_FAILED", "background UI could not start") from exc

    deadline = clock() + timeout_seconds
    while clock() < deadline:
        if probe(port):
            return BackgroundUiResult(True, True, True, port, int(child.pid))
        returncode = child.poll()
        if returncode is not None:
            raise UiProcessError(
                "UI_START_FAILED",
                "background UI exited before becoming healthy",
            )
        sleeper(0.05)
    try:
        child.terminate()
        child.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass
    raise UiProcessError("UI_START_TIMEOUT", "background UI did not become healthy")


def status_background_ui(
    paths: ProductPaths,
    *,
    port: int,
    probe: Probe = probe_ui,
) -> BackgroundUiResult:
    _require_paths(paths)
    _require_port(port)
    if not probe(port):
        return BackgroundUiResult(False, False, False, port, None)
    try:
        record = _read_control_record(paths.ui_control_file)
    except FileNotFoundError:
        return BackgroundUiResult(False, True, False, port, None)
    if record.port != port:
        return BackgroundUiResult(False, True, False, port, None)
    return BackgroundUiResult(False, True, True, port, record.pid)


def stop_background_ui(
    paths: ProductPaths,
    *,
    port: int,
    probe: Probe = probe_ui,
    request_stop: StopRequester | None = None,
    sleeper: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    timeout_seconds: float = 5.0,
) -> BackgroundUiResult:
    """Request graceful shutdown through the token-bound loopback endpoint."""

    _require_paths(paths)
    _require_port(port)
    try:
        record = _read_control_record(paths.ui_control_file)
    except FileNotFoundError:
        if probe(port):
            raise UiProcessError(
                "UI_UNMANAGED",
                "a foreground or unrelated UI owns the requested port",
            )
        return BackgroundUiResult(False, False, False, port, None)
    if record.port != port:
        raise UiProcessError(
            "UI_CONTROL_MISMATCH",
            "the background UI control record uses a different port",
        )
    if not probe(port):
        remove_control_record(paths.ui_control_file, record.payload)
        return BackgroundUiResult(True, False, True, port, record.pid)
    if timeout_seconds <= 0:
        raise UiProcessError("UI_STOP_INVALID", "background stop timeout is invalid")

    requester = _request_stop if request_stop is None else request_stop
    requester(record)
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        if not probe(port):
            remove_control_record(paths.ui_control_file, record.payload)
            return BackgroundUiResult(True, False, True, port, record.pid)
        sleeper(0.05)
    raise UiProcessError("UI_STOP_TIMEOUT", "background UI did not stop cleanly")


def publish_control_record(path: Path, *, pid: int, port: int, token: str) -> bytes:
    """Atomically publish the exact token-bound control record."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise UiProcessError("UI_CONTROL_INVALID", "control path must be absolute")
    _require_pid(pid)
    _require_port(port)
    _require_token(token)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise UiProcessError("UI_CONTROL_INVALID", "control path is not a regular file")
        payload = _encode_control(pid=pid, port=port, token=token)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            descriptor = -1
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return payload
    except UiProcessError:
        raise
    except OSError as exc:
        raise UiProcessError("UI_CONTROL_WRITE_FAILED", "control record could not be written") from exc


def remove_control_record(path: Path, expected_payload: bytes) -> bool:
    """Remove only the exact regular file published by this UI process."""

    try:
        if path.is_symlink() or not path.is_file():
            return False
        current = path.read_bytes()
        if not _constant_bytes_equal(current, expected_payload):
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _request_stop(record: _ControlRecord) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", record.port, timeout=2)
    try:
        connection.request(
            "POST",
            CONTROL_STOP_PATH,
            body=b"",
            headers={
                "Host": f"127.0.0.1:{record.port}",
                "Origin": f"http://127.0.0.1:{record.port}",
                CONTROL_HEADER: record.token,
                "Content-Length": "0",
            },
        )
        response = connection.getresponse()
        response.read(4097)
        if response.status != 202:
            raise UiProcessError(
                "UI_STOP_REJECTED",
                "background UI rejected its control token",
            )
    except UiProcessError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise UiProcessError("UI_STOP_FAILED", "background UI stop request failed") from exc
    finally:
        connection.close()


def _read_control_record(path: Path) -> _ControlRecord:
    try:
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(path)
        payload = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UiProcessError("UI_CONTROL_INVALID", "control record cannot be read") from exc
    if not payload or len(payload) > _MAX_CONTROL_BYTES:
        raise UiProcessError("UI_CONTROL_INVALID", "control record size is invalid")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise UiProcessError("UI_CONTROL_INVALID", "control record is malformed") from exc
    if not isinstance(value, Mapping) or set(value) != {
        "pid",
        "port",
        "schema_version",
        "token",
    }:
        raise UiProcessError("UI_CONTROL_INVALID", "control record fields are invalid")
    if value.get("schema_version") != 1:
        raise UiProcessError("UI_CONTROL_INVALID", "control record schema is unsupported")
    pid = value.get("pid")
    port = value.get("port")
    token = value.get("token")
    _require_pid(pid)
    _require_port(port)
    _require_token(token)
    assert isinstance(pid, int) and isinstance(port, int) and isinstance(token, str)
    if payload != _encode_control(pid=pid, port=port, token=token):
        raise UiProcessError("UI_CONTROL_INVALID", "control record is not canonical")
    return _ControlRecord(pid=pid, port=port, token=token, payload=payload)


def _encode_control(*, pid: int, port: int, token: str) -> bytes:
    return (
        json.dumps(
            {"pid": pid, "port": port, "schema_version": 1, "token": token},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _reject_duplicates(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant_bytes_equal(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)


def _require_paths(paths: object) -> None:
    if not isinstance(paths, ProductPaths):
        raise UiProcessError("UI_CONTROL_INVALID", "product paths are invalid")


def _require_pid(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UiProcessError("UI_CONTROL_INVALID", "control PID is invalid")


def _require_port(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise UiProcessError("UI_CONTROL_INVALID", "control port is invalid")


def _require_token(value: object) -> None:
    if (
        not isinstance(value, str)
        or not 24 <= len(value) <= 256
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise UiProcessError("UI_CONTROL_INVALID", "control token is invalid")
