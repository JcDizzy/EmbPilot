"""
Frame assembly engine, in-memory ring buffer, and Expect-based pattern matcher.

Architecture
------------
┌─────────────────┐     asyncio.Queue     ┌──────────────────┐
│  LogProducer     │───────┬──────────────→│  ExpectConsumer   │
│  (raw bytes in)  │       │              │  (regex match)    │
└─────────────────┘       │              └──────────────────┘
                           │              ┌──────────────────┐
                           └──────────────→│  DbConsumer       │
                                           │  → SessionDatabase│
                                           └──────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────

class LogLine:
    """A single framed log line with a high-precision host timestamp."""

    __slots__ = ("timestamp", "text")

    def __init__(self, timestamp: datetime, text: str) -> None:
        self.timestamp = timestamp
        self.text = text

    def formatted(self) -> str:
        """Return ``[YYYY-MM-DD HH:MM:SS.SSS] <text>``."""
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S.") + f"{self.timestamp.microsecond // 1000:03d}"
        return f"[{ts}] {self.text}"


# ── Ring buffer ─────────────────────────────────────────────────────────────

class RingBuffer:
    """Fixed-length in-memory ring buffer for recent log browsing."""

    def __init__(self, maxlen: int = 2000) -> None:
        self._buffer: deque[tuple[int, LogLine]] = deque(maxlen=maxlen)
        self._next_sequence = 0

    def push(self, line: LogLine) -> None:
        self._buffer.append((self._next_sequence, line))
        self._next_sequence += 1

    def snapshot(self) -> list[LogLine]:
        return [line for _, line in self._buffer]

    def mark(self) -> int:
        """Return a cursor which identifies the next line pushed."""
        return self._next_sequence

    def snapshot_since(self, cursor: int) -> list[LogLine]:
        """Return all retained lines pushed at or after *cursor*."""
        return [line for sequence, line in self._buffer if sequence >= cursor]


# ── Frame assembler ─────────────────────────────────────────────────────────

class FrameAssembler:
    """Accumulate raw bytes, split on ``\\n`` (or ``\\r\\n``), emit complete
    frames immediately; flush partial frames after *timeout* ms of inactivity.

    Parameters
    ----------
    timeout_ms:
        Milliseconds of silence before a partial frame is flushed.
    """

    def __init__(self, timeout_ms: int = 50) -> None:
        self._timeout_s = timeout_ms / 1000.0
        self._buffer = bytearray()
        self._last_byte_time: Optional[datetime] = None

    def feed(self, data: bytes, now: Optional[datetime] = None) -> list[LogLine]:
        """Feed raw bytes into the assembler.

        Returns a (possibly empty) list of completed ``LogLine`` instances.
        """
        now = now or datetime.now(timezone.utc)
        self._buffer.extend(data)
        self._last_byte_time = now
        return self._flush_complete_frames(now)

    def flush_partial(self, now: Optional[datetime] = None) -> Optional[LogLine]:
        """Force-flush any remaining buffer as an incomplete frame.

        Returns ``None`` if the buffer is empty.
        """
        if not self._buffer:
            return None
        now = now or datetime.now(timezone.utc)
        line = self._emit(now)
        return line

    def check_timeout(self, now: Optional[datetime] = None) -> Optional[LogLine]:
        """If the idle timeout has elapsed, flush the partial frame.

        Called periodically by the producer loop.
        """
        if not self._buffer or self._last_byte_time is None:
            return None
        now = now or datetime.now(timezone.utc)
        elapsed = (now - self._last_byte_time).total_seconds()
        if elapsed >= self._timeout_s:
            return self.flush_partial(now)
        return None

    # ── internal ─────────────────────────────────────────────────────

    def _flush_complete_frames(self, now: datetime) -> list[LogLine]:
        lines: list[LogLine] = []
        while True:
            idx = self._buffer.find(b"\n")
            if idx == -1:
                break
            frame = self._buffer[:idx]
            del self._buffer[: idx + 1]
            # Strip trailing \r if present
            text = frame.decode("utf-8", errors="replace").rstrip("\r")
            lines.append(LogLine(now, text))
        return lines

    def _emit(self, now: datetime) -> LogLine:
        text = self._buffer.decode("utf-8", errors="replace")
        self._buffer.clear()
        self._last_byte_time = None
        return LogLine(now, text)


# ── Producers & Consumers ────────────────────────────────────────────────────

class LogProducer:
    """Async read loop: reads bytes from *reader*, feeds them into a
    ``FrameAssembler``, and pushes completed lines onto *queue*.

    Also maintains a ``RingBuffer`` for the live-log resource.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        queue: asyncio.Queue[LogLine],
        ring: RingBuffer,
        framing_timeout_ms: int = 50,
    ) -> None:
        self._reader = reader
        self._queue = queue
        self._ring = ring
        self._assembler = FrameAssembler(timeout_ms=framing_timeout_ms)
        self._running = False

    async def run(self) -> None:
        """Run the read loop indefinitely until the reader is exhausted or
        ``stop()`` is called."""
        self._running = True
        try:
            while self._running:
                byte_data = await self._reader.read(4096)
                if not byte_data:
                    break  # EOF
                now = datetime.now(timezone.utc)
                for line in self._assembler.feed(byte_data, now):
                    await self._queue.put(line)
                    self._ring.push(line)

                # Check timeout for partial frames
                partial = self._assembler.check_timeout(now)
                if partial is not None:
                    await self._queue.put(partial)
                    self._ring.push(partial)
        finally:
            # Flush any remaining data
            remaining = self._assembler.flush_partial()
            if remaining is not None:
                await self._queue.put(remaining)
                self._ring.push(remaining)

    def stop(self) -> None:
        self._running = False


