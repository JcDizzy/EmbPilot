"""
Integration tests for the dual-track database layer (MainDatabase + SessionDatabase).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from embpilot.core.database import MainDatabase, SessionDatabase, _schema_main, _schema_session
from embpilot.core.engine import LogLine
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_main_database_lifecycle():
    """Register a session, insert an operation, close session, verify."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        main = MainDatabase(d / "embpilot_main.db")
        await main.open()

        # Register
        await main.register_session("test-001", "COM3", "serial",
                                     str(d / "sessions" / "test.db"))
        sessions = await main.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["status"] == "active"
        assert sessions[0]["session_id"] == "test-001"

        # Operation
        await main.insert_operation("AI", "call_tool",
                                     {"tool": "send_command"}, session_id="test-001")

        # Close
        await main.end_session("test-001")
        sessions2 = await main.list_sessions()
        assert sessions2[0]["status"] == "closed"

        await main.close()


@pytest.mark.asyncio
async def test_session_database_bulk_insert_and_search():
    """Insert log lines, search, verify analytics."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session = SessionDatabase(d / "session_test.db")
        await session.open()

        lines = [
            LogLine(datetime.now(timezone.utc), f"normal-{i}")
            for i in range(10)
        ]
        lines.append(LogLine(datetime.now(timezone.utc), "ERROR: something broke"))
        lines.append(LogLine(datetime.now(timezone.utc), "panic: kernel oops"))
        await session.bulk_insert_logs(lines, source="serial")

        # Search
        results = await session.search_logs("normal-5")
        assert len(results) == 1
        assert results[0]["text"] == "normal-5"
        assert results[0]["source"] == "serial"
        assert results[0]["level"] == "info"
        assert results[0]["tag"] is None

        # Analytics
        analytics = await session.get_analytics()
        assert len(analytics) >= 2  # ERROR + panic
        assert {row["level"] for row in analytics} >= {"error", "critical"}

        await session.close()


@pytest.mark.asyncio
async def test_main_database_cleanup():
    """Verify that expired session cleanup works."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        main = MainDatabase(d / "embpilot_main.db")
        await main.open()

        # Register a session with a very old timestamp
        old_path = d / "old_session.db"
        old_path.touch()
        await main.register_session("old-001", "OLD", "serial", str(old_path))

        # Manually backdate it (SQLite)
        await main._conn.execute(
            "UPDATE sessions SET started_at = '2020-01-01 00:00:00.000' "
            "WHERE session_id = 'old-001'"
        )
        await main._conn.commit()

        # Run cleanup with max_days=1 => anything older than 1 day is removed
        await main.cleanup_expired_sessions(max_days=1, max_gb=10)

        sessions = await main.list_sessions()
        old_sessions = [s for s in sessions if s["session_id"] == "old-001"]
        # old-001 should be cleaned
        assert len(old_sessions) == 0 or old_sessions[0]["status"] == "cleaned"

        await main.close()


@pytest.mark.asyncio
async def test_schema_loading():
    """Verify schema SQL strings are loadable."""
    assert "CREATE TABLE IF NOT EXISTS sessions" in _schema_main
    assert "CREATE TABLE IF NOT EXISTS operation_history" in _schema_main
    assert "CREATE TABLE IF NOT EXISTS device_logs" in _schema_session


@pytest.mark.asyncio
async def test_main_database_get_session_db_path():
    """Look up a single session's db_path without fetching every session."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        main = MainDatabase(d / "embpilot_main.db")
        await main.open()

        await main.register_session(
            "sess-a", "COM3", "serial", str(d / "sessions" / "a.db")
        )

        assert await main.get_session_db_path("sess-a") == str(d / "sessions" / "a.db")
        assert await main.get_session_db_path("does-not-exist") is None

        await main.close()


@pytest.mark.asyncio
async def test_operation_history_redacts_sensitive_detail():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        main = MainDatabase(d / "embpilot_main.db")
        await main.open()

        await main.insert_operation(
            "AI",
            "connect",
            {
                "config": {
                    "host": "192.0.2.10",
                    "password": "secret",
                    "key_file": "C:/Users/example/.ssh/id_rsa",
                }
            },
            session_id="sess-redact",
        )

        rows = await main.fetch_operation_history(session_id="sess-redact")

        assert len(rows) == 1
        assert "secret" not in rows[0]["detail"]
        assert "id_rsa" not in rows[0]["detail"]
        assert "***REDACTED***" in rows[0]["detail"]

        await main.close()


@pytest.mark.asyncio
async def test_delete_session_refuses_path_outside_allowed_directory():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session_dir = d / "sessions"
        session_dir.mkdir()
        outside_path = d / "outside.db"
        outside_path.touch()
        main = MainDatabase(d / "embpilot_main.db")
        await main.open()
        await main.register_session("outside", "board", "serial", str(outside_path))

        with pytest.raises(ValueError, match="outside managed session directory"):
            await main.delete_session("outside", allowed_dir=session_dir)

        assert outside_path.exists()

        await main.close()


@pytest.mark.asyncio
async def test_delete_session_removes_wal_and_shm_sidecars():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session_dir = d / "sessions"
        session_dir.mkdir()
        db_path = session_dir / "session.db"
        db_path.touch()
        Path(f"{db_path}-wal").touch()
        Path(f"{db_path}-shm").touch()

        main = MainDatabase(d / "embpilot_main.db")
        await main.open()
        await main.register_session("sidecar", "board", "serial", str(db_path))

        await main.delete_session("sidecar", allowed_dir=session_dir)

        assert not db_path.exists()
        assert not Path(f"{db_path}-wal").exists()
        assert not Path(f"{db_path}-shm").exists()

        await main.close()


@pytest.mark.asyncio
async def test_session_database_fetch_logs_is_ordered():
    """fetch_logs returns rows in insertion order with limit/offset paging."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session = SessionDatabase(d / "session_test.db")
        await session.open()

        lines = [LogLine(datetime.now(timezone.utc), f"line-{i}") for i in range(5)]
        await session.bulk_insert_logs(lines, source="serial")

        fetched = await session.fetch_logs()
        assert [r["text"] for r in fetched] == [f"line-{i}" for i in range(5)]
        assert all(set(r) == {"timestamp", "source", "level", "tag", "text"} for r in fetched)

        paged = await session.fetch_logs(limit=2, offset=1)
        assert [r["text"] for r in paged] == ["line-1", "line-2"]

        await session.close()


