"""Command execution with line-ending and output-capture policy."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from embpilot.core.engine import RingBuffer

LineEnding = Literal["none", "lf", "crlf", "cr"]

_LINE_ENDINGS: dict[LineEnding, bytes] = {
    "none": b"",
    "lf": b"\n",
    "crlf": b"\r\n",
    "cr": b"\r",
}


class CommandWriter(Protocol):
    async def write(self, data: bytes) -> None: ...


class NoActiveDeviceError(RuntimeError):
    """Raised when an operation requires an active device session."""


@dataclass(frozen=True)
class CommandResult:
    output: str
    matched: bool
    timed_out: bool
    truncated: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "output": self.output,
            "matched": self.matched,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


class CommandExecutor:
    """Execute text commands and capture only output produced after the write."""

    def __init__(self, writer: CommandWriter, ring: RingBuffer) -> None:
        self._writer = writer
        self._ring = ring

    async def execute(
        self,
        command: str,
        *,
        line_ending: LineEnding = "lf",
        expect_regex: str | None = None,
        timeout_ms: int = 5000,
        max_output_chars: int = 20_000,
    ) -> CommandResult:
        if line_ending not in _LINE_ENDINGS:
            raise ValueError(f"Unsupported line ending: {line_ending}")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be at least 1")
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be at least 1")

        try:
            pattern = re.compile(expect_regex) if expect_regex else None
        except re.error as exc:
            raise ValueError(f"Invalid expect_regex: {exc}") from exc
        cursor = self._ring.mark()
        payload = command.encode("utf-8")
        if not command.endswith(("\r", "\n")):
            payload += _LINE_ENDINGS[line_ending]
        await self._writer.write(payload)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        matched = False
        timed_out = False
        while True:
            lines = self._ring.snapshot_since(cursor)
            if pattern and any(pattern.search(line.text) for line in lines):
                matched = True
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            await asyncio.sleep(min(0.01, remaining))

        output = "\n".join(line.formatted() for line in self._ring.snapshot_since(cursor))
        truncated = len(output) > max_output_chars
        if truncated:
            output = output[:max_output_chars]

        return CommandResult(
            output=output or "(no output captured)",
            matched=matched,
            timed_out=timed_out,
            truncated=truncated,
        )
