"""
Database layer — dual-track SQLite with WAL mode.

MainDatabase (embpilot_main.db)
  Persistent, low-frequency: sessions index + operation_history.

SessionDatabase (session_<ts>_<device>.db)
  Per-connection, high-frequency: device_logs with batch ingest.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from embpilot.runtime.models import LogLine
from embpilot.runtime.safety import ensure_path_within, redact_sensitive

logger = logging.getLogger(__name__)

# ── DDL resources ────────────────────────────────────────────────────────────

_schema_main: str = (Path(__file__).parent / "schema_main.sql").read_text("utf-8")
_schema_session: str = (Path(__file__).parent / "schema_session.sql").read_text("utf-8")


# ── Shared helpers ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}"


# ── MainDatabase ─────────────────────────────────────────────────────────────

class MainDatabase:
    """Central persistent database — session registry + operation history.

    Opens ``embpilot_main.db`` at startup and keeps it open for the
    lifetime of the MCP server.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(_schema_main)
        await self._conn.commit()
        logger.info("Main database opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Sessions ─────────────────────────────────────────────────────

    async def register_session(
        self,
        session_id: str,
        device_name: str,
        interface: str,
        db_path: str,
    ) -> None:
        """Insert a new session record (status='active')."""
        if self._conn is None:
            return
        await self._conn.execute(
            "INSERT INTO sessions (session_id, device_name, interface, started_at, db_path, status) "
            "VALUES (?, ?, ?, ?, ?, 'active')",
            (session_id, device_name, interface, _now_iso(), db_path),
        )
        await self._conn.commit()

    async def end_session(self, session_id: str) -> None:
        """Mark a session as closed and update its file size."""
        if self._conn is None:
            return
        now = _now_iso()
        # Get db_path and check file size
        cursor = await self._conn.execute(
            "SELECT db_path FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        file_size = 0
        if row:
            p = Path(row["db_path"])
            if p.exists():
                file_size = p.stat().st_size

        await self._conn.execute(
            "UPDATE sessions SET ended_at=?, status='closed', file_size=? "
            "WHERE session_id=?",
            (now, file_size, session_id),
        )
        await self._conn.commit()

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return all session records, newest first."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT session_id, device_name, interface, started_at, ended_at, "
            "       db_path, file_size, status "
            "FROM sessions ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_session(
        self, session_id: str, allowed_dir: Path | None = None
    ) -> None:
        """Physically delete the session db file and remove the index entry."""
        if self._conn is None:
            return
        cursor = await self._conn.execute(
            "SELECT db_path FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        if row:
            p = Path(row["db_path"])
            if allowed_dir is not None:
                p = ensure_path_within(p, allowed_dir)
            if p.exists():
                p.unlink()
                logger.info("Deleted session file %s", p)
        await self._conn.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        )
        await self._conn.commit()

    async def get_session_db_path(self, session_id: str) -> Optional[str]:
        """Return the db_path for a session, or None if not found."""
        if self._conn is None:
            return None
        cursor = await self._conn.execute(
            "SELECT db_path FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row["db_path"] if row else None

    # ── Operation history ────────────────────────────────────────────

    async def insert_operation(
        self,
        actor: str,
        action_type: str,
        detail: dict[str, Any],
        session_id: Optional[str] = None,
    ) -> None:
        if self._conn is None:
            return
        detail = redact_sensitive(detail)
        await self._conn.execute(
            "INSERT INTO operation_history (timestamp, session_id, actor, action_type, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), session_id, actor, action_type, json.dumps(detail, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def fetch_operation_history(
        self,
        session_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        query = (
            "SELECT timestamp, session_id, actor, action_type, detail "
            "FROM operation_history"
        )
        params: list[Any] = []
        if session_id is not None:
            query += " WHERE session_id = ?"
            params.append(session_id)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── Cleanup ──────────────────────────────────────────────────────

    async def cleanup_expired_sessions(
        self,
        max_days: int = 30,
        max_gb: int = 5,
        allowed_dir: Path | None = None,
    ) -> None:
        """Auto-delete sessions exceeding retention thresholds.

        Called once at server startup.
        """
        if self._conn is None:
            return
        deleted = 0

        # 1. Remove sessions older than max_days
        cursor = await self._conn.execute(
            "SELECT session_id, db_path FROM sessions "
            "WHERE started_at < datetime('now', ?)",
            (f"-{max_days} days",),
        )
        old_sessions = await cursor.fetchall()
        for row in old_sessions:
            p = Path(row["db_path"])
            if allowed_dir is not None:
                p = ensure_path_within(p, allowed_dir)
            if p.exists():
                p.unlink()
            await self._conn.execute(
                "UPDATE sessions SET status='cleaned' WHERE session_id=?",
                (row["session_id"],),
            )
            deleted += 1

        # 2. If total size exceeds max_gb, remove oldest until under limit
        total_size = 0
        cursor = await self._conn.execute(
            "SELECT session_id, db_path, file_size FROM sessions "
            "WHERE status IN ('active','closed') ORDER BY started_at ASC"
        )
        remaining = await cursor.fetchall()
        for row in remaining:
            total_size += row["file_size"]

        max_bytes = max_gb * 1024**3
        if total_size > max_bytes:
            for row in remaining:
                if total_size <= max_bytes:
                    break
                p = Path(row["db_path"])
                if allowed_dir is not None:
                    p = ensure_path_within(p, allowed_dir)
                if p.exists():
                    total_size -= row["file_size"]
                    p.unlink()
                await self._conn.execute(
                    "UPDATE sessions SET status='cleaned' WHERE session_id=?",
                    (row["session_id"],),
                )
                deleted += 1

        if deleted > 0:
            await self._conn.commit()
            logger.info("Cleaned up %d old/excess session(s)", deleted)


# ── SessionDatabase ──────────────────────────────────────────────────────────

class SessionDatabase:
    """Per-session database — high-frequency device log storage.

    Each session gets its own ``session_<ts>_<device>.db`` file under the
    configured session data directory.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_schema_session)
        await self._conn.commit()
        logger.info("Session database opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            # Flush any remaining WAL
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self._conn.commit()
            await self._conn.close()
            self._conn = None
            logger.info("Session database closed at %s", self._db_path)

    async def bulk_insert_logs(
        self, lines: list[LogLine], source: str = "serial"
    ) -> None:
        """Insert a batch of log lines."""
        if not lines or self._conn is None:
            return
        rows = [
            (line.timestamp.isoformat(" "), source, line.text)
            for line in lines
        ]
        await self._conn.executemany(
            "INSERT INTO device_logs (timestamp, source, text) VALUES (?, ?, ?)",
            rows,
        )
        await self._conn.commit()

    async def search_logs(
        self,
        keyword: str,
        time_window_seconds: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search device_logs by keyword and optional time window."""
        if self._conn is None:
            return []
        query = "SELECT timestamp, source, text FROM device_logs WHERE text LIKE ?"
        params: list[Any] = [f"%{keyword}%"]

        if time_window_seconds is not None:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{time_window_seconds} seconds")

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def fetch_logs(
        self, limit: int = 5000, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return device logs in insertion order (for export)."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT timestamp, source, text FROM device_logs "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_analytics(self, limit: int = 20) -> list[dict[str, Any]]:
        """Aggregate common error-like patterns."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            """
            SELECT text, COUNT(*) as cnt
            FROM device_logs
            WHERE text LIKE '%error%'
               OR text LIKE '%fail%'
               OR text LIKE '%panic%'
               OR text LIKE '%hardfault%'
               OR text LIKE '%fault%'
            GROUP BY text
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_log_count(self) -> int:
        """Return total log rows (for size estimation)."""
        if self._conn is None:
            return 0
        cursor = await self._conn.execute("SELECT COUNT(*) as cnt FROM device_logs")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
