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
    if arguments.command in {"serve", "ui"} and arguments.command_args:
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
        try:
            from .ui import run_ui

            return run_ui()
        except Exception:
            print(
                f"{PROGRAM_NAME}: error: localhost UI could not start",
                file=sys.stderr,
            )
            return 1
    if arguments.command in {"install", "update", "rollback", "register", "uninstall"}:
        return _run_lifecycle(arguments.command, arguments.command_args)
    return _command_error(
        parser,
        f"command {arguments.command!r} is not available yet",
    )


if __name__ == "__main__":
    raise SystemExit(main())
