"""Small bounded newline JSON-RPC client for owned ACP stdio processes."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path


ReverseRequestHandler = Callable[
    [str, Mapping[str, object]],
    Awaitable[Mapping[str, object]],
]
NotificationHandler = Callable[[str, Mapping[str, object]], Awaitable[None]]

_JsonRpcId = int | str
_DEFAULT_REVERSE_REQUEST_ID_LIMIT = 4096
_DEFAULT_ACTIVE_CALLBACK_LIMIT = 32


class AcpStdioError(RuntimeError):
    """Base error for the private ACP wire boundary."""


class AcpProtocolError(AcpStdioError):
    """The owned child sent or required an invalid wire frame."""


class AcpMethodNotFoundError(LookupError):
    """A reverse-request handler does not implement this method."""


class AcpProcessError(AcpStdioError):
    """The exact owned child is unavailable or ended ambiguously."""


class AcpFatalCallbackError(AcpProcessError):
    """A reverse callback failed in a way that makes the session unsafe."""


class AcpRpcError(AcpStdioError):
    """Bounded JSON-RPC error returned by the owned child."""

    def __init__(self, code: int | str | None, message: str, data: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class AcpStdioProcess:
    """Own one shell-free ACP child and its newline JSON-RPC wire."""

    def __init__(
        self,
        *,
        argv: Sequence[str],
        cwd: str | os.PathLike[str],
        env: Mapping[str, str],
        request_handler: ReverseRequestHandler | None = None,
        notification_handler: NotificationHandler | None = None,
        startup_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 30.0,
        close_timeout_seconds: float = 1.0,
        max_line_bytes: int = 1_048_576,
        max_stderr_bytes: int = 65_536,
        max_reverse_request_ids: int = _DEFAULT_REVERSE_REQUEST_ID_LIMIT,
        max_active_reverse_requests: int = _DEFAULT_ACTIVE_CALLBACK_LIMIT,
        max_active_notifications: int = _DEFAULT_ACTIVE_CALLBACK_LIMIT,
    ) -> None:
        if not argv or any(not isinstance(part, str) or "\x00" in part for part in argv):
            raise ValueError("argv must contain non-NUL strings")
        if min(
            startup_timeout_seconds,
            request_timeout_seconds,
            close_timeout_seconds,
        ) <= 0:
            raise ValueError("ACP timeouts must be positive")
        if max_line_bytes < 128:
            raise ValueError("max_line_bytes is too small")
        if max_stderr_bytes < 0:
            raise ValueError("max_stderr_bytes must be nonnegative")
        if min(
            max_reverse_request_ids,
            max_active_reverse_requests,
            max_active_notifications,
        ) <= 0:
            raise ValueError("ACP callback limits must be positive")

        self._argv = tuple(argv)
        self._cwd = Path(cwd)
        self._env = dict(env)
        self._request_handler = request_handler
        self._notification_handler = notification_handler
        self._startup_timeout = startup_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._close_timeout = close_timeout_seconds
        self._max_line_bytes = max_line_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._max_reverse_request_ids = max_reverse_request_ids
        self._max_active_reverse_requests = max_active_reverse_requests
        self._max_active_notifications = max_active_notifications

        self._process: asyncio.subprocess.Process | None = None
        self._returncode: int | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._waiter_task: asyncio.Task[None] | None = None
        self._reverse_tasks: set[asyncio.Task[None]] = set()
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._notification_queue: asyncio.Queue[
            tuple[str, Mapping[str, object]]
        ] = asyncio.Queue(maxsize=max_active_notifications)
        self._notification_outstanding = 0
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Mapping[str, object]]] = {}
        self._seen_reverse_ids: set[_JsonRpcId] = set()
        self._stderr_tail = bytearray()
        self._terminal_error: AcpStdioError | None = None
        self._fatal_callback_error: AcpFatalCallbackError | None = None
        self._started = False
        self._closing = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def returncode(self) -> int | None:
        return (
            self._process.returncode
            if self._process is not None
            else self._returncode
        )

    @property
    def stderr_tail(self) -> bytes:
        return bytes(self._stderr_tail)

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            if self._closing or self._closed:
                raise AcpProcessError("ACP process is closed")
            try:
                self._process = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        *self._argv,
                        cwd=os.fspath(self._cwd),
                        env=self._env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=self._max_line_bytes + 1,
                    ),
                    timeout=self._startup_timeout,
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:
                error = AcpProcessError("ACP process start timed out")
                self._set_terminal(error)
                raise error from exc
            except OSError as exc:
                error = AcpProcessError("ACP process could not be started")
                self._set_terminal(error)
                raise error from exc

            self._started = True
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            self._waiter_task = asyncio.create_task(self._watch_process())
            if self._notification_handler is not None:
                task = asyncio.create_task(self._run_notifications())
                self._notification_tasks.add(task)
                task.add_done_callback(self._notification_tasks.discard)

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        self._ensure_available()
        timeout = self._request_timeout if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("request timeout must be positive")

        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Mapping[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except asyncio.TimeoutError as exc:
                future.cancel()
                raise TimeoutError("ACP request timed out") from exc
            except asyncio.CancelledError:
                future.cancel()
                raise
        except BaseException:
            if not future.done():
                future.cancel()
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        self._ensure_available()
        await self._write(
            {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        )

    async def close(self) -> None:
        async with self._start_lock:
            async with self._close_lock:
                if self._close_task is None:
                    self._closing = True
                    self._close_task = asyncio.create_task(self._close_owned_process())
                close_task = self._close_task
        await asyncio.shield(close_task)

    def _ensure_available(self) -> None:
        if not self._started:
            raise AcpProcessError("ACP process has not started")
        if self._closing or self._closed:
            raise AcpProcessError("ACP process is closed")
        if self._terminal_error is not None:
            raise self._copy_error(self._terminal_error)
        process = self._process
        if process is None or process.returncode is not None:
            raise AcpProcessError("ACP process is unavailable")

    async def _write(self, message: Mapping[str, object]) -> None:
        try:
            payload = json.dumps(
                message,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise AcpProtocolError("ACP request is not JSON serializable") from exc
        if len(payload) > self._max_line_bytes:
            raise AcpProtocolError("ACP request is too large")

        async with self._write_lock:
            if self._closing or self._closed:
                raise AcpProcessError("ACP process is closed")
            process = self._process
            if process is None or process.stdin is None or process.returncode is not None:
                raise AcpProcessError("ACP process is unavailable")
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                error = AcpProcessError("ACP process connection was lost")
                self._set_terminal(error)
                raise error from exc

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as exc:
                    raise AcpProtocolError("ACP response is too large") from exc
                if not line:
                    raise AcpProcessError("ACP process ended before responding")
                if len(line) > self._max_line_bytes:
                    raise AcpProtocolError("ACP response is too large")
                if not line.endswith(b"\n"):
                    raise AcpProtocolError("ACP response is not newline terminated")
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AcpProtocolError("ACP response is malformed") from exc
                if not isinstance(message, Mapping):
                    raise AcpProtocolError("ACP response is not an object")
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except AcpStdioError as exc:
            if not self._closing:
                self._set_terminal(exc)
        except BaseException:
            if not self._closing:
                self._set_terminal(AcpProtocolError("ACP response handling failed"))
        finally:
            if not self._closing and self._terminal_error is None:
                self._set_terminal(AcpProcessError("ACP process ended before responding"))

    async def _dispatch(self, message: Mapping[str, object]) -> None:
        if message.get("jsonrpc") != "2.0":
            raise AcpProtocolError("ACP response has an invalid JSON-RPC version")

        has_id = "id" in message
        has_method = "method" in message
        has_result = "result" in message
        has_error = "error" in message
        if has_result or has_error:
            if has_method or not has_id or has_result == has_error:
                raise AcpProtocolError("ACP response envelope is invalid")
            self._handle_response(message)
            return
        if not has_method:
            raise AcpProtocolError("ACP server message has no method")

        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(method, str) or not method or "\x00" in method:
            raise AcpProtocolError("ACP server method is invalid")
        if not isinstance(params, Mapping):
            raise AcpProtocolError("ACP server params are invalid")
        if not has_id:
            handler = self._notification_handler
            if handler is not None:
                if self._notification_outstanding >= self._max_active_notifications:
                    raise AcpProtocolError("ACP active notification limit exceeded")
                self._notification_outstanding += 1
                try:
                    self._notification_queue.put_nowait((method, dict(params)))
                except asyncio.QueueFull as exc:
                    self._notification_outstanding -= 1
                    raise AcpProtocolError(
                        "ACP active notification limit exceeded"
                    ) from exc
            return

        request_id = self._parse_rpc_id(message.get("id"))
        if not self._remember_reverse_id(request_id):
            return
        if len(self._reverse_tasks) >= self._max_active_reverse_requests:
            raise AcpProtocolError("ACP active reverse request limit exceeded")
        task = asyncio.create_task(self._answer_reverse(request_id, method, params))
        self._reverse_tasks.add(task)
        task.add_done_callback(self._reverse_tasks.discard)

    def _handle_response(self, message: Mapping[str, object]) -> None:
        request_id = self._parse_rpc_id(message.get("id"))
        if not isinstance(request_id, int):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return

        if "error" in message:
            error = message.get("error")
            if not isinstance(error, Mapping):
                raise AcpProtocolError("ACP error response is invalid")
            code = error.get("code")
            if isinstance(code, bool) or not isinstance(code, (int, str, type(None))):
                raise AcpProtocolError("ACP error code is invalid")
            text = error.get("message")
            if not isinstance(text, str):
                raise AcpProtocolError("ACP error message is invalid")
            future.set_exception(AcpRpcError(code, text, error.get("data")))
            return

        result = message.get("result")
        if not isinstance(result, Mapping):
            raise AcpProtocolError("ACP result is invalid")
        future.set_result(result)

    async def _answer_reverse(
        self,
        request_id: _JsonRpcId,
        method: str,
        params: Mapping[str, object],
    ) -> None:
        handler = self._request_handler
        if handler is None:
            response: Mapping[str, object] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        else:
            try:
                result = await handler(method, params)
                if not isinstance(result, Mapping):
                    raise TypeError("reverse handler returned a non-object")
                response = {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}
            except asyncio.CancelledError:
                raise
            except AcpMethodNotFoundError:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            except AcpFatalCallbackError as exc:
                if self._fatal_callback_error is None:
                    self._fatal_callback_error = exc
                self._set_terminal(exc)
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": "Internal error"},
                }
            except Exception:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": "Internal error"},
                }
        try:
            await self._write(response)
        except AcpStdioError as exc:
            if not self._closing:
                self._set_terminal(exc)

    async def _run_notifications(self) -> None:
        handler = self._notification_handler
        assert handler is not None
        while True:
            method, params = await self._notification_queue.get()
            try:
                try:
                    await handler(method, params)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            finally:
                self._notification_outstanding -= 1
                self._notification_queue.task_done()

    def _parse_rpc_id(self, value: object) -> _JsonRpcId:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise AcpProtocolError("ACP JSON-RPC id is invalid")
        if isinstance(value, str) and (not value or len(value.encode("utf-8")) > 256):
            raise AcpProtocolError("ACP JSON-RPC id is invalid")
        return value

    def _remember_reverse_id(self, request_id: _JsonRpcId) -> bool:
        if request_id in self._seen_reverse_ids:
            return False
        if len(self._seen_reverse_ids) >= self._max_reverse_request_ids:
            raise AcpProtocolError("ACP reverse request ID limit exceeded")
        self._seen_reverse_ids.add(request_id)
        return True

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while True:
                chunk = await process.stderr.read(8192)
                if not chunk:
                    return
                if self._max_stderr_bytes == 0:
                    continue
                self._stderr_tail.extend(chunk)
                excess = len(self._stderr_tail) - self._max_stderr_bytes
                if excess > 0:
                    del self._stderr_tail[:excess]
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _watch_process(self) -> None:
        process = self._process
        assert process is not None
        try:
            returncode = await process.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closing:
                self._set_terminal(AcpProcessError("ACP process wait failed"))
            return
        if self._closing:
            return
        reader = self._reader_task
        if reader is not None and not reader.done():
            await asyncio.wait({reader}, timeout=self._close_timeout)
        if self._terminal_error is None:
            self._set_terminal(
                AcpProcessError(
                    f"ACP process exited with code {returncode} before responding"
                )
            )

    async def _close_owned_process(self) -> None:
        cleanup_error: BaseException | None = None
        try:
            process = self._process
            if process is not None:
                await self._stop_process(process)
        except BaseException as exc:
            cleanup_error = exc
        finally:
            callback_tasks = self._reverse_tasks | self._notification_tasks
            io_tasks = {
                task
                for task in (
                    self._reader_task,
                    self._stderr_task,
                    self._waiter_task,
                )
                if task is not None
            }
            # Once the exact child has exited, let public stream/process tasks
            # observe EOF and finish before cancellation. This gives Proactor
            # pipe callbacks a live loop on which to settle.
            if io_tasks:
                done, pending_io = await asyncio.wait(
                    io_tasks, timeout=self._close_timeout
                )
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
                await asyncio.sleep(0)
            else:
                pending_io = set()

            remaining = set(pending_io) | callback_tasks
            for task in remaining:
                if not task.done():
                    task.cancel()
            if remaining:
                done, pending = await asyncio.wait(
                    remaining, timeout=self._close_timeout
                )
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
                await asyncio.sleep(0)
                self._reverse_tasks.difference_update(done)
                self._notification_tasks.difference_update(done)
                if pending:
                    if pending & callback_tasks:
                        timeout_error = AcpProcessError(
                            "ACP callback cleanup timed out"
                        )
                    else:
                        timeout_error = AcpProcessError("ACP I/O cleanup timed out")
                    self._set_terminal(timeout_error)
                    if cleanup_error is None:
                        cleanup_error = timeout_error
            self._discard_queued_notifications()
            terminal_error = self._terminal_error
            if terminal_error is None:
                self._fail_pending(AcpProcessError("ACP process closed"))
            fatal_callback_error = self._fatal_callback_error
            if fatal_callback_error is not None:
                cleanup_error = self._copy_error(fatal_callback_error)
            owned_process = self._process
            if owned_process is not None and owned_process.returncode is not None:
                self._returncode = owned_process.returncode
                self._process = None
                owned_process = None
                process = None
                await asyncio.sleep(0)
            self._closed = True
        if cleanup_error is not None:
            raise cleanup_error

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        stdin = process.stdin
        if stdin is not None:
            stdin.close()
            try:
                await asyncio.wait_for(
                    stdin.wait_closed(), timeout=self._close_timeout
                )
            except (
                BrokenPipeError,
                ConnectionResetError,
                TimeoutError,
                asyncio.TimeoutError,
            ):
                pass

        if process.returncode is not None:
            return
        if await self._wait_process(process):
            return

        try:
            process.terminate()
        except ProcessLookupError:
            pass
        if await self._wait_process(process):
            return

        try:
            process.kill()
        except ProcessLookupError:
            pass
        if not await self._wait_process(process):
            raise AcpProcessError("ACP process did not exit after kill")

    async def _wait_process(self, process: asyncio.subprocess.Process) -> bool:
        try:
            await asyncio.wait_for(process.wait(), timeout=self._close_timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False

    def _discard_queued_notifications(self) -> None:
        while True:
            try:
                self._notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._notification_outstanding -= 1
            self._notification_queue.task_done()

    def _set_terminal(self, error: AcpStdioError) -> None:
        if self._terminal_error is not None:
            return
        self._terminal_error = error
        self._fail_pending(error)

    def _fail_pending(self, error: AcpStdioError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(self._copy_error(error))

    @staticmethod
    def _copy_error(error: AcpStdioError) -> AcpStdioError:
        if isinstance(error, AcpProtocolError):
            return AcpProtocolError(str(error))
        if isinstance(error, AcpFatalCallbackError):
            return AcpFatalCallbackError(str(error))
        if isinstance(error, AcpProcessError):
            return AcpProcessError(str(error))
        return AcpStdioError(str(error))
