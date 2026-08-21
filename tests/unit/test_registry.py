from __future__ import annotations

from dataclasses import dataclass

import pytest

from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness
from subagent_harness_mcp.adapters.registry import AdapterRegistry, RegistryError


@dataclass
class _EntryPoint:
    name: str
    value: str
    loaded: object
    group: str = "subagent_harness_mcp.adapters"

    def load(self):
        if isinstance(self.loaded, BaseException):
            raise self.loaded
        return self.loaded


def _factory():
    return FakeAdapter(FakeHarness())


def test_builtin_adapter_is_selected_by_runtime_id() -> None:
    registry = AdapterRegistry(builtin_factories=(lambda: FakeAdapter(FakeHarness()),))

    adapter = registry.get("fake")
    records = registry.records()

    assert adapter.manifest.runtime_id == "fake"
    assert records[0].state == "available"
    assert records[0].owner == "builtin:fake"


def test_entry_point_name_conflict_quarantines_runtime() -> None:
    registry = AdapterRegistry()
    entries = (
        _EntryPoint("fake", "package_a:create", _factory),
        _EntryPoint("fake", "package_b:create", _factory),
    )

    registry.discover(entries)

    with pytest.raises(RegistryError) as captured:
        registry.get("fake")
    record = registry.record("fake")
    assert captured.value.code == "ADAPTER_QUARANTINED"
    assert record.state == "quarantined"
    assert record.owners == ("entrypoint:package_a:create", "entrypoint:package_b:create")


def test_import_or_manifest_failure_quarantines_only_affected_adapter() -> None:
    registry = AdapterRegistry(builtin_factories=(lambda: FakeAdapter(FakeHarness()),))
    entries = (
        _EntryPoint("broken", "broken_package:create", ImportError("private detail")),
    )

    registry.discover(entries)

    assert registry.get("fake").manifest.runtime_id == "fake"
    broken = registry.record("broken")
    assert broken.state == "quarantined"
    assert broken.reason == "adapter import failed (ImportError)"
    assert "private detail" not in broken.reason
