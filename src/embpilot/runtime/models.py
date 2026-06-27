from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from embpilot.core.engine import LogLine, RingBuffer


@dataclass(slots=True)
class SessionInfo:
    session_id: str
    interface_type: str
    device_name: str
    connection_summary: str
    started_at: datetime
    state: str
    last_log_at: datetime | None
    log_count: int
