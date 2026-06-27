from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from datetime import datetime, timezone
from typing import Protocol

from embpilot.core.database import SessionDatabase
from embpilot.runtime.models import LogLine, RingBuffer

logger = logging.getLogger(__name__)


class LogSink(Protocol):
    async def write(self, line: LogLine) -> None:
        ...

    async def close(self) -> None:
        ...


class RingBufferSink:
    def __init__(self, ring: RingBuffer) -> None:
        self._ring = ring

    async def write(self, line: LogLine) -> None:
        self._ring.push(line)

    async def close(self) -> None:
        return None


class DbSink:
    def __init__(
        self,
        session_db: SessionDatabase,
        batch_size: int = 200,
        source: str = "serial",
    ) -> None:
        self._session_db = session_db
        self._batch_size = batch_size
        self._source = source
        self._batch: list[LogLine] = []

    async def write(self, line: LogLine) -> None:
        self._batch.append(line)
        if len(self._batch) >= self._batch_size:
            await self._flush()

    async def close(self) -> None:
        await self._flush()

    async def _flush(self) -> None:
        if not self._batch:
            return
        batch = self._batch
        self._batch = []
        await self._session_db.bulk_insert_logs(batch, source=self._source)


class SessionDispatcher:
    def __init__(self, sinks: list[LogSink], sink_queue_size: int = 256) -> None:
        self._workers = [_SinkWorker(sink, max_queue_size=sink_queue_size) for sink in sinks]

    async def dispatch(self, line: LogLine) -> None:
        for worker in self._workers:
            await worker.enqueue(line)

    async def close(self) -> None:
        for worker in self._workers:
            await worker.close()


class FrameAssembler:
    def __init__(self, timeout_ms: int = 50) -> None:
        self._timeout_s = timeout_ms / 1000.0
        self._buffer = bytearray()
        self._last_byte_time: datetime | None = None

    def feed(self, data: bytes, now: datetime | None = None) -> list[LogLine]:
        now = now or datetime.now(timezone.utc)
        self._buffer.extend(data)
        self._last_byte_time = now
        lines: list[LogLine] = []
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                break
            frame = self._buffer[:newline_index]
            del self._buffer[: newline_index + 1]
            text = frame.decode("utf-8", errors="replace").rstrip("\r")
            lines.append(LogLine(now, text))
        return lines

    def flush_partial(self, now: datetime | None = None) -> LogLine | None:
        if not self._buffer:
            return None
        now = now or datetime.now(timezone.utc)
        text = self._buffer.decode("utf-8", errors="replace")
        self._buffer.clear()
        self._last_byte_time = None
        return LogLine(now, text)

    def check_timeout(self, now: datetime | None = None) -> LogLine | None:
        if not self._buffer or self._last_byte_time is None:
            return None
        now = now or datetime.now(timezone.utc)
        if (now - self._last_byte_time).total_seconds() < self._timeout_s:
            return None
        return self.flush_partial(now)


class LogProducer:
    def __init__(
        self,
        reader: object,
        dispatcher: SessionDispatcher,
        framing_timeout_ms: int = 50,
    ) -> None:
        self._reader = reader
        self._dispatcher = dispatcher
        self._assembler = FrameAssembler(timeout_ms=framing_timeout_ms)
        self._read_timeout_s = framing_timeout_ms / 1000.0

    async def run(self) -> None:
        pending_read: asyncio.Task[bytes] | None = asyncio.create_task(self._reader.read(4096))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {pending_read}, timeout=self._read_timeout_s
                )
                if not done:
                    partial = self._assembler.check_timeout()
                    if partial is not None:
                        await self._dispatcher.dispatch(partial)
                    continue

                chunk = pending_read.result()
                if not chunk:
                    break
                now = datetime.now(timezone.utc)
                for line in self._assembler.feed(chunk, now):
                    await self._dispatcher.dispatch(line)
                pending_read = asyncio.create_task(self._reader.read(4096))

            remaining = self._assembler.flush_partial()
            if remaining is not None:
                await self._dispatcher.dispatch(remaining)
        finally:
            if pending_read is not None and not pending_read.done():
                pending_read.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_read


class _SinkWorker:
    _SENTINEL = object()

    def __init__(self, sink: LogSink, max_queue_size: int) -> None:
        self._sink = sink
        self._queue: asyncio.Queue[LogLine | object] = asyncio.Queue(maxsize=max_queue_size)
        self._task: asyncio.Task[None] | None = None

    async def enqueue(self, line: LogLine) -> None:
        self._ensure_started()
        await self._queue.put(line)

    async def close(self) -> None:
        self._ensure_started()
        await self._queue.put(self._SENTINEL)
        if self._task is not None:
            await self._task

    def _ensure_started(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is self._SENTINEL:
                    break
                await self._sink.write(item)
            except Exception:
                logger.exception("Log sink write failed")
            finally:
                self._queue.task_done()

        try:
            await self._sink.close()
        except Exception:
            logger.exception("Log sink close failed")
