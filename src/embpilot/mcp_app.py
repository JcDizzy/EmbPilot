from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Optional

import jsonschema
from pydantic import AnyUrl

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ErrorData,
    GetPromptResult,
    INVALID_PARAMS,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    ServerResult,
    TextContent,
    Tool,
)

from embpilot import __version__
from embpilot.config import EmbPilotConfig
from embpilot.runtime.resources import (
    build_session_info_resource,
    render_live_log_snapshot,
)
from embpilot.runtime.session import SessionManager

logger = logging.getLogger(__name__)

_active_manager: Optional[SessionManager] = None
_DEFAULT_COMMAND_TIMEOUT_MAX_MS = 60_000
_DEFAULT_SEARCH_LIMIT_MAX = 1_000
_DEFAULT_EXPORT_LIMIT_MAX = 10_000
_DEFAULT_AUDIT_EXPORT_LIMIT_MAX = 5_000


def get_active_session_manager() -> Optional[SessionManager]:
    return _active_manager


def build_resource_catalog() -> list[Resource]:
    return [
        Resource(
            uri=AnyUrl("device://live_log"),
            name="Live Device Log",
            description="Recent 2000 lines of device output from the active session",
            mimeType="text/plain",
        ),
        Resource(
            uri=AnyUrl("device://session_info"),
            name="Session Info",
            description="Current session metadata for the active device connection",
            mimeType="application/json",
        ),
        Resource(
            uri=AnyUrl("device://analytics"),
            name="Device Analytics",
            description="Aggregated error-like log patterns from the active session",
            mimeType="application/json",
        ),
    ]


