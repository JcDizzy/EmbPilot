from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from re import Pattern

from embpilot.runtime.models import LogLine


class ExpectWindowClosedError(RuntimeError):
    def __init__(self, lines: list[LogLine]) -> None:
        super().__init__("expect window closed before completion")
        self.lines = list(lines)


@dataclass(slots=True)
class CommandWindow:
    expect_regex: Pattern[str] | None
    timeout_ms: int
    lines: list[LogLine]
    future: asyncio.Future[list[LogLine]]
    timeout_handle: asyncio.TimerHandle | None = None

    async def wait(self) -> list[LogLine]:
        return await self.future


class ExpectManager:
    def __init__(self) -> None:
        self._windows: list[CommandWindow] = []
        self._closed = False

    def open_window(self, expect_regex: str | None, timeout_ms: int) -> CommandWindow:
        if self._closed:
            raise RuntimeError("expect manager is closed")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[LogLine]] = loop.create_future()
        window = CommandWindow(
            expect_regex=re.compile(expect_regex) if expect_regex is not None else None,
            timeout_ms=timeout_ms,
            lines=[],
            future=future,
        )
        window.timeout_handle = loop.call_later(
            timeout_ms / 1000.0,
            self._timeout_window,
            window,
        )
        self._windows.append(window)
        return window

    def feed(self, line: LogLine) -> bool:
        return self.handle(line)

    def handle(self, line: LogLine) -> bool:
        matched = False
        for window in list(self._windows):
            if window.future.done():
                self._discard_window(window)
                continue
            window.lines.append(line)
            if window.expect_regex is None:
                continue
            if window.expect_regex.search(line.text) is None:
                continue
            if window.timeout_handle is not None:
                window.timeout_handle.cancel()
            window.future.set_result(list(window.lines))
            self._discard_window(window)
            matched = True
        return matched

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for window in list(self._windows):
            if window.timeout_handle is not None:
                window.timeout_handle.cancel()
            if not window.future.done():
                window.future.set_exception(ExpectWindowClosedError(window.lines))
            self._discard_window(window)

    def cancel_window(self, window: CommandWindow) -> None:
        if window.timeout_handle is not None:
            window.timeout_handle.cancel()
        if not window.future.done():
            window.future.cancel()
        self._discard_window(window)

    def _timeout_window(self, window: CommandWindow | None) -> None:
        if window is None:
            return
        if not window.future.done():
            window.future.set_result(list(window.lines))
        self._discard_window(window)

    def _discard_window(self, window: CommandWindow) -> None:
        with contextlib.suppress(ValueError):
            self._windows.remove(window)
