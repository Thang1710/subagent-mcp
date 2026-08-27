from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from .core import write_json_atomic


_MAX_BYTES = 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 30.0
_MAX_VERSION_BYTES = 128
_VERSION = re.compile(r"^grok \d{1,16}\.\d{1,16}\.\d{1,16} \([0-9a-f]{7,64}\)$")
_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_MODEL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_WINDOWS_ABSOLUTE_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_HELP_HEADERS = (
    "=== grok --help ===",
    "=== grok agent --help ===",
    "=== grok agent stdio --help ===",
)
_INSPECT_KEYS = {
    "mcp_count",
    "hook_count",
    "plugin_count",
    "cached_native_login",
    "api_key_override",
    "builtin_tool_names_complete",
    "builtin_tool_names",
}
_PROBE_ARGV = (
    ("--no-auto-update", "--version"),
    ("--no-auto-update", "--help"),
    ("--no-auto-update", "agent", "--help"),
    ("--no-auto-update", "agent", "stdio", "--help"),
    ("--no-auto-update", "inspect", "--json"),
    ("--no-auto-update", "models"),
)
_INSTALLED_KEYS = {
    "schema_version",
    "pair_state",
    "version_state",
    "help_state",
    "catalog_state",
    "extensions_discovered",
    "provider_key_environment_omitted",
    "cached_native_login",
    "no_extra_spend",
    "builtin_tool_inventory",
    "provider_readiness",
}
_CHILD_ENV_ALLOW = frozenset({
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "CURL_CA_BUNDLE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NODE_EXTRA_CA_CERTS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
})

@dataclass(frozen=True)
class GrokHelpContract:
    agent_stdio: bool = False
    no_leader: bool = False
    no_subagents: bool = False
    disable_web_search: bool = False
    deny: bool = False
    disallowed_tools: bool = False
    permission_mode: bool = False
    model: bool = False
    reasoning_effort: bool = False
    cwd: bool = False


@dataclass(frozen=True)
class GrokCliObservation:
    version: str
    help_contract: GrokHelpContract
    inspect_summary: Mapping[str, Any]
    models: tuple[Mapping[str, str], ...]

    @classmethod
    def unknown(cls) -> "GrokCliObservation":
        return cls("", GrokHelpContract(), {}, ())


def _bounded_text(value: Any, maximum: int = _MAX_BYTES) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        if len(value.encode("utf-8")) > maximum:
            return None
    except UnicodeError:
        return None
    if any(unicodedata.category(char) == "Cc" and char not in "\t\r\n" for char in value):
        return None
    return value


def _scalar(value: Any, *, whitespace_forbidden: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8")) > 256:
            return False
    except UnicodeError:
        return False
    if any(unicodedata.category(char) == "Cc" for char in value):
        return False
    return not any(char.isspace() for char in value) if whitespace_forbidden else bool(value.strip())


def parse_version(text: Any) -> str:
    value = _bounded_text(text, _MAX_VERSION_BYTES)
    if value is None:
        return ""
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n") or value.endswith("\r"):
        value = value[:-1]
    return value if _VERSION.fullmatch(value) else ""


def parse_help_contract(text: Any) -> GrokHelpContract:
    value = _bounded_text(text)
    if value is None:
        return GrokHelpContract()
    sections: dict[str, list[str]] = {}
    current = -1
    for line in value.splitlines():
        if line.startswith("===") and line.endswith("==="):
            if current + 1 == len(_HELP_HEADERS) or line != _HELP_HEADERS[current + 1]:
                return GrokHelpContract()
            current += 1
            sections[line] = []
        elif current < 0:
            if line.strip():
                return GrokHelpContract()
        else:
            sections[_HELP_HEADERS[current]].append(line)
    if current != len(_HELP_HEADERS) - 1:
        return GrokHelpContract()
    root, agent, stdio = (sections[header] for header in _HELP_HEADERS)
    def has_usage(lines: list[str], expected: str) -> bool:
        return [line for line in lines if line.startswith("Usage:")] == [expected]

    if not (
        has_usage(root, "Usage: grok [OPTIONS] [PROMPT] [COMMAND]")
        and has_usage(agent, "Usage: grok agent [OPTIONS] [COMMAND]")
        and has_usage(stdio, "Usage: grok agent stdio [OPTIONS]")
    ):
        return GrokHelpContract()

    def has_option(lines: list[str], name: str) -> bool:
        return any(
            re.match(
                rf"^\s{{2,}}(?:--{re.escape(name)}|-[A-Za-z0-9],\s+--{re.escape(name)})(?:[\s=<>\[]|$)",
                line,
            )
            for line in lines
        )

    def has_command(lines: list[str], name: str) -> bool:
        return any(re.match(rf"^\s{{2,}}{re.escape(name)}(?:[ \t]+|$)", line) for line in lines)

    return GrokHelpContract(
        agent_stdio=has_command(root, "agent") and has_command(agent, "stdio"),
        no_leader=has_option(agent, "no-leader"),
        no_subagents=has_option(root, "no-subagents"),
        disable_web_search=has_option(root, "disable-web-search"),
        deny=has_option(root, "deny"),
        disallowed_tools=has_option(root, "disallowed-tools"),
        permission_mode=has_option(root, "permission-mode"),
        model=has_option(root, "model"),
        reasoning_effort=has_option(root, "reasoning-effort"),
        cwd=has_option(root, "cwd"),
    )


def _inspect_payload(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, str):
        text = _bounded_text(value)
        if text is None:
            return None
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, RecursionError, UnicodeError):
            return None
    if not isinstance(value, dict):
        return None
    try:
        serialized = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(serialized.encode("utf-8")) > _MAX_BYTES:
            return None
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None
    return value