def build_tool_catalog() -> list[Tool]:
    device_name_schema = {
        "type": "string",
        "minLength": 1,
        "description": "Optional user/agent supplied device name for session labeling.",
    }
    serial_config_schema = {
        "type": "object",
        "properties": {
            "port": {"type": "string", "minLength": 1},
            "baudrate": {"type": "integer", "minimum": 1, "default": 115200},
            "bytesize": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
            "parity": {"type": "string", "enum": ["N", "E", "O", "M", "S"], "default": "N"},
            "stopbits": {"type": "number", "enum": [1, 1.5, 2], "default": 1},
            "timeout": {"type": "number", "minimum": 0, "default": 5.0},
            "device_name": device_name_schema,
        },
        "required": ["port"],
        "additionalProperties": False,
    }
    telnet_config_schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "minLength": 1},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 23},
            "timeout": {"type": "number", "minimum": 0, "default": 10.0},
            "device_name": device_name_schema,
        },
        "required": ["host"],
        "additionalProperties": False,
    }
    ssh_config_schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "minLength": 1},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 22},
            "username": {"type": "string", "default": ""},
            "password": {"type": "string"},
            "key_file": {"type": "string", "minLength": 1},
            "known_hosts": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "device_name": device_name_schema,
        },
        "required": ["host"],
        "additionalProperties": False,
    }
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
                "additionalProperties": False,
                "allOf": [
                    {
                        "if": {"properties": {"interface_type": {"const": "serial"}}},
                        "then": {"properties": {"config": serial_config_schema}},
                    },
                    {
                        "if": {"properties": {"interface_type": {"const": "telnet"}}},
                        "then": {"properties": {"config": telnet_config_schema}},
                    },
                    {
                        "if": {"properties": {"interface_type": {"const": "ssh"}}},
                        "then": {"properties": {"config": ssh_config_schema}},
                    },
                ],
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
                        "minimum": 1,
                        "maximum": _DEFAULT_COMMAND_TIMEOUT_MAX_MS,
                        "default": 5000,
                    },
                    "line_ending": {
                        "type": "string",
                        "enum": ["as-is", "none", "lf", "crlf", "cr"],
                        "default": "as-is",
                        "description": (
                            "How EmbPilot should terminate the command before "
                            "writing it to the device."
                        ),
                    },
                    "confirm_dangerous_command": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Must be true to send commands matching EmbPilot's "
                            "dangerous-command patterns."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
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
                "additionalProperties": False,
            },
        ),
        Tool(
            name="disconnect_device",
            description="Disconnect the active device and close the session.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="list_sessions",
            description="List all recorded device sessions, newest first.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="delete_session",
            description=(
                "Delete a recorded session's database file and remove its index "
                "entry. The active session cannot be deleted — disconnect first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                    "confirm": {
                        "type": "boolean",
                        "default": False,
                        "description": "Must be true to delete a recorded session.",
                    },
                },
                "required": ["session_id", "confirm"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="search_history_logs",
            description=(
                "Search a session's device logs by keyword, optionally within a "
                "recent time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                    "keyword": {"type": "string", "minLength": 1},
                    "time_window_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional: restrict to the last N seconds.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _DEFAULT_SEARCH_LIMIT_MAX,
                        "default": 50,
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": ["session_id", "keyword"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="export_session",
            description=(
                "Export a session's device logs as text or JSON. Output is capped "
                "by limit (default 2000)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                    "format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "default": "text",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _DEFAULT_EXPORT_LIMIT_MAX,
                        "default": 2000,
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="export_operation_history",
            description=(
                "Export redacted operation audit history as JSON, optionally "
                "filtered by session id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _DEFAULT_AUDIT_EXPORT_LIMIT_MAX,
                        "default": 200,
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="ingest_doc",
            description=(
                "Ingest one documentation chunk into the optional local RAG store. "
                "Requires embpilot[rag]."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "source": {"type": "string", "minLength": 1, "default": "unknown"},
                    "metadata": {"type": "object"},
                    "doc_id": {"type": "string", "minLength": 1},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="search_docs",
            description=(
                "Search the optional local RAG store and return reference snippets "
                "as text plus structuredContent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    "source": {"type": "string", "minLength": 1},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="list_doc_sources",
            description="List distinct source labels in the optional local RAG store.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="delete_doc",
            description="Delete one documentation chunk from the optional local RAG store.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "minLength": 1},
                    "confirm": {
                        "type": "boolean",
                        "default": False,
                        "description": "Must be true to delete a stored document chunk.",
                    },
                },
                "required": ["doc_id", "confirm"],
                "additionalProperties": False,
            },
        ),
    ]


async def dispatch_tool(
    manager: SessionManager, name: str, arguments: dict[str, Any]
) -> CallToolResult:
    try:
        result = await _execute_tool(manager, name, arguments)
    except Exception as exc:  # noqa: BLE001 — tool execution errors stay in result space
        logger.exception("Tool %s failed", name)
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {exc}")],
            isError=True,
        )
    if isinstance(result, CallToolResult):
        return result
    return CallToolResult(content=result, isError=False)


async def _execute_tool(
    manager: SessionManager, name: str, arguments: dict[str, Any]
) -> list[TextContent] | CallToolResult:
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
            line_ending=arguments.get("line_ending", "as-is"),
            confirm_dangerous_command=arguments.get(
                "confirm_dangerous_command", False
            ),
        )
        return [TextContent(type="text", text=output)]
    if name == "reset_target":
        message = await manager.reset_target(method=arguments.get("method", "reboot"))
        return [TextContent(type="text", text=message)]
    if name == "disconnect_device":
        await manager.disconnect_device()
        return [TextContent(type="text", text="Disconnected.")]
    if name == "list_sessions":
        sessions = await manager.list_sessions()
        payload = json.dumps(sessions, ensure_ascii=False, indent=2)
        return [TextContent(type="text", text=payload)]
    if name == "delete_session":
        await manager.delete_session(
            session_id=arguments["session_id"],
            confirm=arguments["confirm"],
        )
        return [
            TextContent(
                type="text", text=f"Deleted session {arguments['session_id']!r}."
            )
        ]
    if name == "search_history_logs":
        logs = await manager.search_session_logs(
            session_id=arguments["session_id"],
            keyword=arguments["keyword"],
            time_window_seconds=arguments.get("time_window_seconds"),
            limit=arguments.get("limit", 50),
            offset=arguments.get("offset", 0),
        )
        payload = json.dumps(logs, ensure_ascii=False, indent=2)
        return [TextContent(type="text", text=payload)]
    if name == "export_session":
        text = await manager.export_session(
            session_id=arguments["session_id"],
            fmt=arguments.get("format", "text"),
            limit=arguments.get("limit", 2000),
            offset=arguments.get("offset", 0),
        )
        return [TextContent(type="text", text=text)]
    if name == "export_operation_history":
        text = await manager.export_operation_history(
            session_id=arguments.get("session_id"),
            limit=arguments.get("limit", 200),
            offset=arguments.get("offset", 0),
        )
        return [TextContent(type="text", text=text)]
    if name == "ingest_doc":
        payload = await manager.ingest_doc(
            text=arguments["text"],
            source=arguments.get("source", "unknown"),
            metadata=arguments.get("metadata"),
            doc_id=arguments.get("doc_id"),
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
            ],
            structuredContent=payload,
            isError=False,
        )
    if name == "search_docs":
        results = await manager.search_docs(
            query=arguments["query"],
            top_k=arguments.get("top_k", 5),
            source=arguments.get("source"),
        )
        payload = {"results": results}
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=False,
        )
    if name == "list_doc_sources":
        sources = await manager.list_doc_sources()
        payload = {"sources": sources}
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            ],
            structuredContent=payload,
            isError=False,
        )
    if name == "delete_doc":
        payload = await manager.delete_doc(
            doc_id=arguments["doc_id"],
            confirm=arguments["confirm"],
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
            ],
            structuredContent=payload,
            isError=False,
        )
    raise ValueError(f"Unknown tool: {name}")


