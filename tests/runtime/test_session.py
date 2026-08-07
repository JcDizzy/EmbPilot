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


def test_send_command_applies_line_ending_strategy(tmp_path, monkeypatch):
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
                manager.send_command(
                    "status\n",
                    expect_regex=r"ready",
                    timeout_ms=500,
                    line_ending="crlf",
                )
            )
            await asyncio.sleep(0)
            fake.emit_line("ready")

            await task

            assert fake.writes == [b"status\r\n"]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_rejects_unknown_line_ending(tmp_path, monkeypatch):
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
            await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(ValueError, match="Unsupported line ending"):
                await manager.send_command("status", line_ending="newline")

            assert fake.writes == []
            assert len(manager._expect._windows) == 0
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_rejects_empty_encoded_payload_before_device_write(
    tmp_path, monkeypatch
):
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
            await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(ValueError, match="empty payload"):
                await manager.send_command("", line_ending="as-is")

            assert fake.writes == []
            assert len(manager._expect._windows) == 0
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_allows_blank_line_when_line_ending_produces_bytes(
    tmp_path, monkeypatch
):
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
                manager.send_command(
                    "",
                    expect_regex=r"ready",
                    timeout_ms=500,
                    line_ending="lf",
                )
            )
            await asyncio.sleep(0)
            fake.emit_line("ready")
            await task

            assert fake.writes == [b"\n"]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_requires_confirmation_for_dangerous_command(tmp_path, monkeypatch):
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
            await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(PermissionError, match="Dangerous command"):
                await manager.send_command("reboot\n")

            assert fake.writes == []
            assert len(manager._expect._windows) == 0
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_allows_confirmed_dangerous_command(tmp_path, monkeypatch):
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
                manager.send_command(
                    "reboot",
                    expect_regex=r"rebooting",
                    timeout_ms=500,
                    line_ending="lf",
                    confirm_dangerous_command=True,
                )
            )
            await asyncio.sleep(0)
            fake.emit_line("rebooting")

            await task

            assert fake.writes == [b"reboot\n"]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_rejects_timeout_above_configured_cap(tmp_path, monkeypatch):
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
            command_timeout_max_ms=100,
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(ValueError, match="timeout_ms exceeds"):
                await manager.send_command("status", timeout_ms=101)

            assert fake.writes == []
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


