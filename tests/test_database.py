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

        # Analytics
        analytics = await session.get_analytics()
        assert len(analytics) >= 2  # ERROR + panic

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
        assert all(set(r) == {"timestamp", "source", "text"} for r in fetched)

        paged = await session.fetch_logs(limit=2, offset=1)
        assert [r["text"] for r in paged] == ["line-1", "line-2"]

        await session.close()
