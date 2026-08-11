"""REPL tests with a fake session manager - no live targets."""

from __future__ import annotations

import pytest

from embpilot.cli_shell import shell_loop
from embpilot.core.commands import CommandResult


class FakeManager:
    def __init__(self) -> None:
        self.connected = False
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
