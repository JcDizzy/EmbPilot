"""Session manager integration tests without physical hardware."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from embpilot.config import EmbPilotConfig
from embpilot.drivers.base import BaseDevice
from embpilot.server import SessionManager


class EchoDevice(BaseDevice):
    def __init__(self) -> None:
        super().__init__()
        self.reader = asyncio.StreamReader()
        self.writes: list[bytes] = []

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self.reader.feed_eof()
        self._connected = False

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        self.reader.feed_data(b"status: ready\n")

    def get_reader(self) -> asyncio.StreamReader:
        return self.reader


class PromptDevice(EchoDevice):
    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        self.reader.feed_data(b"login:")


class StreamDevice(EchoDevice):
    """Device that emits boot lines shortly after connecting, on its own."""

    def __init__(self, delay_s: float = 0.2) -> None:
        super().__init__()
        self._delay_s = delay_s

    async def connect(self) -> None:
        self._connected = True
        loop = asyncio.get_running_loop()
        loop.call_later(self._delay_s, self._emit_boot_lines)

    def _emit_boot_lines(self) -> None:
        self.reader.feed_data(b"boot: starting\nboot: ready\n")


@pytest.mark.asyncio
async def test_read_output_observes_device_stream_without_writing(tmp_path: Path) -> None:
    device = StreamDevice()
    config = EmbPilotConfig(data_dir=tmp_path, framing_timeout_ms=20)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _interface, _config: device)
    await manager.start()
    try:
        await manager.connect_device("serial", {"port": "COM3"})

        result = await manager.read_output(duration_ms=2000, expect_regex="ready")

        assert device.writes == []  # read_output must never write to the device
        assert result.matched is True
        assert result.timed_out is False
        assert "boot: ready" in result.output
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_read_output_times_out_without_expect_match(tmp_path: Path) -> None:
    device = StreamDevice()
    config = EmbPilotConfig(data_dir=tmp_path, framing_timeout_ms=20)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _interface, _config: device)
    await manager.start()
    try:
        await manager.connect_device("serial", {"port": "COM3"})

        result = await manager.read_output(duration_ms=2000, expect_regex="never-appears")

        assert result.matched is False
        assert result.timed_out is True
        assert "boot: starting" in result.output  # collected everything in the window
        assert "boot: ready" in result.output
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_read_output_requires_active_connection(tmp_path: Path) -> None:
    config = EmbPilotConfig(data_dir=tmp_path)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _interface, _config: EchoDevice())
    await manager.start()
    try:
        with pytest.raises(Exception) as excinfo:
            await manager.read_output(duration_ms=100)
        assert "No active device connection" in str(excinfo.value)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_session_manager_executes_command_through_transport(tmp_path: Path) -> None:
    device = EchoDevice()
    config = EmbPilotConfig(data_dir=tmp_path)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _interface, _config: device)
    await manager.start()
    try:
        await manager.connect_device(
            "serial",
            {"port": "COM3", "line_ending": "crlf"},
        )

        result = await manager.send_command(
            "status",
            expect_regex="ready",
            timeout_ms=1000,
        )

        assert device.writes == [b"status\r\n"]
        assert result.matched is True
        assert "status: ready" in result.output
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_session_manager_captures_prompt_without_newline(tmp_path: Path) -> None:
    device = PromptDevice()
    config = EmbPilotConfig(data_dir=tmp_path, framing_timeout_ms=20)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _interface, _config: device)
    await manager.start()
    try:
        await manager.connect_device("serial", {"port": "COM3"})

        result = await manager.send_command(
            "",
            expect_regex="login:",
            timeout_ms=500,
        )

        assert result.matched is True
        assert "login:" in result.output
    finally:
        await manager.shutdown()
