"""
High-throughput simulation test for the full producer-consumer pipeline.

Simulates a device producing 10 000 log lines per second and verifies
that the FrameAssembler, RingBuffer, DbConsumer, and SessionDatabase
all keep up without data loss.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path

import pytest

from embpilot.core.database import SessionDatabase
from embpilot.core.engine import (
    FrameAssembler,
    LogProducer,
    LogLine,
    RingBuffer,
    DbConsumer,
)

logger = logging.getLogger(__name__)


class _MockReader:
    """Simulates a device that produces lines at a given rate."""

    def __init__(self, total_lines: int = 10000, batch_size: int = 500) -> None:
        self._lines = [
            f"LOG {i:08d}: simulated device output line number {i}\n".encode()
            for i in range(total_lines)
        ]
        self._batch_size = batch_size
        self._pos = 0
        self._closed = False

    async def read(self, n: int = 4096) -> bytes:
        if self._closed or self._pos >= len(self._lines):
            self._closed = True
            return b""

        end = min(self._pos + self._batch_size, len(self._lines))
        chunk = b"".join(self._lines[self._pos : end])
        self._pos = end
        # Simulate realistic I/O delay
        await asyncio.sleep(0.001)
        return chunk


@pytest.mark.asyncio
async def test_high_throughput_pipeline():
    """Run 10 000 log lines through the full pipeline and verify no loss."""
    total = 10000

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "session_throughput.db"
        session_db = SessionDatabase(db_path)
        await session_db.open()
        try:
            queue: asyncio.Queue[LogLine] = asyncio.Queue(maxsize=5000)
            ring = RingBuffer(maxlen=2000)
            reader = _MockReader(total_lines=total, batch_size=500)
            producer = LogProducer(reader, queue, ring, framing_timeout_ms=50)
            db_consumer = DbConsumer(queue=queue, session_db=session_db, batch_size=200)

            # Start consumers
            db_consumer.start()
            producer_task = asyncio.create_task(producer.run())

            # Wait for producer to finish
            await asyncio.wait_for(producer_task, timeout=30.0)

            # Give db_consumer a moment to flush remaining lines
            await asyncio.sleep(1.0)
            await db_consumer.stop()

            # Verify ring buffer (should have last 2000 lines)
            snap = ring.snapshot()
            assert len(snap) == 2000, f"Expected 2000 in ring, got {len(snap)}"
            assert snap[0].text == f"LOG {8000:08d}: simulated device output line number {8000}"
            assert snap[-1].text == f"LOG {9999:08d}: simulated device output line number {9999}"

            # Verify database has all lines
            total_in_db = await session_db.get_log_count()
            assert total_in_db == total, f"Expected {total} in DB, got {total_in_db}"

            # Verify search works
            results = await session_db.search_logs("LOG 00005000", limit=5)
            assert len(results) == 1
            assert "LOG 00005000" in results[0]["text"]
        finally:
            await session_db.close()


@pytest.mark.asyncio
async def test_frame_assembler_benchmark():
    """Benchmark FrameAssembler under high throughput."""
    total_bytes = b"line data content\n" * 5000
    assembler = FrameAssembler(timeout_ms=50)

    start = time.monotonic()
    lines = assembler.feed(total_bytes)
    elapsed = time.monotonic() - start

    assert len(lines) == 5000
    lines_per_sec = 5000 / elapsed if elapsed > 0 else float("inf")
    print(f"\nFrameAssembler: {5000} lines in {elapsed*1000:.2f}ms "
          f"({lines_per_sec:.0f} lines/sec)")
    # Should handle at least 100k lines/sec
    assert lines_per_sec > 100_000, f"Too slow: {lines_per_sec:.0f} lines/sec"


@pytest.mark.asyncio
async def test_ring_buffer_overflow():
    """Verify RingBuffer correctly discards old entries."""
    rb = RingBuffer(maxlen=100)
    for i in range(5000):
        rb.push(LogLine(__import__("datetime").datetime.now(), f"line-{i}"))
    snap = rb.snapshot()
    assert len(snap) == 100
    assert snap[0].text == "line-4900"
    assert snap[-1].text == "line-4999"
