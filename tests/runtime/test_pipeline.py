from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from embpilot.core.database import SessionDatabase
from embpilot.runtime.models import LogLine
from embpilot.runtime.pipeline import DbSink, LogProducer, SessionDispatcher


class CollectSink:
    def __init__(self) -> None:
        self.lines: list[LogLine] = []
        self.closed = False

    async def write(self, line: LogLine) -> None:
        self.lines.append(line)

    async def close(self) -> None:
        self.closed = True


class FakeReader:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class PausingReader:
    def __init__(self, *steps: tuple[float, bytes]) -> None:
        self._steps = list(steps)

    async def read(self, _size: int) -> bytes:
        if not self._steps:
            return b""
        delay_s, chunk = self._steps.pop(0)
        await asyncio.sleep(delay_s)
        return chunk


class SlowSink(CollectSink):
    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self._delay_s = delay_s

    async def write(self, line: LogLine) -> None:
        await asyncio.sleep(self._delay_s)
        await super().write(line)


class FailingSink:
    def __init__(self) -> None:
        self.attempts = 0
        self.closed = False

    async def write(self, line: LogLine) -> None:
        self.attempts += 1
        raise RuntimeError(f"boom:{line.text}")

    async def close(self) -> None:
        self.closed = True


class BlockingReader:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return b""


class BlockingSink(CollectSink):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, line: LogLine) -> None:
        self.lines.append(line)
        self.started.set()
        await self.release.wait()


def test_session_dispatcher_fans_out_to_multiple_sinks():
    async def scenario() -> None:
        sink_a = CollectSink()
        sink_b = CollectSink()
        dispatcher = SessionDispatcher([sink_a, sink_b])
        line = LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "boot ok")

        await dispatcher.dispatch(line)
        await dispatcher.close()

        assert [item.text for item in sink_a.lines] == ["boot ok"]
        assert [item.text for item in sink_b.lines] == ["boot ok"]
        assert sink_a.closed is True
        assert sink_b.closed is True

    asyncio.run(scenario())


def test_db_sink_flushes_remaining_batch_on_close(tmp_path: Path):
    async def scenario() -> None:
        session_db = SessionDatabase(tmp_path / "session.db")
        await session_db.open()
        sink = DbSink(session_db, batch_size=3)

        await sink.write(LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "line-1"))
        await sink.write(LogLine(datetime(2026, 6, 27, 12, 0, 1, tzinfo=timezone.utc), "line-2"))

        assert await session_db.get_log_count() == 0

        await sink.close()

        assert await session_db.get_log_count() == 2
        rows = await session_db.search_logs("line-")
        assert [row["text"] for row in rows] == ["line-2", "line-1"]
        await session_db.close()

    asyncio.run(scenario())


def test_log_producer_dispatches_complete_lines():
    async def scenario() -> None:
        sink = CollectSink()
        dispatcher = SessionDispatcher([sink])
        reader = FakeReader(b"alpha", b" one\nbeta", b" two\n")
        producer = LogProducer(reader, dispatcher)

        await producer.run()
        await dispatcher.close()

        assert [line.text for line in sink.lines] == ["alpha one", "beta two"]

    asyncio.run(scenario())


def test_log_producer_flushes_partial_frame_after_idle_timeout():
    async def scenario() -> None:
        sink = CollectSink()
        dispatcher = SessionDispatcher([sink])
        reader = PausingReader(
            (0.0, b"prompt> "),
            (0.08, b"next line\n"),
        )
        producer = LogProducer(reader, dispatcher, framing_timeout_ms=50)

        await producer.run()
        await dispatcher.close()

        assert [line.text for line in sink.lines] == ["prompt> ", "next line"]

    asyncio.run(scenario())


def test_session_dispatcher_isolates_slow_and_failing_sinks():
    async def scenario() -> None:
        fast_sink = CollectSink()
        slow_sink = SlowSink(delay_s=0.2)
        failing_sink = FailingSink()
        dispatcher = SessionDispatcher([fast_sink, slow_sink, failing_sink])
        first = LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "first")
        second = LogLine(datetime(2026, 6, 27, 12, 0, 1, tzinfo=timezone.utc), "second")

        await dispatcher.dispatch(first)
        await dispatcher.dispatch(second)
        await asyncio.sleep(0.02)

        assert [line.text for line in fast_sink.lines] == ["first", "second"]

        await dispatcher.close()

        assert [line.text for line in slow_sink.lines] == ["first", "second"]
        assert failing_sink.attempts == 2
        assert failing_sink.closed is True

    asyncio.run(scenario())


def test_log_producer_cancels_inflight_read_when_run_is_cancelled():
    async def scenario() -> None:
        sink = CollectSink()
        dispatcher = SessionDispatcher([sink])
        reader = BlockingReader()
        producer_task = asyncio.create_task(LogProducer(reader, dispatcher).run())

        await reader.started.wait()
        producer_task.cancel()

        try:
            await producer_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("producer task should propagate cancellation")

        await asyncio.wait_for(reader.cancelled.wait(), timeout=0.2)
        await dispatcher.close()

    asyncio.run(scenario())


def test_session_dispatcher_applies_backpressure_with_bounded_sink_queue():
    async def scenario() -> None:
        blocking_sink = BlockingSink()
        fast_sink = CollectSink()
        dispatcher = SessionDispatcher(
            [blocking_sink, fast_sink],
            sink_queue_size=1,
        )
        first = LogLine(datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc), "first")
        second = LogLine(datetime(2026, 6, 27, 12, 0, 1, tzinfo=timezone.utc), "second")
        third = LogLine(datetime(2026, 6, 27, 12, 0, 2, tzinfo=timezone.utc), "third")

        await dispatcher.dispatch(first)
        await blocking_sink.started.wait()
        await dispatcher.dispatch(second)

        third_dispatch = asyncio.create_task(dispatcher.dispatch(third))
        await asyncio.sleep(0.02)

        assert third_dispatch.done() is False
        assert [line.text for line in fast_sink.lines] == ["first", "second"]

        blocking_sink.release.set()
        await third_dispatch
        await dispatcher.close()

        assert [line.text for line in fast_sink.lines] == ["first", "second", "third"]

    asyncio.run(scenario())