@pytest.mark.asyncio
async def test_session_database_extracts_level_and_tag_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session = SessionDatabase(d / "session_test.db")
        await session.open()

        await session.bulk_insert_logs(
            [
                LogLine(datetime.now(timezone.utc), "[WIFI] ERROR: link down"),
                LogLine(datetime.now(timezone.utc), "[BOOT] warning: fallback"),
            ],
            source="serial",
        )

        rows = await session.fetch_logs()

        assert rows[0]["level"] == "error"
        assert rows[0]["tag"] == "WIFI"
        assert rows[1]["level"] == "warning"
        assert rows[1]["tag"] == "BOOT"

        await session.close()


@pytest.mark.asyncio
async def test_session_database_analytics_uses_configurable_patterns():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session = SessionDatabase(d / "session_test.db")
        await session.open()

        await session.bulk_insert_logs(
            [
                LogLine(datetime.now(timezone.utc), "brownout detected"),
                LogLine(datetime.now(timezone.utc), "all good"),
            ],
            source="serial",
        )

        analytics = await session.get_analytics(patterns=["brownout"])

        assert len(analytics) == 1
        assert analytics[0]["text"] == "brownout detected"

        await session.close()


@pytest.mark.asyncio
async def test_session_database_migrates_legacy_log_table_to_metadata_and_fts():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        db_path = d / "legacy.db"
        import aiosqlite

        conn = await aiosqlite.connect(db_path)
        await conn.execute(
            "CREATE TABLE device_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, "
            "source TEXT NOT NULL, "
            "text TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO device_logs (timestamp, source, text) VALUES (?, ?, ?)",
            ("2026-07-07 00:00:00.000", "serial", "legacy ERROR"),
        )
        await conn.commit()
        await conn.close()

        session = SessionDatabase(db_path)
        await session.open()

        results = await session.search_logs("legacy")

        assert len(results) == 1
        assert results[0]["level"] == "info"
        assert results[0]["tag"] is None

        await session.close()


@pytest.mark.asyncio
async def test_session_database_skips_fts_rebuild_when_counts_match(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session = SessionDatabase(d / "session_test.db")
        await session.open()
        await session.bulk_insert_logs(
            [LogLine(datetime.now(timezone.utc), "ERROR: once")],
            source="serial",
        )
        await session.close()

        reopened = SessionDatabase(d / "session_test.db")

        async def fail_rebuild() -> None:
            raise AssertionError("FTS rebuild should not run when counts match")

        monkeypatch.setattr(reopened, "_rebuild_fts", fail_rebuild)
        await reopened.open()
        await reopened.close()


@pytest.mark.asyncio
async def test_session_database_rebuilds_stale_external_content_fts_index():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        db_path = d / "stale_fts.db"
        import aiosqlite

        conn = await aiosqlite.connect(db_path)
        await conn.execute(
            "CREATE TABLE device_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, "
            "source TEXT NOT NULL, "
            "level TEXT NOT NULL DEFAULT 'info', "
            "tag TEXT, "
            "text TEXT NOT NULL)"
        )
        await conn.execute(
            "CREATE VIRTUAL TABLE device_logs_fts "
            "USING fts5(text, content='device_logs', content_rowid='id')"
        )
        await conn.execute(
            "INSERT INTO device_logs (timestamp, source, text) VALUES (?, ?, ?)",
            ("2026-07-07 00:00:00.000", "serial", "stale ERROR token"),
        )
        await conn.commit()
        await conn.close()

        session = SessionDatabase(db_path)
        await session.open()

        results = await session.search_logs("stale")

        assert len(results) == 1
        assert results[0]["text"] == "stale ERROR token"

        await session.close()