def build_prompt_catalog() -> list[Prompt]:
    return [
        Prompt(
            name="analyze_crash_log",
            description=(
                "Analyze a recent crash, panic, hang, or unexpected reboot captured "
                "in the live device log."
            ),
            arguments=[
                PromptArgument(
                    name="context",
                    description="Optional extra context (board, firmware, symptoms).",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="hardware_sanity_check",
            description="Guide a hardware sanity check of the connected device.",
            arguments=[
                PromptArgument(
                    name="focus",
                    description="Optional area to focus on (power, peripherals, boot, ...).",
                    required=False,
                ),
            ],
        ),
    ]


def render_prompt(name: str, arguments: dict[str, Any]) -> str:
    if name == "analyze_crash_log":
        context = (arguments.get("context") or "").strip()
        body = (
            "You are an embedded debugging assistant. Read the live device log snapshot "
            "via the device://live_log resource and look for crash "
            "signatures, panics, hangs, or unexpected reboots. Use search_docs to "
            "retrieve relevant datasheet, error manual, or troubleshooting KB "
            "snippets when available, and cite those snippets in the analysis. "
            "Form a root-cause hypothesis and propose the next diagnostic commands "
            "to send via send_command."
        )
        if context:
            body += f"\n\nAdditional context:\n{context}"
        return body
    if name == "hardware_sanity_check":
        focus = (arguments.get("focus") or "general health").strip()
        return (
            f"You are an embedded debugging assistant. Perform a hardware sanity check "
            f"focused on: {focus}. Inspect device://session_info to confirm the connection "
            f"is active. Determine the appropriate diagnostic commands for THIS board from "
            f"the session context and the user — do not assume a fixed command set. Use "
            f"send_command to run the agreed commands and read device://live_log to "
            f"interpret the results."
        )
    raise ValueError(f"Unknown prompt: {name!r}")


def _prompt_result(name: str, text: str) -> GetPromptResult:
    return GetPromptResult(
        description=f"EmbPilot prompt: {name}",
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=text)),
        ],
    )


def _protocol_error(message: str, data: Any | None = None) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=message, data=data))


def _tool_by_name(name: str) -> Tool | None:
    return next((tool for tool in build_tool_catalog() if tool.name == name), None)


class _RateLimiter:
    def __init__(self, max_calls_per_minute: int) -> None:
        self._max_calls = max_calls_per_minute
        self._timestamps: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        if self._max_calls <= 0:
            return True
        now = now if now is not None else time.monotonic()
        cutoff = now - 60.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max_calls:
            return False
        self._timestamps.append(now)
        return True


def _tool_error_result(message: str) -> ServerResult:
    return ServerResult(
        CallToolResult(
            content=[TextContent(type="text", text=message)],
            isError=True,
        )
    )


async def _handle_call_tool_request(
    manager: SessionManager, request: CallToolRequest, rate_limiter: _RateLimiter
) -> ServerResult:
    name = request.params.name
    arguments = request.params.arguments or {}
    tool = _tool_by_name(name)
    if tool is None:
        raise _protocol_error(f"Unknown tool: {name}", {"tool": name})

    try:
        jsonschema.validate(instance=arguments, schema=tool.inputSchema)
    except jsonschema.ValidationError as exc:
        raise _protocol_error(
            f"Invalid arguments for tool {name}: {exc.message}",
            {"tool": name},
        ) from exc
    if not rate_limiter.allow():
        return _tool_error_result("Rate limit exceeded for MCP tool calls")

    return ServerResult(await dispatch_tool(manager, name, arguments))


def create_mcp_app(config: EmbPilotConfig) -> tuple[Server, SessionManager]:
    manager = SessionManager(config)
    app = Server("embpilot", version=__version__)
    rate_limiter = _RateLimiter(config.tool_rate_limit_per_minute)

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
        if uri_text == "device://analytics":
            analytics = await manager.get_analytics()
            return json.dumps(analytics, ensure_ascii=False, indent=2)
        raise _protocol_error(
            "Resource not found",
            {"uri": uri_text},
        )

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return build_tool_catalog()

    async def call_tool_handler(request: CallToolRequest) -> ServerResult:
        return await _handle_call_tool_request(manager, request, rate_limiter)

    app.request_handlers[CallToolRequest] = call_tool_handler

    @app.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return build_prompt_catalog()

    @app.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, Any]) -> GetPromptResult:
        return _prompt_result(name, render_prompt(name, arguments))

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
