from __future__ import annotations

import asyncio

import pytest

from embpilot.config import EmbPilotConfig
from embpilot.runtime.session import SessionManager


class _FakeDevice:
    def __init__(self) -> None:
        self._reader = asyncio.StreamReader()
        self.writes: list[bytes] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self._reader.feed_eof()

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    def get_reader(self) -> asyncio.StreamReader:
        return self._reader

    def emit_line(self, text: str) -> None:
        self._reader.feed_data(f"{text}\n".encode("utf-8"))


class _BlockingReader:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return b""


class _NonClosingDevice:
    def __init__(self) -> None:
        self._reader = _BlockingReader()
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def write(self, data: bytes) -> None:
        return None

    def get_reader(self) -> _BlockingReader:
        return self._reader


class _WriteFailDevice(_FakeDevice):
    async def write(self, data: bytes) -> None:
        raise OSError("boom")


class _FailingReader:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        self.started.set()
        raise RuntimeError("reader boom")


class _ProducerFailDevice:
    def __init__(self) -> None:
        self._reader = _FailingReader()
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def write(self, data: bytes) -> None:
        return None

    def get_reader(self) -> _FailingReader:
        return self._reader


def test_session_manager_prefers_explicit_device_name(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _FakeDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )

        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            session_id = await manager.connect_device(
                "serial",
                {"port": "COM9", "device_name": "board-b"},
            )
            info = manager.get_session_info()

            assert session_id == info.session_id
            assert info.device_name == "board-b"
            assert info.connection_summary == "serial://board-b"
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_returns_window_until_expect_match(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _FakeDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )

        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
            framing_timeout_ms=5,
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            await manager.connect_device("serial", {"port": "COM9"})

            task = asyncio.create_task(
                manager.send_command("status\n", expect_regex=r"ready", timeout_ms=500)
            )
            await asyncio.sleep(0)
            fake.emit_line("booting")
            fake.emit_line("ready")
            fake.emit_line("after-match")

            result = await task

            assert "booting" in result
            assert "ready" in result
            assert "after-match" not in result
            assert result.count("[") == 2
            assert fake.writes == [b"status\n"]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_disconnect_and_shutdown_do_not_hang_when_reader_stays_open(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _NonClosingDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )

        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
            framing_timeout_ms=5,
        )
        manager = SessionManager(config)
        await manager.start()
        await manager.connect_device("serial", {"port": "COM9"})
        await asyncio.wait_for(fake._reader.started.wait(), timeout=0.2)

        await asyncio.wait_for(manager.disconnect_device(), timeout=0.2)
        await asyncio.wait_for(fake._reader.cancelled.wait(), timeout=0.2)
        await asyncio.wait_for(manager.shutdown(), timeout=0.2)

    asyncio.run(scenario())


def test_send_command_cleans_up_window_when_write_fails(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _WriteFailDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )

        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(OSError, match="boom"):
                await manager.send_command("status\n", expect_regex=r"ready", timeout_ms=500)

            assert len(manager._expect._windows) == 0
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_disconnect_and_shutdown_ignore_stale_background_producer_failure(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _ProducerFailDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )

        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
            framing_timeout_ms=5,
        )
        manager = SessionManager(config)
        await manager.start()
        await manager.connect_device("serial", {"port": "COM9"})
        await asyncio.wait_for(fake._reader.started.wait(), timeout=0.2)
        await asyncio.sleep(0)

        await asyncio.wait_for(manager.disconnect_device(), timeout=0.2)
        await asyncio.wait_for(manager.shutdown(), timeout=0.2)

    asyncio.run(scenario())


def test_overlapping_send_command_calls_do_not_mix_outputs(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _FakeDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )

        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
            framing_timeout_ms=5,
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            await manager.connect_device("serial", {"port": "COM9"})

            first_task = asyncio.create_task(
                manager.send_command("first\n", expect_regex=r"first done", timeout_ms=500)
            )
            await asyncio.sleep(0)

            second_task = asyncio.create_task(
                manager.send_command("second\n", expect_regex=r"second done", timeout_ms=500)
            )
            await asyncio.sleep(0)

            fake.emit_line("first output")
            fake.emit_line("first done")
            first_result = await first_task

            fake.emit_line("second output")
            fake.emit_line("second done")
            second_result = await second_task

            assert "first output" in first_result
            assert "first done" in first_result
            assert "second output" not in first_result

            assert "second output" in second_result
            assert "second done" in second_result
            assert "first output" not in second_result
            assert "first done" not in second_result
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
