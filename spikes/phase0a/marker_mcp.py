from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import threading
import time
from pathlib import Path


marker = os.environ.get("SUBAGENT_HARNESS_MCP_PHASE0A_MARKER")
marker_exit = os.environ.get("SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_EXIT")
marker_token = os.environ.get("SUBAGENT_HARNESS_MCP_PHASE0A_MARKER_TOKEN")
if not marker or not marker_exit or not marker_token or re.fullmatch(r"[0-9a-f]{32}", marker_token) is None:
    raise SystemExit("marker start, exit, and ownership token are required")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _process_image() -> Path:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            kernel32.GetCurrentProcess(), 0, buffer, ctypes.byref(size),
        ):
            raise OSError(ctypes.get_last_error(), "QueryFullProcessImageNameW failed")
        return Path(buffer.value).resolve(strict=True)
    if sys.platform.startswith("linux"):
        return Path("/proc/self/exe").resolve(strict=True)
    return Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)


def _creation_identity() -> str:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
        return f"windows:{(created.high << 32) | created.low}"
    if sys.platform.startswith("linux"):
        fields = Path("/proc/self/stat").read_text(encoding="ascii").rpartition(") ")[2].split()
        return f"linux:{fields[19]}"
    return "unsupported"


def _write_record(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _create_record(path: Path, value: dict[str, object]) -> None:
    data = json.dumps(value, sort_keys=True).encode("utf-8")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _deadline_exit() -> None:
    time.sleep(30)
    os._exit(124)


threading.Thread(target=_deadline_exit, daemon=True).start()
record = {
    "schema_version": 1,
    "pid": os.getpid(),
    "ownership_token": marker_token,
    "creation_identity": _creation_identity(),
    "executable_sha256": _sha256(_process_image()),
}
_create_record(Path(marker), record)

try:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "subagent-harness-mcp-phase0a-marker", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
finally:
    _write_record(Path(marker_exit), record)
