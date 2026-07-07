"""
Tests for device drivers (Serial, Telnet, SSH).
Uses mocks to avoid real hardware.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from embpilot.drivers.serial_dev import SerialDevice
from embpilot.drivers.telnet_dev import TelnetDevice
from embpilot.drivers.ssh_dev import SshDevice


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_streams():
    """Return a (reader, writer) pair with mock dependencies."""
    reader = MagicMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(return_value=b"")
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


# ── SerialDevice ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_serial_connect_disconnect(mock_streams):
    reader, writer = mock_streams

    with patch("embpilot.drivers.serial_dev.serial_asyncio.open_serial_connection",
               new=AsyncMock(return_value=(reader, writer))):
        dev = SerialDevice(port="COM3", baudrate=115200)
        assert not dev.is_connected

        await dev.connect()
        assert dev.is_connected
        assert dev.get_reader() is reader

        await dev.disconnect()
        assert not dev.is_connected


@pytest.mark.asyncio
async def test_serial_write(mock_streams):
    reader, writer = mock_streams

    with patch("embpilot.drivers.serial_dev.serial_asyncio.open_serial_connection",
               new=AsyncMock(return_value=(reader, writer))):
        dev = SerialDevice(port="COM3")
        await dev.connect()

        await dev.write(b"help\n")
        writer.write.assert_called_once_with(b"help\n")
        writer.drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_serial_write_without_connect():
    dev = SerialDevice(port="COM3")
    with pytest.raises(RuntimeError, match="Not connected"):
        await dev.write(b"test")


@pytest.mark.asyncio
async def test_serial_get_reader_without_connect():
    dev = SerialDevice(port="COM3")
    with pytest.raises(RuntimeError, match="Not connected"):
        dev.get_reader()


# ── TelnetDevice ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telnet_connect_disconnect(mock_streams):
    reader, writer = mock_streams

    with patch("embpilot.drivers.telnet_dev.telnetlib3.open_connection",
               new=AsyncMock(return_value=(reader, writer))):
        dev = TelnetDevice(host="192.168.1.100", port=23)
        assert not dev.is_connected

        await dev.connect()
        assert dev.is_connected

        await dev.disconnect()
        assert not dev.is_connected


@pytest.mark.asyncio
async def test_telnet_write(mock_streams):
    reader, writer = mock_streams

    with patch("embpilot.drivers.telnet_dev.telnetlib3.open_connection",
               new=AsyncMock(return_value=(reader, writer))):
        dev = TelnetDevice(host="192.168.1.100")
        await dev.connect()

        await dev.write(b"ifconfig\n")
        writer.write.assert_called_once_with("ifconfig\n")
        writer.drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_telnet_reader_returns_bytes_from_text_stream(mock_streams):
    reader, writer = mock_streams
    reader.read = AsyncMock(return_value="boot ok\n")

    with patch(
        "embpilot.drivers.telnet_dev.telnetlib3.open_connection",
        new=AsyncMock(return_value=(reader, writer)),
    ):
        dev = TelnetDevice(host="192.168.1.100")
        await dev.connect()

        data = await dev.get_reader().read(4096)

        assert data == b"boot ok\n"


# ── SshDevice ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ssh_connect_disconnect():
    """SSH is slightly different — we mock the asyncssh.connect path."""

    mock_chan = MagicMock()
    mock_chan.stdout = asyncio.StreamReader()
    mock_chan.stdin = MagicMock()
    mock_chan.stdin.drain = AsyncMock()
    mock_chan.wait_closed = AsyncMock()

    mock_conn = MagicMock()
    mock_conn.create_process = AsyncMock(return_value=mock_chan)
    mock_conn.wait_closed = AsyncMock()

    with patch("embpilot.drivers.ssh_dev.asyncssh.connect",
               new=AsyncMock(return_value=mock_conn)):
        dev = SshDevice(host="192.168.1.100", username="root", password="secret")
        assert not dev.is_connected

        await dev.connect()
        assert dev.is_connected

        await dev.write(b"reboot\n")
        mock_conn.create_process.assert_awaited_once_with()
        mock_chan.stdin.write.assert_called_once_with("reboot\n")
        mock_chan.stdin.drain.assert_awaited_once()

        await dev.disconnect()
        assert not dev.is_connected


@pytest.mark.asyncio
async def test_ssh_with_key_file():
    """Ensure key_file is passed to asyncssh.connect as client_keys."""

    mock_chan = MagicMock()
    mock_chan.stdout = asyncio.StreamReader()
    mock_chan.stdin = MagicMock()
    mock_chan.stdin.drain = AsyncMock()
    mock_chan.wait_closed = AsyncMock()

    mock_conn = MagicMock()
    mock_conn.create_process = AsyncMock(return_value=mock_chan)
    mock_conn.wait_closed = AsyncMock()

    with patch("embpilot.drivers.ssh_dev.asyncssh.connect",
               new=AsyncMock(return_value=mock_conn)) as mock_connect:
        dev = SshDevice(
            host="10.0.0.1",
            username="admin",
            key_file="/home/user/.ssh/id_rsa",
        )
        await dev.connect()

        _call_kwargs = mock_connect.call_args[1]
        assert _call_kwargs["client_keys"] == ["/home/user/.ssh/id_rsa"]
        assert "password" not in _call_kwargs


@pytest.mark.asyncio
async def test_ssh_reader_returns_bytes_from_text_stream():
    mock_chan = MagicMock()
    mock_chan.stdout = MagicMock()
    mock_chan.stdout.read = AsyncMock(return_value="ready\n")
    mock_chan.stdin = MagicMock()
    mock_chan.stdin.drain = AsyncMock()
    mock_chan.wait_closed = AsyncMock()

    mock_conn = MagicMock()
    mock_conn.create_process = AsyncMock(return_value=mock_chan)
    mock_conn.wait_closed = AsyncMock()

    with patch(
        "embpilot.drivers.ssh_dev.asyncssh.connect",
        new=AsyncMock(return_value=mock_conn),
    ):
        dev = SshDevice(host="192.168.1.100", username="root")
        await dev.connect()

        data = await dev.get_reader().read(4096)

        assert data == b"ready\n"
