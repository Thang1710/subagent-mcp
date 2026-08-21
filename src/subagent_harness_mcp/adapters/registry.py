"""Explicit built-in and Python entry-point adapter discovery."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from typing import Callable, Iterable

from ..contracts import AdapterManifest, ContractError
from .base import Adapter


ENTRY_POINT_GROUP = "subagent_harness_mcp.adapters"
AdapterFactory = Callable[[], Adapter]


class RegistryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AdapterRecord:
    runtime_id: str
    state: str
    owner: str
    owners: tuple[str, ...]
    manifest: AdapterManifest | None = None
    reason: str | None = None


class AdapterRegistry:
    def __init__(
        self,
        *,
        builtin_factories: Iterable[AdapterFactory] = (),
    ) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._records: dict[str, AdapterRecord] = {}
        for factory in builtin_factories:
            self._load_factory(factory, owner_hint="builtin")

    def discover(self, entry_points: Iterable[object] | None = None) -> None:
        if entry_points is None:
            entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
        grouped: dict[str, list[object]] = {}
        for entry_point in entry_points:
            if getattr(entry_point, "group", ENTRY_POINT_GROUP) != ENTRY_POINT_GROUP:
                continue
            name = str(getattr(entry_point, "name", ""))
            if not name:
                continue
            grouped.setdefault(name, []).append(entry_point)

        for runtime_id in sorted(grouped):
            entries = grouped[runtime_id]
            entry_owners = tuple(
                f"entrypoint:{getattr(entry, 'value', runtime_id)}" for entry in entries
            )
            existing = self._records.get(runtime_id)
            if len(entries) != 1 or existing is not None:
                owners = (() if existing is None else existing.owners) + entry_owners
                self._quarantine(runtime_id, owners, "adapter entry-point conflict")
                continue
            entry = entries[0]
            owner = entry_owners[0]
            try:
                factory = entry.load()  # type: ignore[attr-defined]
            except BaseException as exc:
                self._quarantine(
                    runtime_id,
                    (owner,),
                    f"adapter import failed ({type(exc).__name__})",
                )
                continue
            if not callable(factory):
                self._quarantine(runtime_id, (owner,), "adapter factory is not callable")
                continue
            self._load_factory(
                factory,
                owner_hint=owner,
                expected_runtime_id=runtime_id,
            )

    def get(self, runtime_id: str) -> Adapter:
        record = self._records.get(runtime_id)
        if record is None:
            raise RegistryError("ADAPTER_NOT_FOUND", f"adapter {runtime_id!r} is not installed")
        if record.state != "available":
            raise RegistryError(
                "ADAPTER_QUARANTINED",
                f"adapter {runtime_id!r} is quarantined",
            )
        return self._adapters[runtime_id]

    def record(self, runtime_id: str) -> AdapterRecord:
        record = self._records.get(runtime_id)
        if record is None:
            raise RegistryError("ADAPTER_NOT_FOUND", f"adapter {runtime_id!r} is not installed")
        return record

    def records(self) -> tuple[AdapterRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def _load_factory(
        self,
        factory: AdapterFactory,
        *,
        owner_hint: str,
        expected_runtime_id: str | None = None,
    ) -> None:
        runtime_hint = expected_runtime_id or getattr(factory, "__name__", "unknown")
        try:
            adapter = factory()
            manifest = adapter.manifest
            if not isinstance(manifest, AdapterManifest):
                raise TypeError("manifest has the wrong type")
            _require_adapter_operations(adapter)
            if expected_runtime_id is not None and manifest.runtime_id != expected_runtime_id:
                raise ContractError(
                    "ADAPTER_INVALID",
                    "entry-point name does not match runtime_id",
                )
        except BaseException as exc:
            self._quarantine(
                runtime_hint,
                (owner_hint,),
                f"adapter manifest failed ({type(exc).__name__})",
            )
            return

        runtime_id = manifest.runtime_id
        owner = (
            f"builtin:{runtime_id}"
            if owner_hint == "builtin"
            else owner_hint
        )
        existing = self._records.get(runtime_id)
        if existing is not None:
            self._quarantine(
                runtime_id,
                existing.owners + (owner,),
                "adapter runtime_id conflict",
            )
            return
        self._adapters[runtime_id] = adapter
        self._records[runtime_id] = AdapterRecord(
            runtime_id=runtime_id,
            state="available",
            owner=owner,
            owners=(owner,),
            manifest=manifest,
        )

    def _quarantine(
        self,
        runtime_id: str,
        owners: tuple[str, ...],
        reason: str,
    ) -> None:
        self._adapters.pop(runtime_id, None)
        unique_owners = tuple(dict.fromkeys(owners))
        self._records[runtime_id] = AdapterRecord(
            runtime_id=runtime_id,
            state="quarantined",
            owner=unique_owners[0] if unique_owners else "unknown",
            owners=unique_owners,
            reason=reason,
        )


def _require_adapter_operations(adapter: object) -> None:
    for name in (
        "probe",
        "resolve_context",
        "spawn",
        "send",
        "snapshot",
        "interrupt",
        "close",
        "open_session",
    ):
        if not callable(getattr(adapter, name, None)):
            raise TypeError(f"adapter operation {name} is missing")