def test_reset_target_reboot_requires_confirmation(tmp_path, monkeypatch):
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
            await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(PermissionError, match="confirm=true"):
                await manager.reset_target()

            assert fake.writes == []

            message = await manager.reset_target(confirm=True)

            assert message == "Reset command sent (reboot)."
            assert fake.writes == [b"reboot\n"]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_reset_target_rejects_unsupported_method(tmp_path, monkeypatch):
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
            await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(ValueError):
                await manager.reset_target(method="dtr")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_reset_target_requires_active_connection(tmp_path):
    async def scenario() -> None:
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            with pytest.raises(RuntimeError, match="No active device connection"):
                await manager.reset_target()
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_list_sessions_returns_recorded_sessions(tmp_path, monkeypatch):
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
            await manager.connect_device("serial", {"port": "COM9"})

            sessions = await manager.list_sessions()

            assert len(sessions) == 1
            assert sessions[0]["interface"] == "serial"
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_delete_session_refuses_active_session(tmp_path, monkeypatch):
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
            session_id = await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(RuntimeError, match="active session"):
                await manager.delete_session(session_id)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_delete_session_requires_confirmation_for_historical_session(tmp_path):
    async def scenario() -> None:
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            session_path = tmp_path / "sessions" / "hist-delete.db"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.touch()
            await manager._main_db.register_session(
                "hist-delete", "board-x", "serial", str(session_path)
            )

            with pytest.raises(PermissionError, match="confirm=true"):
                await manager.delete_session("hist-delete")

            assert session_path.exists()

            await manager.delete_session("hist-delete", confirm=True)

            assert not session_path.exists()
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_search_and_export_work_on_historical_session(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from embpilot.core.database import SessionDatabase
    from embpilot.core.engine import LogLine

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
            # record a historical session directly, then leave it closed so the
            # active-session shortcut in _open_session_db does NOT fire
            hist_path = tmp_path / "sessions" / "hist.db"
            hist_db = SessionDatabase(hist_path)
            await hist_db.open()
            await hist_db.bulk_insert_logs(
                [
                    LogLine(datetime.now(timezone.utc), "boot ok"),
                    LogLine(datetime.now(timezone.utc), "ERROR: boom"),
                ],
                source="serial",
            )
            await hist_db.close()
            await manager._main_db.register_session(
                "hist-1", "board-x", "serial", str(hist_path)
            )

            results = await manager.search_session_logs("hist-1", "boom")
            assert len(results) == 1
            assert "boom" in results[0]["text"]

            exported = await manager.export_session("hist-1", fmt="text")
            assert "boot ok" in exported
            assert "boom" in exported

            as_json = await manager.export_session("hist-1", fmt="json")
            assert "boom" in as_json
            assert "boot ok" in as_json
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_search_and_export_limits_are_capped(tmp_path):
    async def scenario() -> None:
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
            search_limit_max=2,
            export_limit_max=2,
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            with pytest.raises(ValueError, match="limit exceeds"):
                await manager.search_session_logs("missing", "x", limit=3)
            with pytest.raises(ValueError, match="limit exceeds"):
                await manager.export_session("missing", limit=3)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_export_operation_history_returns_redacted_json(tmp_path, monkeypatch):
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
            await manager.connect_device(
                "ssh",
                {
                    "host": "192.0.2.10",
                    "password": "secret",
                    "key_file": "C:/Users/example/.ssh/id_rsa",
                },
            )

            exported = await manager.export_operation_history()

            assert "secret" not in exported
            assert "id_rsa" not in exported
            assert "***REDACTED***" in exported
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_send_command_audit_redacts_inline_secrets(tmp_path, monkeypatch):
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
                manager.send_command(
                    'AT+CWJAP="lab","wifi_secret" token=abc123 '
                    'Authorization: Bearer ey.secret password plain_secret',
                    expect_regex=r"OK",
                    timeout_ms=500,
                )
            )
            await asyncio.sleep(0)
            fake.emit_line("OK")

            await task
            exported = await manager.export_operation_history()

            assert "wifi_secret" not in exported
            assert "abc123" not in exported
            assert "ey.secret" not in exported
            assert "plain_secret" not in exported
            assert "***REDACTED***" in exported
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_search_session_logs_raises_for_unknown_session(tmp_path):
    async def scenario() -> None:
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            with pytest.raises(KeyError):
                await manager.search_session_logs("nope", "anything")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_open_session_db_reuses_active_connection(tmp_path, monkeypatch):
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
            await manager.connect_device("serial", {"port": "COM9"})
            active_id = manager.get_session_info().session_id
            conn_before = manager._session_db._conn

            # search/export on the ACTIVE session must short-circuit and reuse
            # the live connection rather than opening/closing a historical one
            await manager.search_session_logs(active_id, "anything")
            await manager.export_session(active_id, fmt="json")

            assert manager._session_db is not None
            assert manager._session_db._conn is conn_before
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_get_analytics_returns_empty_without_active_session(tmp_path):
    async def scenario() -> None:
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            assert await manager.get_analytics() == []
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_get_analytics_aggregates_active_session_errors(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from embpilot.core.engine import LogLine

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
            await manager.connect_device("serial", {"port": "COM9"})
            await manager._session_db.bulk_insert_logs(
                [
                    LogLine(datetime.now(timezone.utc), "ERROR: boom"),
                    LogLine(datetime.now(timezone.utc), "ERROR: boom"),
                    LogLine(datetime.now(timezone.utc), "all good here"),
                ],
                source="serial",
            )

            analytics = await manager.get_analytics()

            boom = next(a for a in analytics if "boom" in a["text"])
            assert boom["cnt"] >= 2
            assert all("all good" not in a["text"] for a in analytics)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_search_session_logs_flushes_pending_batch_for_active(tmp_path, monkeypatch):
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
            active_id = manager.get_session_info().session_id

            # feed a unique marker that lands in the DbSink batch (< batch_size)
            # and is NOT yet in the db (periodic flush is 1s away)
            fake.emit_line("UNIQUE_MARKER_xyz")
            await asyncio.sleep(0.05)

            results = await manager.search_session_logs(active_id, "UNIQUE_MARKER_xyz")
            assert any("UNIQUE_MARKER_xyz" in r["text"] for r in results)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
