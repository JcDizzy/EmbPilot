from __future__ import annotations

import asyncio

import asyncssh
import pytest
import telnetlib3

from embpilot.drivers.ssh_dev import SshDevice
from embpilot.drivers.telnet_dev import TelnetDevice


@pytest.mark.asyncio
async def test_telnet_device_round_trips_bytes_against_fake_server() -> None:
    async def shell(reader, writer) -> None:
        writer.write("ready\n")
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                break
            writer.write(f"echo:{line}")
            await writer.drain()

    server = await telnetlib3.create_server(
        host="127.0.0.1",
        port=0,
        shell=shell,
        connect_maxwait=1,
    )
    port = server.sockets[0].getsockname()[1]
    device = TelnetDevice(
        host="127.0.0.1",
        port=port,
        timeout=1,
        connect_minwait=0,
        connect_maxwait=1,
    )

    try:
        await device.connect()

        greeting = await asyncio.wait_for(device.get_reader().read(16), timeout=2)
        assert b"ready" in greeting
        await device.write(b"status\n")
        response = await asyncio.wait_for(device.get_reader().read(12), timeout=2)

        assert b"echo:status" in response
    finally:
        await device.disconnect()
        server.close()
        await server.wait_closed()


class _FakeSshServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False

    def session_requested(self):
        async def handler(stdin, stdout, stderr) -> None:
            stdout.write("ready\n")
            async for line in stdin:
                stdout.write(f"echo:{line}")

        return handler


@pytest.mark.asyncio
async def test_ssh_device_round_trips_bytes_against_fake_shell_server() -> None:
    host_key = asyncssh.generate_private_key("ssh-rsa")
    server = await asyncssh.create_server(
        _FakeSshServer,
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        encoding="utf-8",
    )
    port = server.get_port()
    device = SshDevice(
        host="127.0.0.1",
        port=port,
        username="tester",
        known_hosts=None,
        encoding="utf-8",
    )

    try:
        await device.connect()

        greeting = await asyncio.wait_for(device.get_reader().read(16), timeout=2)
        assert b"ready" in greeting
        await device.write(b"status\n")
        response = await asyncio.wait_for(device.get_reader().read(12), timeout=2)

        assert b"echo:status" in response
    finally:
        await device.disconnect()
        server.close()
        await server.wait_closed()
