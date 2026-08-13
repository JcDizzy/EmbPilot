"""
MCP protocol layer — registers Tools, Resources, and Prompts with the MCP SDK.
Manages session lifecycle (connect → session DB → disconnect) and device I/O pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import AnyUrl

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
    Resource,
    Prompt,
    PromptMessage,
    PromptArgument,
    GetPromptResult,
)

from embpilot.config import EmbPilotConfig
from embpilot.session_manager import (
    DeviceSession,
    SessionManager,
)

# Backward-compatible re-exports: older code imports SessionManager from
# embpilot.server; the business layer now lives in embpilot.session_manager.
__all__ = ["DeviceSession", "SessionManager", "serve"]
from embpilot.mcp_contracts import build_tool_definitions, dispatch_tool

logger = logging.getLogger(__name__)


# ── Active device session ─────────────────────────────────────────────────────

# ── Resource catalog ────────────────────────────────────────────────────────


def build_resources() -> list[Resource]:
    """The read-only data sources advertised to agents."""
    return [
        Resource(
            uri=AnyUrl("device://live_log"),
            name="Live Device Log",
            description="Recent 2000 lines of device output from the active session",
            mimeType="text/plain",
        ),
        Resource(
            uri=AnyUrl("device://sysinfo"),
            name="System Info Snapshot",
            description="Aggregated system information from the connected device",
            mimeType="text/markdown",
        ),
        Resource(
            uri=AnyUrl("device://analytics"),
            name="Error Analytics",
            description="Frequency table of error-like patterns in the current session",
            mimeType="text/plain",
        ),
        Resource(
            uri=AnyUrl("device://session_info"),
            name="Session Info",
            description="Current session metadata: id, ring depth, stored log rows",
            mimeType="application/json",
        ),
    ]


async def render_resource(manager: SessionManager, uri: AnyUrl) -> str | bytes:
    """Render one device resource against the given session manager."""
    uri_str = str(uri)

    if uri_str == "device://live_log":
        if manager.active_ring is None:
            return "No active device connection."
        lines = [line.formatted() for line in manager.active_ring.snapshot()]
        return "\n".join(lines) or "(no log data yet)"

    elif uri_str == "device://sysinfo":
        if manager._active is None:
            return "No active device connection."
        # Collect sysinfo via command sequence
        cmds = ["help", "version", "free", "ps", "uname -a"]
        parts = [f"# System Info ({datetime.now(timezone.utc).isoformat()})", ""]
        for cmd in cmds:
            try:
                out = (await manager.send_command(cmd, timeout_ms=3000)).output
                parts.append(f"## {cmd}")
                parts.append(out)
                parts.append("")
            except Exception:
                parts.append(f"## {cmd}")
                parts.append("(command failed)")
                parts.append("")
        return "\n".join(parts)

    elif uri_str == "device://analytics":
        if manager._active and manager._active.session_db:
            rows = await manager._active.session_db.get_analytics()
            if not rows:
                return "No error patterns detected."
            lines = [f"{r['cnt']:5d}x | {r['text']}" for r in rows]
            return "Count | Pattern\n" + "-" * 40 + "\n" + "\n".join(lines)
        return "No active session."

    elif uri_str == "device://session_info":
        if manager._active is None:
            return "No active device connection."
        active = manager._active
        info: dict = {
            "session_id": active.session_id,
            "ring_buffer_lines": len(active.ring.snapshot()),
            "stored_log_rows": await active.session_db.count_logs(),
        }
        # Session registry metadata: interface, device, start time.
        sessions = await manager.main_db().list_sessions()
        match = [s for s in sessions if s["session_id"] == active.session_id]
        if match:
            row = match[0]
            for key in ("interface", "device_name", "started_at", "status"):
                if row.get(key) is not None:
                    info[key] = row[key]
        # Recent error-like patterns from the session analytics.
        rows = await active.session_db.get_analytics()
        if rows:
            info["recent_error_patterns"] = [
                {"count": r["cnt"], "pattern": r["text"]} for r in rows[:5]
            ]
        return json.dumps(info, ensure_ascii=False, indent=2)

    return f"Unknown resource: {uri}"


# ── Prompt catalog ──────────────────────────────────────────────────────────


def build_prompts() -> list[Prompt]:
    """The scenario templates advertised to agents."""
    return [
        Prompt(
            name="analyze_crash_log",
            description="Analyse a crash or panic log from the device",
            arguments=[
                PromptArgument(
                    name="context",
                    description="Additional context about the crash (optional)",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="hardware_sanity_check",
            description="Run a hardware sanity check against the connected device",
            arguments=[
                PromptArgument(
                    name="focus",
                    description="Specific subsystem to check (e.g. 'memory', 'peripherals')",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="connect_and_explore",
            description="Connect to a device and summarize what it can do",
            arguments=[
                PromptArgument(
                    name="interface",
                    description="Connection type: serial, ssh, or telnet",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="capture_boot_log",
            description="Capture a full boot sequence from a reboot",
            arguments=[
                PromptArgument(
                    name="marker",
                    description="Regex matching the boot-complete marker (optional)",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="diagnose_connection",
            description="Diagnose a failed device connection by error code",
        ),
        Prompt(
            name="design_expect",
            description="Design a reliable expect_regex for a device command",
        ),
        Prompt(
            name="session_handoff",
            description="Summarize a debugging session for handoff",
        ),
    ]


def render_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
    """Render one scenario template into an actionable user message."""
    if name == "analyze_crash_log":
        ctx = (arguments or {}).get("context", "")
        user_msg = f"The device has crashed. Here is the context:\n\n{ctx}\n\n1. Capture the crash log from device://live_log\n2. Identify the fault type (HardFault, Panic, Segfault)\n3. Check the register state if available\n4. Cross-reference with the knowledge base for known error codes\n5. Propose a root-cause analysis and fix."
    elif name == "hardware_sanity_check":
        focus = (arguments or {}).get("focus", "general")
        user_msg = f"Perform a hardware sanity check focusing on: {focus}\n\n1. Connect to the device if not already connected\n2. Run diagnostic commands (help, version, dmesg)\n3. Check peripheral status if applicable\n4. Report any anomalies found"
    elif name == "connect_and_explore":
        interface = (arguments or {}).get("interface", "serial")
        user_msg = (
            f"Explore the device over {interface}.\n\n"
            f"1. Call connect_{interface} with the connection parameters.\n"
            "2. Send 'version' and 'help' via send_command (anchor expect_regex "
            "on the shell prompt, timeout_ms 5000).\n"
            "3. Summarize the device: firmware version, available commands, "
            "and the suggested next debugging steps."
        )
    elif name == "capture_boot_log":
        marker = (arguments or {}).get("marker", "login:")
        user_msg = (
            "Capture the device boot log from scratch.\n\n"
            "1. Call reset_target with method 'reboot'.\n"
            f"2. Immediately call read_output with duration_ms 15000 and "
            f"expect_regex '{marker}'.\n"
            "3. If no marker matches, review the collected output and pick a "
            "better regex or extend duration_ms.\n"
            "4. Report the boot sequence and any errors or warnings."
        )
    elif name == "diagnose_connection":
        user_msg = (
            "A device connection failed. Diagnose it.\n\n"
            "1. Review the error envelope: code, message, retryable, suggestion.\n"
            "2. Timeouts: verify the address/port, network path, and firewall; "
            "for serial, check the baudrate.\n"
            "3. Authentication errors: verify credentials, key-file permissions, "
            "and host-key settings.\n"
            "4. Refused errors: confirm the service is listening and the port "
            "is not occupied.\n"
            "5. Retry once with corrected parameters, then report the outcome."
        )
    elif name == "design_expect":
        user_msg = (
            "Design a reliable expect_regex for a device command.\n\n"
            "1. Anchor on the shell prompt or a completion marker "
            "(e.g. '^root@board:~#' or 'OK').\n"
            "2. Avoid greedy patterns; keep it specific enough to not match "
            "mid-command echo.\n"
            "3. Pair expect_regex with a bounded timeout_ms; if the marker "
            "never appears the call returns timed_out with everything "
            "captured so far.\n"
            "4. For streaming output with no completion marker, prefer "
            "read_output instead."
        )
    elif name == "session_handoff":
        user_msg = (
            "Summarize the debugging session for handoff.\n\n"
            "1. Call list_sessions to identify the session(s).\n"
            "2. If the session is still active, review recent output with "
            "read_output.\n"
            "3. Summarize: device, commands run, key findings, open "
            "questions, and the recommended next step."
        )
    else:
        raise ValueError(f"Unknown prompt: {name}")
    return GetPromptResult(
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=user_msg),
            ),
        ],
    )


# ── MCP Server implementation ────────────────────────────────────────────────

_active_manager: Optional[SessionManager] = None


def get_session_manager() -> Optional[SessionManager]:
    return _active_manager


def serve(config: EmbPilotConfig) -> None:
    """Start the MCP server with stdio transport."""
    global _active_manager

    config.ensure_data_dirs()
    logger.info("EmbPilot 0.1.0 starting | data_dir=%s", config.data_dir)

    manager = SessionManager(config)
    _active_manager = manager

    app = Server("embpilot", version="0.1.0")

    # ── Tools ────────────────────────────────────────────────────────

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return build_tool_definitions()

    @app.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return await dispatch_tool(manager, name, arguments)

    # ── Resources ────────────────────────────────────────────────────

    @app.list_resources()
    async def list_resources() -> list[Resource]:
        return build_resources()

    @app.read_resource()
    async def read_resource(uri: AnyUrl) -> str | bytes:
        return await render_resource(manager, uri)

    # ── Prompts ──────────────────────────────────────────────────────

    @app.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return build_prompts()

    @app.get_prompt()
    async def get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
        return render_prompt(name, arguments)

    # ── Run ──────────────────────────────────────────────────────────

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
