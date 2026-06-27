-- EmbPilot session database schema (session_<ts>_<device>.db)
-- Created per-connection; holds high-frequency device log output.
-- Loaded automatically by database.SessionDatabase on session start.

CREATE TABLE IF NOT EXISTS device_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,   -- YYYY-MM-DD HH:MM:SS.SSS
    source      TEXT    NOT NULL,   -- 'serial' | 'telnet' | 'ssh'
    text        TEXT    NOT NULL    -- raw log text (stripped of \\r\\n)
);

CREATE INDEX IF NOT EXISTS idx_device_logs_timestamp ON device_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_device_logs_text      ON device_logs(text);
