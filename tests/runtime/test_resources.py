from __future__ import annotations

from datetime import datetime, timezone

from embpilot.core.engine import LogLine as CoreLogLine
from embpilot.core.engine import RingBuffer as CoreRingBuffer
from embpilot.runtime.models import LogLine, RingBuffer, SessionInfo
from embpilot.runtime.resources import (
    build_session_info_resource,
    render_live_log_snapshot,
)


def test_build_session_info_resource_includes_device_name():
    info = SessionInfo(
        session_id="abcd1234",
        interface_type="serial",
        device_name="board-a",
        connection_summary="serial://COM7@115200",
        started_at=datetime(2026, 6, 27, tzinfo=timezone.utc),
        state="active",
        last_log_at=None,
        log_count=0,
    )

    payload = build_session_info_resource(info)

    assert payload["device_name"] == "board-a"
    assert payload["interface_type"] == "serial"
    assert payload["state"] == "active"


def test_render_live_log_snapshot_formats_ring_lines():
    ring = RingBuffer(maxlen=4)
    ring.push(LogLine(datetime(2026, 6, 27, 8, 0, 0, tzinfo=timezone.utc), "boot ok"))
    ring.push(
        LogLine(datetime(2026, 6, 27, 8, 0, 1, tzinfo=timezone.utc), "shell ready")
    )

    text = render_live_log_snapshot(ring)

    assert (
        text
        == "[2026-06-27 08:00:00.000] boot ok\n"
        "[2026-06-27 08:00:01.000] shell ready"
    )


def test_render_live_log_snapshot_returns_fallback_for_empty_ring():
    ring = RingBuffer(maxlen=4)

    assert render_live_log_snapshot(ring) == "(no log data yet)"


def test_runtime_models_reuse_canonical_log_types():
    assert LogLine is CoreLogLine
    assert RingBuffer is CoreRingBuffer
