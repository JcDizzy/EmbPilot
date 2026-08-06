"""MCP discovery and JSON contract tests."""

from __future__ import annotations

import pytest

from embpilot.core.commands import CommandResult
from embpilot.mcp_contracts import build_tool_definitions, dispatch_tool


def test_connection_tools_have_distinct_strict_json_contracts() -> None:
    tools = {tool.name: tool for tool in build_tool_definitions()}

    assert "connect_device" not in tools
    assert {"connect_serial", "connect_ssh", "connect_telnet"} <= tools.keys()

    serial_schema = tools["connect_serial"].inputSchema
    assert serial_schema["required"] == ["port"]
    assert serial_schema["additionalProperties"] is False
    assert serial_schema["properties"]["line_ending"]["default"] == "lf"
    assert serial_schema["examples"] == [
        {"port": "COM3", "baudrate": 115200, "line_ending": "crlf"},
        {"port": "/dev/ttyUSB0", "baudrate": 115200, "line_ending": "lf"},
    ]

    ssh_schema = tools["connect_ssh"].inputSchema
    assert ssh_schema["required"] == ["host", "username"]
    assert ssh_schema["additionalProperties"] is False
    assert ssh_schema["properties"]["insecure_skip_host_key_check"]["default"] is False

    telnet_schema = tools["connect_telnet"].inputSchema
    assert telnet_schema["required"] == ["host"]
    assert telnet_schema["additionalProperties"] is False


class RecordingManager:
    def __init__(self) -> None:
        self.connection: tuple[str, dict] | None = None

    async def connect_device(self, interface: str, config: dict) -> str:
        self.connection = (interface, config)
        return "session-123"


@pytest.mark.asyncio
async def test_connect_dispatch_returns_structured_content() -> None:
    manager = RecordingManager()

    result = await dispatch_tool(
        manager,
        "connect_serial",
        {"port": "COM3", "baudrate": 115200, "line_ending": "crlf"},
    )

    assert manager.connection == (
        "serial",
        {"port": "COM3", "baudrate": 115200, "line_ending": "crlf"},
    )
    assert result.isError is False
    assert result.structuredContent == {
        "ok": True,
        "data": {"session_id": "session-123", "interface": "serial"},
    }
    assert result.content[0].text == "Connected over serial. Session ID: session-123"


class FailingManager(RecordingManager):
    async def connect_device(self, interface: str, config: dict) -> str:
        raise ConnectionError("COM9 is unavailable")


@pytest.mark.asyncio
async def test_dispatch_returns_stable_structured_error() -> None:
    result = await dispatch_tool(
        FailingManager(),
        "connect_serial",
        {"port": "COM9"},
    )

    assert result.isError is True
    assert result.structuredContent == {
        "ok": False,
        "error": {
            "code": "CONNECTION_FAILED",
            "message": "COM9 is unavailable",
            "retryable": True,
            "suggestion": "Check the device address, credentials, and availability, then retry.",
        },
    }


class CommandManager(RecordingManager):
    def __init__(self) -> None:
        super().__init__()
        self.command_arguments: dict | None = None

    async def send_command(self, **arguments) -> CommandResult:
        self.command_arguments = arguments
        return CommandResult(
            output="device ready",
            matched=True,
            timed_out=False,
            truncated=False,
        )


@pytest.mark.asyncio
async def test_send_command_dispatch_returns_command_state() -> None:
    manager = CommandManager()

    result = await dispatch_tool(
        manager,
        "send_command",
        {
            "command": "status",
            "line_ending": "crlf",
            "expect_regex": "ready",
            "timeout_ms": 2000,
            "max_output_chars": 5000,
        },
    )

    assert manager.command_arguments == {
        "command": "status",
        "line_ending": "crlf",
        "expect_regex": "ready",
        "timeout_ms": 2000,
        "max_output_chars": 5000,
    }
    assert result.structuredContent == {
        "ok": True,
        "data": {
            "output": "device ready",
            "matched": True,
            "timed_out": False,
            "truncated": False,
        },
    }


class CompleteManager(CommandManager):
    async def disconnect_device(self) -> None:
        return None

    async def reset_target(self, method: str = "reboot") -> str:
        return f"reset:{method}"

    async def search_history_logs(self, **arguments) -> list[dict]:
        return [{"timestamp": "2026-08-06T00:00:00Z", "text": arguments["keyword"]}]

    async def list_sessions(self) -> list[dict]:
        return [{"session_id": "session-123", "status": "closed"}]

    async def delete_session(self, session_id: str) -> None:
        return None

    async def export_session(self, session_id: str, target_path: str) -> str:
        return f"{target_path}/{session_id}.db"


@pytest.mark.asyncio
async def test_existing_session_tools_remain_dispatchable() -> None:
    manager = CompleteManager()
    calls = [
        ("disconnect_device", {}),
        ("reset_target", {"method": "reboot"}),
        ("search_history_logs", {"keyword": "panic"}),
        ("list_sessions", {}),
        ("delete_session", {"session_id": "session-123"}),
        (
            "export_session",
            {"session_id": "session-123", "target_path": "exports"},
        ),
    ]

    for name, arguments in calls:
        result = await dispatch_tool(manager, name, arguments)
        assert result.isError is False, name
        assert result.structuredContent["ok"] is True, name


class DisconnectedManager(CommandManager):
    async def send_command(self, **arguments) -> CommandResult:
        raise RuntimeError("No active device connection")


@pytest.mark.asyncio
async def test_runtime_errors_remain_machine_readable() -> None:
    result = await dispatch_tool(
        DisconnectedManager(),
        "send_command",
        {"command": "status"},
    )

    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "NO_ACTIVE_DEVICE"
    assert result.structuredContent["error"]["retryable"] is False
    assert "connect_" in result.structuredContent["error"]["suggestion"]
