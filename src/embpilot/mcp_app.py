from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from pydantic import AnyUrl

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource

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
