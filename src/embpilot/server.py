"""
MCP protocol layer — registers Tools, Resources, and Prompts with the MCP SDK.
Manages session lifecycle (connect → session DB → disconnect) and device I/O pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, cast

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
from embpilot.core.database import MainDatabase, SessionDatabase
from embpilot.core.engine import (
    LogProducer,
    DbConsumer,
    RingBuffer,
    LogLine,
)
from embpilot.core.commands import (
    CommandExecutor,
    CommandResult,
    LineEnding,
    NoActiveDeviceError,
)
from embpilot.drivers.base import BaseDevice
from embpilot.drivers.serial_dev import SerialDevice
from embpilot.drivers.telnet_dev import TelnetDevice
from embpilot.drivers.ssh_dev import SshDevice
from embpilot.mcp_contracts import build_tool_definitions, dispatch_tool

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
        command_executor: CommandExecutor,
        line_ending: LineEnding,
    ) -> None:
        self.session_id = session_id
        self.device = device
        self.session_db = session_db
        self.ring = ring
        self.producer = producer
        self.db_consumer = db_consumer
        self.command_executor = command_executor
        self.line_ending = line_ending


# ── Session & Device Manager ─────────────────────────────────────────────────

class SessionManager:
    """Manages database sessions and active device connections."""

    def __init__(
        self,
        config: EmbPilotConfig,
        device_factory: Callable[[str, dict[str, Any]], BaseDevice] | None = None,
    ) -> None:
        self._config = config
        self._device_factory = device_factory or _build_device
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
        device = self._device_factory(interface_type, config)
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
        command_executor = CommandExecutor(device, ring)
        line_ending = config.get("line_ending", "lf")
        if line_ending not in ("none", "lf", "crlf", "cr"):
            raise ValueError(f"Unsupported line ending: {line_ending}")

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
            command_executor=command_executor,
            line_ending=cast(LineEnding, line_ending),
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
        await ds.db_consumer.stop()

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
        line_ending: str = "session",
        expect_regex: Optional[str] = None,
        timeout_ms: int = 5000,
        max_output_chars: int = 20_000,
    ) -> CommandResult:
        """Send a command to the active device and capture output.

        If *expect_regex* is provided, the method returns as soon as the
        pattern is matched (plus a short window).  Otherwise it waits
        for the full timeout.
        """
        if self._active is None:
            raise NoActiveDeviceError("No active device connection")

        active = self._active
        effective_line_ending = active.line_ending if line_ending == "session" else line_ending
        result = await active.command_executor.execute(
            command,
            line_ending=cast(LineEnding, effective_line_ending),
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            max_output_chars=max_output_chars,
        )

        await self._main_db.insert_operation(
            actor="AI",
            action_type="call_tool",
            detail={
                "tool": "send_command",
                "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest()[:12],
                "command_length": len(command),
                "expect_provided": expect_regex is not None,
            },
            session_id=active.session_id,
        )
        return result

    async def read_output(
        self,
        duration_ms: int = 1000,
        expect_regex: Optional[str] = None,
        max_chars: int = 20_000,
    ) -> CommandResult:
        """Observe device output without sending any bytes.

        Collects ring-buffer lines pushed after the call starts, returning
        early when *expect_regex* matches or when *duration_ms* elapses.
        """
        if self._active is None:
            raise NoActiveDeviceError("No active device connection")
        if duration_ms < 1:
            raise ValueError("duration_ms must be at least 1")
        if max_chars < 1:
            raise ValueError("max_chars must be at least 1")
        try:
            pattern = re.compile(expect_regex) if expect_regex else None
        except re.error as exc:
            raise ValueError(f"Invalid expect_regex: {exc}") from exc

        ring = self._active.ring
        cursor = ring.mark()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration_ms / 1000.0
        matched = False
        while True:
            lines = ring.snapshot_since(cursor)
            if pattern and any(pattern.search(line.text) for line in lines):
                matched = True
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))

        output = "\n".join(line.formatted() for line in ring.snapshot_since(cursor))
        truncated = len(output) > max_chars
        if truncated:
            output = output[:max_chars]
        return CommandResult(
            output=output or "(no output captured)",
            matched=matched,
            timed_out=not matched,
            truncated=truncated,
        )

    async def reset_target(self, method: str = "reboot") -> str:
        """Reset the target device."""
        if self._active is None:
            raise NoActiveDeviceError("No active device connection")
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

    async def search_history_logs(
        self,
        keyword: str,
        time_window_seconds: int | None = None,
        limit: int = 50,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search logs by keyword.

        Without *session_id* the active session is searched (a connection is
        required). With *session_id*, the closed historical session is opened
        read-only and searched without touching the active connection.
        """
        if session_id is None:
            if self._active is None:
                raise NoActiveDeviceError("No active device connection")
            return await self._active.session_db.search_logs(
                keyword=keyword,
                time_window_seconds=time_window_seconds,
                limit=limit,
            )

        sessions = await self._main_db.list_sessions()
        match = [s for s in sessions if s["session_id"] == session_id]
        if not match:
            raise ValueError(f"Session not found: {session_id}")
        db_path = Path(match[0]["db_path"])
        if not db_path.exists():
            raise FileNotFoundError(f"Session db file gone: {db_path}")

        session_db = SessionDatabase(db_path)
        await session_db.open(readonly=True)
        try:
            return await session_db.search_logs(
                keyword=keyword,
                time_window_seconds=time_window_seconds,
                limit=limit,
            )
        finally:
            await session_db.close()

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
            timeout=cfg.get("timeout_ms", 5000) / 1000.0,
        )
    elif interface == "telnet":
        return TelnetDevice(
            host=cfg["host"],
            port=cfg.get("port", 23),
            timeout=cfg.get("timeout_ms", 10_000) / 1000.0,
        )
    elif interface == "ssh":
        return SshDevice(
            host=cfg["host"],
            port=cfg.get("port", 22),
            username=cfg.get("username", ""),
            password=cfg.get("password"),
            key_file=cfg.get("key_file"),
            known_hosts=cfg.get("known_hosts"),
            insecure_skip_host_key_check=cfg.get("insecure_skip_host_key_check", False),
            connect_timeout=cfg.get("timeout_ms", 10_000) / 1000.0,
        )
    else:
        raise ValueError(f"Unsupported interface: {interface}")


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

        return f"Unknown resource: {uri}"

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
