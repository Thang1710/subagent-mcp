from __future__ import annotations

import copy
import errno
import json
import os
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .paths import ProductPaths


CONFIG_SCHEMA_VERSION = 1
_MAX_CONFIG_BYTES = 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 5.0
_PROCESS_CONFIG_LOCK = threading.Lock()


class ConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DuplicateKeyError(ValueError):
    pass


class ConfigStore:
    def __init__(self, paths: ProductPaths) -> None:
        self._paths = paths

    def load(self) -> dict[str, Any]:
        return copy.deepcopy(_read_config(self._paths.config_file))

    def save(
        self,
        document: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ConfigError("CONFIG_INVALID", "expected_revision must be an integer")
        if not isinstance(document, Mapping):
            raise ConfigError("CONFIG_INVALID", "config must be an object")
        candidate = copy.deepcopy(dict(document))
        validate_config(candidate)
        if candidate["revision"] != expected_revision:
            raise ConfigError(
                "CONFIG_INVALID",
                "document revision must equal expected_revision",
            )

        try:
            self._paths.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError("CONFIG_WRITE_FAILED", "cannot create config directory") from exc

        try:
            with _PROCESS_CONFIG_LOCK:
                with _exclusive_file_lock(
                    self._paths.config_file.with_name("config.json.lock"),
                    timeout_seconds=_LOCK_TIMEOUT_SECONDS,
                ):
                    current = _read_config(self._paths.config_file)
                    if current["revision"] != expected_revision:
                        raise ConfigError(
                            "REVISION_CONFLICT",
                            "config changed since it was read",
                        )
                    candidate["revision"] = expected_revision + 1
                    validate_config(candidate)
                    payload = _encode_config(candidate)
                    _atomic_write_bytes(self._paths.config_file, payload)
        except TimeoutError as exc:
            raise ConfigError("CONFIG_LOCK_TIMEOUT", "config is busy") from exc
        except ConfigError:
            raise
        except (OSError, ValueError) as exc:
            raise ConfigError("CONFIG_WRITE_FAILED", "config write failed") from exc
        return copy.deepcopy(candidate)

    def set_variant_quota_state(
        self,
        runtime_id: str,
        variant_id: str,
        *,
        paused: bool,
        reason_code: str | None = None,
        expected_reason_code: str | None = None,
    ) -> dict[str, Any]:
        """Persist one exact model's quota state and demote it when paused."""

        _require_bounded_text(runtime_id, "runtime id", 128)
        _require_bounded_text(variant_id, "variant id", 128)
        if type(paused) is not bool:
            raise ConfigError("CONFIG_INVALID", "paused must be boolean")
        if paused:
            _require_bounded_text(reason_code, "quota reason code", 128)
        if expected_reason_code is not None:
            if paused:
                raise ConfigError(
                    "CONFIG_INVALID",
                    "expected quota reason is valid only when clearing a pause",
                )
            _require_bounded_text(
                expected_reason_code,
                "expected quota reason code",
                128,
            )
        try:
            with _PROCESS_CONFIG_LOCK:
                with _exclusive_file_lock(
                    self._paths.config_file.with_name("config.json.lock"),
                    timeout_seconds=_LOCK_TIMEOUT_SECONDS,
                ):
                    current = _read_config(self._paths.config_file)
                    candidate = copy.deepcopy(current)
                    policies = candidate.get("runtimes")
                    policy = policies.get(runtime_id) if isinstance(policies, dict) else None
                    variants = policy.get("variants") if isinstance(policy, dict) else None
                    if not isinstance(variants, list):
                        return copy.deepcopy(current)
                    index = next(
                        (
                            position
                            for position, item in enumerate(variants)
                            if isinstance(item, dict) and item.get("id") == variant_id
                        ),
                        None,
                    )
                    if index is None:
                        return copy.deepcopy(current)
                    variant = variants[index]
                    if paused:
                        variant["availability"] = {
                            "state": "quota_paused",
                            "reason_code": reason_code,
                        }
                        if index != len(variants) - 1:
                            variants.append(variants.pop(index))
                    else:
                        availability = variant.get("availability")
                        if (
                            expected_reason_code is not None
                            and (
                                not isinstance(availability, dict)
                                or availability.get("reason_code")
                                != expected_reason_code
                            )
                        ):
                            return copy.deepcopy(current)
                        if (
                            isinstance(availability, dict)
                            and availability.get("state") == "quota_paused"
                        ):
                            variant.pop("availability", None)
                    if candidate == current:
                        return copy.deepcopy(current)
                    candidate["revision"] = current["revision"] + 1
                    validate_config(candidate)
                    _atomic_write_bytes(self._paths.config_file, _encode_config(candidate))
                    return copy.deepcopy(candidate)
        except TimeoutError as exc:
            raise ConfigError("CONFIG_LOCK_TIMEOUT", "config is busy") from exc
        except ConfigError:
            raise
        except (OSError, ValueError) as exc:
            raise ConfigError("CONFIG_WRITE_FAILED", "config write failed") from exc


def validate_config(document: object) -> None:
    if not isinstance(document, dict):
        raise ConfigError("CONFIG_INVALID", "config must be an object")

    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ConfigError("CONFIG_INVALID", "schema_version must be an integer")
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigError(
            "CONFIG_VERSION_UNSUPPORTED",
            f"config schema version {schema_version} is unsupported",
        )

    revision = document.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ConfigError("CONFIG_INVALID", "revision must be a nonnegative integer")

    runtimes = document.get("runtimes")
    if not isinstance(runtimes, dict):
        raise ConfigError("CONFIG_INVALID", "runtimes must be an object")
    for runtime_id, policy in runtimes.items():
        _require_bounded_text(runtime_id, "runtime id", 128)
        _validate_runtime_policy(policy, runtime_id)


def _validate_runtime_policy(policy: object, runtime_id: str) -> None:
    if not isinstance(policy, dict):
        raise ConfigError("CONFIG_INVALID", f"runtime {runtime_id} must be an object")
    if type(policy.get("enabled")) is not bool:
        raise ConfigError("CONFIG_INVALID", f"runtime {runtime_id} enabled must be boolean")
    selection_mode = policy.get("selection_mode")
    if selection_mode not in {"fixed", "lead-selects"}:
        raise ConfigError("CONFIG_INVALID", f"runtime {runtime_id} selection_mode is invalid")
    if policy.get("fallback") is not False:
        raise ConfigError("CONFIG_INVALID", f"runtime {runtime_id} fallback must be false")
    priority = policy.get("delegation_priority", 0)
    if type(priority) is not int or not 0 <= priority <= 100:
        raise ConfigError(
            "CONFIG_INVALID",
            f"runtime {runtime_id} delegation_priority must be an integer from 0 to 100",
        )

    variants = policy.get("variants")
    if not isinstance(variants, list):
        raise ConfigError("CONFIG_INVALID", f"runtime {runtime_id} variants must be an array")
    if selection_mode == "fixed" and len(variants) != 1:
        raise ConfigError("CONFIG_INVALID", f"runtime {runtime_id} fixed mode needs one variant")
    if policy["enabled"] and not variants:
        raise ConfigError("CONFIG_INVALID", f"runtime {runtime_id} has no selectable variant")

    seen: set[str] = set()
    for variant in variants:
        if not isinstance(variant, dict):
            raise ConfigError("CONFIG_INVALID", "variant must be an object")
        variant_id = variant.get("id")
        _require_bounded_text(variant_id, "variant id", 128)
        if variant_id in seen:
            raise ConfigError("CONFIG_INVALID", f"duplicate variant id {variant_id}")
        seen.add(variant_id)
        _require_bounded_text(variant.get("model"), "model", 256)
        if not isinstance(variant.get("reasoning"), dict):
            raise ConfigError("CONFIG_INVALID", "reasoning must be an object")


def _require_bounded_text(value: object, label: str, max_bytes: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("CONFIG_INVALID", f"{label} must be nonempty")
    if len(value.encode("utf-8")) > max_bytes:
        raise ConfigError("CONFIG_INVALID", f"{label} is too long")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ConfigError("CONFIG_INVALID", f"{label} contains a control character")


def _default_config() -> dict[str, Any]:
    return {"schema_version": CONFIG_SCHEMA_VERSION, "revision": 0, "runtimes": {}}


def _read_config(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _default_config()
    except OSError as exc:
        raise ConfigError("CONFIG_CORRUPT", "config cannot be read") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ConfigError("CONFIG_CORRUPT", "config exceeds 1 MiB")
    try:
        text = raw.decode("utf-8-sig")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
        raise ConfigError("CONFIG_CORRUPT", "config is malformed") from exc
    validate_config(value)
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate key {key}")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError(f"non-finite number {value}")


def _encode_config(document: dict[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ConfigError("CONFIG_INVALID", "config is not JSON serializable") from exc
    if len(encoded) > _MAX_CONFIG_BYTES:
        raise ConfigError("CONFIG_INVALID", "config exceeds 1 MiB")
    return encoded


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _is_busy_lock_error(error: OSError, *, windows: bool) -> bool:
    if windows:
        return error.errno == errno.EACCES or getattr(error, "winerror", None) == 33
    return error.errno in {errno.EACCES, errno.EAGAIN}


def _try_lock(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _is_busy_lock_error(exc, windows=True):
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if _is_busy_lock_error(exc, windows=False):
            return False
        raise
    return True


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.path.getsize(path) == 0:
            os.write(descriptor, b"\0")
        deadline = time.monotonic() + timeout_seconds
        while not _try_lock(descriptor):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("lock timeout")
            time.sleep(min(poll_seconds, remaining))
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock(descriptor)
        finally:
            os.close(descriptor)
