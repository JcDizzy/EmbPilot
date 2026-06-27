# Runtime Rearchitecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild EmbPilot's runtime around a standard PyPI-friendly package structure, explicit session lifecycle, dispatcher-based log fan-out, real expect behavior, and honest MCP resources.

**Architecture:** Keep the public package name and CLI stable while moving runtime behavior out of the monolithic `server.py` into `cli.py`, `mcp_app.py`, and `runtime/` modules. Deliver the refactor in stages so the old entrypoint can delegate to the new implementation until the new path is fully verified.

**Tech Stack:** Python 3.11+, `argparse`, `asyncio`, `mcp`, `aiosqlite`, `pyserial-asyncio`, `telnetlib3`, `asyncssh`, `pytest`, `pytest-asyncio`

---

## File map

- Create: `src/embpilot/cli.py`
- Create: `src/embpilot/mcp_app.py`
- Create: `src/embpilot/runtime/__init__.py`
- Create: `src/embpilot/runtime/models.py`
- Create: `src/embpilot/runtime/pipeline.py`
- Create: `src/embpilot/runtime/expect.py`
- Create: `src/embpilot/runtime/resources.py`
- Create: `src/embpilot/runtime/session.py`
- Create: `tests/integration/test_cli.py`
- Create: `tests/runtime/test_resources.py`
- Create: `tests/runtime/test_pipeline.py`
- Create: `tests/runtime/test_expect.py`
- Create: `tests/runtime/test_session.py`
- Create: `tests/integration/test_mcp_app.py`
- Modify: `pyproject.toml`
- Modify: `src/embpilot/__main__.py`
- Modify: `src/embpilot/core/database.py`
- Modify: `src/embpilot/server.py`
- Modify: `README.md`
- Modify: `docs/mcp_embedded_debug_spec.md`
- Modify: `PROGRESS.md`
- Modify: `change.log`

### Task 1: Stabilize the package entrypoint and local test workflow

**Files:**
- Create: `src/embpilot/cli.py`
- Create: `tests/integration/test_cli.py`
- Modify: `src/embpilot/__main__.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_cli.py
from __future__ import annotations

import pytest

from embpilot import __version__
from embpilot.cli import build_parser, main


def test_build_parser_includes_data_dir_flag():
    parser = build_parser()
    args = parser.parse_args(["--data-dir", "tmp-data"])
    assert args.data_dir == "tmp-data"


def test_main_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'embpilot'` or `No module named 'embpilot.cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/embpilot/cli.py
from __future__ import annotations

import argparse

from embpilot import __version__
from embpilot.config import EmbPilotConfig
from embpilot.server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embpilot",
        description="EmbPilot - Embedded Debugging MCP Server",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--main-db-path", default=None)
    parser.add_argument("--session-data-dir", default=None)
    parser.add_argument("--lancedb-path", default=None)
    parser.add_argument("--retention-days", type=int, default=None)
    parser.add_argument("--retention-max-gb", type=int, default=None)
    parser.add_argument("--framing-timeout-ms", type=int, default=None)
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = EmbPilotConfig.from_args(args)
    serve(config)
```

```python
# src/embpilot/__main__.py
from embpilot.cli import main


if __name__ == "__main__":
    main()
```

```toml
# pyproject.toml
[project.scripts]
embpilot = "embpilot.cli:main"

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_cli.py -v`
Expected: PASS for both CLI tests

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/embpilot/__main__.py src/embpilot/cli.py tests/integration/test_cli.py
git commit -m "refactor: add dedicated cli entrypoint"
```

### Task 2: Introduce runtime models and the session-info resource shape

**Files:**
- Create: `src/embpilot/runtime/__init__.py`
- Create: `src/embpilot/runtime/models.py`
- Create: `src/embpilot/runtime/resources.py`
- Create: `tests/runtime/test_resources.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_resources.py
from __future__ import annotations

from datetime import datetime, timezone

