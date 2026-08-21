from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from subagent_harness_mcp import config as config_module
from subagent_harness_mcp.config import ConfigError, ConfigStore
from subagent_harness_mcp.paths import resolve_paths


def _paths(tmp_path: Path):
    return resolve_paths(
        {"SUBAGENT_MCP_HOME": str(tmp_path / "home")},
        os_name="nt",
    )


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": 0,
        "runtimes": {
            "claude-code": {
                "enabled": True,
                "delegation_priority": 73,
                "selection_mode": "fixed",
                "fallback": False,
                "variants": [
                    {
                        "id": "future-default",
                        "model": "provider/future-model-1",
                        "reasoning": {"provider_mode": "deep"},
                    }
                ],
            }
        },
    }


def test_missing_config_returns_revision_zero_without_creating_home(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)

    loaded = ConfigStore(paths).load()

    assert loaded == {"schema_version": 1, "revision": 0, "runtimes": {}}
    assert not (tmp_path / "home").exists()


def test_save_increments_revision_and_round_trips_unknown_additive_fields(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    store = ConfigStore(paths)
    document = _document()
    document["future_root"] = {"kept": True}
    runtime = document["runtimes"]["claude-code"]  # type: ignore[index]
    runtime["future_runtime"] = [1, 2, 3]
    runtime["variants"][0]["future_variant"] = "kept"  # type: ignore[index]

    saved = store.save(document, expected_revision=0)
    saved["runtimes"]["claude-code"]["enabled"] = False  # type: ignore[index]
    saved_again = store.save(saved, expected_revision=1)

    assert saved["revision"] == 1
    assert saved_again["revision"] == 2
    assert store.load() == saved_again
    assert saved_again["future_root"] == {"kept": True}
    assert saved_again["runtimes"]["claude-code"]["future_runtime"] == [1, 2, 3]  # type: ignore[index]
    assert saved_again["runtimes"]["claude-code"]["variants"][0]["future_variant"] == "kept"  # type: ignore[index]


def test_concurrent_writers_with_same_revision_have_exactly_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    store = ConfigStore(paths)
    base = store.save(_document(), expected_revision=0)
    original_write = config_module._atomic_write_bytes
    entered_write = threading.Event()
    release_write = threading.Event()

    def delayed_write(path: Path, payload: bytes) -> None:
        entered_write.set()
        assert release_write.wait(2)
        original_write(path, payload)

    monkeypatch.setattr(config_module, "_atomic_write_bytes", delayed_write)
    start = threading.Barrier(3)
    results: list[tuple[str, object]] = []
    result_lock = threading.Lock()

    def writer(enabled: bool) -> None:
        candidate = copy.deepcopy(base)
        candidate["runtimes"]["claude-code"]["enabled"] = enabled  # type: ignore[index]
        start.wait()
        try:
            value: tuple[str, object] = (
                "saved",
                ConfigStore(paths).save(candidate, expected_revision=1),
            )
        except ConfigError as exc:
            value = ("error", exc.code)
        with result_lock:
            results.append(value)

    threads = [
        threading.Thread(target=writer, args=(False,)),
        threading.Thread(target=writer, args=(True,)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    assert entered_write.wait(2)
    release_write.set()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    assert sum(kind == "saved" for kind, _ in results) == 1
    assert results.count(("error", "REVISION_CONFLICT")) == 1
    assert store.load()["revision"] == 2


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"schema_version":1,,}', "CONFIG_CORRUPT"),
        (
            b'{"schema_version":1,"revision":0,"revision":1,"runtimes":{}}',
            "CONFIG_CORRUPT",
        ),
        (
            b'{"schema_version":2,"revision":9,"runtimes":{}}',
            "CONFIG_VERSION_UNSUPPORTED",
        ),
    ],
)
def test_malformed_duplicate_or_newer_config_is_never_overwritten(
    tmp_path: Path,
    raw: bytes,
    code: str,
) -> None:
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_bytes(raw)
    store = ConfigStore(paths)

    with pytest.raises(ConfigError) as loaded:
        store.load()
    with pytest.raises(ConfigError) as saved:
        store.save(_document(), expected_revision=0)

    assert loaded.value.code == code
    assert saved.value.code == code
    assert paths.config_file.read_bytes() == raw


def test_failed_atomic_replace_preserves_previous_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    store = ConfigStore(paths)
    saved = store.save(_document(), expected_revision=0)
    before = paths.config_file.read_bytes()
    saved["runtimes"]["claude-code"]["enabled"] = False  # type: ignore[index]

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)
    with pytest.raises(ConfigError) as captured:
        store.save(saved, expected_revision=1)

    assert captured.value.code == "CONFIG_WRITE_FAILED"
    assert paths.config_file.read_bytes() == before
    assert list(paths.config_dir.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["runtimes"]["claude-code"].update(fallback=True),
        lambda doc: doc["runtimes"]["claude-code"]["variants"][0].update(
            reasoning=[]
        ),
        lambda doc: doc["runtimes"]["claude-code"]["variants"][0].update(
            model="bad\nmodel"
        ),
        lambda doc: doc["runtimes"]["claude-code"]["variants"][0].update(
            model="m" * 257
        ),
        lambda doc: doc["runtimes"]["claude-code"]["variants"].append(
            copy.deepcopy(doc["runtimes"]["claude-code"]["variants"][0])
        ),
    ],
)
def test_invalid_runtime_policy_fails_before_any_config_write(
    tmp_path: Path,
    mutate,
) -> None:
    paths = _paths(tmp_path)
    document = _document()
    mutate(document)

    with pytest.raises(ConfigError) as captured:
        ConfigStore(paths).save(document, expected_revision=0)

    assert captured.value.code == "CONFIG_INVALID"
    assert not paths.config_file.exists()


def test_public_config_schema_matches_revisioned_model_agnostic_contract() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "config-v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["properties"]["revision"]["minimum"] == 0
    runtime = schema["properties"]["runtimes"]["additionalProperties"]
    assert runtime["properties"]["fallback"] == {"const": False}
    assert runtime["properties"]["delegation_priority"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
        "default": 0,
    }
    variant = runtime["properties"]["variants"]["items"]
    assert variant["properties"]["reasoning"]["type"] == "object"
    assert runtime["additionalProperties"] is True
    assert variant["additionalProperties"] is True


@pytest.mark.parametrize("priority", [-1, 101, True, 1.5])
def test_delegation_priority_is_a_bounded_integer(
    tmp_path: Path,
    priority: object,
) -> None:
    document = _document()
    document["runtimes"]["claude-code"]["delegation_priority"] = priority  # type: ignore[index]

    with pytest.raises(ConfigError) as captured:
        ConfigStore(_paths(tmp_path)).save(document, expected_revision=0)

    assert captured.value.code == "CONFIG_INVALID"
