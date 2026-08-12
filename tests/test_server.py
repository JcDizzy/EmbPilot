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



def test_prompt_catalog_includes_new_scenario_templates() -> None:
    from embpilot.server import build_prompts

    names = {prompt.name for prompt in build_prompts()}
    assert {
        "connect_and_explore",
        "capture_boot_log",
        "diagnose_connection",
        "design_expect",
        "session_handoff",
    } <= names


def test_prompt_texts_contain_actionable_tool_sequences() -> None:
    from embpilot.server import render_prompt

    boot = render_prompt("capture_boot_log").messages[0].content.text
    assert "reset_target" in boot
    assert "read_output" in boot

    explore = render_prompt("connect_and_explore", {"interface": "ssh"}).messages[0].content.text
    assert "connect_ssh" in explore

    expect = render_prompt("design_expect").messages[0].content.text
    assert "expect_regex" in expect
    assert "read_output" in expect

    with pytest.raises(ValueError):
        render_prompt("bogus_prompt")


@pytest.mark.asyncio
async def test_search_history_logs_by_session_id_after_disconnect(
    tmp_path: Path,
) -> None:
    """A closed session's logs are searchable without an active connection."""
    from embpilot.core.database import MainDatabase, SessionDatabase
    from embpilot.core.engine import LogLine
    from datetime import datetime, timezone

    device = EchoDevice()
    config = EmbPilotConfig(data_dir=tmp_path)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _i, _c: device)
    await manager.start()
    session_id: str | None = None
    try:
        session_id = await manager.connect_device("serial", {"port": "COM3"})
        # Push some log lines into the session database through the pipeline.
        ring = manager.active_ring
        assert ring is not None
        from embpilot.core.engine import LogProducer

        queue: asyncio.Queue = asyncio.Queue()
        producer = LogProducer(device.get_reader(), queue, ring, framing_timeout_ms=20)
        task = asyncio.create_task(producer.run())
        device.reader.feed_data(b"error: sdio timeout\nboot: ok\n")
        await asyncio.sleep(0.2)
        task.cancel()
        await manager.disconnect_device()

        # No active connection now; searching by session_id must still work.
        rows = await manager.search_history_logs(keyword="sdio", session_id=session_id)
        assert len(rows) == 1
        assert "sdio timeout" in rows[0]["text"]

        with pytest.raises(ValueError):
            await manager.search_history_logs(
                keyword="x", session_id="no-such-session"
            )
    finally:
        await manager.shutdown()


def test_resource_catalog_includes_session_info() -> None:
    from embpilot.server import build_resources

    uris = {str(resource.uri) for resource in build_resources()}
    assert "device://session_info" in uris
    assert "device://live_log" in uris


@pytest.mark.asyncio
async def test_session_info_resource_reports_active_session(tmp_path: Path) -> None:
    from embpilot.server import render_resource

    device = EchoDevice()
    config = EmbPilotConfig(data_dir=tmp_path)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _i, _c: device)
    await manager.start()
    try:
        session_id = await manager.connect_device("serial", {"port": "COM3"})
        import json

        from mcp.types import AnyUrl

        payload = await render_resource(manager, AnyUrl("device://session_info"))
        info = json.loads(payload)
        assert info["session_id"] == session_id
        assert info["ring_buffer_lines"] == 0
        assert info["stored_log_rows"] == 0
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_session_info_without_connection_is_graceful(tmp_path: Path) -> None:
    from embpilot.server import render_resource

    from mcp.types import AnyUrl

    config = EmbPilotConfig(data_dir=tmp_path)
    config.ensure_data_dirs()
    manager = SessionManager(config, device_factory=lambda _i, _c: EchoDevice())
    await manager.start()
    try:
        payload = await render_resource(manager, AnyUrl("device://session_info"))
        assert payload == "No active device connection."
    finally:
        await manager.shutdown()
