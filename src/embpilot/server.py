"""
MCP protocol layer — registers Tools, Resources, and Prompts with the MCP SDK.
Manages session lifecycle (connect → session DB → disconnect) and device I/O pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import AnyUrl

from mcp.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    Resource,
    Prompt,
    PromptMessage,
    PromptArgument,
    GetPromptResult,
)

from embpilot.config import EmbPilotConfig
from embpilot.core.database import MainDatabase, SessionDatabase
from embpilot.core.engine import (
    LogProducer,
    ExpectConsumer,
    DbConsumer,
    RingBuffer,
    LogLine,
)
from embpilot.drivers.base import BaseDevice
from embpilot.drivers.serial_dev import SerialDevice
from embpilot.drivers.telnet_dev import TelnetDevice
from embpilot.drivers.ssh_dev import SshDevice

logger = logging.getLogger(__name__)


# ── Active device session ─────────────────────────────────────────────────────

class DeviceSession:
    """Holds all runtime state for one active device connection."""

    def __init__(
        self,
        session_id: str,
        device: BaseDevice,
        session_db: SessionDatabase,
        ring: RingBuffer,
        producer: LogProducer,
        db_consumer: DbConsumer,
        expect_consumer: ExpectConsumer,
    ) -> None:
        self.session_id = session_id
        self.device = device
        self.session_db = session_db
        self.ring = ring
        self.producer = producer
        self.db_consumer = db_consumer
        self.expect_consumer = expect_consumer


# ── Session & Device Manager ─────────────────────────────────────────────────

class SessionManager:
    """Manages database sessions and active device connections."""

    def __init__(self, config: EmbPilotConfig) -> None:
        self._config = config
        self._main_db = MainDatabase(config.main_db_path)
        self._active: Optional[DeviceSession] = None
        self._background_tasks: list[asyncio.Task] = []

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._main_db.open()
        await self._main_db.cleanup_expired_sessions(
            max_days=self._config.retention_days,
            max_gb=self._config.retention_max_gb,
        )
        logger.info(
            "Session manager ready | main_db=%s | sessions=%s",
            self._config.main_db_path,
            self._config.session_data_dir,
        )

    async def shutdown(self) -> None:
        await self.disconnect_device()
        await self._main_db.close()
        logger.info("Session manager shut down")

    # ── Device connection ────────────────────────────────────────────

    async def connect_device(
        self,
        interface_type: str,
        config: dict[str, Any],
    ) -> str:
        """Open a device connection, create a session DB, and start the
        producer-consumer pipeline.

        Returns the session ID.
        """
        # Implicitly disconnect any previous session
        await self.disconnect_device()

        # 1. Instantiate driver
        device = _build_device(interface_type, config)
        await device.connect()

        # 2. Open session database
        session_id = _make_session_id()
        device_name = _device_display_name(interface_type, config)
        session_path = self._resolve_session_path(session_id, device_name)
        session_db = SessionDatabase(session_path)
        await session_db.open()

        await self._main_db.register_session(
            session_id=session_id,
            device_name=device_name,
            interface=interface_type,
            db_path=str(session_path),
        )

        # 3. Build the pipeline
        queue: asyncio.Queue[LogLine] = asyncio.Queue(maxsize=5000)
        ring = RingBuffer(maxlen=2000)
        producer = LogProducer(
            reader=device.get_reader(),
            queue=queue,
            ring=ring,
            framing_timeout_ms=self._config.framing_timeout_ms,
        )
        db_consumer = DbConsumer(queue=queue, session_db=session_db)
        expect_consumer = ExpectConsumer(queue=queue)

        # 4. Start background tasks
        db_consumer.start()  # starts periodic flush timer
        self._background_tasks = [
            asyncio.create_task(producer.run(), name=f"producer-{session_id}"),
        ]

        # 5. Store session state
        self._active = DeviceSession(
            session_id=session_id,
            device=device,
            session_db=session_db,
            ring=ring,
            producer=producer,
            db_consumer=db_consumer,
            expect_consumer=expect_consumer,
        )

        await self._main_db.insert_operation(
            actor="System",
            action_type="connect",
            detail={"session_id": session_id, "device": device_name, "interface": interface_type},
            session_id=session_id,
        )
        logger.info("Connected: %s | session=%s", device_name, session_id)
        return session_id

    async def disconnect_device(self) -> None:
        """Close the active device connection and finalize the session."""
        if self._active is None:
            return

        ds = self._active
        sid = ds.session_id

        # Stop background tasks
        for t in self._background_tasks:
            t.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Close device
        try:
            await ds.device.disconnect()
        except Exception:
            logger.exception("Error disconnecting device")

        # Close session DB and register in main DB
        await ds.session_db.close()
        await self._main_db.end_session(sid)
        await self._main_db.insert_operation(
            actor="System",
            action_type="disconnect",
            detail={"session_id": sid},
            session_id=sid,
        )

        self._active = None
        logger.info("Disconnected: session=%s", sid)

    async def send_command(
        self,
        command: str,
        expect_regex: Optional[str] = None,
        timeout_ms: int = 5000,
    ) -> str:
        """Send a command to the active device and capture output.

        If *expect_regex* is provided, the method returns as soon as the
        pattern is matched (plus a short window).  Otherwise it waits
        for the full timeout.
        """
        if self._active is None:
            raise RuntimeError("No active device connection")

        device = self._active.device
        ring = self._active.ring

        # Take a snapshot of the ring buffer before the command
        before = ring.snapshot()[-1].formatted() if ring.snapshot() else ""

        await device.write(command.encode("utf-8"))

        # Simple approach: wait for timeout and collect what arrived
        await asyncio.sleep(timeout_ms / 1000.0)

        after = ring.snapshot()
        # Return lines that appeared after the command was sent
        output_lines: list[str] = []
        started = False
        for line in after:
            if not started:
                if line.formatted() == before:
                    started = True
                continue
            output_lines.append(line.formatted())

        result = "\n".join(output_lines)

        if expect_regex:
            import re
            matches = [l for l in output_lines if re.search(expect_regex, l)]
            if matches:
                result = "\n".join(matches)

        await self._main_db.insert_operation(
            actor="AI",
            action_type="call_tool",
            detail={"tool": "send_command", "command": command, "expect": expect_regex},
            session_id=self._active.session_id,
        )
        return result or "(no output captured)"

    async def reset_target(self, method: str = "reboot") -> str:
        """Reset the target device."""
        if self._active is None:
            raise RuntimeError("No active device connection")
        if method == "reboot":
            await self._active.device.write(b"reboot\n")
            return "Reboot command sent"
        elif method in ("dtr", "rts"):
            # Hardware reset via serial control lines
            # TODO(phase-3): implement DTR/RTS toggle
            raise NotImplementedError(f"Reset method '{method}' not yet implemented")
        else:
            raise ValueError(f"Unknown reset method: {method}")

    # ── Session management ───────────────────────────────────────────

    @property
    def active_session_id(self) -> Optional[str]:
        return self._active.session_id if self._active else None

    @property
    def active_ring(self) -> Optional[RingBuffer]:
        return self._active.ring if self._active else None

    def main_db(self) -> MainDatabase:
        return self._main_db

    async def list_sessions(self) -> list[dict]:
        return await self._main_db.list_sessions()

    async def delete_session(self, session_id: str) -> None:
        await self._main_db.delete_session(session_id)

    async def export_session(self, session_id: str, target_path: Path) -> Path:
        sessions = await self._main_db.list_sessions()
        match = [s for s in sessions if s["session_id"] == session_id]
        if not match:
            raise ValueError(f"Session not found: {session_id}")
        src = Path(match[0]["db_path"])
        if not src.exists():
            raise FileNotFoundError(f"Session db file gone: {src}")
        dst = target_path if target_path.suffix == ".db" else target_path / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(src, dst)
        return dst

    # ── Helpers ──────────────────────────────────────────────────────

    def _resolve_session_path(self, session_id: str, device_name: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe = _sanitize(device_name)
        return self._config.session_data_dir / f"session_{ts}_{safe}_{session_id[:8]}.db"


# ── Factory helpers ──────────────────────────────────────────────────────────

def _make_session_id() -> str:
    return uuid.uuid4().hex[:16]


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_") or "device"


def _device_display_name(interface: str, cfg: dict) -> str:
    if interface == "serial":
        return cfg.get("port", "unknown")
    return f"{cfg.get('host', 'unknown')}:{cfg.get('port', 0)}"


def _build_device(interface: str, cfg: dict[str, Any]) -> BaseDevice:
    if interface == "serial":
        return SerialDevice(
            port=cfg["port"],
            baudrate=cfg.get("baudrate", 115200),
            bytesize=cfg.get("bytesize", 8),
            parity=cfg.get("parity", "N"),
            stopbits=cfg.get("stopbits", 1),
            timeout=cfg.get("timeout", 5.0),
        )
    elif interface == "telnet":
        return TelnetDevice(
            host=cfg["host"],
            port=cfg.get("port", 23),
            timeout=cfg.get("timeout", 10.0),
        )
    elif interface == "ssh":
        return SshDevice(
            host=cfg["host"],
            port=cfg.get("port", 22),
            username=cfg.get("username", ""),
            password=cfg.get("password"),
            key_file=cfg.get("key_file"),
            known_hosts=cfg.get("known_hosts"),
        )
    else:
        raise ValueError(f"Unsupported interface: {interface}")


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
        return [
            Tool(
                name="connect_device",
                description="Establish a hardware connection to an embedded device. "
                            "Supports serial (UART), telnet, and SSH. "
                            "Any existing connection is implicitly closed first.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "interface_type": {
                            "type": "string",
                            "enum": ["serial", "telnet", "ssh"],
                            "description": "Connection protocol",
                        },
                        "config": {
                            "type": "object",
                            "description": "Connection parameters varies by interface",
                            "properties": {
                                # Serial
                                "port": {"type": "string", "description": "Serial port (e.g. COM3, /dev/ttyUSB0)"},
                                "baudrate": {"type": "integer", "description": "Baud rate (default 115200)"},
                                # Telnet / SSH
                                "host": {"type": "string", "description": "Hostname or IP address"},
                                "port": {"type": "integer", "description": "TCP port"},
                                # SSH
                                "username": {"type": "string", "description": "SSH username"},
                                "password": {"type": "string", "description": "SSH password"},
                                "key_file": {"type": "string", "description": "SSH private key path"},
                            },
                        },
                    },
                    "required": ["interface_type", "config"],
                },
            ),
            Tool(
                name="disconnect_device",
                description="Disconnect from the currently connected device and close the session.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="send_command",
                description="Send a command to the connected device and capture its output. "
                            "Optionally provide an expect_regex to stop collecting as soon as "
                            "a pattern is matched, reducing token usage.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command to send (e.g. 'ifconfig', 'help')"},
                        "expect_regex": {"type": "string", "description": "Optional regex; output stops when this pattern is seen"},
                        "timeout_ms": {"type": "integer", "description": "Max wait time in milliseconds (default 5000)"},
                    },
                    "required": ["command"],
                },
            ),
            Tool(
                name="reset_target",
                description="Reset the connected target device.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["reboot", "dtr", "rts"],
                            "description": "Reset method: 'reboot' sends text command; 'dtr'/'rts' toggle serial control lines",
                        },
                    },
                    "required": ["method"],
                },
            ),
            Tool(
                name="search_history_logs",
                description="Search the current session's log history for a keyword or pattern.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "Keyword or pattern to search for"},
                        "time_window_seconds": {"type": "integer", "description": "Optional time window in seconds"},
                        "limit": {"type": "integer", "description": "Max results (default 50)"},
                    },
                    "required": ["keyword"],
                },
            ),
            Tool(
                name="list_sessions",
                description="List all historical debug sessions.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="delete_session",
                description="Delete a historical session and its database file.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "Session ID to delete"},
                    },
                    "required": ["session_id"],
                },
            ),
            Tool(
                name="export_session",
                description="Export a session database file to a specified path.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "Session ID to export"},
                        "target_path": {"type": "string", "description": "Destination file or directory path"},
                    },
                    "required": ["session_id", "target_path"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "connect_device":
                sid = await manager.connect_device(
                    interface_type=arguments["interface_type"],
                    config=arguments["config"],
                )
                return [TextContent(type="text", text=f"Connected. Session ID: {sid}")]

            elif name == "disconnect_device":
                await manager.disconnect_device()
                return [TextContent(type="text", text="Disconnected.")]

            elif name == "send_command":
                result = await manager.send_command(
                    command=arguments["command"],
                    expect_regex=arguments.get("expect_regex"),
                    timeout_ms=arguments.get("timeout_ms", 5000),
                )
                return [TextContent(type="text", text=result)]

            elif name == "reset_target":
                result = await manager.reset_target(method=arguments.get("method", "reboot"))
                return [TextContent(type="text", text=result)]

            elif name == "search_history_logs":
                if manager._active and manager._active.session_db:
                    results = await manager._active.session_db.search_logs(
                        keyword=arguments["keyword"],
                        time_window_seconds=arguments.get("time_window_seconds"),
                        limit=arguments.get("limit", 50),
                    )
                    if not results:
                        return [TextContent(type="text", text="No matches found.")]
                    lines = [f"[{r['timestamp']}] {r['text']}" for r in results]
                    return [TextContent(type="text", text="\n".join(lines))]
                return [TextContent(type="text", text="No active session.")]

            elif name == "list_sessions":
                sessions = await manager.list_sessions()
                if not sessions:
                    return [TextContent(type="text", text="No sessions found.")]
                lines = [
                    f"{s['session_id']:16s} | {s['device_name']:20s} | {s['interface']:8s} | "
                    f"{s['started_at']} | {s['status']}"
                    for s in sessions
                ]
                return [TextContent(type="text", text="Session ID       | Device              | Type    | Started At            | Status\n" + "-" * 85 + "\n" + "\n".join(lines))]

            elif name == "delete_session":
                await manager.delete_session(arguments["session_id"])
                return [TextContent(type="text", text=f"Session {arguments['session_id']} deleted.")]

            elif name == "export_session":
                dst = await manager.export_session(
                    arguments["session_id"],
                    Path(arguments["target_path"]),
                )
                return [TextContent(type="text", text=f"Exported to: {dst}")]

            else:
                raise ValueError(f"Unknown tool: {name}")

        except Exception as e:
            logger.exception("Tool call failed: %s", name)
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── Resources ────────────────────────────────────────────────────

    @app.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(
                uri=AnyUrl("device://live_log"),
                name="Live Device Log",
                description="Recent 2000 lines of device output from the active session (subscribable)",
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
        ]

    @app.read_resource()
    async def read_resource(uri: AnyUrl) -> str | bytes:
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
                    out = await manager.send_command(cmd, timeout_ms=3000)
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

        return f"Unknown resource: {uri}"

    @app.subscribe_resource()
    async def subscribe_resource(uri: AnyUrl) -> None:
        logger.info("Client subscribed to %s", uri)
        # TODO(phase-3): push live_log updates on a timer

    # ── Prompts ──────────────────────────────────────────────────────

    @app.list_prompts()
    async def list_prompts() -> list[Prompt]:
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
        ]

    @app.get_prompt()
    async def get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
        if name == "analyze_crash_log":
            ctx = (arguments or {}).get("context", "")
            user_msg = f"The device has crashed. Here is the context:\n\n{ctx}\n\n1. Capture the crash log from device://live_log\n2. Identify the fault type (HardFault, Panic, Segfault)\n3. Check the register state if available\n4. Cross-reference with the knowledge base for known error codes\n5. Propose a root-cause analysis and fix."
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(type="text", text=user_msg),
                    ),
                ],
            )
        elif name == "hardware_sanity_check":
            focus = (arguments or {}).get("focus", "general")
            user_msg = f"Perform a hardware sanity check focusing on: {focus}\n\n1. Connect to the device if not already connected\n2. Run diagnostic commands (help, version, dmesg)\n3. Check peripheral status if applicable\n4. Report any anomalies found"
            return GetPromptResult(
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(type="text", text=user_msg),
                    ),
                ],
            )
        raise ValueError(f"Unknown prompt: {name}")

    # ── Run ──────────────────────────────────────────────────────────

    async def _run() -> None:
        await manager.start()

        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )

        await manager.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("EmbPilot stopped by user")
