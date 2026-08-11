"""Interactive REPL that reuses the MCP dispatch layer."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable

from embpilot.cli_format import format_result
from embpilot.config import EmbPilotConfig
from embpilot.mcp_contracts import SessionOperations, build_tool_definitions, dispatch_tool

_PROMPT = "embpilot> "


def _read_piped_line() -> str:
    """Read one line from a pipe, decoding UTF-8 (with BOM) explicitly.

    PowerShell and other Windows tools often emit a UTF-8 BOM on native
    stdin; relying on the locale ``input()`` decoder (e.g. GBK) would turn
    the BOM bytes into garbage characters.
    """
    raw = sys.stdin.buffer.readline()
    if not raw:
        raise EOFError
    try:
        line = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        line = raw.decode(sys.stdin.encoding or "utf-8", errors="replace")
    return line.rstrip("\r\n")


def _default_read_line() -> Callable[[], Awaitable[str]]:
    """Choose a line reader: interactive console keeps line editing."""
    if sys.stdin.isatty():
        return lambda: asyncio.to_thread(input, _PROMPT)

    async def read_piped() -> str:
        return await asyncio.to_thread(_read_piped_line)

    return read_piped


async def shell_loop(
    manager: SessionOperations,
    *,
    json_output: bool = False,
    read_line: Callable[[], Awaitable[str]] | None = None,
) -> None:
    """Run the REPL against one persistent session manager."""
    if read_line is None:
        read_line = _default_read_line()

    tool_names = {t.name for t in build_tool_definitions()}
    print(
        "EmbPilot shell - tools: "
        + ", ".join(sorted(tool_names))
        + "\nUsage: <tool> <json-args> | help | exit"
    )

    while True:
        try:
            raw = await read_line()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = raw.lstrip("\ufeff").strip()
        if not line:
            continue
        if line in ("exit", "quit"):
            return
        if line == "help":
            print("Tools: " + ", ".join(sorted(tool_names)))
            print('Example: connect_serial {"port": "COM3", "baudrate": 115200}')
            continue

        name, _, rest = line.partition(" ")
        if name not in tool_names:
            print(f"error: unknown tool '{name}' (see help)")
            continue
        try:
            arguments = json.loads(rest) if rest.strip() else {}
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON arguments: {exc}")
            continue
        if not isinstance(arguments, dict):
            print("error: arguments must be a JSON object")
            continue

        result = await dispatch_tool(manager, name, arguments)
        print(format_result(result, json_output=json_output))


async def run_shell(config: EmbPilotConfig, *, json_output: bool = False) -> None:
    """Start a persistent SessionManager and run the REPL."""
    config.ensure_data_dirs()
    from embpilot.server import SessionManager

    manager = SessionManager(config)
    await manager.start()
    try:
        await shell_loop(manager, json_output=json_output)
    finally:
        await manager.shutdown()