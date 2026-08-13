"""
Session management: device connections, the producer-consumer log pipeline,
command execution, and session persistence. This is the business layer that
both the MCP server (server.py) and the CLI entry points drive; it is
transport-agnostic and has no knowledge of the MCP protocol.
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

from embpilot.config import EmbPilotConfig
from embpilot.core.commands import (
    CommandExecutor,
    CommandResult,
    LineEnding,
    NoActiveDeviceError,
)
from embpilot.core.database import MainDatabase, SessionDatabase
from embpilot.core.engine import (
    DbConsumer,
    LogProducer,
    RingBuffer,
    LogLine,
)
from embpilot.drivers.base import BaseDevice
from embpilot.drivers.serial_dev import SerialDevice
from embpilot.drivers.telnet_dev import TelnetDevice
from embpilot.drivers.ssh_dev import SshDevice

logger = logging.getLogger(__name__)


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

        # Stop background tasks. A cancelled task must never wedge the
        # disconnect path (asyncio wait_for + cancel + feed_data races on the
        # Windows proactor loop can delay a task's cancellation delivery), so
        # bound the wait and log any stragglers instead of hanging forever.
        for t in self._background_tasks:
            t.cancel()
        _done, pending = await asyncio.wait(self._background_tasks, timeout=2.0)
        if pending:
            logger.warning(
                "Background tasks did not stop within 2s: %s",
                [t.get_name() for t in pending],
            )
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
            # Respect the session's line-ending policy instead of hardcoding
            # \n (a crlf console would treat the bare \n as part of the command).
            endings = {"none": b"", "lf": b"\n", "crlf": b"\r\n", "cr": b"\r"}
            await self._active.device.write(b"reboot" + endings[self._active.line_ending])
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
            # NOT_FOUND (spec): FileNotFoundError maps to that error code.
            raise FileNotFoundError(f"Session not found: {session_id}")
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

    async def export_operation_history(
        self,
        session_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Export the redacted operation audit trail (newest first)."""
        return await self._main_db.export_operation_history(
            session_id=session_id,
            limit=limit,
            offset=offset,
        )

    async def export_session(self, session_id: str, target_path: Path) -> Path:
        sessions = await self._main_db.list_sessions()
        match = [s for s in sessions if s["session_id"] == session_id]
        if not match:
            # NOT_FOUND (spec): FileNotFoundError maps to that error code.
            raise FileNotFoundError(f"Session not found: {session_id}")
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
