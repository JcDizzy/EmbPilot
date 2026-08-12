"""RPC daemon/client tests over loopback transports - no live targets.

The TCP loopback path runs everywhere (Windows included); the unix-socket
path is exercised on POSIX only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from pytest_asyncio import fixture as async_fixture

from embpilot.core.commands import CommandResult, NoActiveDeviceError
from embpilot.rpc import (
    RpcClient,
    RpcServer,
    default_endpoint,
    parse_endpoint,
    resolve_endpoint,
    write_endpoint_file,
)

UNIX_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="unix sockets are unavailable on Windows",
)


class FakeManager:
    def __init__(self) -> None:
        self.connected = False
        self.sent: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    async def connect_device(self, interface: str, config: dict) -> str:
        self.connected = True
        self.calls.append(("connect_device", config))
        return "session-1"

    async def send_command(self, **kwargs: object) -> CommandResult:
        if not self.connected:
            raise NoActiveDeviceError("No active device connection")
        command = str(kwargs["command"])
        self.sent.append(command)
        self.calls.append(("send_command", kwargs))
        return CommandResult(
            output=f"out:{command}",
            matched=True,
            timed_out=False,
            truncated=False,
        )

    async def read_output(self, **kwargs: object) -> CommandResult:
        if not self.connected:
            raise NoActiveDeviceError("No active device connection")
        self.calls.append(("read_output", kwargs))
        return CommandResult(
            output="",
            matched=False,
            timed_out=True,
            truncated=False,
        )

    async def disconnect_device(self) -> None:
        self.connected = False
        self.calls.append(("disconnect_device", {}))

    async def reset_target(self, method: str = "reboot") -> str:
        self.calls.append(("reset_target", {"method": method}))
        return "reset sent"

    async def search_history_logs(self, **kwargs: object) -> list[dict]:
        self.calls.append(("search_history_logs", kwargs))
        return []

    async def list_sessions(self) -> list[dict]:
        self.calls.append(("list_sessions", {}))
        return [{"session_id": "session-1", "status": "open"}] if self.connected else []

    async def delete_session(self, session_id: str) -> None:
        self.calls.append(("delete_session", {"session_id": session_id}))

    async def export_session(self, session_id: str, target_path: object) -> object:
        self.calls.append(
            ("export_session", {"session_id": session_id, "target_path": target_path})
        )
        return target_path


@async_fixture
async def tcp_server(tmp_path):
    manager = FakeManager()
    server = RpcServer(manager, endpoint="tcp:127.0.0.1:0")
    await server.start()
    task = asyncio.create_task(server.serve_forever())
    try:
        yield manager, server.listening_endpoint
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await server.close()


def test_parse_endpoint_forms() -> None:
    assert parse_endpoint("unix:/tmp/x.sock") == ("unix", "/tmp/x.sock")
    assert parse_endpoint("tcp:127.0.0.1:1234") == ("tcp", "127.0.0.1:1234")
    with pytest.raises(ValueError):
        parse_endpoint("bogus")
    with pytest.raises(ValueError):
        parse_endpoint("tcp:127.0.0.1")


def test_default_endpoint_is_platform_appropriate(tmp_path: Path) -> None:
    endpoint = default_endpoint(tmp_path)
    if sys.platform == "win32":
        assert endpoint.startswith("tcp:127.0.0.1:")
    else:
        assert endpoint == f"unix:{tmp_path / 'embpilot.sock'}"


def test_resolve_endpoint_accepts_daemon_file(tmp_path: Path) -> None:
    path = write_endpoint_file(tmp_path, "tcp:127.0.0.1:9")
    assert resolve_endpoint(str(path)) == "tcp:127.0.0.1:9"
    assert resolve_endpoint("unix:/tmp/x.sock") == "unix:/tmp/x.sock"
    with pytest.raises(ValueError):
        resolve_endpoint(str(tmp_path / "missing.json"))


@pytest.mark.asyncio
async def test_round_trip_returns_envelope_with_text(tcp_server) -> None:
    _manager, endpoint = tcp_server
    client = RpcClient(endpoint)
    await client.connect()
    try:
        response = await client.call("list_sessions", {})
        assert response["ok"] is True
        assert response["data"] == {"sessions": []}
        assert response["id"] == 1
        assert "Found 0 session(s)." in response["text"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_state_survives_across_calls(tcp_server) -> None:
    manager, endpoint = tcp_server
    client = RpcClient(endpoint)
    await client.connect()
    try:
        connected = await client.call("connect_serial", {"port": "COM3"})
        assert connected["ok"] is True
        assert connected["data"]["session_id"] == "session-1"

        command = await client.call("send_command", {"command": "help"})
        assert command["ok"] is True
        assert command["data"]["output"] == "out:help"

        listed = await client.call("list_sessions", {})
        assert listed["data"]["sessions"] == [
            {"session_id": "session-1", "status": "open"}
        ]

        disconnected = await client.call("disconnect_device", {})
        assert disconnected["ok"] is True
        assert manager.sent == ["help"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_two_clients_share_one_manager(tcp_server) -> None:
    manager, endpoint = tcp_server
    first = RpcClient(endpoint)
    second = RpcClient(endpoint)
    await first.connect()
    await second.connect()
    try:
        assert (await first.call("connect_serial", {"port": "COM3"}))["ok"] is True
        # The second client sees the connection made by the first.
        listed = await second.call("list_sessions", {})
        assert listed["data"]["sessions"] == [
            {"session_id": "session-1", "status": "open"}
        ]
        assert manager.connected is True
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_client_can_reconnect_after_close(tcp_server) -> None:
    _manager, endpoint = tcp_server
    first = RpcClient(endpoint)
    await first.connect()
    assert (await first.call("list_sessions", {}))["ok"] is True
    await first.close()

    second = RpcClient(endpoint)
    await second.connect()
    try:
        assert (await second.call("list_sessions", {}))["ok"] is True
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_tool_failure_returns_structured_error(tcp_server) -> None:
    _manager, endpoint = tcp_server
    client = RpcClient(endpoint)
    await client.connect()
    try:
        response = await client.call("send_command", {"command": "x"})
        assert response["ok"] is False
        assert response["error"]["code"] == "NO_ACTIVE_DEVICE"
        assert "NO_ACTIVE_DEVICE" in response["text"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_malformed_request_gets_parse_error(tcp_server) -> None:
    _manager, endpoint = tcp_server
    host, port_text = endpoint[len("tcp:") :].rsplit(":", 1)
    reader, writer = await asyncio.open_connection(host, int(port_text))
    writer.write(b"{bad json\n")
    await writer.drain()
    raw = await reader.readline()
    response = json.loads(raw.decode("utf-8"))
    writer.close()
    await writer.wait_closed()

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_ARGUMENT"


@UNIX_SKIP
@pytest.mark.asyncio
async def test_unix_socket_round_trip_and_cleanup() -> None:
    sock = Path(os.environ.get("TMPDIR", "/tmp")) / f"emb_test_{os.getpid()}.sock"
    manager = FakeManager()
    server = RpcServer(manager, endpoint=f"unix:{sock}")
    await server.start()
    task = asyncio.create_task(server.serve_forever())
    client = RpcClient(f"unix:{sock}")
    try:
        await client.connect()
        response = await client.call("list_sessions", {})
        assert response["ok"] is True
    finally:
        await client.close()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await server.close()
    assert not sock.exists()  # close() removes the socket file
