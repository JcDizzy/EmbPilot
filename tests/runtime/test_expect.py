from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from embpilot.runtime import LogLine
from embpilot.runtime.expect import CommandWindow, ExpectManager, ExpectWindowClosedError


def test_expect_manager_completes_when_pattern_matches():
    async def scenario() -> None:
        manager = ExpectManager()
        window = manager.open_window(r"ready>\s*$", timeout_ms=200)
        boot = LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "booting...")
        prompt = LogLine(datetime(2026, 6, 27, 12, 0, 1, tzinfo=timezone.utc), "ready> ")

        assert isinstance(window, CommandWindow)
        assert manager.feed(boot) is False

        assert manager.handle(prompt) is True
        assert await asyncio.wait_for(window.wait(), timeout=0.2) == [boot, prompt]

        manager.close()

    asyncio.run(scenario())


def test_expect_manager_times_out_without_match():
    async def scenario() -> None:
        manager = ExpectManager()
        window = manager.open_window(r"PASS", timeout_ms=20)
        line = LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "still waiting")

        assert manager.feed(line) is False
        assert await asyncio.wait_for(window.wait(), timeout=0.2) == [line]

        manager.close()

    asyncio.run(scenario())


def test_expect_manager_timeout_returns_empty_lines_when_nothing_arrives():
    async def scenario() -> None:
        manager = ExpectManager()
        window = manager.open_window(r"PASS", timeout_ms=20)

        assert await asyncio.wait_for(window.wait(), timeout=0.2) == []

        manager.close()

    asyncio.run(scenario())


def test_expect_manager_timeout_only_window_collects_lines_without_regex():
    async def scenario() -> None:
        manager = ExpectManager()
        window = manager.open_window(None, timeout_ms=20)
        first = LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "cmd out 1")
        second = LogLine(datetime(2026, 6, 27, 12, 0, 1, tzinfo=timezone.utc), "cmd out 2")

        assert manager.feed(first) is False
        assert manager.handle(second) is False
        assert await asyncio.wait_for(window.wait(), timeout=0.2) == [first, second]

        manager.close()

    asyncio.run(scenario())


def test_expect_manager_close_marks_pending_window_as_shutdown():
    async def scenario() -> None:
        manager = ExpectManager()
        window = manager.open_window(r"PASS", timeout_ms=200)
        line = LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "partial output")

        assert manager.feed(line) is False
        manager.close()

        try:
            await window.wait()
        except ExpectWindowClosedError as exc:
            assert exc.lines == [line]
        else:
            raise AssertionError("pending window should end with a shutdown signal")

    asyncio.run(scenario())
