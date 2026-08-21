"""Console entry point for Subagent MCP."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__


PROGRAM_NAME = "subagent-harness-mcp"
MCP_REGISTRATION_NAME = "subagent-mcp"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Subagent MCP local orchestration service.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("command", nargs="?", help="Command to run")
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    return parser


def _command_error(parser: argparse.ArgumentParser, message: str) -> int:
    parser.print_usage(sys.stderr)
    print(
        f"{PROGRAM_NAME}: error: {message}; run '{PROGRAM_NAME} --help'",
        file=sys.stderr,
    )
    return 2


def _lifecycle_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{PROGRAM_NAME} {command}")
    parser.add_argument("--dry-run", action="store_true")
    if command in {"install", "update"}:
        parser.add_argument("--runtime", type=Path, required=True)
        parser.add_argument("--runtime-version", required=True)
    if command in {"register", "uninstall"}:
        parser.add_argument(
            "--client",
            choices=("codex",),
            default="codex",
            help="Official MCP client lifecycle to use",
        )
    return parser


def _ui_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("PORT must be 0 through 65535") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("PORT must be 0 through 65535")
    return port


def _ui_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{PROGRAM_NAME} ui")
    parser.add_argument(
        "--port",
        type=_ui_port,
        default=8765,
        metavar="PORT",
        help="Loopback port (default: 8765; use 0 for an ephemeral port)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--background",
        action="store_true",
        help="Keep the localhost UI running after this command exits",
    )
    mode.add_argument("--status", action="store_true", help="Show localhost UI status")
    mode.add_argument("--stop", action="store_true", help="Stop the managed background UI")
    mode.add_argument("--background-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the UI in the default browser",
    )
    return parser


def _print_ui_result(action: str, result: object) -> None:
    from .ui_process import BackgroundUiResult

    if not isinstance(result, BackgroundUiResult):
        raise TypeError("invalid UI process result")
    origin = f"http://127.0.0.1:{result.port}"
    if action == "start":
        state = "started in background" if result.changed else "already running"
        print(f"ui: {state} at {origin}")
    elif action == "status":
        if not result.running:
            print("ui: stopped")
        else:
            mode = "background" if result.managed else "foreground"
            print(f"ui: running in {mode} at {origin}")
    elif result.changed:
        print("ui: stopped")
    else:
        print("ui: already stopped")


def _run_ui_command(command_args: Sequence[str]) -> int:
    from .paths import resolve_paths
    from .ui import UiError, run_ui
    from .ui_process import (
        UiProcessError,
        start_background_ui,
        status_background_ui,
        stop_background_ui,
    )

    parser = _ui_parser()
    arguments = parser.parse_args(command_args)
    if (arguments.background or arguments.background_child) and arguments.port == 0:
        parser.error("background UI requires a fixed PORT")
    if (arguments.stop or arguments.status) and arguments.no_open:
        parser.error("--no-open is only valid when starting the UI")
    try:
        if arguments.background:
            result = start_background_ui(
                resolve_paths(),
                port=arguments.port,
                open_browser=not arguments.no_open,
            )
            _print_ui_result("start", result)
            return 0
        if arguments.status:
            result = status_background_ui(resolve_paths(), port=arguments.port)
            _print_ui_result("status", result)
            return 0
        if arguments.stop:
            result = stop_background_ui(resolve_paths(), port=arguments.port)
            _print_ui_result("stop", result)
            return 0
        control_file = resolve_paths().ui_control_file if arguments.background_child else None
        return run_ui(
            port=arguments.port,
            open_browser=not arguments.no_open,
            control_file=control_file,
        )
    except (UiError, UiProcessError) as exc:
        print(f"{PROGRAM_NAME}: error: {exc.code}: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(f"{PROGRAM_NAME}: error: localhost UI could not start", file=sys.stderr)
        return 1


def _print_lifecycle_result(result: object) -> None:
    from .install import LifecycleResult

    if not isinstance(result, LifecycleResult):
        raise TypeError("invalid lifecycle result")
    state = "recovery-required" if result.recovery_required else (
        "planned" if result.dry_run else ("changed" if result.changed else "unchanged")
    )
    suffix = f" ({', '.join(result.actions)})" if result.actions else ""
    print(f"{result.operation}: {state}{suffix}")


def _run_lifecycle(command: str, command_args: Sequence[str]) -> int:
    from .install import (
        CodexRegistrationBackend,
        SubprocessHealthBackend,
        WindowsLifecycleManager,
    )
    from .launcher import LifecycleError
    from .paths import resolve_paths

    parser = _lifecycle_parser(command)
    arguments = parser.parse_args(command_args)
    manager = WindowsLifecycleManager(resolve_paths())
    health = SubprocessHealthBackend()
    registration = CodexRegistrationBackend()
    try:
        if command == "install":
            result = manager.install(
                arguments.runtime,
                version=arguments.runtime_version,
                health=health,
                dry_run=arguments.dry_run,
            )
        elif command == "update":
            result = manager.update(
                arguments.runtime,
                version=arguments.runtime_version,
                health=health,
                dry_run=arguments.dry_run,
            )
        elif command == "rollback":
            result = manager.rollback(health=health, dry_run=arguments.dry_run)
        elif command == "register":
            result = manager.register(
                MCP_REGISTRATION_NAME,
                backend=registration,
                dry_run=arguments.dry_run,
            )
        else:
            result = manager.uninstall(
                registration_backend=registration,
                dry_run=arguments.dry_run,
            )
    except LifecycleError as exc:
        print(f"{PROGRAM_NAME}: error: {exc.code}: {exc}", file=sys.stderr)
        return 1
    _print_lifecycle_result(result)
    return 1 if result.recovery_required else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI without importing an adapter or starting a provider process."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        return _command_error(parser, "a command is required")
    if arguments.command == "serve" and arguments.command_args:
        return _command_error(
            parser,
            f"command {arguments.command!r} does not accept arguments",
        )
    if arguments.command == "serve":
        try:
            from .server import run_stdio

            return run_stdio()
        except Exception:
            print(
                f"{PROGRAM_NAME}: error: stdio server could not start",
                file=sys.stderr,
            )
            return 1
    if arguments.command == "ui":
        return _run_ui_command(arguments.command_args)
    if arguments.command in {"install", "update", "rollback", "register", "uninstall"}:
        return _run_lifecycle(arguments.command, arguments.command_args)
    return _command_error(
        parser,
        f"command {arguments.command!r} is not available yet",
    )


if __name__ == "__main__":
    raise SystemExit(main())
