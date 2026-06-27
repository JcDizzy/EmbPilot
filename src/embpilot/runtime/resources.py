from __future__ import annotations

from embpilot.runtime.models import RingBuffer, SessionInfo


def build_session_info_resource(info: SessionInfo) -> dict[str, object]:
    return {
        "session_id": info.session_id,
        "interface_type": info.interface_type,
        "device_name": info.device_name,
        "connection_summary": info.connection_summary,
        "started_at": info.started_at.isoformat(),
        "state": info.state,
        "last_log_at": info.last_log_at.isoformat() if info.last_log_at else None,
        "log_count": info.log_count,
    }


def render_live_log_snapshot(ring: RingBuffer) -> str:
    lines = [line.formatted() for line in ring.snapshot()]
    return "\n".join(lines) if lines else "(no log data yet)"
