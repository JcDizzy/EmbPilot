"""Command execution behavior at the device-session seam."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from embpilot.core.commands import CommandExecutor
from embpilot.core.engine import LogLine, RingBuffer


class RespondingDevice:
    """Small transport adapter which emits configured lines after each write."""

    def __init__(self, ring: RingBuffer, responses: list[str]) -> None:
        self.ring = ring
        self.responses = responses
        self.writes: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        for text in self.responses:
            self.ring.push(LogLine(datetime.now(timezone.utc), text))
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_command_applies_line_ending_and_captures_first_output() -> None:
    ring = RingBuffer()
    device = RespondingDevice(ring, ["status: ready"])
    executor = CommandExecutor(device, ring)

    result = await executor.execute(
        "status",
        line_ending="crlf",
        timeout_ms=10,
    )

    assert device.writes == [b"status\r\n"]
    assert "status: ready" in result.output
    assert result.matched is False
    assert result.timed_out is True
    assert result.truncated is False


@pytest.mark.asyncio
async def test_command_returns_early_when_expect_matches() -> None:
    ring = RingBuffer()
    device = RespondingDevice(ring, ["booting", "login: ready"])
    executor = CommandExecutor(device, ring)

    started = time.monotonic()
    result = await executor.execute(
        "reboot",
        expect_regex=r"login:\s+ready",
        timeout_ms=1000,
    )

    assert time.monotonic() - started < 0.25
    assert result.matched is True
    assert result.timed_out is False
    assert "booting" in result.output
    assert "login: ready" in result.output


@pytest.mark.asyncio
async def test_invalid_expect_expression_is_an_argument_error() -> None:
    ring = RingBuffer()
    executor = CommandExecutor(RespondingDevice(ring, []), ring)

    with pytest.raises(ValueError, match="Invalid expect_regex"):
        await executor.execute("status", expect_regex="[")
