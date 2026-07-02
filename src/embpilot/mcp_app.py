from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from pydantic import AnyUrl

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool

from embpilot import __version__
from embpilot.config import EmbPilotConfig
from embpilot.runtime.resources import (
    build_session_info_resource,
    render_live_log_snapshot,
)
from embpilot.runtime.session import SessionManager

logger = logging.getLogger(__name__)

_active_manager: Optional[SessionManager] = None


def get_active_session_manager() -> Optional[SessionManager]:
    return _active_manager


def build_resource_catalog() -> list[Resource]:
    return [
        Resource(
            uri=AnyUrl("device://live_log"),
            name="Live Device Log",
            description="Recent 2000 lines of device output from the active session (subscribable)",
            mimeType="text/plain",
        ),
        Resource(
            uri=AnyUrl("device://session_info"),
            name="Session Info",
            description="Current session metadata for the active device connection",
            mimeType="application/json",
        ),
    ]


def build_tool_catalog() -> list[Tool]:
    return [
        Tool(
            name="connect_device",
            description=(
                "Connect to an embedded device over Serial, Telnet, or SSH. "
                "Replaces any active connection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "interface_type": {
                        "type": "string",
                        "enum": ["serial", "telnet", "ssh"],
                    },
                    "config": {
                        "type": "object",
                        "description": "Interface-specific connection parameters.",
                    },
                },
                "required": ["interface_type", "config"],
            },
        ),
        Tool(
            name="send_command",
            description=(
                "Send a command line to the active device and return captured output "
                "until the expect window closes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "expect_regex": {
                        "type": "string",
                        "description": "Optional regex marking the end of the response.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "default": 5000,
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="reset_target",
            description=(
                "Reset the active device. Only the 'reboot' method is currently supported."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["reboot"],
                        "default": "reboot",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="disconnect_device",
            description="Disconnect the active device and close the session.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


async def dispatch_tool(
    manager: SessionManager, name: str, arguments: dict
) -> list[TextContent]:
    try:
        if name == "connect_device":
            session_id = await manager.connect_device(
                interface_type=arguments["interface_type"],
                config=arguments.get("config") or {},
            )
            return [TextContent(type="text", text=f"Connected. session_id={session_id}")]
        if name == "send_command":
            output = await manager.send_command(
                command=arguments["command"],
                expect_regex=arguments.get("expect_regex"),
                timeout_ms=arguments.get("timeout_ms", 5000),
            )
            return [TextContent(type="text", text=output)]
        if name == "reset_target":
            message = await manager.reset_target(
                method=arguments.get("method", "reboot")
            )
            return [TextContent(type="text", text=message)]
        if name == "disconnect_device":
            await manager.disconnect_device()
            return [TextContent(type="text", text="Disconnected.")]
        return [
            TextContent(type="text", text=f"Error: unknown tool {name!r}")
        ]
    except Exception as exc:  # noqa: BLE001 — surface tool failures to the client
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {exc}")]


def create_mcp_app(config: EmbPilotConfig) -> tuple[Server, SessionManager]:
    manager = SessionManager(config)
    app = Server("embpilot", version=__version__)

    @app.list_resources()
    async def list_resources() -> list[Resource]:
        return build_resource_catalog()

    @app.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        uri_text = str(uri)
        if uri_text == "device://live_log":
            ring = manager.active_ring()
            if ring is None:
                return "No active device connection."
            return render_live_log_snapshot(ring)
        if uri_text == "device://session_info":
            try:
                info = manager.get_session_info()
            except RuntimeError:
                return json.dumps(
                    {"error": "No active device connection."},
                    ensure_ascii=False,
                    indent=2,
                )
            return json.dumps(
                build_session_info_resource(info),
                ensure_ascii=False,
                indent=2,
            )
        return f"Unknown resource: {uri_text}"

    @app.subscribe_resource()
    async def subscribe_resource(uri: AnyUrl) -> None:
        logger.info("Client subscribed to %s", uri)

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return build_tool_catalog()

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await dispatch_tool(manager, name, arguments)

    return app, manager


def run_stdio_mcp_server(config: EmbPilotConfig) -> None:
    global _active_manager

    config.ensure_data_dirs()
    logger.info("EmbPilot %s starting | data_dir=%s", __version__, config.data_dir)

    app, manager = create_mcp_app(config)
    _active_manager = manager

    async def _run() -> None:
        await manager.start()
        try:
            async with stdio_server() as (read_stream, write_stream):
                await app.run(
                    read_stream,
                    write_stream,
                    app.create_initialization_options(),
                )
        finally:
            await manager.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("EmbPilot stopped by user")
    finally:
        _active_manager = None
