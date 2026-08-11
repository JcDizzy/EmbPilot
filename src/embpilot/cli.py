"""EmbPilot command-line interface.

The CLI is a thin wrapper over the same dispatch layer the MCP server uses
(``dispatch_tool`` + ``build_tool_definitions``), so every tool available to
agents is available from the shell with identical validation and error codes.

Usage:
    embpilot                          # start MCP server (stdio)
    embpilot --data-dir ~/embdata    # custom data directory
    embpilot tools                    # list available MCP tools
    embpilot tool <name> --json '<args>'   # one-shot tool call
    embpilot shell                    # interactive REPL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from embpilot import __version__
from embpilot.cli_format import format_result
from embpilot.cli_shell import run_shell
from embpilot.config import EmbPilotConfig
from embpilot.mcp_contracts import build_tool_definitions, dispatch_tool

_JSON_NOTE = "Pass arguments as a JSON object, not as a JSON-encoded string. "


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    """Shared data-path and runtime options, identical to the server CLI."""
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Root data directory for all database files "
             "(default: XDG-compliant platform path)",
    )
    parser.add_argument(
        "--main-db-path",
        default=None,
        help="Central database file path (default: <data-dir>/embpilot_main.db)",
    )
    parser.add_argument(
        "--session-data-dir",
        default=None,
        help="Directory for per-session database files "
             "(default: <data-dir>/sessions)",
    )
    parser.add_argument(
        "--lancedb-path",
        default=None,
        help="LanceDB vector store directory (default: <data-dir>/lancedb)",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Auto-delete session files older than N days (default: 30)",
    )
    parser.add_argument(
        "--retention-max-gb",
        type=int,
        default=None,
        help="Cap total session storage at N GB (default: 5)",
    )
    parser.add_argument(
        "--framing-timeout-ms",
        type=int,
        default=None,
        help="Frame assembly timeout in milliseconds (default: 50)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the full ``embpilot`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="embpilot",
        description="EmbPilot - Embedded Debugging MCP Server",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    _add_config_args(parser)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("tools", help="List all available MCP tools")

    tool_p = sub.add_parser("tool", help="Run one MCP tool in one-shot mode")
    tool_p.add_argument("name", help="Tool name, e.g. connect_serial")
    tool_p.add_argument(
        "--json",
        dest="json_args",
        default="{}",
        help="Tool arguments as a JSON object, e.g. '{\"port\": \"COM3\"}' (default: {})",
    )
    tool_p.add_argument(
        "--json-output",
        action="store_true",
        help="Print the structured result (ok/data/error) as JSON",
    )

    shell_p = sub.add_parser(
        "shell",
        help="Start an interactive REPL with a persistent connection",
    )
    shell_p.add_argument(
        "--json-output",
        action="store_true",
        help="Print structured results as JSON",
    )

    return parser


def tools_text() -> str:
    """Render the tool catalog for ``embpilot tools``."""
    lines: list[str] = []
    for tool in sorted(build_tool_definitions(), key=lambda item: item.name):
        description = tool.description.replace(_JSON_NOTE, "")
        lines.append(f"{tool.name}\n  {description}")
    return "\n".join(lines) + "\n"


def _run_one_shot(args: argparse.Namespace, config: EmbPilotConfig) -> int:
    """Execute one tool call in a fresh process-scoped session manager."""
    try:
        arguments = json.loads(args.json_args)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON for --json: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("error: --json must be a JSON object", file=sys.stderr)
        return 2

    tool_names = {t.name for t in build_tool_definitions()}
    if args.name not in tool_names:
        print(f"error: unknown tool '{args.name}' (see 'embpilot tools')", file=sys.stderr)
        return 2

    async def _run() -> int:
        from embpilot.server import SessionManager

        config.ensure_data_dirs()
        manager = SessionManager(config)
        await manager.start()
        try:
            result = await dispatch_tool(manager, args.name, arguments)
        finally:
            await manager.shutdown()

        print(format_result(result, json_output=args.json_output))
        payload = result.structuredContent or {}
        if payload.get("ok") is True:
            return 0
        code = (payload.get("error") or {}).get("code", "OPERATION_FAILED")
        return 2 if code in ("UNKNOWN_TOOL", "INVALID_ARGUMENT") else 1

    return asyncio.run(_run())


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point: server by default, subcommands for tool access."""
    parser = build_parser()
    args = parser.parse_args(argv)
    config = EmbPilotConfig.from_args(args)

    if args.command is None:
        from embpilot.server import serve

        serve(config)
        return

    if args.command == "tools":
        print(tools_text(), end="")
        return

    if args.command == "tool":
        raise SystemExit(_run_one_shot(args, config))

    if args.command == "shell":
        try:
            asyncio.run(run_shell(config, json_output=args.json_output))
        except KeyboardInterrupt:
            print("bye")
        return

    parser.error(f"unknown command: {args.command}")