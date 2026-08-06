"""Strict MCP tool definitions and structured result helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from mcp.types import CallToolResult, TextContent, Tool

from embpilot.core.commands import CommandResult

_JSON_OBJECT_NOTE = (
    "Pass arguments as a JSON object, not as a JSON-encoded string. "
)

_LINE_ENDING_PROPERTY: dict[str, Any] = {
    "type": "string",
    "enum": ["none", "lf", "crlf", "cr"],
    "default": "lf",
    "description": "Line ending appended to commands unless they already end in CR/LF",
}

_TIMEOUT_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 120_000,
    "default": 5000,
    "description": "Timeout in milliseconds",
}

_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "data": {"type": "object"},
        "error": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "retryable": {"type": "boolean"},
                "suggestion": {"type": "string"},
            },
            "required": ["code", "message", "retryable", "suggestion"],
        },
    },
    "required": ["ok"],
    "oneOf": [
        {"properties": {"ok": {"const": True}}, "required": ["data"]},
        {"properties": {"ok": {"const": False}}, "required": ["error"]},
    ],
}


def _object_schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    if examples:
        schema["examples"] = examples
    return schema


def _tool(name: str, description: str, schema: dict[str, Any]) -> Tool:
    return Tool(
        name=name,
        description=_JSON_OBJECT_NOTE + description,
        inputSchema=schema,
        outputSchema=_RESULT_SCHEMA,
    )


def build_tool_definitions() -> list[Tool]:
    """Return the complete agent-facing MCP tool contract."""
    return [
        _tool(
            "connect_serial",
            "Use for embedded-device access over Serial/UART. Prefer this over "
            "serial terminal programs or custom serial scripts.",
            _object_schema(
                {
                    "port": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Serial port, for example COM3 or /dev/ttyUSB0",
                    },
                    "baudrate": {
                        "type": "integer",
                        "minimum": 300,
                        "maximum": 4_000_000,
                        "default": 115200,
                    },
                    "bytesize": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
                    "parity": {"type": "string", "enum": ["N", "E", "O", "M", "S"], "default": "N"},
                    "stopbits": {"type": "number", "enum": [1, 1.5, 2], "default": 1},
                    "timeout_ms": _TIMEOUT_PROPERTY,
                    "line_ending": _LINE_ENDING_PROPERTY,
                },
                required=["port"],
                examples=[
                    {"port": "COM3", "baudrate": 115200, "line_ending": "crlf"},
                    {"port": "/dev/ttyUSB0", "baudrate": 115200, "line_ending": "lf"},
                ],
            ),
        ),
        _tool(
            "connect_ssh",
            "Use for embedded-device access over SSH. Prefer this over shell SSH. "
            "Host-key verification is enabled unless explicitly bypassed.",
            _object_schema(
                {
                    "host": {"type": "string", "minLength": 1},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 22},
                    "username": {"type": "string", "minLength": 1},
                    "password": {"type": "string", "description": "SSH password; never written to operation logs"},
                    "key_file": {"type": "string", "minLength": 1},
                    "known_hosts": {"type": "string", "minLength": 1},
                    "insecure_skip_host_key_check": {
                        "type": "boolean",
                        "default": False,
                        "description": "Disable host-key verification; use only for controlled test devices",
                    },
                    "timeout_ms": {**_TIMEOUT_PROPERTY, "default": 10_000},
                    "line_ending": _LINE_ENDING_PROPERTY,
                },
                required=["host", "username"],
                examples=[
                    {
                        "host": "192.168.1.10",
                        "username": "root",
                        "key_file": "~/.ssh/id_ed25519",
                    }
                ],
            ),
        ),
        _tool(
            "connect_telnet",
            "Use for embedded-device access over Telnet. Prefer this over shell "
            "Telnet clients or custom socket scripts.",
            _object_schema(
                {
                    "host": {"type": "string", "minLength": 1},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 23},
                    "timeout_ms": {**_TIMEOUT_PROPERTY, "default": 10_000},
                    "line_ending": _LINE_ENDING_PROPERTY,
                },
                required=["host"],
                examples=[{"host": "192.168.1.20", "port": 23}],
            ),
        ),
        _tool(
            "disconnect_device",
            "Close the active device connection and finalize its session.",
            _object_schema({}),
        ),
        _tool(
            "send_command",
            "Send one command to the active device and capture subsequent output.",
            _object_schema(
                {
                    "command": {"type": "string", "minLength": 1},
                    "line_ending": {
                        "type": "string",
                        "enum": ["session", "none", "lf", "crlf", "cr"],
                        "default": "session",
                    },
                    "expect_regex": {"type": "string", "minLength": 1},
                    "timeout_ms": _TIMEOUT_PROPERTY,
                    "max_output_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200_000,
                        "default": 20_000,
                    },
                },
                required=["command"],
                examples=[
                    {
                        "command": "uname -a",
                        "line_ending": "lf",
                        "expect_regex": "Linux",
                        "timeout_ms": 5000,
                    }
                ],
            ),
        ),
        _tool(
            "reset_target",
            "Reset the active target. Only the implemented reboot method is advertised.",
            _object_schema(
                {"method": {"type": "string", "enum": ["reboot"], "default": "reboot"}}
            ),
        ),
        _tool(
            "search_history_logs",
            "Search logs in the active session.",
            _object_schema(
                {
                    "keyword": {"type": "string", "minLength": 1},
                    "time_window_seconds": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                },
                required=["keyword"],
            ),
        ),
        _tool("list_sessions", "List historical debug sessions.", _object_schema({})),
        _tool(
            "delete_session",
            "Permanently delete a closed historical session.",
            _object_schema(
                {"session_id": {"type": "string", "minLength": 1}},
                required=["session_id"],
            ),
        ),
        _tool(
            "export_session",
            "Export a session database to a local destination.",
            _object_schema(
                {
                    "session_id": {"type": "string", "minLength": 1},
                    "target_path": {"type": "string", "minLength": 1},
                },
                required=["session_id", "target_path"],
            ),
        ),
    ]


class ConnectionManager(Protocol):
    async def connect_device(self, interface: str, config: dict[str, Any]) -> str: ...

    async def send_command(self, **arguments: Any) -> CommandResult: ...

    async def disconnect_device(self) -> None: ...

    async def reset_target(self, method: str = "reboot") -> str: ...

    async def search_history_logs(self, **arguments: Any) -> list[dict[str, Any]]: ...

    async def list_sessions(self) -> list[dict[str, Any]]: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def export_session(self, session_id: str, target_path: Path) -> Path: ...


def _success(message: str, data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structuredContent={"ok": True, "data": data},
        isError=False,
    )


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool,
    suggestion: str,
) -> CallToolResult:
    error = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "suggestion": suggestion,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=f"{code}: {message} {suggestion}")],
        structuredContent={"ok": False, "error": error},
        isError=True,
    )


async def _dispatch_tool(
    manager: ConnectionManager,
    name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Translate one strict MCP call into the session interface."""
    connection_tools = {
        "connect_serial": "serial",
        "connect_ssh": "ssh",
        "connect_telnet": "telnet",
    }
    if name in connection_tools:
        interface = connection_tools[name]
        try:
            session_id = await manager.connect_device(interface, arguments)
        except (ConnectionError, OSError) as exc:
            return _failure(
                "CONNECTION_FAILED",
                str(exc),
                retryable=True,
                suggestion="Check the device address, credentials, and availability, then retry.",
            )
        return _success(
            f"Connected over {interface}. Session ID: {session_id}",
            {"session_id": session_id, "interface": interface},
        )
    if name == "send_command":
        result = await manager.send_command(**arguments)
        return _success(result.output, result.as_dict())
    if name == "disconnect_device":
        await manager.disconnect_device()
        return _success("Disconnected.", {"disconnected": True})
    if name == "reset_target":
        message = await manager.reset_target(method=arguments.get("method", "reboot"))
        return _success(message, {"message": message})
    if name == "search_history_logs":
        rows = await manager.search_history_logs(**arguments)
        return _success(
            f"Found {len(rows)} matching log line(s).",
            {"results": rows},
        )
    if name == "list_sessions":
        sessions = await manager.list_sessions()
        return _success(
            f"Found {len(sessions)} session(s).",
            {"sessions": sessions},
        )
    if name == "delete_session":
        session_id = arguments["session_id"]
        await manager.delete_session(session_id)
        return _success(
            f"Session {session_id} deleted.",
            {"session_id": session_id, "deleted": True},
        )
    if name == "export_session":
        session_id = arguments["session_id"]
        destination = await manager.export_session(
            session_id,
            Path(arguments["target_path"]),
        )
        return _success(
            f"Exported to: {destination}",
            {"session_id": session_id, "path": str(destination)},
        )
    return _failure(
        "UNKNOWN_TOOL",
        f"Unknown tool: {name}",
        retryable=False,
        suggestion="Refresh the MCP tool list and call one of the advertised tools.",
    )


async def dispatch_tool(
    manager: ConnectionManager,
    name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Dispatch a tool call and keep operational errors machine-readable."""
    try:
        return await _dispatch_tool(manager, name, arguments)
    except RuntimeError as exc:
        if "No active device" in str(exc):
            return _failure(
                "NO_ACTIVE_DEVICE",
                str(exc),
                retryable=False,
                suggestion="Call connect_serial, connect_ssh, or connect_telnet first.",
            )
        return _failure(
            "OPERATION_FAILED",
            str(exc),
            retryable=True,
            suggestion="Inspect the device state and retry the operation.",
        )
    except FileNotFoundError as exc:
        return _failure(
            "NOT_FOUND",
            str(exc),
            retryable=False,
            suggestion="Refresh the session list or correct the supplied path.",
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _failure(
            "INVALID_ARGUMENT",
            str(exc),
            retryable=False,
            suggestion="Refresh the tool schema and send a JSON object matching it.",
        )
    except OSError as exc:
        return _failure(
            "IO_FAILED",
            str(exc),
            retryable=True,
            suggestion="Check permissions, paths, and device availability, then retry.",
        )