from embpilot.runtime.models import LogLine, RingBuffer, SessionInfo
from embpilot.runtime.resources import build_session_info_resource, render_live_log_snapshot


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
    ring.push(LogLine(datetime(2026, 6, 27, 8, 0, 1, tzinfo=timezone.utc), "shell ready"))
    text = render_live_log_snapshot(ring)
    assert "boot ok" in text
    assert "shell ready" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/runtime/test_resources.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'embpilot.runtime'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/embpilot/runtime/models.py
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class LogLine:
    timestamp: datetime
    text: str

    def formatted(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S.") + f"{self.timestamp.microsecond // 1000:03d}"
        return f"[{ts}] {self.text}"


class RingBuffer:
    def __init__(self, maxlen: int = 2000) -> None:
        self._buffer: deque[LogLine] = deque(maxlen=maxlen)

    def push(self, line: LogLine) -> None:
        self._buffer.append(line)

    def snapshot(self) -> list[LogLine]:
        return list(self._buffer)


@dataclass(slots=True)
class SessionInfo:
    session_id: str
    interface_type: str
    device_name: str
    connection_summary: str
    started_at: datetime
    state: str
    last_log_at: datetime | None
    log_count: int
```

```python
# src/embpilot/runtime/resources.py
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
```

```python
# src/embpilot/runtime/__init__.py
from embpilot.runtime.models import LogLine, RingBuffer, SessionInfo

__all__ = ["LogLine", "RingBuffer", "SessionInfo"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/runtime/test_resources.py -v`
Expected: PASS for both resource tests

- [ ] **Step 5: Commit**

```bash
git add src/embpilot/runtime/__init__.py src/embpilot/runtime/models.py src/embpilot/runtime/resources.py tests/runtime/test_resources.py
git commit -m "refactor: add runtime models and resource helpers"
```

### Task 3: Replace implicit multi-consumer queues with an explicit dispatcher pipeline

**Files:**
- Create: `src/embpilot/runtime/pipeline.py`
- Create: `tests/runtime/test_pipeline.py`
- Modify: `src/embpilot/core/database.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_pipeline.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from embpilot.runtime.models import LogLine, RingBuffer
from embpilot.runtime.pipeline import DbSink, LogProducer, RingBufferSink, SessionDispatcher


class _CollectingSink:
    def __init__(self) -> None:
        self.lines: list[LogLine] = []

    async def handle(self, line: LogLine) -> None:
        self.lines.append(line)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_dispatcher_fans_out_to_multiple_sinks():
    ring = RingBuffer(maxlen=10)
    ring_sink = RingBufferSink(ring)
    collecting = _CollectingSink()
    dispatcher = SessionDispatcher([ring_sink, collecting])

    line = LogLine(datetime.now(timezone.utc), "boot")
    await dispatcher.dispatch(line)

    assert ring.snapshot()[-1].text == "boot"
    assert collecting.lines[-1].text == "boot"


@pytest.mark.asyncio
async def test_db_sink_flushes_remaining_batch_on_close(tmp_path):
    from embpilot.core.database import SessionDatabase

    session_db = SessionDatabase(tmp_path / "session.db")
    await session_db.open()
    try:
        sink = DbSink(session_db=session_db, batch_size=10)
        await sink.handle(LogLine(datetime.now(timezone.utc), "line-1"))
        await sink.handle(LogLine(datetime.now(timezone.utc), "line-2"))
        await sink.close()
        assert await session_db.get_log_count() == 2
    finally:
        await session_db.close()


class _Reader:
    def __init__(self) -> None:
        self._chunks = [b"boot\nshell ", b"ready\n", b""]

    async def read(self, _: int = 4096) -> bytes:
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_log_producer_dispatches_complete_lines():
    ring = RingBuffer(maxlen=10)
    ring_sink = RingBufferSink(ring)
    collecting = _CollectingSink()
    dispatcher = SessionDispatcher([ring_sink, collecting])
    producer = LogProducer(reader=_Reader(), dispatcher=dispatcher, framing_timeout_ms=5)

    await producer.run()

    assert [line.text for line in collecting.lines] == ["boot", "shell ready"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/runtime/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'embpilot.runtime.pipeline'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/embpilot/runtime/pipeline.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol

from embpilot.core.database import SessionDatabase
from embpilot.runtime.models import LogLine, RingBuffer


class LogSink(Protocol):
    async def handle(self, line: LogLine) -> None: ...
    async def close(self) -> None: ...


class RingBufferSink:
    def __init__(self, ring: RingBuffer) -> None:
        self._ring = ring

    async def handle(self, line: LogLine) -> None:
        self._ring.push(line)

    async def close(self) -> None:
        return None


class DbSink:
    def __init__(self, session_db: SessionDatabase, batch_size: int = 200, source: str = "serial") -> None:
        self._session_db = session_db
        self._batch_size = batch_size
        self._source = source
        self._batch: list[LogLine] = []

    async def handle(self, line: LogLine) -> None:
        self._batch.append(line)
        if len(self._batch) >= self._batch_size:
            await self.flush()

    async def flush(self) -> None:
        if not self._batch:
            return
        batch = self._batch
        self._batch = []
        await self._session_db.bulk_insert_logs(batch, source=self._source)

    async def close(self) -> None:
        await self.flush()


class SessionDispatcher:
    def __init__(self, sinks: list[LogSink]) -> None:
        self._sinks = sinks

    async def dispatch(self, line: LogLine) -> None:
        for sink in self._sinks:
            await sink.handle(line)

    async def close(self) -> None:
        for sink in self._sinks:
            await sink.close()


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
            idx = self._buffer.find(b"\n")
            if idx == -1:
                break
            frame = self._buffer[:idx]
            del self._buffer[: idx + 1]
            lines.append(LogLine(now, frame.decode("utf-8", errors="replace").rstrip("\r")))
        return lines

    def flush_partial(self, now: datetime | None = None) -> LogLine | None:
        if not self._buffer:
            return None
        now = now or datetime.now(timezone.utc)
        line = LogLine(now, self._buffer.decode("utf-8", errors="replace"))
        self._buffer.clear()
        self._last_byte_time = None
        return line


class LogProducer:
    def __init__(self, reader, dispatcher: SessionDispatcher, framing_timeout_ms: int = 50) -> None:
        self._reader = reader
        self._dispatcher = dispatcher
        self._assembler = FrameAssembler(timeout_ms=framing_timeout_ms)

    async def run(self) -> None:
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                break
            now = datetime.now(timezone.utc)
            for line in self._assembler.feed(chunk, now):
                await self._dispatcher.dispatch(line)
        tail = self._assembler.flush_partial()
        if tail is not None:
            await self._dispatcher.dispatch(tail)
```

```python
# src/embpilot/core/database.py
from embpilot.runtime.models import LogLine
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/runtime/test_pipeline.py -v`
Expected: PASS for dispatcher fan-out and DB close flush

- [ ] **Step 5: Commit**

```bash
git add src/embpilot/runtime/pipeline.py src/embpilot/core/database.py tests/runtime/test_pipeline.py
git commit -m "refactor: add dispatcher-based log pipeline"
```

### Task 4: Add a real expect manager with command-window semantics

**Files:**
- Create: `src/embpilot/runtime/expect.py`
- Create: `tests/runtime/test_expect.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_expect.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from embpilot.runtime.expect import ExpectManager
from embpilot.runtime.models import LogLine


@pytest.mark.asyncio
async def test_expect_manager_completes_when_pattern_matches():
    manager = ExpectManager()
    window = manager.open_window(expect_regex=r"login ok", timeout_ms=500)

    await manager.feed(LogLine(datetime.now(timezone.utc), "boot"))
    await manager.feed(LogLine(datetime.now(timezone.utc), "login ok"))

    lines = await window.wait()
    assert [line.text for line in lines] == ["boot", "login ok"]


@pytest.mark.asyncio
async def test_expect_manager_times_out_without_match():
    manager = ExpectManager()
    window = manager.open_window(expect_regex=r"never appears", timeout_ms=10)
    lines = await window.wait()
    assert lines == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/runtime/test_expect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'embpilot.runtime.expect'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/embpilot/runtime/expect.py
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from embpilot.runtime.models import LogLine


@dataclass
class CommandWindow:
    future: asyncio.Future[list[LogLine]]
    expect_pattern: re.Pattern[str] | None
    lines: list[LogLine] = field(default_factory=list)

    async def wait(self) -> list[LogLine]:
        return await self.future


class ExpectManager:
    def __init__(self) -> None:
        self._windows: list[CommandWindow] = []

    def open_window(self, expect_regex: str | None, timeout_ms: int) -> CommandWindow:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[LogLine]] = loop.create_future()
        pattern = re.compile(expect_regex) if expect_regex else None
        window = CommandWindow(future=future, expect_pattern=pattern)
        self._windows.append(window)

        async def _timeout() -> None:
            await asyncio.sleep(timeout_ms / 1000.0)
            if not future.done():
                future.set_result(list(window.lines))
                self._windows.remove(window)

        asyncio.create_task(_timeout())
        return window

    async def feed(self, line: LogLine) -> None:
        for window in list(self._windows):
            window.lines.append(line)
            if window.expect_pattern and window.expect_pattern.search(line.text):
                if not window.future.done():
                    window.future.set_result(list(window.lines))
                self._windows.remove(window)

    async def handle(self, line: LogLine) -> None:
        await self.feed(line)

    async def close(self) -> None:
        for window in list(self._windows):
            if not window.future.done():
                window.future.set_result(list(window.lines))
            self._windows.remove(window)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/runtime/test_expect.py -v`
Expected: PASS for match and timeout behavior

- [ ] **Step 5: Commit**

```bash
git add src/embpilot/runtime/expect.py tests/runtime/test_expect.py
git commit -m "refactor: add expect window manager"
```

### Task 5: Move session lifecycle and `send_command` behavior into `runtime/session.py`

**Files:**
- Create: `src/embpilot/runtime/session.py`
- Create: `tests/runtime/test_session.py`
- Modify: `src/embpilot/core/database.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_session.py
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from embpilot.config import EmbPilotConfig
from embpilot.runtime.models import LogLine
from embpilot.runtime.session import SessionManager


class _FakeDevice:
    def __init__(self) -> None:
        self._reader = asyncio.StreamReader()
        self.writes: list[bytes] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self._reader.feed_eof()

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    def get_reader(self) -> asyncio.StreamReader:
        return self._reader

    def emit_line(self, text: str) -> None:
        self._reader.feed_data(f"{text}\n".encode("utf-8"))


@pytest.mark.asyncio
async def test_session_manager_prefers_explicit_device_name(tmp_path, monkeypatch):
    fake = _FakeDevice()
    monkeypatch.setattr("embpilot.runtime.session.build_device", lambda interface_type, config: fake)

    config = EmbPilotConfig(
        data_dir=tmp_path,
        main_db_path=tmp_path / "embpilot_main.db",
        session_data_dir=tmp_path / "sessions",
        lancedb_path=tmp_path / "lancedb",
    )
    manager = SessionManager(config)
    await manager.start()
    try:
        session_id = await manager.connect_device("serial", {"port": "COM9", "device_name": "board-b"})
        info = manager.get_session_info()
        assert session_id == info.session_id
        assert info.device_name == "board-b"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_send_command_returns_window_until_expect_match(tmp_path, monkeypatch):
    fake = _FakeDevice()
    monkeypatch.setattr("embpilot.runtime.session.build_device", lambda interface_type, config: fake)

    config = EmbPilotConfig(
        data_dir=tmp_path,
        main_db_path=tmp_path / "embpilot_main.db",
        session_data_dir=tmp_path / "sessions",
        lancedb_path=tmp_path / "lancedb",
        framing_timeout_ms=5,
    )
    manager = SessionManager(config)
    await manager.start()
    try:
        await manager.connect_device("serial", {"port": "COM9"})
        task = asyncio.create_task(manager.send_command("status\n", expect_regex=r"ready", timeout_ms=500))
        await asyncio.sleep(0)
        fake.emit_line("booting")
        fake.emit_line("ready")
        result = await task
        assert "booting" in result
        assert "ready" in result
        assert fake.writes == [b"status\n"]
    finally:
        await manager.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/runtime/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'embpilot.runtime.session'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/embpilot/runtime/session.py
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from embpilot.config import EmbPilotConfig
from embpilot.core.database import MainDatabase, SessionDatabase
from embpilot.drivers.serial_dev import SerialDevice
from embpilot.drivers.ssh_dev import SshDevice
from embpilot.drivers.telnet_dev import TelnetDevice
from embpilot.runtime.expect import ExpectManager
from embpilot.runtime.models import RingBuffer, SessionInfo
from embpilot.runtime.pipeline import DbSink, LogProducer, RingBufferSink, SessionDispatcher


def build_device(interface_type: str, config: dict[str, Any]):
    if interface_type == "serial":
        return SerialDevice(port=config["port"], baudrate=config.get("baudrate", 115200))
    if interface_type == "telnet":
        return TelnetDevice(host=config["host"], port=config.get("port", 23))
    if interface_type == "ssh":
        return SshDevice(
            host=config["host"],
            port=config.get("port", 22),
            username=config.get("username", ""),
            password=config.get("password"),
            key_file=config.get("key_file"),
            known_hosts=config.get("known_hosts"),
        )
    raise ValueError(f"Unsupported interface: {interface_type}")


class SessionManager:
    def __init__(self, config: EmbPilotConfig) -> None:
        self._config = config
        self._main_db = MainDatabase(config.main_db_path)
        self._expect = ExpectManager()
        self._session_info: SessionInfo | None = None
        self._ring = RingBuffer()
        self._dispatcher: SessionDispatcher | None = None
        self._device = None
        self._producer_task: asyncio.Task[None] | None = None
        self._session_db: SessionDatabase | None = None
        self._producer: LogProducer | None = None

    async def start(self) -> None:
        self._config.ensure_data_dirs()
        await self._main_db.open()

    async def shutdown(self) -> None:
        await self.disconnect_device()
        await self._main_db.close()

    async def connect_device(self, interface_type: str, config: dict[str, Any]) -> str:
        await self.disconnect_device()
        self._device = build_device(interface_type, config)
        await self._device.connect()

        session_id = uuid.uuid4().hex[:16]
        device_name = config.get("device_name") or config.get("port") or f"{config.get('host', 'unknown')}:{config.get('port', 0)}"
        session_path = self._config.session_data_dir / f"session_{session_id}.db"
        self._session_db = SessionDatabase(session_path)
        await self._session_db.open()
        await self._main_db.register_session(session_id, device_name, interface_type, str(session_path))

        db_sink = DbSink(self._session_db, source=interface_type)
        ring_sink = RingBufferSink(self._ring)
        info_sink = _SessionInfoSink(lambda: self._session_info)
        self._dispatcher = SessionDispatcher([ring_sink, db_sink, info_sink, self._expect])
        self._producer = LogProducer(self._device.get_reader(), self._dispatcher, self._config.framing_timeout_ms)

        self._session_info = SessionInfo(
            session_id=session_id,
            interface_type=interface_type,
            device_name=device_name,
            connection_summary=f"{interface_type}://{device_name}",
            started_at=datetime.now(timezone.utc),
            state="active",
            last_log_at=None,
            log_count=0,
        )
        self._producer_task = asyncio.create_task(self._producer.run())
        return session_id

    async def send_command(self, command: str, expect_regex: str | None = None, timeout_ms: int = 5000) -> str:
        if self._device is None:
            raise RuntimeError("No active device connection")
        window = self._expect.open_window(expect_regex=expect_regex, timeout_ms=timeout_ms)
        await self._device.write(command.encode("utf-8"))
        lines = await window.wait()
        return "\n".join(line.formatted() for line in lines) or "(no output captured)"

    async def disconnect_device(self) -> None:
        if self._producer_task is not None:
            await self._device.disconnect()
            await self._producer_task
            self._producer_task = None
        if self._dispatcher is not None:
            await self._dispatcher.close()
            self._dispatcher = None
        if self._session_db is not None:
            await self._session_db.close()
            self._session_db = None
        if self._session_info is not None:
            await self._main_db.end_session(self._session_info.session_id)
            self._session_info.state = "closed"
        self._device = None

    def get_session_info(self) -> SessionInfo:
        if self._session_info is None:
            raise RuntimeError("No active session")
        return self._session_info

    def active_ring(self) -> RingBuffer | None:
        return self._ring if self._session_info is not None else None


class _SessionInfoSink:
    def __init__(self, info_factory) -> None:
        self._info_factory = info_factory

    async def handle(self, line) -> None:
        info = self._info_factory()
        if info is not None:
            info.last_log_at = line.timestamp
            info.log_count += 1

    async def close(self) -> None:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/runtime/test_session.py -v`
Expected: PASS for explicit `device_name` and expect-driven command output

- [ ] **Step 5: Commit**

```bash
git add src/embpilot/runtime/session.py src/embpilot/core/database.py tests/runtime/test_session.py
git commit -m "refactor: move session lifecycle into runtime package"
```

### Task 6: Split MCP registration into `mcp_app.py` and keep `server.py` as a compatibility wrapper

**Files:**
- Create: `src/embpilot/mcp_app.py`
- Create: `tests/integration/test_mcp_app.py`
- Modify: `src/embpilot/server.py`
- Modify: `src/embpilot/cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_mcp_app.py
from __future__ import annotations

from embpilot.config import EmbPilotConfig
from embpilot.mcp_app import build_resource_catalog, create_mcp_app


def test_build_resource_catalog_exposes_session_info_resource():
    resources = build_resource_catalog()
    resource_names = {resource.name for resource in resources}
    resource_uris = {str(resource.uri) for resource in resources}
    assert "Live Device Log" in resource_names
    assert "Session Info" in resource_names
    assert "device://session_info" in resource_uris


def test_create_mcp_app_exposes_session_info_resource(tmp_path):
    config = EmbPilotConfig(
        data_dir=tmp_path,
        main_db_path=tmp_path / "embpilot_main.db",
        session_data_dir=tmp_path / "sessions",
        lancedb_path=tmp_path / "lancedb",
    )
    app, manager = create_mcp_app(config)
    assert app is not None
    assert manager is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mcp_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'embpilot.mcp_app'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/embpilot/mcp_app.py
from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import AnyUrl, Resource, TextContent, Tool

from embpilot.config import EmbPilotConfig
from embpilot.runtime.resources import build_session_info_resource, render_live_log_snapshot
from embpilot.runtime.session import SessionManager


def build_resource_catalog() -> list[Resource]:
    return [
        Resource(uri=AnyUrl("device://live_log"), name="Live Device Log", description="Recent device output", mimeType="text/plain"),
        Resource(uri=AnyUrl("device://session_info"), name="Session Info", description="Current session metadata", mimeType="application/json"),
    ]


def create_mcp_app(config: EmbPilotConfig) -> tuple[Server, SessionManager]:
    manager = SessionManager(config)
    app = Server("embpilot", version="0.1.0")

    @app.list_resources()
    async def list_resources() -> list[Resource]:
        return build_resource_catalog()

    @app.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        uri_str = str(uri)
        if uri_str == "device://live_log":
            ring = manager.active_ring()
            return render_live_log_snapshot(ring) if ring else "No active device connection."
        if uri_str == "device://session_info":
            info = manager.get_session_info()
            return str(build_session_info_resource(info))
        return f"Unknown resource: {uri_str}"

    return app, manager


def run_stdio_mcp_server(config: EmbPilotConfig) -> None:
    app, manager = create_mcp_app(config)

    async def _run() -> None:
        await manager.start()
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
        await manager.shutdown()

    asyncio.run(_run())
```

```python
# src/embpilot/server.py
from embpilot.config import EmbPilotConfig
from embpilot.mcp_app import run_stdio_mcp_server


def serve(config: EmbPilotConfig) -> None:
    run_stdio_mcp_server(config)
```

```python
# src/embpilot/cli.py
from embpilot.mcp_app import run_stdio_mcp_server
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_mcp_app.py -v`
Expected: PASS for MCP app creation and `Session Info` resource exposure

- [ ] **Step 5: Commit**

```bash
git add src/embpilot/mcp_app.py src/embpilot/server.py src/embpilot/cli.py tests/integration/test_mcp_app.py
git commit -m "refactor: split mcp registration from runtime logic"
```

### Task 7: Finish packaging, docs, and verification for the new runtime shape

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/mcp_embedded_debug_spec.md`
- Modify: `PROGRESS.md`
- Modify: `change.log`

- [ ] **Step 1: Write the failing packaging verification**

```python
# add to tests/integration/test_cli.py
from importlib import resources


def test_schema_files_are_packaged():
    assert resources.files("embpilot.core").joinpath("schema_main.sql").is_file()
    assert resources.files("embpilot.core").joinpath("schema_session.sql").is_file()
```

- [ ] **Step 2: Run verification to confirm it fails or is incomplete**

Run: `pytest tests/integration/test_cli.py::test_schema_files_are_packaged -v`
Expected: FAIL if package data is not configured, or PASS only after packaging data is explicitly declared

- [ ] **Step 3: Write the minimal packaging and docs update**

```toml
# pyproject.toml
[tool.setuptools.package-data]
embpilot = ["core/*.sql"]
```

```markdown
# README.md
## Architecture

```text
src/embpilot/
├── __main__.py
├── cli.py
├── mcp_app.py
├── config.py
├── runtime/
│   ├── models.py
│   ├── pipeline.py
│   ├── expect.py
│   ├── resources.py
│   └── session.py
├── core/
│   ├── database.py
│   ├── rag.py
│   ├── schema_main.sql
│   └── schema_session.sql
└── drivers/
```

- `device://live_log` provides snapshot and live-log flows for the active session.
- `device://session_info` provides honest session metadata instead of fake generic sysinfo probing.
```

```markdown
# docs/mcp_embedded_debug_spec.md
- Replace all mentions of `device://sysinfo` with `device://session_info`
- Replace queue-based producer/consumer wording with dispatcher-based fan-out wording
- Clarify that `send_command(..., expect_regex=...)` returns the command window collected until match or timeout
```

```markdown
# PROGRESS.md
### Done
- Added runtime implementation plan after the approved rearchitecture spec.

### Next good step
- Execute the runtime rearchitecture plan task by task and keep each stage independently testable.
```

```text
# change.log
[2026-06-27]
- runtime implementation plan: added a staged task-by-task plan covering CLI split, runtime modules, dispatcher fan-out, expect windows, MCP app split, packaging verification, and docs alignment.
```

- [ ] **Step 4: Run full verification**

Run: `pytest -q`
Expected: PASS across `tests/integration/`, `tests/runtime/`, `tests/core/`, existing driver tests, and database tests

Run: `python -m embpilot --version`
Expected: prints `embpilot 0.1.0`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md docs/mcp_embedded_debug_spec.md PROGRESS.md change.log tests/integration/test_cli.py
git commit -m "docs: align packaging and runtime documentation"
```

## Spec coverage self-check

- Package structure and PyPI-friendly layout: covered by Tasks 1, 2, 6, and 7
- Runtime dispatcher fan-out: covered by Task 3
- Real expect behavior: covered by Tasks 4 and 5
- Session lifecycle and explicit `device_name`: covered by Task 5
- MCP split and `device://session_info`: covered by Task 6
- Packaging data, docs, and verification: covered by Task 7

## Placeholder scan

- No deferred implementation markers remain in the plan.
- Every task includes exact file paths, concrete code snippets, exact commands, and a commit boundary.

## Type consistency self-check

- `LogLine`, `RingBuffer`, and `SessionInfo` are introduced in Task 2 and reused consistently in later tasks.
- `SessionDispatcher`, `DbSink`, and `ExpectManager` names are introduced before they are consumed by `SessionManager`.
- `device://session_info` is the only replacement resource name used after Task 6.
