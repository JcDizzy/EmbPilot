"""
Shared line-driven dispatch machinery for shell, batch, and (future) serve.

The REPL (``shell``), the scripted ``batch`` mode, and the planned ``serve``
daemon all follow the same pattern: read one line, parse it into a tool call,
dispatch through the shared contract layer, and render one result.  This module
owns that pattern so the three entry points stay thin and behave identically.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable

from embpilot.cli_format import format_result
from embpilot.mcp_compat import result_structured
from embpilot.mcp_contracts import SessionOperations, build_tool_definitions, dispatch_tool

_PROMPT = "embpilot> "


def known_tool_names() -> frozenset[str]:
    """Names of every advertised tool, shared by all CLI entry points."""
    return frozenset(tool.name for tool in build_tool_definitions())


def read_piped_line() -> str:
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


def make_line_reader() -> Callable[[], Awaitable[str]]:
    """Choose a line reader: interactive console keeps line editing."""
    if sys.stdin.isatty():
        return lambda: asyncio.to_thread(input, _PROMPT)

    async def read_piped() -> str:
        return await asyncio.to_thread(read_piped_line)

    return read_piped


async def dispatch_and_render(
    manager: SessionOperations,
    name: str,
    arguments: dict,
    *,
    json_output: bool,
) -> str:
    """Dispatch one tool call and render it for terminal display."""
    result = await dispatch_tool(manager, name, arguments)
    return format_result(result, json_output=json_output)


async def dispatch_payload(
    manager: SessionOperations,
    name: str,
    arguments: dict,
) -> dict:
    """Dispatch one tool call and return the structured ``ok/data/error`` dict."""
    result = await dispatch_tool(manager, name, arguments)
    return result_structured(result) or {}


async def batch_loop(
    manager: SessionOperations,
    *,
    read_line: Callable[[], Awaitable[str]] | None = None,
    fail_fast: bool = False,
) -> int:
    """Run scripted JSONL requests, printing one result envelope per line.

    Each stdin line is a request object: ``{"tool": "<name>", "args": {...}}``.
    Empty lines and ``#`` comments are ignored; ``exit``/``quit`` stop early.
    No banner or prompt is printed, so the stdout is pure JSONL.

    Returns the process exit code: 0 when every request succeeded, 1 when at
    least one tool call failed (or ``--fail-fast`` stopped early), and 2 on
    malformed input or an unknown tool name.
    """
    if read_line is None:
        read_line = make_line_reader()
    tool_names = known_tool_names()
    exit_code = 0

    while True:
        try:
            raw = await read_line()
        except (EOFError, KeyboardInterrupt):
            break
        line = raw.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        if line in ("exit", "quit"):
            break

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON on stdin: {exc}", file=sys.stderr)
            return 2
        if not isinstance(request, dict) or "tool" not in request:
            print(
                'error: each stdin line must be {"tool": "<name>", "args": {...}}',
                file=sys.stderr,
            )
            return 2
        name = request["tool"]
        arguments = request.get("args")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            print("error: args must be a JSON object", file=sys.stderr)
            return 2
        if name not in tool_names:
            print(f"error: unknown tool '{name}'", file=sys.stderr)
            return 2

        payload = await dispatch_payload(manager, name, arguments)
        print(json.dumps(payload, ensure_ascii=False))
        if payload.get("ok") is not True:
            exit_code = 1
            if fail_fast:
                break

    return exit_code
