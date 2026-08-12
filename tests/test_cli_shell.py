"""REPL tests with a fake session manager - no live targets."""

from __future__ import annotations

import asyncio

import pytest

from embpilot.cli_loop import batch_loop
from embpilot.cli_shell import shell_loop
from embpilot.core.commands import CommandResult


class FakeManager:
    def __init__(self) -> None:
        self.connected = False
        self.active_ring = None
        self.sent: list[str] = []
        self.sessions: list[dict] = []

    async def connect_device(self, interface: str, config: dict) -> str:
        self.connected = True
        return "session-1"

    async def send_command(self, **kwargs: object) -> CommandResult:
        command = str(kwargs["command"])
        self.sent.append(command)
        return CommandResult(
            output=f"out:{command}",
            matched=True,
            timed_out=False,
            truncated=False,
        )

    async def list_sessions(self) -> list[dict]:
        return self.sessions

    async def disconnect_device(self) -> None:
        self.connected = False

    async def reset_target(self, method: str = "reboot") -> str:
        return "reset sent"

    async def search_history_logs(self, **kwargs: object) -> list[dict]:
        return []

    async def delete_session(self, session_id: str) -> None:
        return None

    async def export_session(self, session_id: str, target_path: object) -> object:
        return target_path


async def _run_loop(manager: FakeManager, lines: list[str]) -> None:
    iterator = iter(lines)

    async def read_line() -> str:
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError from None

    await shell_loop(manager, read_line=read_line)


@pytest.mark.asyncio
async def test_shell_runs_tools_against_persistent_manager(capsys) -> None:
    manager = FakeManager()
    await _run_loop(
        manager,
        [
            'connect_serial {"port": "COM3", "baudrate": 115200}',
            'send_command {"command": "help"}',
            'list_sessions {}',
            "exit",
        ],
    )

    captured = capsys.readouterr().out
    assert manager.connected is True
    assert manager.sent == ["help"]
    assert "out:help" in captured
    assert "Session ID: session-1" in captured


@pytest.mark.asyncio
async def test_shell_reports_unknown_tool_and_invalid_json(capsys) -> None:
    manager = FakeManager()
    await _run_loop(
        manager,
        [
            "bogus {}",
            'send_command {"command": "x"',
            "exit",
        ],
    )

    captured = capsys.readouterr().out
    assert "unknown tool 'bogus'" in captured
    assert "invalid JSON arguments" in captured


@pytest.mark.asyncio
async def test_shell_help_and_empty_lines(capsys) -> None:
    manager = FakeManager()
    await _run_loop(manager, ["", "help", "quit"])

    captured = capsys.readouterr().out
    assert "Tools:" in captured
    assert "connect_serial" in captured
@pytest.mark.asyncio
async def test_shell_strips_utf8_bom_from_first_line(capsys) -> None:
    manager = FakeManager()
    await _run_loop(
        manager,
        [
            "\ufefflist_sessions {}",
            "exit",
        ],
    )

    captured = capsys.readouterr().out
    assert "unknown tool" not in captured
    assert "Found 0 session(s)." in captured
class FailingSendManager(FakeManager):
    """A fake manager whose send_command always raises."""

    async def send_command(self, **kwargs: object) -> CommandResult:
        raise RuntimeError("device exploded")


async def _run_batch(
    manager: object,
    lines: list[str],
    *,
    fail_fast: bool = False,
) -> int:
    iterator = iter(lines)

    async def read_line() -> str:
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError from None

    return await batch_loop(manager, read_line=read_line, fail_fast=fail_fast)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_batch_runs_full_sequence_and_returns_zero(capsys) -> None:
    manager = FakeManager()
    code = await _run_batch(
        manager,
        [
            '{"tool": "connect_serial", "args": {"port": "COM3"}}',
            '{"tool": "send_command", "args": {"command": "help"}}',
            '{"tool": "disconnect_device", "args": {}}',
        ],
    )

    out = capsys.readouterr().out
    assert code == 0
    assert manager.sent == ["help"]
    assert manager.connected is False  # disconnected at the end
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 3
    assert all('"ok": true' in line for line in lines)


@pytest.mark.asyncio
async def test_batch_continues_after_failure_and_returns_1(capsys) -> None:
    manager = FailingSendManager()
    code = await _run_batch(
        manager,
        [
            '{"tool": "send_command", "args": {"command": "a"}}',
            '{"tool": "list_sessions", "args": {}}',
        ],
    )

    out = capsys.readouterr().out
    assert code == 1
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert '"ok": false' in lines[0]
    assert 'OPERATION_FAILED' in lines[0]
    assert '"ok": true' in lines[1]


@pytest.mark.asyncio
async def test_batch_fail_fast_stops_after_first_failure(capsys) -> None:
    manager = FailingSendManager()
    code = await _run_batch(
        manager,
        [
            '{"tool": "send_command", "args": {"command": "a"}}',
            '{"tool": "list_sessions", "args": {}}',
        ],
        fail_fast=True,
    )

    out = capsys.readouterr().out
    assert code == 1
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert '"ok": false' in lines[0]


@pytest.mark.asyncio
async def test_batch_invalid_json_returns_2(capsys) -> None:
    code = await _run_batch(FakeManager(), ['{"tool": "list_sessions"'])

    assert code == 2
    assert "invalid JSON" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_batch_unknown_tool_returns_2(capsys) -> None:
    code = await _run_batch(FakeManager(), ['{"tool": "bogus"}'])

    assert code == 2
    assert "unknown tool 'bogus'" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_batch_non_object_args_returns_2(capsys) -> None:
    code = await _run_batch(FakeManager(), ['{"tool": "list_sessions", "args": []}'])

    assert code == 2
    assert "args must be a JSON object" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_batch_ignores_comments_empty_lines_and_exit(capsys) -> None:
    manager = FakeManager()
    code = await _run_batch(
        manager,
        [
            "",
            "# a comment",
            '{"tool": "list_sessions", "args": {}}',
            "exit",
            '{"tool": "list_sessions", "args": {}}',  # must not run
        ],
    )

    out = capsys.readouterr().out
    assert code == 0
    assert len([line for line in out.splitlines() if line.strip()]) == 1


@pytest.mark.asyncio
async def test_monitor_streams_logs_and_prefixes_commands(capsys) -> None:
    from datetime import datetime, timezone

    from embpilot.core.engine import LogLine, RingBuffer

    manager = FakeManager()
    ring = RingBuffer()
    manager.active_ring = ring
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def read_line() -> str:
        return await queue.get()

    async def feeder() -> None:
        await queue.put("monitor")
        await asyncio.sleep(0.25)
        ring.push(LogLine(datetime.now(timezone.utc), "hello from device"))
        await asyncio.sleep(0.25)
        await queue.put('send_command {"command": "x"}')
        await asyncio.sleep(0.15)
        await queue.put("stop")
        await queue.put("exit")

    await asyncio.gather(shell_loop(manager, read_line=read_line), feeder())

    out = capsys.readouterr().out
    assert "[log] [" in out and "hello from device" in out
    assert "[cmd] out:x" in out
    assert "monitor off" in out


@pytest.mark.asyncio
async def test_monitor_requires_active_connection(capsys) -> None:
    manager = FakeManager()
    await _run_loop(manager, ["monitor", "exit"])

    out = capsys.readouterr().out
    assert "monitor needs an active device connection" in out


@pytest.mark.asyncio
async def test_stop_without_monitor_reports_not_running(capsys) -> None:
    manager = FakeManager()
    await _run_loop(manager, ["stop", "exit"])

    out = capsys.readouterr().out
    assert "monitor is not running" in out
