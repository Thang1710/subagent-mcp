from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


_SECRET_PATTERNS = (
    re.compile(r"(?i)(ANTHROPIC_API_KEY\s*=\s*)\S+"),
    re.compile(r"(?i)(ANTHROPIC_AUTH_TOKEN\s*=\s*)\S+"),
    re.compile(r"(?i)(CLAUDE_CODE_OAUTH_TOKEN\s*=\s*)\S+"),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
)

_SENSITIVE_KEY = re.compile(
    r"(?i)^(authorization|api[_-]?key|auth[_-]?token|oauth[_-]?token|"
    r"password|secret|cookie|email|org(?:anization)?_?id|orgId|request_?id)$"
)


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda match: match.group(1) + "[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def redact_data(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.fullmatch(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(item_key): redact_data(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    return value


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_fd_bounded(fd: int, limit: int) -> bytes:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(64 * 1024, limit + 1 - min(total, limit)))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError(f"input exceeds {limit} bytes")
        chunks.append(chunk)


@dataclass(frozen=True)
class ProbeResult:
    name: str
    argv: tuple[str, ...]
    cwd: str | None
    started_at: str
    duration_ms: int
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_argv(
    name: str,
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout_seconds: float = 30,
    env: Mapping[str, str] | None = None,
) -> ProbeResult:
    if not argv or any(not isinstance(part, str) for part in argv):
        raise ValueError("argv must be a non-empty sequence of strings")
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timed_out = True
    return ProbeResult(
        name=name,
        argv=tuple(argv),
        cwd=str(Path(cwd).resolve()) if cwd is not None else None,
        started_at=started_at,
        duration_ms=round((time.perf_counter() - started) * 1000),
        exit_code=exit_code,
        stdout=redact_text(stdout),
        stderr=redact_text(stderr),
        timed_out=timed_out,
    )


def write_json_atomic(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=target.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()
