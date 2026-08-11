from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from embpilot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embpilot",
        description="EmbPilot — Embedded Debugging MCP Server",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
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
    parser.add_argument(
        "--command-timeout-max-ms",
        type=int,
        default=None,
        help="Maximum send_command timeout in milliseconds (default: 60000)",
    )
    parser.add_argument(
        "--search-limit-max",
        type=int,
        default=None,
        help="Maximum search_history_logs result limit (default: 1000)",
    )
    parser.add_argument(
        "--export-limit-max",
        type=int,
        default=None,
        help="Maximum export_session row limit (default: 10000)",
    )
    parser.add_argument(
        "--audit-export-limit-max",
        type=int,
        default=None,
        help="Maximum export_operation_history row limit (default: 5000)",
    )
    parser.add_argument(
        "--tool-rate-limit-per-minute",
        type=int,
        default=None,
        help="Maximum MCP tool calls per minute; <=0 disables the limiter (default: 120)",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="Run environment diagnostics and exit",
    )
    from embpilot.agent_install.cli import add_agent_subcommands

    add_agent_subcommands(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        from embpilot.doctor import run_doctor

        sys.exit(run_doctor())

    if args.command in {"install", "uninstall"}:
        from embpilot.agent_install.cli import run_agent_command

        try:
            run_agent_command(args)
        except ValueError as exc:
            parser.error(str(exc))
        return

    from embpilot.config import EmbPilotConfig
    from embpilot.mcp_app import run_stdio_mcp_server

    config = EmbPilotConfig.from_args(args)
    run_stdio_mcp_server(config)
