-- EmbPilot central database schema (embpilot_main.db)
-- Holds persistent metadata across all sessions.
-- Loaded automatically by database.MainDatabase on startup.

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL UNIQUE,       -- UUID or timestamp-based
    device_name TEXT    NOT NULL,              -- e.g. "COM3", "192.168.1.100:23"
    interface   TEXT    NOT NULL,              -- 'serial' | 'telnet' | 'ssh'
    started_at  TEXT    NOT NULL,              -- ISO-8601
    ended_at    TEXT,                          -- NULL while active
    db_path     TEXT    NOT NULL,              -- path to the session .db file
    file_size   INTEGER DEFAULT 0,            -- bytes (updated lazily)
    status      TEXT    NOT NULL DEFAULT 'active'  -- 'active' | 'closed' | 'cleaned'
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

CREATE TABLE IF NOT EXISTS operation_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    session_id  TEXT,                          -- optional link to a session
    actor       TEXT    NOT NULL,              -- 'AI' | 'Human' | 'System'
    action_type TEXT    NOT NULL,              -- 'call_tool' | 'connect' | 'disconnect' | ...
    detail      TEXT    NOT NULL               -- JSON-serialized context
);

CREATE INDEX IF NOT EXISTS idx_op_history_timestamp ON operation_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_op_history_session   ON operation_history(session_id);
CREATE INDEX IF NOT EXISTS idx_op_history_actor     ON operation_history(actor);