def parse_inspect_summary(value: Any) -> Mapping[str, Any]:
    payload = _inspect_payload(value)
    if payload is None or set(payload) != _INSPECT_KEYS:
        return {}
    tools = payload["builtin_tool_names"]
    if not (
        all(type(payload[name]) is int and payload[name] == 0 for name in ("mcp_count", "hook_count", "plugin_count"))
        and payload["cached_native_login"] is True
        and payload["api_key_override"] is False
        and payload["builtin_tool_names_complete"] is True
        and isinstance(tools, list)
        and tools
        and all(_scalar(tool, whitespace_forbidden=True) and _TOOL.fullmatch(tool) for tool in tools)
        and tools == sorted(tools)
        and len(tools) == len(set(tools))
    ):
        return {}
    return MappingProxyType({
        "mcp_count": 0,
        "hook_count": 0,
        "plugin_count": 0,
        "cached_native_login": True,
        "api_key_override": False,
        "builtin_tool_names": tuple(tools),
    })


def parse_model_catalog(text: Any) -> tuple[Mapping[str, str], ...]:
    value = _bounded_text(text)
    if value is None or not value:
        return ()
    rows: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for line in value.splitlines():
        if not line or line.count("\t") > 1:
            return ()
        if "\t" in line:
            model, label = line.split("\t")
        else:
            model = label = line
            if not any(char.isdigit() or char in "._:/@+-" for char in model):
                return ()
        if (
            not _scalar(model, whitespace_forbidden=True)
            or not _MODEL_VALUE.fullmatch(model)
            or _WINDOWS_ABSOLUTE_DRIVE.match(model)
            or not _scalar(label, whitespace_forbidden=False)
            or model in seen
        ):
            return ()
        seen.add(model)
        rows.append(MappingProxyType({"value": model, "label": label}))
        if len(rows) > 128:
            return ()
    return tuple(rows)


def _valid_inspect(summary: Mapping[str, Any]) -> bool:
    if not isinstance(summary, Mapping):
        return False
    tools = summary.get("builtin_tool_names")
    return (
        set(summary)
        == {
            "mcp_count",
            "hook_count",
            "plugin_count",
            "cached_native_login",
            "api_key_override",
            "builtin_tool_names",
        }
        and all(type(summary.get(name)) is int and summary[name] == 0 for name in ("mcp_count", "hook_count", "plugin_count"))
        and summary.get("cached_native_login") is True
        and summary.get("api_key_override") is False
        and isinstance(tools, tuple)
        and bool(tools)
        and all(_scalar(tool, whitespace_forbidden=True) and _TOOL.fullmatch(tool) for tool in tools)
        and tools == tuple(sorted(tools))
        and len(tools) == len(set(tools))
    )


def _valid_models(models: tuple[Mapping[str, str], ...]) -> bool:
    if not isinstance(models, tuple) or not models or len(models) > 128:
        return False
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, Mapping) or set(model) != {"value", "label"}:
            return False
        value, label = model["value"], model["label"]
        if (
            not _scalar(value, whitespace_forbidden=True)
            or not _scalar(label, whitespace_forbidden=False)
            or value in seen
        ):
            return False
        seen.add(value)
    return True


