"""Interactive REPL and scripted batch mode over the MCP dispatch layer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from embpilot.cli_loop import (
    batch_loop,
    dispatch_and_render,
    known_tool_names,
    make_line_reader,
)
from embpilot.config import EmbPilotConfig
from embpilot.core.engine import RingBuffer
from embpilot.mcp_contracts import SessionOperations

_LOG_PREFIX = "[log] "
_CMD_PREFIX = "[cmd] "
_MONITOR_POLL_S = 0.1


def _prefix_lines(text: str, prefix: str) -> str:
    """Prefix every line of *text*; used while the monitor is running."""
    if not text:
        return text
    return "\n".join(prefix + line for line in text.splitlines())


async def _monitor_logs(ring: RingBuffer, *, poll_s: float = _MONITOR_POLL_S) -> None:
    """Print new ring-buffer lines continuously until cancelled."""
    cursor = ring.mark()
    while True:
        lines = ring.snapshot_since(cursor)
        for line in lines:
            print(_LOG_PREFIX + line.formatted())
        if lines:
            # Advance past everything already printed so lines are not repeated.
            cursor = ring.mark()
        await asyncio.sleep(poll_s)


async def shell_loop(
    manager: SessionOperations,
    *,
    json_output: bool = False,
    read_line: Callable[[], Awaitable[str]] | None = None,
) -> None:
    """Run the REPL against one persistent session manager."""
    if read_line is None:
        read_line = make_line_reader()

    tool_names = known_tool_names()
    print(
        "EmbPilot shell - tools: "
        + ", ".join(sorted(tool_names))
        + "\nUsage: <tool> <json-args> | help | monitor | exit"
    )

    monitor_task: asyncio.Task[None] | None = None
    try:
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
                print(
                    "Commands: help, help <tool>, exit, monitor (live output), "
                    "stop (exit monitor)"
                )
                continue
            if line.startswith("help "):
                from embpilot.cli_loop import tool_help_text

                text = tool_help_text(line[len("help ") :].strip())
                if text is None:
                    print(f"error: unknown tool '{line[len('help '):].strip()}'")
                else:
                    print(text, end="")
                continue
            if line == "monitor":
                ring = getattr(manager, "active_ring", None)
                if ring is None:
                    print(
                        "error: monitor needs an active device connection "
                        "(connect first)"
                    )
                    continue
                if monitor_task is not None and not monitor_task.done():
                    print("monitor is already running")
                    continue
                monitor_task = asyncio.create_task(_monitor_logs(ring))
                print(
                    "monitor on - live output below; commands still work; "
                    "type 'stop' to exit"
                )
                continue
            if line == "stop":
                if monitor_task is not None and not monitor_task.done():
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass
                    monitor_task = None
                    print("monitor off")
                else:
                    print("monitor is not running")
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

            text = await dispatch_and_render(
                manager, name, arguments, json_output=json_output
            )
            if monitor_task is not None and not monitor_task.done():
                text = _prefix_lines(text, _CMD_PREFIX)
            print(text)
    finally:
        if monitor_task is not None:
            monitor_task.cancel()


async def run_batch(config: EmbPilotConfig, *, fail_fast: bool = False) -> int:
    """Run a scripted JSONL batch against one persistent SessionManager.

    Returns the process exit code (0 / 1 / 2, see ``batch_loop``).
    """
    config.ensure_data_dirs()
    from embpilot.server import SessionManager

    manager = SessionManager(config)
    await manager.start()
    try:
        return await batch_loop(manager, fail_fast=fail_fast)
    finally:
        await manager.shutdown()


async def run_serve(config: EmbPilotConfig, *, endpoint: str | None = None) -> None:
    """Run a persistent daemon sharing one SessionManager across clients.

    The daemon writes its real endpoint to ``<data-dir>/daemon.json`` so
    clients can discover it with ``--socket <that-file>``.
    """
    config.ensure_data_dirs()
    from embpilot.rpc import (
        RpcServer,
        default_endpoint,
        write_endpoint_file,
    )
    from embpilot.server import SessionManager

    manager = SessionManager(config)
    server = RpcServer(
        manager,
        endpoint=endpoint or default_endpoint(config.data_dir),
    )
    await manager.start()
    await server.start()
    endpoint_file = write_endpoint_file(config.data_dir, server.listening_endpoint)
    print(f"embpilot serve listening on {server.listening_endpoint}")
    print(f"endpoint file: {endpoint_file}")
    try:
        await server.serve_forever()
    finally:
        await server.close()
        await manager.shutdown()


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
