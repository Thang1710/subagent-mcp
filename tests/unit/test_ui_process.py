from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subagent_harness_mcp import cli
from subagent_harness_mcp.paths import ProductPaths
from subagent_harness_mcp.ui import LoopbackUiServer
from subagent_harness_mcp.ui_process import (
    BackgroundUiResult,
    UiProcessError,
    publish_control_record,
    start_background_ui,
    status_background_ui,
    stop_background_ui,
)


def _paths(tmp_path: Path) -> ProductPaths:
    return ProductPaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


class _Process:
    def __init__(self, pid: int = 41) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is None or timeout >= 0
        return 1 if self.returncode is None else self.returncode


def test_background_start_uses_the_installed_module_without_a_shell(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    process = _Process()
    probes = iter((False, False, True))

    def popen(argv, **kwargs):
        calls.append((tuple(argv), dict(kwargs)))
        return process

    result = start_background_ui(
        paths,
        port=8765,
        open_browser=False,
        executable="C:/Python/python.exe",
        popen_factory=popen,
        probe=lambda _port: next(probes),
        sleeper=lambda _seconds: None,
        timeout_seconds=1,
        platform="nt",
    )

    assert result == BackgroundUiResult(
        changed=True,
        running=True,
        managed=True,
        port=8765,
        pid=41,
    )
    assert calls[0][0] == (
        "C:/Python/python.exe",
        "-I",
        "-B",
        "-m",
        "subagent_harness_mcp.cli",
        "ui",
        "--port",
        "8765",
        "--background-child",
        "--no-open",
    )
    kwargs = calls[0][1]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert int(kwargs["creationflags"]) != 0


def test_background_start_is_idempotent_when_product_ui_is_healthy(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    called = False

    def popen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not spawn")

    result = start_background_ui(
        paths,
        port=8765,
        open_browser=True,
        popen_factory=popen,
        probe=lambda _port: True,
    )

    assert result.changed is False
    assert result.port == 8765
    assert called is False


def test_background_start_rejects_an_ephemeral_port(tmp_path: Path) -> None:
    with pytest.raises(UiProcessError) as caught:
        start_background_ui(_paths(tmp_path), port=0, open_browser=False)

    assert caught.value.code == "UI_BACKGROUND_PORT_REQUIRED"


def test_control_stop_is_authenticated_and_removes_its_exact_record(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    token = "control-token-with-enough-entropy-for-the-test"
    server = LoopbackUiServer(
        lambda: {},
        lambda _patch, revision: {"revision": revision},
        control_token=token,
    )
    thread = server.start()
    record = publish_control_record(
        paths.ui_control_file,
        pid=123,
        port=server.bound_port,
        token=token,
    )
    try:
        result = stop_background_ui(
            paths,
            port=server.bound_port,
            timeout_seconds=3,
        )
    finally:
        server.close()

    assert result.changed is True
    assert result.managed is True
    assert result.pid == 123
    assert not thread.is_alive()
    assert not paths.ui_control_file.exists()
    assert record.endswith(b"\n")


def test_stop_refuses_a_healthy_unmanaged_ui(tmp_path: Path) -> None:
    with pytest.raises(UiProcessError) as caught:
        stop_background_ui(
            _paths(tmp_path),
            port=8765,
            probe=lambda _port: True,
        )

    assert caught.value.code == "UI_UNMANAGED"


def test_status_distinguishes_managed_running_and_stopped(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    stopped = status_background_ui(paths, port=8765, probe=lambda _port: False)
    assert stopped == BackgroundUiResult(False, False, False, 8765, None)

    foreground = status_background_ui(paths, port=8765, probe=lambda _port: True)
    assert foreground == BackgroundUiResult(False, True, False, 8765, None)

    publish_control_record(
        paths.ui_control_file,
        pid=77,
        port=8765,
        token="another-control-token-with-enough-entropy",
    )
    running = status_background_ui(paths, port=8765, probe=lambda _port: True)
    assert running == BackgroundUiResult(False, True, True, 8765, 77)


def test_cli_routes_background_status_and_stop_without_starting_a_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subagent_harness_mcp.paths as paths_module
    import subagent_harness_mcp.ui_process as process_module

    paths = _paths(tmp_path)
    calls: list[tuple[str, int, bool | None]] = []
    monkeypatch.setattr(paths_module, "resolve_paths", lambda: paths)

    def start(_paths, *, port: int, open_browser: bool):
        assert _paths is paths
        calls.append(("start", port, open_browser))
        return BackgroundUiResult(True, True, True, port, 11)

    def status(_paths, *, port: int):
        assert _paths is paths
        calls.append(("status", port, None))
        return BackgroundUiResult(False, True, True, port, 11)

    def stop(_paths, *, port: int):
        assert _paths is paths
        calls.append(("stop", port, None))
        return BackgroundUiResult(True, False, True, port, 11)

    monkeypatch.setattr(process_module, "start_background_ui", start)
    monkeypatch.setattr(process_module, "status_background_ui", status)
    monkeypatch.setattr(process_module, "stop_background_ui", stop)

    assert cli.main(["ui", "--background", "--no-open"]) == 0
    assert cli.main(["ui", "--status"]) == 0
    assert cli.main(["ui", "--stop"]) == 0
    assert calls == [
        ("start", 8765, False),
        ("status", 8765, None),
        ("stop", 8765, None),
    ]
    output = capsys.readouterr()
    assert "started in background" in output.out
    assert "running in background" in output.out
    assert "stopped" in output.out
