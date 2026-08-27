from __future__ import annotations

import asyncio
import gc
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from subagent_harness_mcp.adapters.acp_stdio import (
    AcpMethodNotFoundError,
    AcpProcessError,
    AcpProtocolError,
    AcpRpcError,
    AcpStdioProcess,
)


FAKE_ACP = (
    Path(__file__).parents[1] / "fixtures" / "fake_grok_acp.py"
).resolve(strict=True)


def _child_env(**extra: str) -> dict[str, str]:
    names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(extra)
    return env


def _client(
    tmp_path: Path,
    scenario: str,
    *,
    request_handler: Any = None,
    notification_handler: Any = None,
    request_timeout_seconds: float = 1.0,
    close_timeout_seconds: float = 0.1,
    max_line_bytes: int = 1_048_576,
    max_stderr_bytes: int = 4096,
    max_reverse_request_ids: int = 64,
    max_active_reverse_requests: int = 16,
    max_active_notifications: int = 16,
    extra_argv: tuple[str, ...] = (),
    env: Mapping[str, str] | None = None,
) -> AcpStdioProcess:
    return AcpStdioProcess(
        argv=(sys.executable, "-I", str(FAKE_ACP), scenario, *extra_argv),
        cwd=tmp_path,
        env=dict(env or _child_env()),
        request_handler=request_handler,
        notification_handler=notification_handler,
        startup_timeout_seconds=1.0,
        request_timeout_seconds=request_timeout_seconds,
        close_timeout_seconds=close_timeout_seconds,
        max_line_bytes=max_line_bytes,
        max_stderr_bytes=max_stderr_bytes,
        max_reverse_request_ids=max_reverse_request_ids,
        max_active_reverse_requests=max_active_reverse_requests,
        max_active_notifications=max_active_notifications,
    )


def test_start_request_notify_and_close_are_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        updates: list[tuple[str, Mapping[str, object]]] = []
        update_seen = asyncio.Event()

        async def notify(method: str, params: Mapping[str, object]) -> None:
            updates.append((method, params))
            update_seen.set()

        client = _client(tmp_path, "happy", notification_handler=notify)
        await client.start()
        process = client._process
        await client.start()
        assert client._process is process

        assert await client.request("initialize", {"protocolVersion": 1}) == {
            "requestId": 1,
            "method": "initialize",
            "params": {"protocolVersion": 1},
        }
        await client.notify("initialized", {})
        await asyncio.wait_for(update_seen.wait(), timeout=1)
        assert updates == [("session/update", {"kind": "ready"})]

        await client.close()
        await client.close()
        assert client.closed is True
        assert client.returncode == 0
        assert all(
            task is None or task.done()
            for task in (client._reader_task, client._stderr_task, client._waiter_task)
        )
        assert not client._reverse_tasks
        assert not client._notification_tasks

    asyncio.run(scenario())


