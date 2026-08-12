"""Strict MCP tool definitions and structured result helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

import jsonschema
from mcp.types import CallToolResult, TextContent, Tool

from embpilot.mcp_compat import tool_input_schema

from embpilot.core.commands import CommandResult, NoActiveDeviceError

logger = logging.getLogger(__name__)

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


def _tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    *,
    when_to_use: str | None = None,
    avoid_when: str | None = None,
    typical_flow: str | None = None,
    pitfalls: str | None = None,
) -> Tool:
    """Build one tool whose description guides agent tool selection.

    Every description is prefixed with the JSON-object note, then carries the
    one-line purpose plus optional structured guidance rendered as distinct
    sections so agents can pick tools and arguments reliably.
    """
    parts = [_JSON_OBJECT_NOTE + description]
    if when_to_use:
        parts.append(f"When to use: {when_to_use}")
    if avoid_when:
        parts.append(f"Avoid when: {avoid_when}")
    if typical_flow:
        parts.append(f"Typical flow: {typical_flow}")
    if pitfalls:
        parts.append(f"Pitfalls: {pitfalls}")
    return Tool(
        name=name,
        description="\n".join(parts),
        inputSchema=schema,
        outputSchema=_RESULT_SCHEMA,
    )


def _connection_failure_suggestion(exc: Exception) -> str:
    """Pick a recovery hint from the failure category."""
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return (
            "Connection timed out. Check the address, network path, and "
            "firewall; for serial, verify the baudrate matches the device."
        )
    if isinstance(exc, PermissionError) or any(
        word in text for word in ("authentication", "permission denied", "host key")
    ):
        return (
            "Authentication or authorization failed. Check credentials, "
            "key-file permissions, and host-key verification settings."
        )
    if isinstance(exc, ConnectionRefusedError) or any(
        word in text for word in ("refused", "could not open", "cannot open")
    ):
        return (
            "Connection refused or port unavailable. Confirm the service is "
            "listening on that port; for serial, check the port is not "
            "occupied by another program and the device is powered on."
        )
    return (
        "Check the device address, credentials, and availability, then retry."
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
            when_to_use="The device has a UART port exposed on this host; pick the "
            "right line_ending for the console (interactive shells usually want "
            "crlf, bare kernel consoles usually lf).",
            avoid_when="The target is reachable over the network; prefer connect_ssh "
            "or connect_telnet instead.",
            typical_flow="connect_serial -> send_command (or read_output) -> "
            "disconnect_device.",
            pitfalls="The port must be free: a terminal emulator already holding "
            "COM3 will make the connection fail. Wrong baudrate produces garbage "
            "or no output.",
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
            when_to_use="The target runs an SSH server; prefer a key_file over a "
            "password and keep host-key verification enabled.",
            avoid_when="The device only exposes a plain console; use connect_serial "
            "or connect_telnet.",
            typical_flow="connect_ssh -> send_command -> disconnect_device.",
            pitfalls="A password or private key that needs a passphrase may require "
            "interaction; verify the key-file permissions and the known_hosts "
            "entry before retrying.",
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
            when_to_use="The target exposes a Telnet console and has no SSH server.",
            avoid_when="Credentials or sensitive data are involved; Telnet is "
            "unencrypted, prefer SSH.",
            typical_flow="connect_telnet -> send_command -> disconnect_device.",
            pitfalls="Many boards only accept Telnet from a few source addresses; "
            "check access lists if the connection is refused.",
        ),
        _tool(
            "disconnect_device",
            "Close the active device connection and finalize its session.",
            _object_schema({}),
            when_to_use="Always at the end of a debugging task, before abandoning a "
            "session, or before connecting to a different device.",
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
            when_to_use="You need to run one command and see its output.",
            avoid_when="The device emits logs on its own (boot messages, periodic "
            "status); use read_output so nothing is sent.",
            typical_flow="Set expect_regex to a completion marker (prompt or 'OK') and "
            "a bounded timeout_ms; if the marker appears the call returns early.",
            pitfalls="Streaming output with no completion marker keeps capturing "
            "until timeout_ms; prefer expect_regex, or read_output for passive "
            "observation.",
        ),
        _tool(
            "reset_target",
            "Reset the active target. Only the implemented reboot method is advertised.",
            _object_schema(
                {"method": {"type": "string", "enum": ["reboot"], "default": "reboot"}}
            ),
            when_to_use="Recovering a hung target or capturing a clean boot sequence.",
            pitfalls="reboot is a software reset: it interrupts any command currently "
            "running on the device and restarts the boot flow.",
        ),
        _tool(
            "read_output",
            "Read device output without sending any bytes. Use to observe logs "
            "the device emits on its own (boot messages, periodic status) until "
            "expect_regex matches or the duration window elapses.",
            _object_schema(
                {
                    "duration_ms": {
                        **_TIMEOUT_PROPERTY,
                        "default": 1000,
                        "description": "Collection window in milliseconds",
                    },
                    "expect_regex": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Return early when this pattern appears in device output",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200_000,
                        "default": 20_000,
                        "description": "Maximum output characters",
                    },
                },
                examples=[{"duration_ms": 1000, "expect_regex": "Login:"}],
            ),
            when_to_use="Watching the device without interacting: boot logs, "
            "heartbeat/status lines, or waiting for a marker.",
            avoid_when="You must run a command; use send_command instead.",
            typical_flow="Call right after reset_target or connect to capture what "
            "the device prints next; set expect_regex to return early on a "
            "completion marker.",
            pitfalls="Only output produced after the call starts is collected; "
            "earlier ring-buffer lines are not replayed.",
        ),
        _tool(
            "search_history_logs",
            "Search captured device logs by keyword, in the active session or a "
            "closed historical one.",
            _object_schema(
                {
                    "keyword": {"type": "string", "minLength": 1},
                    "time_window_seconds": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                    "session_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Optional: search a closed historical session "
                        "read-only instead of the active one",
                    },
                },
                required=["keyword"],
            ),
            when_to_use="Finding when an error or pattern appeared in a session's "
            "captured logs; pass session_id (from list_sessions) to search a "
            "closed session after disconnection.",
            avoid_when="The session database file has been deleted; list_sessions "
            "first to confirm the session still exists.",
        ),
        _tool(
            "list_sessions",
            "List historical debug sessions.",
            _object_schema({}),
            when_to_use="Before exporting or reviewing past work; sessions are "
            "persisted in SQLite after disconnects.",
        ),
        _tool(
            "delete_session",
            "Permanently delete a closed historical session.",
            _object_schema(
                {"session_id": {"type": "string", "minLength": 1}},
                required=["session_id"],
            ),
            when_to_use="Cleaning up sessions that are no longer needed; this cannot "
            "be undone.",
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
            when_to_use="Handing a session over to external tooling (SQLite queries, "
            "backup, or sharing).",
        ),
    ]


class SessionOperations(Protocol):
    async def connect_device(self, interface: str, config: dict[str, Any]) -> str: ...

    async def send_command(self, **arguments: Any) -> CommandResult: ...

    async def read_output(self, **arguments: Any) -> CommandResult: ...

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
    manager: SessionOperations,
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
                suggestion=_connection_failure_suggestion(exc),
            )
        return _success(
            f"Connected over {interface}. Session ID: {session_id}",
            {"session_id": session_id, "interface": interface},
        )
    if name == "send_command":
        result = await manager.send_command(**arguments)
        message = result.output
        if result.timed_out and not arguments.get("expect_regex"):
            message += (
                "\n(hint: the command timed out; if it has a completion marker, "
                "pass expect_regex, or increase timeout_ms)"
            )
        return _success(message, result.as_dict())
    if name == "read_output":
        result = await manager.read_output(**arguments)
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
    manager: SessionOperations,
    name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Dispatch a tool call and keep operational errors machine-readable."""
    tool = next((item for item in build_tool_definitions() if item.name == name), None)
    if tool is not None:
        try:
            jsonschema.validate(instance=arguments, schema=tool_input_schema(tool))
        except jsonschema.ValidationError as exc:
            return _failure(
                "INVALID_ARGUMENT",
                exc.message,
                retryable=False,
                suggestion="Refresh the tool schema and send a JSON object matching it.",
            )
    try:
        return await _dispatch_tool(manager, name, arguments)
    except NoActiveDeviceError as exc:
        return _failure(
            "NO_ACTIVE_DEVICE",
            str(exc),
            retryable=False,
            suggestion="Call connect_serial, connect_ssh, or connect_telnet first.",
        )
    except RuntimeError as exc:
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
    except Exception:
        logger.exception("Unexpected MCP tool failure: %s", name)
        return _failure(
            "INTERNAL_ERROR",
            "Unexpected internal error",
            retryable=False,
            suggestion="Inspect the EmbPilot server logs before retrying.",
        )
