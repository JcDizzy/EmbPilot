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