class ExpectConsumer:
    """Asynchronously reads from *queue* and checks each ``LogLine`` against a
    list of ``(regex, callback)`` matchers.

    When a match is found, the *matched* event is set and the matching line
    (plus any captured groups) is stored for the caller to retrieve.
    """

    def __init__(self, queue: asyncio.Queue[LogLine]) -> None:
        self._queue = queue
        self._patterns: list[tuple[re.Pattern, Callable[[re.Match], None]]] = []

    def add_pattern(self, regex: str, callback: Callable[[re.Match], None]) -> None:
        self._patterns.append((re.compile(regex), callback))

    async def consume(self) -> None:
        """Read lines from the queue and test each against registered patterns.

        Runs until the producer signals completion (or a sentinel is received).
        """
        while True:
            line = await self._queue.get()
            if line is None:  # sentinel
                break
            for pattern, cb in self._patterns:
                m = pattern.search(line.text)
                if m:
                    cb(m)


class DbConsumer:
    """Reads framed lines from *queue* and batches them into a
    ``SessionDatabase`` on a periodic timer.

    Parameters
    ----------
    queue:
        Source of framed ``LogLine`` instances.
    session_db:
        Destination ``SessionDatabase`` for bulk log insertion.
    batch_size:
        Maximum lines to accumulate before forcing a write.
    flush_interval_s:
        Interval in seconds for periodic timed flushes (default: 50 ms).
    """

    def __init__(
        self,
        queue: asyncio.Queue[LogLine],
        session_db: SessionDatabase,
        batch_size: int = 200,
        flush_interval_s: float = 0.05,
    ) -> None:
        self._queue = queue
        self._db = session_db
        self._batch: list[LogLine] = []
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._consume_task: Optional[asyncio.Task[None]] = None
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._running = False

    def start(self) -> None:
        """Start the consumption and periodic flush background tasks."""
        self._running = True
        self._consume_task = asyncio.create_task(self._consume_loop())
        self._flush_task = asyncio.create_task(self._periodic_flush())

    async def stop(self) -> None:
        """Stop background tasks and flush any remaining lines."""
        self._running = False
        if self._consume_task is not None:
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_batch()

    def collect(self, line: LogLine) -> None:
        """Add a line to the current batch.

        Immediately flushes to DB when the batch reaches ``batch_size``.
        """
        self._batch.append(line)
        if len(self._batch) >= self._batch_size:
            asyncio.create_task(self._flush_batch())

    async def _consume_loop(self) -> None:
        """Read lines from the queue and accumulate them into batches."""
        while self._running:
            try:
                line = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                self.collect(line)
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("DbConsumer consume error")
                break

    async def _periodic_flush(self) -> None:
        """Loop that flushes the current batch on a fixed interval."""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            await self._flush_batch()

    async def _flush_batch(self, source: str = "serial") -> None:
        """Write accumulated lines to the session database."""
        if not self._batch:
            return
        batch = self._batch
        self._batch = []
        try:
            await self._db.bulk_insert_logs(batch, source=source)
        except Exception:
            logger.exception("DbConsumer flush failed, %d lines dropped", len(batch))
