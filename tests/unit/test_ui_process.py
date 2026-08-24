from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from subagent_harness_mcp import cli
from subagent_harness_mcp.paths import ProductPaths
from subagent_harness_mcp.ui import LoopbackUiServer
from subagent_harness_mcp.ui_process import (
    BackgroundUiResult,
    UiProcessError,
    open_background_ui,
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
        publish_control_record(
            paths.ui_control_file,
            pid=process.pid,
            port=8765,
            token="startup-control-token-with-enough-entropy",
        )
        return process

    result = start_background_ui(
        paths,
        port=8765,
        open_browser=False,
        executable="C:/Python/python.exe",
        popen_factory=popen,
        probe=lambda _port: next(probes),
        verify_control=lambda _record: True,
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


def test_background_start_accepts_an_authenticated_indirect_child_pid(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    launcher = _Process(pid=41)
    probes = iter((False, True))

    def popen(*_args, **_kwargs):
        publish_control_record(
            paths.ui_control_file,
            pid=77,
            port=8765,
            token="indirect-child-control-token-with-enough-entropy",
        )
        return launcher

    result = start_background_ui(
        paths,
        port=8765,
        open_browser=False,
        popen_factory=popen,
        probe=lambda _port: next(probes),
        verify_control=lambda _record: True,
        sleeper=lambda _seconds: None,
        timeout_seconds=1,
    )

    assert result == BackgroundUiResult(True, True, True, 8765, 77)
    assert launcher.terminated is False


def test_background_start_is_idempotent_when_product_ui_is_healthy(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    publish_control_record(
        paths.ui_control_file,
        pid=77,
        port=8765,
        token="idempotent-control-token-with-enough-entropy",
    )
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
        verify_control=lambda _record: True,
    )

    assert result.changed is False
    assert result.managed is True
    assert result.port == 8765
    assert result.pid == 77
    assert called is False


def test_background_start_rejects_a_counterfeit_or_unmanaged_ui(
    tmp_path: Path,
) -> None:
    called = False

    def popen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not spawn")

    with pytest.raises(UiProcessError) as caught:
        start_background_ui(
            _paths(tmp_path),
            port=8765,
            open_browser=True,
            popen_factory=popen,
            probe=lambda _port: True,
        )

    assert caught.value.code == "UI_UNMANAGED"
    assert called is False


def test_background_start_rejects_a_port_squatter_during_startup(
    tmp_path: Path,
) -> None:
    process = _Process()
    probes = iter((False, True))

    with pytest.raises(UiProcessError) as caught:
        start_background_ui(
            _paths(tmp_path),
            port=8765,
            open_browser=False,
            popen_factory=lambda *_args, **_kwargs: process,
            probe=lambda _port: next(probes),
            sleeper=lambda _seconds: None,
            timeout_seconds=1,
        )

    assert caught.value.code == "UI_UNMANAGED"
    assert process.terminated is True


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


def test_status_requires_exact_control_identity_for_a_matching_record(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    publish_control_record(
        paths.ui_control_file,
        pid=77,
        port=8765,
        token="matching-record-control-token-with-enough-entropy",
    )

    counterfeit = status_background_ui(
        paths,
        port=8765,
        probe=lambda _port: True,
        verify_control=lambda _record: False,
    )
    managed = status_background_ui(
        paths,
        port=8765,
        probe=lambda _port: True,
        verify_control=lambda _record: True,
    )

    assert counterfeit == BackgroundUiResult(False, True, False, 8765, None)
    assert managed == BackgroundUiResult(False, True, True, 8765, 77)


def test_status_proves_the_real_managed_ui_control_identity(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    control_token = "real-status-control-token-with-enough-entropy"
    server = LoopbackUiServer(
        lambda: {},
        lambda _patch, revision: {"revision": revision},
        control_token=control_token,
    )
    server.start()
    publish_control_record(
        paths.ui_control_file,
        pid=123,
        port=server.bound_port,
        token=control_token,
    )
    try:
        result = status_background_ui(paths, port=server.bound_port)
    finally:
        server.close()

    assert result == BackgroundUiResult(
        False,
        True,
        True,
        server.bound_port,
        123,
    )


def test_control_open_passes_one_validated_bootstrap_directly_to_browser(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    control_token = "control-token-with-enough-entropy-for-open"
    server = LoopbackUiServer(
        lambda: {},
        lambda _patch, revision: {"revision": revision},
        control_token=control_token,
    )
    server.start()
    publish_control_record(
        paths.ui_control_file,
        pid=123,
        port=server.bound_port,
        token=control_token,
    )
    opened: list[str] = []
    try:
        result = open_background_ui(
            paths,
            port=server.bound_port,
            browser_opener=lambda url: not opened.append(url),
        )
    finally:
        server.close()

    assert result == BackgroundUiResult(False, True, True, server.bound_port, 123)
    assert len(opened) == 1
    parsed = urlsplit(opened[0])
    bootstrap = parse_qs(parsed.fragment)["token"][0]
    assert parsed.netloc == server.host_header
    assert parsed.path == "/"
    assert not parsed.query
    assert bootstrap != control_token


def test_open_refuses_unmanaged_or_malformed_background_ui(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with pytest.raises(UiProcessError) as unmanaged:
        open_background_ui(paths, port=8765, probe=lambda _port: True)
    assert unmanaged.value.code == "UI_UNMANAGED"

    publish_control_record(
        paths.ui_control_file,
        pid=77,
        port=8765,
        token="control-token-with-enough-entropy-for-malformed",
    )
    opened: list[str] = []
    with pytest.raises(UiProcessError) as malformed:
        open_background_ui(
            paths,
            port=8765,
            probe=lambda _port: True,
            request_open=lambda _record: "https://attacker.invalid/#token=x",
            browser_opener=lambda url: not opened.append(url),
        )
    assert malformed.value.code == "UI_OPEN_RESPONSE_INVALID"
    assert opened == []


def test_open_never_sends_bearer_token_and_rejects_an_unproven_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subagent_harness_mcp.ui_process as process_module

    paths = _paths(tmp_path)
    control_token = "stale-control-token-with-enough-entropy"
    publish_control_record(
        paths.ui_control_file,
        pid=77,
        port=8765,
        token=control_token,
    )
    captured_headers: dict[str, str] = {}

    class Response:
        status = 200

        @staticmethod
        def read(_limit: int) -> bytes:
            return (
                b'{"bootstrap_url":"http://127.0.0.1:8765/#token='
                + b"b" * 40
                + b'","proof":"'
                + b"0" * 64
                + b'"}'
            )

    class Connection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, _method, _path, *, body, headers) -> None:
            assert body == b""
            captured_headers.update(headers)

        @staticmethod
        def getresponse() -> Response:
            return Response()

        @staticmethod
        def close() -> None:
            pass

    monkeypatch.setattr(process_module.http.client, "HTTPConnection", Connection)
    with pytest.raises(UiProcessError) as rejected:
        open_background_ui(
            paths,
            port=8765,
            probe=lambda _port: True,
            browser_opener=lambda _url: pytest.fail("browser must not open"),
        )

    assert rejected.value.code == "UI_OPEN_RESPONSE_INVALID"
    assert process_module.CONTROL_HEADER not in captured_headers
    assert control_token not in json.dumps(captured_headers, sort_keys=True)


def test_open_reports_browser_refusal_without_printing_bootstrap(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    publish_control_record(
        paths.ui_control_file,
        pid=77,
        port=8765,
        token="control-token-with-enough-entropy-for-browser",
    )
    bootstrap_url = "http://127.0.0.1:8765/#token=" + "b" * 40
    with pytest.raises(UiProcessError) as refused:
        open_background_ui(
            paths,
            port=8765,
            probe=lambda _port: True,
            request_open=lambda _record: bootstrap_url,
            browser_opener=lambda _url: False,
        )
    assert refused.value.code == "UI_BROWSER_OPEN_FAILED"
    assert "b" * 40 not in str(refused.value)


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
    running = status_background_ui(
        paths,
        port=8765,
        probe=lambda _port: True,
        verify_control=lambda _record: True,
    )
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

    def open_ui(_paths, *, port: int):
        assert _paths is paths
        calls.append(("open", port, None))
        return BackgroundUiResult(False, True, True, port, 11)

    monkeypatch.setattr(process_module, "start_background_ui", start)
    monkeypatch.setattr(process_module, "status_background_ui", status)
    monkeypatch.setattr(process_module, "stop_background_ui", stop)
    monkeypatch.setattr(process_module, "open_background_ui", open_ui)

    assert cli.main(["ui", "--background", "--no-open"]) == 0
    assert cli.main(["ui", "--status"]) == 0
    assert cli.main(["ui", "--open"]) == 0
    assert cli.main(["ui", "--stop"]) == 0
    assert calls == [
        ("start", 8765, False),
        ("status", 8765, None),
        ("open", 8765, None),
        ("stop", 8765, None),
    ]
    output = capsys.readouterr()
    assert "started in background" in output.out
    assert "running in background" in output.out
    assert "opened" in output.out
    assert "stopped" in output.out
    assert "token=" not in output.out + output.err
