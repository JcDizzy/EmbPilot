"""
Shared line-driven dispatch machinery for shell, batch, and serve.

The REPL (``shell``), the scripted ``batch`` mode, and the ``serve`` daemon
all follow the same pattern: read one line, parse it into a tool call,
dispatch through the shared contract layer, and render one result.  This
module owns that pattern so the three entry points stay thin and behave
identically.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable

from embpilot.cli_format import format_result
from embpilot.mcp_compat import result_structured, tool_input_schema
from embpilot.mcp_contracts import SessionOperations, build_tool_definitions, dispatch_tool

_JSON_NOTE = "Pass arguments as a JSON object, not as a JSON-encoded string. "


_PROMPT = "embpilot> "


class RequestParseError(ValueError):
    """Raised when a JSONL request line is malformed (shared by batch and
    the serve daemon's request parser)."""


def parse_request_line(line: str) -> tuple[dict, str, dict]:
    """Parse one JSONL tool request: ``{"tool": ..., "args": {...}}``.

    Returns ``(request, name, arguments)``; raises ``RequestParseError`` on
    malformed input (bad JSON, missing tool, non-object args).
    """
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RequestParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(request, dict) or "tool" not in request:
        raise RequestParseError(
            'request must be {"tool": "<name>", "args": {...}}'
        )
    arguments = request.get("args")
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        raise RequestParseError("args must be a JSON object")
    return request, request["tool"], arguments


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
    dispatcher: Callable[[SessionOperations, str, dict], Awaitable[dict]] | None = None,
) -> int:
    """Run scripted JSONL requests, printing one result envelope per line.

    Each stdin line is a request object: ``{"tool": "<name>", "args": {...}}``.
    Empty lines and ``#`` comments are ignored; ``exit``/``quit`` stop early.
    No banner or prompt is printed, so the stdout is pure JSONL.

    *dispatcher* replaces the default local ``dispatch_tool`` path, e.g. to
    forward requests to a daemon; it must return an ``ok/data/error`` dict.

    Returns the process exit code: 0 when every request succeeded, 1 when at
    least one tool call failed (or ``--fail-fast`` stopped early), and 2 on
    malformed input or an unknown tool name.
    """
    if read_line is None:
        read_line = make_line_reader()
    tool_names = known_tool_names()
    dispatch = dispatcher or dispatch_payload
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
            _request, name, arguments = parse_request_line(line)
        except RequestParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if name not in tool_names:
            print(f"error: unknown tool '{name}'", file=sys.stderr)
            return 2

        payload = await dispatch(manager, name, arguments)
        print(json.dumps(payload, ensure_ascii=False))
        if payload.get("ok") is not True:
            exit_code = 1
            if fail_fast:
                break

    return exit_code


def tool_help_text(name: str) -> str | None:
    """Render detailed help for one tool, or None if it is unknown."""
    tool = next(
        (item for item in build_tool_definitions() if item.name == name),
        None,
    )
    if tool is None:
        return None

    lines = [name, "=" * len(name), tool.description.replace(_JSON_NOTE, ""), ""]
    schema = tool_input_schema(tool)
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    lines.append("Arguments (JSON object):")
    for prop_name in sorted(properties):
        prop = properties[prop_name]
        marks = ["required"] if prop_name in required else []
        if "default" in prop:
            marks.append(f"default: {prop['default']}")
        if prop.get("enum"):
            marks.append(f"enum: {prop['enum']}")
        suffix = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"  {prop_name}{suffix}: {prop.get('description', '')}")
    examples = schema.get("examples") or []
    if examples:
        lines.append("")
        lines.append("Examples:")
        for example in examples:
            lines.append(f"  {json.dumps(example, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"

