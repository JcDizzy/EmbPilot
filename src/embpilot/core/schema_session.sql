-- EmbPilot session database schema (session_<ts>_<device>.db)
-- Created per-connection; holds high-frequency device log output.
-- Loaded automatically by database.SessionDatabase on session start.

CREATE TABLE IF NOT EXISTS device_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,   -- YYYY-MM-DD HH:MM:SS.SSS
    source      TEXT    NOT NULL,   -- 'serial' | 'telnet' | 'ssh'
    level       TEXT    NOT NULL DEFAULT 'info',
    tag         TEXT,
    text        TEXT    NOT NULL    -- raw log text (stripped of \\r\\n)
);

CREATE INDEX IF NOT EXISTS idx_device_logs_timestamp ON device_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_device_logs_level     ON device_logs(level);
CREATE INDEX IF NOT EXISTS idx_device_logs_tag       ON device_logs(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS device_logs_fts
USING fts5(text, content='device_logs', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS device_logs_ai AFTER INSERT ON device_logs BEGIN
    INSERT INTO device_logs_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS device_logs_ad AFTER DELETE ON device_logs BEGIN
    INSERT INTO device_logs_fts(device_logs_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS device_logs_au AFTER UPDATE ON device_logs BEGIN
    INSERT INTO device_logs_fts(device_logs_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO device_logs_fts(rowid, text) VALUES (new.id, new.text);
END;