def test_process_launch_preserves_argv_cwd_and_explicit_environment(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        marker = "literal & | ; $(not-a-shell)"
        client = _client(
            tmp_path,
            "happy",
            extra_argv=(marker,),
            env=_child_env(ACP_TEST_ENV="explicit-only"),
        )
        await client.start()
        try:
            result = await client.request("identity", {})
            assert result == {
                "argv": [marker],
                "cwd": str(tmp_path),
                "env": "explicit-only",
            }
        finally:
            await client.close()

    asyncio.run(scenario())


def test_concurrent_requests_correlate_reverse_order_with_monotonic_ids(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "correlate")
        await client.start()
        try:
            first, second = await asyncio.gather(
                client.request("first", {"value": 1}),
                client.request("second", {"value": 2}),
            )
            assert first == {
                "requestId": 1,
                "method": "first",
                "params": {"value": 1},
            }
            assert second == {
                "requestId": 2,
                "method": "second",
                "params": {"value": 2},
            }
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("scenario_name", ["reverse", "reverse-duplicate"])
def test_reverse_request_is_answered_exactly_once(
    tmp_path: Path, scenario_name: str
) -> None:
    async def scenario() -> None:
        calls = 0

        async def handle(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            nonlocal calls
            calls += 1
            assert method == "fs/read_text_file"
            assert params == {"path": "README.md"}
            return {"content": "bounded"}

        client = _client(tmp_path, scenario_name, request_handler=handle)
        await client.start()
        try:
            result = await client.request("trigger/reverse", {})
            assert result["reverseResponse"] == {
                "jsonrpc": "2.0",
                "id": "reverse-1",
                "result": {"content": "bounded"},
            }
            assert calls == 1
        finally:
            await client.close()

    asyncio.run(scenario())


def test_unknown_reverse_method_gets_method_not_found(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "reverse")
        await client.start()
        try:
            result = await client.request("trigger/reverse", {})
            response = result["reverseResponse"]
            assert response["id"] == "reverse-1"
            assert response["error"] == {
                "code": -32601,
                "message": "Method not found",
            }
        finally:
            await client.close()

    asyncio.run(scenario())


def test_typed_unknown_reverse_method_is_method_not_found_without_terminal_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async def handle(
            method: str, params: Mapping[str, object]
        ) -> Mapping[str, object]:
            raise AcpMethodNotFoundError(method)

        client = _client(
            tmp_path,
            "filesystem-unknown",
            request_handler=handle,
        )
        await client.start()
        try:
            result = await client.request("trigger/filesystem", {})
            assert result["reverseResponse"] == {
                "jsonrpc": "2.0",
                "id": "filesystem-1",
                "error": {"code": -32601, "message": "Method not found"},
            }
            assert (await client.request("still/alive", {}))["method"] == "still/alive"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_reverse_ids_are_lifetime_bounded_without_reexecuting_evicted_ids(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def handle(
            method: str, params: Mapping[str, object]
        ) -> Mapping[str, object]:
            calls.append(str(params["path"]))
            return {"content": "bounded"}

        client = _client(
            tmp_path,
            "reverse-id-cap",
            request_handler=handle,
            request_timeout_seconds=2,
        )
        await client.start()
        try:
            with pytest.raises(AcpProtocolError, match="reverse request ID limit"):
                await client.request("trigger/reverse-cap", {})
            assert len(calls) == 64
            assert calls.count("file-0.txt") == 1
            assert "duplicate.txt" not in calls
            assert "over-cap.txt" not in calls
        finally:
            await client.close()

    asyncio.run(scenario())


def test_active_reverse_callback_flood_fails_closed_without_task_exhaustion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        calls = 0

        async def handle(
            method: str, params: Mapping[str, object]
        ) -> Mapping[str, object]:
            nonlocal calls
            calls += 1
            await release.wait()
            return {"content": "bounded"}

        client = _client(
            tmp_path,
            "reverse-active-flood",
            request_handler=handle,
            request_timeout_seconds=1,
        )
        await client.start()
        try:
            with pytest.raises(AcpProtocolError, match="active reverse request limit"):
                await client.request("trigger/reverse-flood", {})
            assert calls <= 16
            assert len(client._reverse_tasks) <= 16
        finally:
            release.set()
            await client.close()

    asyncio.run(scenario())


def test_unknown_and_duplicate_response_ids_do_not_corrupt_pending_requests(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "unknown-and-duplicate")
        await client.start()
        try:
            assert (await client.request("one", {}))["requestId"] == 1
            assert (await client.request("two", {}))["requestId"] == 2
        finally:
            await client.close()

    asyncio.run(scenario())


def test_notification_callbacks_do_not_block_response_correlation_or_exit_drain(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def notify(method: str, params: Mapping[str, object]) -> None:
            started.set()
            await release.wait()

        client = _client(
            tmp_path,
            "slow-notification-response",
            notification_handler=notify,
            request_timeout_seconds=1,
            close_timeout_seconds=0.2,
        )
        await client.start()
        try:
            result = await client.request("immediate", {})
            assert result["method"] == "immediate"
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            release.set()
            await client.close()

    asyncio.run(scenario())


def test_notification_callbacks_complete_in_wire_order_without_blocking_response(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        notifications_done = asyncio.Event()
        observed: list[int] = []

        async def notify(method: str, params: Mapping[str, object]) -> None:
            index = int(params["index"])
            if index == 1:
                first_started.set()
                await release_first.wait()
            observed.append(index)
            if len(observed) == 2:
                notifications_done.set()

        client = _client(
            tmp_path,
            "ordered-notifications-response",
            notification_handler=notify,
            request_timeout_seconds=1,
            close_timeout_seconds=0.2,
        )
        await client.start()
        try:
            result = await client.request("immediate", {})
            assert result["method"] == "immediate"
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert observed == []
            release_first.set()
            await asyncio.wait_for(notifications_done.wait(), timeout=1)
            assert observed == [1, 2]
        finally:
            release_first.set()
            await client.close()

    asyncio.run(scenario())


def test_active_notification_callback_flood_fails_closed_without_task_exhaustion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        calls = 0

        async def notify(method: str, params: Mapping[str, object]) -> None:
            nonlocal calls
            calls += 1
            await release.wait()

        client = _client(
            tmp_path,
            "notification-active-flood",
            notification_handler=notify,
            request_timeout_seconds=1,
        )
        await client.start()
        try:
            with pytest.raises(AcpProtocolError, match="active notification limit"):
                await client.request("trigger/notification-flood", {})
            assert calls <= 16
            assert len(client._notification_tasks) == 1
        finally:
            release.set()
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "scenario_name",
    ["malformed", "invalid-envelope", "invalid-utf8", "unterminated", "oversized"],
)
def test_malformed_or_oversized_stdout_is_terminal(
    tmp_path: Path, scenario_name: str
) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, scenario_name)
        await client.start()
        try:
            with pytest.raises(AcpProtocolError):
                await client.request("initialize", {})
            with pytest.raises(AcpProtocolError):
                await client.request("later", {})
        finally:
            await client.close()

    asyncio.run(scenario())


def test_eof_before_response_is_terminal(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "eof")
        await client.start()
        try:
            with pytest.raises(AcpProcessError, match="ended before responding"):
                await client.request("initialize", {})
        finally:
            await client.close()

    asyncio.run(scenario())


def test_outgoing_frames_are_bounded_before_write(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "happy", max_line_bytes=256)
        await client.start()
        try:
            with pytest.raises(AcpProtocolError, match="request is too large"):
                await client.request("large", {"value": "x" * 512})
            assert (await client.request("small", {}))["method"] == "small"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_stderr_is_drained_without_deadlock_and_keeps_only_bounded_tail(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "stderr", max_stderr_bytes=2048)
        await client.start()
        try:
            assert (await client.request("initialize", {}))["method"] == "initialize"
            await asyncio.sleep(0)
            assert len(client.stderr_tail) == 2048
            assert client.stderr_tail == b"e" * 2048
        finally:
            await client.close()

    asyncio.run(scenario())


def test_request_timeout_removes_pending_and_process_remains_usable(tmp_path: Path) -> None:
    async def scenario() -> None:
        seen = asyncio.Event()

        async def notify(method: str, params: Mapping[str, object]) -> None:
            if method == "request/seen":
                seen.set()

        client = _client(
            tmp_path,
            "hang-once",
            notification_handler=notify,
            request_timeout_seconds=0.05,
        )
        await client.start()
        try:
            request = asyncio.create_task(client.request("slow", {}))
            await asyncio.wait_for(seen.wait(), timeout=1)
            with pytest.raises(TimeoutError):
                await request
            assert not client._pending
            assert (await client.request("next", {}))["method"] == "next"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_caller_cancellation_removes_pending_and_process_remains_usable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        seen = asyncio.Event()

        async def notify(method: str, params: Mapping[str, object]) -> None:
            if method == "request/seen":
                seen.set()

        client = _client(tmp_path, "hang-once", notification_handler=notify)
        await client.start()
        try:
            request = asyncio.create_task(client.request("slow", {}))
            await asyncio.wait_for(seen.wait(), timeout=1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not client._pending
            assert (await client.request("next", {}))["method"] == "next"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_close_settles_pending_and_cleans_all_owned_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        seen = asyncio.Event()

        async def notify(method: str, params: Mapping[str, object]) -> None:
            if method == "request/seen":
                seen.set()

        client = _client(tmp_path, "graceful-hang", notification_handler=notify)
        await client.start()
        request = asyncio.create_task(client.request("slow", {}))
        await asyncio.wait_for(seen.wait(), timeout=1)
        await client.close()
        with pytest.raises(AcpProcessError, match="closed"):
            await request
        assert not client._pending
        assert client.returncode == 0
        assert all(
            task is None or task.done()
            for task in (client._reader_task, client._stderr_task, client._waiter_task)
        )
        assert not client._reverse_tasks
        assert not client._notification_tasks

    asyncio.run(scenario())


def test_close_terminates_the_owned_child_after_grace_period(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "ignore-eof", close_timeout_seconds=0.05)
        await client.start()
        await asyncio.wait_for(client.close(), timeout=1)
        assert client.returncode is not None
        assert all(
            task is None or task.done()
            for task in (client._reader_task, client._stderr_task, client._waiter_task)
        )
        assert not client._reverse_tasks
        assert not client._notification_tasks

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows Proactor transport regression")
def test_close_settles_fresh_short_loop_proactor_transports(tmp_path: Path) -> None:
    for _ in range(12):
        async def scenario() -> None:
            client = _client(tmp_path, "happy")
            await client.start()
            await client.close()
            assert all(
                task is None or task.done()
                for task in (
                    client._reader_task,
                    client._stderr_task,
                    client._waiter_task,
                )
            )

        asyncio.run(scenario())
        gc.collect()


def test_close_uses_stdin_then_terminate_then_kill_on_the_exact_owned_handle(
    tmp_path: Path,
) -> None:
    class Stdin:
        def __init__(self, events: list[str]) -> None:
            self._events = events

        def close(self) -> None:
            self._events.append("stdin-close")

        async def wait_closed(self) -> None:
            self._events.append("stdin-wait")

    class Process:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.stdin = Stdin(self.events)
            self.stdout = None
            self.stderr = None
            self.returncode: int | None = None
            self._killed = asyncio.Event()

        async def wait(self) -> int:
            self.events.append("wait")
            await self._killed.wait()
            return 9

        def terminate(self) -> None:
            self.events.append("terminate")

        def kill(self) -> None:
            self.events.append("kill")
            self.returncode = 9
            self._killed.set()

    async def scenario() -> None:
        client = _client(tmp_path, "happy", close_timeout_seconds=0.01)
        process = Process()
        client._process = process  # type: ignore[assignment]
        client._started = True

        await asyncio.wait_for(client.close(), timeout=1)

        assert process.events == [
            "stdin-close",
            "stdin-wait",
            "wait",
            "terminate",
            "wait",
            "kill",
            "wait",
        ]
        assert client._process is None
        assert client.returncode == 9

    asyncio.run(scenario())


def test_close_waits_for_inflight_start_and_cleans_the_created_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        create_process = asyncio.create_subprocess_exec

        async def delayed_create(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            return await create_process(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
        client = _client(tmp_path, "happy")
        start = asyncio.create_task(client.start())
        await asyncio.wait_for(entered.wait(), timeout=1)
        close = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        release.set()
        process: asyncio.subprocess.Process | None = None
        try:
            await start
            process = client._process
            await close
            assert client.closed is True
            assert client.returncode is not None
        finally:
            if process is not None and process.returncode is None:
                if process.stdin is not None:
                    process.stdin.close()
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=1)
            tasks = tuple(
                task
                for task in (
                    client._reader_task,
                    client._stderr_task,
                    client._waiter_task,
                )
                if task is not None and not task.done()
            )
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_close_is_bounded_when_callback_delays_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handle(
            method: str, params: Mapping[str, object]
        ) -> Mapping[str, object]:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return {"content": "late"}

        client = _client(
            tmp_path,
            "happy",
            request_handler=handle,
            close_timeout_seconds=0.05,
        )
        await client.start()
        await client._dispatch(
            {
                "jsonrpc": "2.0",
                "id": "slow-reverse",
                "method": "fs/read_text_file",
                "params": {"path": "README.md"},
            }
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        close = asyncio.create_task(client.close())
        try:
            with pytest.raises(AcpProcessError, match="callback cleanup timed out"):
                await asyncio.wait_for(asyncio.shield(close), timeout=0.5)
        finally:
            release.set()
            if not close.done():
                await asyncio.wait_for(close, timeout=1)
            for _ in range(20):
                if not client._reverse_tasks:
                    break
                await asyncio.sleep(0)
            assert not client._reverse_tasks

    asyncio.run(scenario())


def test_rpc_error_preserves_bounded_wire_fields_without_interpretation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "rpc-error")
        await client.start()
        try:
            with pytest.raises(AcpRpcError) as caught:
                await client.request("session/prompt", {})
            assert caught.value.code == -32603
            assert caught.value.message == "bounded provider detail"
            assert caught.value.data == {"providerCode": "TEST_ONLY"}
        finally:
            await client.close()

    asyncio.run(scenario())


def test_provider_like_fields_are_returned_opaque(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _client(tmp_path, "happy")
        await client.start()
        try:
            provider_data = {
                "stopReason": "quota",
                "model": "opaque-model",
                "sessionId": "native-session",
                "permission": "writer",
            }
            result = await client.request("opaque", provider_data)
            assert result["params"] == provider_data
        finally:
            await client.close()

    asyncio.run(scenario())
