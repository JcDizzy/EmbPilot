"""Compatibility wrapper for the MCP app runner."""

from __future__ import annotations

from typing import Optional

from embpilot import mcp_app
from embpilot.config import EmbPilotConfig
from embpilot.runtime.session import SessionManager


def get_session_manager() -> Optional[SessionManager]:
    return mcp_app.get_active_session_manager()


def serve(config: EmbPilotConfig) -> None:
    """Backward-compatible entrypoint that delegates to the new MCP app."""
    mcp_app.run_stdio_mcp_server(config)
