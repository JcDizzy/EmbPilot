from __future__ import annotations

import asyncio
from contextlib import suppress
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from embpilot.config import EmbPilotConfig
from embpilot.core.database import MainDatabase, SessionDatabase
from embpilot.drivers.base import BaseDevice
from embpilot.runtime.expect import ExpectManager
from embpilot.runtime.models import LogLine, RingBuffer, SessionInfo
from embpilot.runtime.pipeline import DbSink, LogProducer, RingBufferSink, SessionDispatcher


def build_device(interface_type: str, config: dict[str, Any]) -> BaseDevice:
    if interface_type == "serial":
        from embpilot.drivers.serial_dev import SerialDevice

        return SerialDevice(
            port=config["port"],
            baudrate=config.get("baudrate", 115200),
            bytesize=config.get("bytesize", 8),
            parity=config.get("parity", "N"),
            stopbits=config.get("stopbits", 1),
            timeout=config.get("timeout", 5.0),
        )
    if interface_type == "telnet":
        from embpilot.drivers.telnet_dev import TelnetDevice

        return TelnetDevice(
            host=config["host"],
            port=config.get("port", 23),
            timeout=config.get("timeout", 10.0),
        )
    if interface_type == "ssh":
        from embpilot.drivers.ssh_dev import SshDevice

        return SshDevice(
            host=config["host"],
            port=config.get("port", 22),
            username=config.get("username", ""),
            password=config.get("password"),
            key_file=config.get("key_file"),
            known_hosts=config.get("known_hosts"),
        )
    raise ValueError(f"Unsupported interface: {interface_type}")


