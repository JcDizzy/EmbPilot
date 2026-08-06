"""End-to-end stdio MCP contract smoke test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_stdio_server_exposes_agent_first_contract(tmp_path: Path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "embpilot", "--data-dir", str(tmp_path)],
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}

            assert "connect_device" not in names
            assert {"connect_serial", "connect_ssh", "connect_telnet"} <= names

            result = await session.call_tool(
                "send_command",
                arguments={"command": "status"},
            )

    assert result.isError is True
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["error"]["code"] == "NO_ACTIVE_DEVICE"