def adjudicate_no_model_contract(observation: GrokCliObservation) -> dict[str, str]:
    allowed = (
        isinstance(observation, GrokCliObservation)
        and bool(parse_version(observation.version))
        and isinstance(observation.help_contract, GrokHelpContract)
        and observation.help_contract == GrokHelpContract(*(True,) * 10)
        and _valid_inspect(observation.inspect_summary)
        and _valid_models(observation.models)
    )
    return (
        {"read_review": "pass", "bounded_writer": "candidate"}
        if allowed
        else {"read_review": "blocked", "bounded_writer": "blocked"}
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    overflow: bool = False
    malformed_utf8: bool = False
    read_failed: bool = False


class ProbeCleanupError(RuntimeError):
    pass


class _OutputBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.total = 0
        self.lock = threading.Lock()
        self.overflow = threading.Event()
        self.read_failed = threading.Event()

    def capture(self, chunk: bytes) -> bytes:
        with self.lock:
            remaining = max(0, self.limit - self.total)
            self.total += len(chunk)
            if self.total > self.limit:
                self.overflow.set()
            return chunk[:remaining]


def _read_pipe(stream: Any, chunks: list[bytes], budget: _OutputBudget) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            kept = budget.capture(chunk)
            if kept:
                chunks.append(kept)
    except Exception:
        budget.read_failed.set()
    finally:
        try:
            stream.close()
        except Exception:
            budget.read_failed.set()


def _bounded_wait(process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
    try:
        process.wait(timeout=timeout_seconds)
        return True
    except subprocess.TimeoutExpired:
        return False
    except BaseException:
        return False


def _stop_owned_child(
    process: subprocess.Popen[bytes], *, timeout_seconds: float
) -> None:
    try:
        exited = process.poll() is not None
    except BaseException:
        exited = False
    if exited:
        if _bounded_wait(process, timeout_seconds):
            return
        raise ProbeCleanupError("probe process cleanup failed")

    try:
        process.terminate()
    except BaseException:
        pass
    if _bounded_wait(process, timeout_seconds):
        return

    try:
        process.kill()
    except BaseException:
        pass
    if _bounded_wait(process, timeout_seconds):
        return
    raise ProbeCleanupError("probe process cleanup failed")


def _run_command(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS,
    cleanup_timeout_seconds: float = 2.0,
) -> CommandResult:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        shell=False,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    budget = _OutputBudget(_MAX_BYTES)
    readers: list[threading.Thread] = []
    streams = (process.stdout, process.stderr)
    timed_out = False
    try:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("probe pipes unavailable")
        for stream, chunks in (
            (process.stdout, stdout_chunks),
            (process.stderr, stderr_chunks),
        ):
            reader = threading.Thread(
                target=_read_pipe,
                args=(stream, chunks, budget),
                daemon=True,
            )
            reader.start()
            readers.append(reader)
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if budget.overflow.is_set() or budget.read_failed.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)
    finally:
        cleanup_error: ProbeCleanupError | None = None
        try:
            _stop_owned_child(process, timeout_seconds=cleanup_timeout_seconds)
        except BaseException:
            cleanup_error = ProbeCleanupError("probe process cleanup failed")
        for reader in readers:
            try:
                reader.join(timeout=cleanup_timeout_seconds)
            except BaseException:
                cleanup_error = ProbeCleanupError("probe reader cleanup failed")
        for reader in readers:
            try:
                if reader.is_alive():
                    cleanup_error = ProbeCleanupError("probe reader cleanup failed")
            except BaseException:
                cleanup_error = ProbeCleanupError("probe reader cleanup failed")
        for stream in streams:
            if stream is not None:
                try:
                    stream.close()
                except BaseException:
                    cleanup_error = ProbeCleanupError("probe pipe cleanup failed")
        if cleanup_error is not None:
            raise cleanup_error

    malformed_utf8 = False
    try:
        stdout = b"".join(stdout_chunks).decode("utf-8", "strict")
        stderr = b"".join(stderr_chunks).decode("utf-8", "strict")
    except UnicodeDecodeError:
        stdout = ""
        stderr = ""
        malformed_utf8 = True
    return CommandResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        overflow=budget.overflow.is_set(),
        malformed_utf8=malformed_utf8,
        read_failed=budget.read_failed.is_set(),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> tuple[tuple[int, int, int, int], str]:
    before = path.stat()
    digest = _sha256_file(path)
    after = path.stat()
    first = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    second = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if first != second:
        raise RuntimeError("executable identity changed")
    return first, digest


def _workspace_root() -> Path:
    current = Path.cwd().resolve(strict=True)
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate.resolve(strict=True)
    return current


def _is_workspace_local(path: Path) -> bool:
    try:
        root = os.path.normcase(str(_workspace_root()))
        candidate = os.path.normcase(str(path.resolve(strict=True)))
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False
    except OSError:
        return True


def _child_env(environ: Mapping[str, str]) -> dict[str, str]:
    child: dict[str, str] = {}
    for key, value in environ.items():
        normalized = key.upper()
        if normalized in _CHILD_ENV_ALLOW and normalized not in child:
            child[normalized] = value
    child["GROK_DISABLE_AUTOUPDATER"] = "1"
    return child


def _combined_help(outputs: Mapping[tuple[str, ...], str]) -> str:
    sections = (
        ("=== grok --help ===", ("--no-auto-update", "--help")),
        ("=== grok agent --help ===", ("--no-auto-update", "agent", "--help")),
        ("=== grok agent stdio --help ===", ("--no-auto-update", "agent", "stdio", "--help")),
    )
    return "\n\n".join(f"{marker}\n{outputs[argv].strip()}" for marker, argv in sections) + "\n"


def _normalized_result(result: Any) -> CommandResult:
    stdout = getattr(result, "stdout", "")
    stderr = getattr(result, "stderr", "")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return CommandResult(None, "", "", overflow=True)
    malformed_utf8 = getattr(result, "malformed_utf8", False) is True
    read_failed = getattr(result, "read_failed", False) is True
    try:
        stdout_bytes = stdout.encode("utf-8", "strict")
        stderr_bytes = stderr.encode("utf-8", "strict")
    except UnicodeError:
        malformed_utf8 = True
        stdout_bytes = b""
        stderr_bytes = b""
    too_large = len(stdout_bytes) + len(stderr_bytes) > _MAX_BYTES
    if malformed_utf8 or too_large:
        stdout = ""
        stderr = ""
    return CommandResult(
        returncode=getattr(result, "returncode", None),
        stdout=stdout,
        stderr=stderr,
        timed_out=getattr(result, "timed_out", False) is True,
        overflow=getattr(result, "overflow", False) is True or too_large,
        malformed_utf8=malformed_utf8,
        read_failed=read_failed,
    )


def _command_ok(result: CommandResult) -> bool:
    return (
        not result.timed_out
        and not result.overflow
        and not result.malformed_utf8
        and not result.read_failed
        and result.returncode == 0
    )


def _extensions_category(value: Any) -> str:
    if not isinstance(value, dict):
        return "unavailable"
    if set(value) == _INSPECT_KEYS:
        counts = (value.get("mcp_count"), value.get("hook_count"), value.get("plugin_count"))
        if any(type(count) is not int or count < 0 for count in counts):
            return "unavailable"
        return "present" if any(counts) else "none"
    fields = (value.get("mcpServers"), value.get("hooks"), value.get("plugins"))
    if any(not isinstance(items, list) for items in fields):
        return "unavailable"
    return "present" if any(fields) else "none"


def validate_sanitized_output(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != _INSTALLED_KEYS:
        raise ValueError("schema must contain exact categorical keys")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("schema version is unsupported")
    categories = {
        "pair_state": {"observed", "drifted", "unavailable"},
        "version_state": {"recognized", "unrecognized", "unavailable"},
        "help_state": {"recognized", "unrecognized", "unavailable"},
        "catalog_state": {"available", "unavailable"},
        "extensions_discovered": {"none", "present", "unavailable"},
        "cached_native_login": {"not_exposed"},
        "no_extra_spend": {"not_exposed"},
        "builtin_tool_inventory": {"not_exposed"},
        "provider_readiness": {"not_authorized", "needs_canary"},
    }
    if any(payload[name] not in allowed for name, allowed in categories.items()):
        raise ValueError("schema contains a non-categorical value")
    if payload["provider_key_environment_omitted"] is not True:
        raise ValueError("schema requires provider-key environment omission")


def _categorical_probe(
    output: str | Path,
    *,
    runner: Callable[..., Any],
    locator: Callable[[], str | None],
    environ: Mapping[str, str],
) -> int:
    child_env = _child_env(environ)
    pair_state = "unavailable"
    executable: Path | None = None
    file_identity: tuple[int, int, int, int] | None = None
    executable_sha256 = ""
    located = locator()
    if located:
        try:
            candidate = Path(located).resolve(strict=True)
            if not _is_workspace_local(candidate):
                executable = candidate
                file_identity, executable_sha256 = _identity(executable)
                pair_state = "observed"
        except (OSError, RuntimeError):
            executable = None

    results: dict[tuple[str, ...], CommandResult] = {
        suffix: CommandResult(None, "", "") for suffix in _PROBE_ARGV
    }
    if executable is not None:
        local_pair_digest = hashlib.sha256(_canonical_json({
            "canonical_identity": os.path.normcase(str(executable)),
            "file_identity": file_identity,
            "executable_sha256": executable_sha256,
        })).hexdigest()
        for suffix in _PROBE_ARGV:
            try:
                results[suffix] = _normalized_result(
                    runner([str(executable), *suffix], env=child_env)
                )
            except ProbeCleanupError:
                raise
            except Exception:
                results[suffix] = CommandResult(None, "", "")
        try:
            located_after = locator()
            if (
                not located_after
                or Path(located_after).resolve(strict=True) != executable
                or _identity(executable) != (file_identity, executable_sha256)
            ):
                pair_state = "drifted"
        except (OSError, RuntimeError):
            pair_state = "drifted"
        del local_pair_digest

    version_result = results[("--no-auto-update", "--version")]
    version_state = (
        "recognized" if _command_ok(version_result) and parse_version(version_result.stdout)
        else "unrecognized" if _command_ok(version_result)
        else "unavailable"
    )
    help_suffixes = (
        ("--no-auto-update", "--help"),
        ("--no-auto-update", "agent", "--help"),
        ("--no-auto-update", "agent", "stdio", "--help"),
    )
    if all(_command_ok(results[suffix]) for suffix in help_suffixes):
        help_text = _combined_help({suffix: results[suffix].stdout for suffix in help_suffixes})
        root_help = results[("--no-auto-update", "--help")].stdout
        help_state = (
            "recognized"
            if parse_help_contract(help_text) == GrokHelpContract(*(True,) * 10)
            and re.search(r"(?m)^\s{2,}--no-auto-update(?:\s|$)", root_help)
            else "unrecognized"
        )
    else:
        help_state = "unavailable"

    catalog_result = results[("--no-auto-update", "models")]
    catalog_state = (
        "available"
        if _command_ok(catalog_result) and parse_model_catalog(catalog_result.stdout)
        else "unavailable"
    )
    inspect_result = results[("--no-auto-update", "inspect", "--json")]
    if _command_ok(inspect_result):
        try:
            inspect_value = json.loads(inspect_result.stdout)
        except (json.JSONDecodeError, RecursionError, UnicodeError):
            extensions_discovered = "unavailable"
        else:
            extensions_discovered = _extensions_category(inspect_value)
    else:
        extensions_discovered = "unavailable"

    provider_readiness = (
        "not_authorized"
        if (
            pair_state == "observed"
            and version_state == "recognized"
            and help_state == "recognized"
            and catalog_state == "available"
            and extensions_discovered == "none"
        )
        else "needs_canary"
    )
    payload = {
        "schema_version": 1,
        "pair_state": pair_state,
        "version_state": version_state,
        "help_state": help_state,
        "catalog_state": catalog_state,
        "extensions_discovered": extensions_discovered,
        "provider_key_environment_omitted": True,
        "cached_native_login": "not_exposed",
        "no_extra_spend": "not_exposed",
        "builtin_tool_inventory": "not_exposed",
        "provider_readiness": provider_readiness,
    }
    validate_sanitized_output(payload)
    write_json_atomic(output, payload)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    runner: Callable[..., Any] | None = None,
    locator: Callable[[], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="grok-build-no-model")
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe")
    probe.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "probe":
        return _categorical_probe(
            args.output,
            runner=runner or _run_command,
            locator=locator or (lambda: shutil.which("grok")),
            environ=os.environ if environ is None else environ,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