class SessionManager:
    def __init__(self, config: EmbPilotConfig) -> None:
        self._config = config
        self._main_db = MainDatabase(config.main_db_path)
        self._expect = ExpectManager()
        self._command_lock = asyncio.Lock()
        self._session_info: SessionInfo | None = None
        self._ring: RingBuffer | None = None
        self._dispatcher: SessionDispatcher | None = None
        self._device: BaseDevice | None = None
        self._producer: LogProducer | None = None
        self._producer_task: asyncio.Task[None] | None = None
        self._session_db: SessionDatabase | None = None

    async def start(self) -> None:
        self._config.ensure_data_dirs()
        await self._main_db.open()
        await self._main_db.cleanup_expired_sessions(
            max_days=self._config.retention_days,
            max_gb=self._config.retention_max_gb,
        )

    async def shutdown(self) -> None:
        await self.disconnect_device()
        await self._main_db.close()

    async def connect_device(self, interface_type: str, config: dict[str, Any]) -> str:
        await self.disconnect_device()

        device = build_device(interface_type, config)
        await device.connect()

        session_id = uuid.uuid4().hex[:16]
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

        ring = RingBuffer()
        dispatcher = SessionDispatcher(
            [
                RingBufferSink(ring),
                DbSink(session_db, source=interface_type),
                _SessionInfoSink(lambda: self._session_info),
                _ExpectSink(self._expect),
            ]
        )
        producer = LogProducer(
            reader=device.get_reader(),
            dispatcher=dispatcher,
            framing_timeout_ms=self._config.framing_timeout_ms,
        )

        self._device = device
        self._session_db = session_db
        self._ring = ring
        self._dispatcher = dispatcher
        self._producer = producer
        self._session_info = SessionInfo(
            session_id=session_id,
            interface_type=interface_type,
            device_name=device_name,
            connection_summary=f"{interface_type}://{device_name}",
            started_at=datetime.now(timezone.utc),
            state="active",
            last_log_at=None,
            log_count=0,
        )
        self._producer_task = asyncio.create_task(producer.run())

        await self._main_db.insert_operation(
            actor="System",
            action_type="connect",
            detail={"session_id": session_id, "device": device_name, "interface": interface_type},
            session_id=session_id,
        )
        return session_id

    async def send_command(
        self,
        command: str,
        expect_regex: str | None = None,
        timeout_ms: int = 5000,
    ) -> str:
        async with self._command_lock:
            if self._device is None:
                raise RuntimeError("No active device connection")

            window = self._expect.open_window(expect_regex=expect_regex, timeout_ms=timeout_ms)
            try:
                await self._device.write(command.encode("utf-8"))
            except Exception:
                self._expect.cancel_window(window)
                raise
            lines = await window.wait()

            if self._session_info is not None:
                await self._main_db.insert_operation(
                    actor="AI",
                    action_type="call_tool",
                    detail={"tool": "send_command", "command": command, "expect": expect_regex},
                    session_id=self._session_info.session_id,
                )

            formatted = "\n".join(line.formatted() for line in lines)
            return formatted or "(no output captured)"

    async def reset_target(self, method: str = "reboot") -> str:
        if method != "reboot":
            raise ValueError(
                f"Unsupported reset method: {method!r} (only 'reboot' is supported)"
            )
        async with self._command_lock:
            if self._device is None:
                raise RuntimeError("No active device connection")
            # reset_target sends a fixed, complete reboot instruction including the
            # line terminator; send_command instead leaves termination to the caller.
            await self._device.write(b"reboot\n")
            if self._session_info is not None:
                await self._main_db.insert_operation(
                    actor="AI",
                    action_type="call_tool",
                    detail={"tool": "reset_target", "method": method},
                    session_id=self._session_info.session_id,
                )
            return "Reset command sent (reboot)."

    async def disconnect_device(self) -> None:
        if self._device is None:
            return

        device = self._device
        session_info = self._session_info
        session_db = self._session_db
        dispatcher = self._dispatcher
        producer_task = self._producer_task

        self._device = None
        self._session_db = None
        self._dispatcher = None
        self._producer = None
        self._producer_task = None
        self._ring = None
        self._session_info = None

        try:
            await device.disconnect()
        finally:
            try:
                if producer_task is not None:
                    if producer_task.done():
                        with suppress(asyncio.CancelledError, Exception):
                            producer_task.result()
                    else:
                        producer_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await producer_task
            finally:
                self._expect.close()
                self._expect = ExpectManager()
                if dispatcher is not None:
                    await dispatcher.close()
                if session_db is not None:
                    await session_db.close()
                if session_info is not None:
                    await self._main_db.end_session(session_info.session_id)
                    await self._main_db.insert_operation(
                        actor="System",
                        action_type="disconnect",
                        detail={"session_id": session_info.session_id},
                        session_id=session_info.session_id,
                    )

    def get_session_info(self) -> SessionInfo:
        if self._session_info is None:
            raise RuntimeError("No active session")
        return self._session_info

    def active_ring(self) -> RingBuffer | None:
        return self._ring

    def _resolve_session_path(self, session_id: str, device_name: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name = _sanitize_device_name(device_name)
        return self._config.session_data_dir / f"session_{stamp}_{safe_name}_{session_id[:8]}.db"


def _device_display_name(interface_type: str, config: dict[str, Any]) -> str:
    explicit_name = config.get("device_name")
    if explicit_name:
        return str(explicit_name)
    if interface_type == "serial":
        return str(config.get("port", "unknown"))
    host = str(config.get("host", "unknown"))
    port = config.get("port")
    return f"{host}:{port}" if port is not None else host


def _sanitize_device_name(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name).strip("_") or "device"


class _SessionInfoSink:
    def __init__(self, info_factory: Callable[[], SessionInfo | None]) -> None:
        self._info_factory = info_factory

    async def write(self, line: LogLine) -> None:
        info = self._info_factory()
        if info is None:
            return
        info.last_log_at = line.timestamp
        info.log_count += 1

    async def close(self) -> None:
        return None


class _ExpectSink:
    def __init__(self, manager: ExpectManager) -> None:
        self._manager = manager

    async def write(self, line: LogLine) -> None:
        self._manager.handle(line)

    async def close(self) -> None:
        self._manager.close()
