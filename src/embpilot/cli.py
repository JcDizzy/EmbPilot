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
from embpilot.cli_flags import add_schema_flags, collect_flag_arguments
from embpilot.cli_loop import known_tool_names, tool_help_text
from embpilot.cli_format import format_result
from embpilot.cli_shell import run_batch, run_serve, run_shell
from embpilot.config import EmbPilotConfig
from embpilot.installer.targets import list_target_ids
from embpilot.mcp_contracts import build_tool_definitions, dispatch_tool
from embpilot.mcp_compat import result_structured, tool_input_schema

_JSON_NOTE = "Pass arguments as a JSON object, not as a JSON-encoded string. "


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _force_utf8_stdio() -> None:
    """Emit UTF-8 on Windows consoles/pipes so non-ASCII system messages
    (e.g. Chinese serial-port errors) stay parseable by agents; replacement
    chars keep output valid when the console codepage cannot represent them.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _validate_socket_endpoint(endpoint: str) -> str:
    """Reject non-loopback TCP endpoints: the daemon is a local-only service."""
    if not endpoint.startswith("tcp:"):
        return endpoint
    host = endpoint[len("tcp:") :].rsplit(":", 1)[0]
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing non-loopback daemon endpoint '{endpoint}': "
            "bind 127.0.0.1 (or use unix:PATH on POSIX)"
        )
    return endpoint


def _target_ids() -> str:
    return ",".join(list_target_ids())


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
    parser.add_argument(
        "--socket",
        default=None,
        metavar="ENDPOINT",
        help="Talk to a running 'embpilot serve' daemon instead of a local "
        "session. Accepts unix:PATH, tcp:HOST:PORT, or the path of the "
        "daemon.json endpoint file the daemon wrote.",
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

    batch_p = sub.add_parser(
        "batch",
        help="Run a scripted sequence of tool calls (JSONL in, JSONL out)",
    )
    batch_p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first failing tool call (default: continue)",
    )
    batch_p.add_argument(
        "--json-output",
        action="store_true",
        help="Accepted for symmetry; batch always prints one JSON envelope per line",
    )

    serve_p = sub.add_parser(
        "serve",
        help="Run a persistent daemon sharing one session manager",
    )
    serve_p.add_argument(
        "--socket",
        default=None,
        help="Listen endpoint: unix:PATH (POSIX) or tcp:HOST:PORT (Windows); "
        "defaults to a platform-appropriate loopback endpoint",
    )

    help_p = sub.add_parser(
        "help",
        help="Show detailed help for one tool (schema, examples, guidance)",
    )
    help_p.add_argument("name", help="Tool name, e.g. connect_serial")

    run_p = sub.add_parser(
        "run",
        help="Connect, run several commands, and disconnect in one invocation",
    )
    run_p.add_argument(
        "--interface",
        choices=["serial", "ssh", "telnet"],
        default="serial",
        help="Connection type (default: serial)",
    )
    run_p.add_argument(
        "--connect",
        default=None,
        help="Connection arguments as a JSON object, e.g. '{\"port\": \"COM3\"}'; "
        "omitted to run commands without connecting",
    )
    run_p.add_argument(
        "--timeout-ms",
        type=int,
        default=5000,
        help="Default timeout for each command (default: 5000)",
    )
    run_p.add_argument(
        "--line-ending",
        choices=["session", "none", "lf", "crlf", "cr"],
        default="session",
        help="Line ending for each command (default: session)",
    )
    run_p.add_argument(
        "commands",
        nargs="+",
        help="Commands to run in order",
    )

    for sub_name, sub_help in (
        ("install", "Wire EmbPilot into agent harnesses (MCP config + instructions)"),
        ("uninstall", "Remove EmbPilot wiring from agent harnesses"),
    ):
        inst_p = sub.add_parser(sub_name, help=sub_help)
        inst_p.add_argument(
            "--target",
            default=None,
            help=f"Targets: auto (detected), all, none, or comma list [{_target_ids()}] "
            "(interactive picker when omitted)",
        )
        inst_p.add_argument(
            "--location",
            choices=["global", "local"],
            default=None,
            help="Scope: project files (local) or user files (global); default: local",
        )
        inst_p.add_argument(
            "--yes",
            action="store_true",
            help="Non-interactive: use auto-detected targets and local scope without confirmation",
        )
        inst_p.add_argument(
            "--check",
            action="store_true",
            help="Report configuration state without writing anything",
        )
        inst_p.add_argument(
            "--print-config",
            metavar="TARGET",
            help="Print the manual config snippet for one target without writing",
        )

    return parser


def tools_text() -> str:
    """Render the tool catalog for ``embpilot tools``."""
    lines: list[str] = []
    for tool in sorted(build_tool_definitions(), key=lambda item: item.name):
        description = tool.description.replace(_JSON_NOTE, "")
        lines.append(f"{tool.name}\n  {description}")
        schema = tool_input_schema(tool)
        examples = schema.get("examples") or []
        if examples:
            lines.append(f"  example: {json.dumps(examples[0], ensure_ascii=False)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"



def _parse_tool_arguments(args: argparse.Namespace) -> dict | None:
    """Parse --json and merge schema flags; prints the error and returns
    None on invalid input (shared by local and --socket paths)."""
    try:
        arguments = collect_flag_arguments(
            args, args.name, json.loads(args.json_args)
        )
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON for --json: {exc}", file=sys.stderr)
        return None
    if not isinstance(arguments, dict):
        print("error: --json must be a JSON object", file=sys.stderr)
        return None
    return arguments


def _exit_code_for(payload: dict) -> int:
    """Map an ok/data/error envelope to the CLI exit-code contract."""
    if payload.get("ok") is True:
        return 0
    code = (payload.get("error") or {}).get("code", "OPERATION_FAILED")
    return 2 if code in ("UNKNOWN_TOOL", "INVALID_ARGUMENT") else 1


def _run_one_shot(args: argparse.Namespace, config: EmbPilotConfig) -> int:
    """Execute one tool call in a fresh process-scoped session manager."""
    arguments = _parse_tool_arguments(args)
    if arguments is None:
        return 2
    if args.name not in known_tool_names():
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
        return _exit_code_for(result_structured(result) or {})

    return asyncio.run(_run())


def _run_via_socket(args: argparse.Namespace) -> int:
    """Forward tool/tools/batch calls to a running daemon."""
    from embpilot.rpc import RpcClient, resolve_endpoint

    try:
        endpoint = _validate_socket_endpoint(resolve_endpoint(args.socket))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "tools":
        # The catalog is a static contract; render it locally.
        print(tools_text(), end="")
        return 0

    if args.command == "tool":
        # Mirror the local one-shot path exactly: schema flags merge over
        # --json (a --socket call must behave identically to a local one).
        arguments = _parse_tool_arguments(args)
        if arguments is None:
            return 2

        async def _one_shot() -> int:
            client = RpcClient(endpoint)
            try:
                await client.connect()
            except (OSError, ConnectionError) as exc:
                print(
                    f"error: cannot reach daemon at {endpoint}: {exc} "
                    "(start 'embpilot serve' first)",
                    file=sys.stderr,
                )
                return 1
            try:
                response = await client.call(args.name, arguments)
            finally:
                await client.close()

            if args.json_output:
                payload = {
                    key: response[key]
                    for key in ("ok", "data", "error")
                    if key in response
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                if response.get("text"):
                    print(response["text"])
                if response.get("ok") is not True and response.get("error"):
                    error = response["error"]
                    print(f"error ({error.get('code')}): {error.get('message')}")
                    if error.get("suggestion"):
                        print(f"suggestion: {error['suggestion']}")
            return _exit_code_for(response)

        return asyncio.run(_one_shot())

    # batch: forward each request over the daemon connection.
    from embpilot.cli_loop import batch_loop

    async def _batch() -> int:
        client = RpcClient(endpoint)
        try:
            await client.connect()
        except (OSError, ConnectionError) as exc:
            print(
                f"error: cannot reach daemon at {endpoint}: {exc} "
                "(start 'embpilot serve' first)",
                file=sys.stderr,
            )
            return 1
        try:
            async def dispatcher(
                _manager: object,
                name: str,
                arguments: dict,
            ) -> dict:
                response = await client.call(name, arguments)
                return {
                    key: response[key]
                    for key in ("ok", "data", "error")
                    if key in response
                }

            return await batch_loop(
                client,  # type: ignore[arg-type]
                fail_fast=args.fail_fast,
                dispatcher=dispatcher,
            )
        finally:
            await client.close()

    return asyncio.run(_batch())


def _run_command_sequence(args: argparse.Namespace, config: EmbPilotConfig) -> int:
    """Implement ``run``: [connect] -> commands -> disconnect as one batch.

    Without ``--connect`` the command sequence runs without a connection
    (each command fails with NO_ACTIVE_DEVICE, as in batch).
    """
    requests: list[dict] = []
    if args.connect is not None:
        try:
            connect_arguments = json.loads(args.connect)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON for --connect: {exc}", file=sys.stderr)
            return 2
        if not isinstance(connect_arguments, dict):
            print("error: --connect must be a JSON object", file=sys.stderr)
            return 2
        requests.append(
            {"tool": f"connect_{args.interface}", "args": connect_arguments}
        )
    for command in args.commands:
        requests.append(
            {
                "tool": "send_command",
                "args": {
                    "command": command,
                    "timeout_ms": args.timeout_ms,
                    "line_ending": args.line_ending,
                },
            }
        )
    requests.append({"tool": "disconnect_device", "args": {}})

    lines = iter(json.dumps(request, ensure_ascii=False) for request in requests)

    async def read_line() -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    from embpilot.cli_loop import batch_loop
    from embpilot.server import SessionManager

    async def _run() -> int:
        config.ensure_data_dirs()
        manager = SessionManager(config)
        await manager.start()
        try:
            # fail-fast: a failed connect must not cascade NO_ACTIVE_DEVICE
            # noise through every remaining command.
            return await batch_loop(manager, read_line=read_line, fail_fast=True)
        finally:
            await manager.shutdown()

    return asyncio.run(_run())


def _tool_subparser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Return the ``tool`` subparser for dynamic schema-flag registration."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparser = action.choices.get("tool")
            if subparser is not None:
                return subparser
    raise RuntimeError("tool subparser not found")


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point: server by default, subcommands for tool access."""
    _force_utf8_stdio()
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # `embpilot tool <name> --help` shows the tool's own help (schema,
    # examples, guidance) instead of the argparse usage. Intercept before
    # parsing, because argparse's -h action would exit first.
    if "--help" in argv or "-h" in argv:
        try:
            tool_index = argv.index("tool")
        except ValueError:
            tool_index = -1
        if tool_index != -1 and tool_index + 1 < len(argv):
            candidate = argv[tool_index + 1]
            if not candidate.startswith("-"):
                text = tool_help_text(candidate)
                if text is not None:
                    print(text, end="")
                    return
    # The tool subcommand gets schema-driven flags; probe the tool name first.
    probe = parser.parse_known_args(argv)[0]
    if probe.command == "tool" and probe.name:
        add_schema_flags(_tool_subparser(parser), probe.name)
    args = parser.parse_args(argv)
    config = EmbPilotConfig.from_args(args)

    if args.command is None:
        from embpilot.server import serve

        serve(config)
        return

    if args.command in ("tools", "tool", "batch") and args.socket:
        raise SystemExit(_run_via_socket(args))

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

    if args.command == "batch":
        raise SystemExit(asyncio.run(run_batch(config, fail_fast=args.fail_fast)))

    if args.command == "serve":
        try:
            endpoint = _validate_socket_endpoint(args.socket) if args.socket else None
            asyncio.run(run_serve(config, endpoint=endpoint))
        except KeyboardInterrupt:
            print("bye")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
        return

    if args.command == "help":
        text = tool_help_text(args.name)
        if text is None:
            print(
                f"error: unknown tool '{args.name}' (see 'embpilot tools')",
                file=sys.stderr,
            )
            raise SystemExit(2)
        print(text, end="")
        return

    if args.command == "run":
        raise SystemExit(_run_command_sequence(args, config))

    if args.command in ("install", "uninstall"):
        from embpilot.installer.install import (
            run_check,
            run_install,
            run_interactive_install,
            run_interactive_uninstall,
            run_print_config,
            run_uninstall,
        )

        try:
            if args.print_config:
                print(run_print_config(args.print_config))
                return
            if args.check:
                lines, code = run_check(
                    target=args.target or "auto",
                    location=args.location or "local",
                )
                print("\n".join(lines))
                raise SystemExit(code)
            if args.target is None and not args.yes:
                # Interactive picker (CodeGraph-style) when nothing is pinned.
                if args.command == "install":
                    print("\n".join(run_interactive_install()))
                else:
                    print("\n".join(run_interactive_uninstall()))
                return
            if args.command == "install":
                try:
                    print(
                        "\n".join(
                            run_install(
                                target=args.target or "auto",
                                location=args.location or "local",
                            )
                        )
                    )
                except KeyboardInterrupt:
                    print("cancelled — nothing written")
                    raise SystemExit(130)
                return
            if args.command == "uninstall":
                try:
                    print(
                        "\n".join(
                            run_uninstall(
                                target=args.target or "auto",
                                location=args.location or "local",
                            )
                        )
                    )
                except KeyboardInterrupt:
                    print("cancelled — nothing written")
                    raise SystemExit(130)
                return
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)

    parser.error(f"unknown command: {args.command}")