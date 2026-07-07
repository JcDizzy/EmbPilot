"""
SSH device driver — wraps asyncssh.
"""

from __future__ import annotations

import logging
from typing import Optional

import asyncssh

from embpilot.drivers.base import (
    BaseDevice,
    ByteReader,
    TextReaderAsBytes,
    write_bytes_to_text_stream,
)

logger = logging.getLogger(__name__)


class SshDevice(BaseDevice):
    """Connect to a device over SSH and open an interactive shell session.

    Parameters
    ----------
    host:
        Target hostname or IP address.
    port:
        SSH port (default 22).
    username:
        Login username.
    password:
        Password for authentication (optional if key-based).
    key_file:
        Path to a private key file (optional).
    known_hosts:
        Path to known_hosts file, or ``None`` to skip host-key verification.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        password: Optional[str] = None,
        key_file: Optional[str] = None,
        known_hosts: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._key_file = key_file
        self._known_hosts = known_hosts
        self._extra_kwargs = kwargs

        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._chan: Optional[asyncssh.SSHClientProcess] = None
        self._byte_reader: Optional[ByteReader] = None

    async def connect(self) -> None:
        logger.info("Connecting via SSH to %s@%s:%d", self._username, self._host, self._port)

        connect_kwargs = {
            "host": self._host,
            "port": self._port,
            "username": self._username,
            "known_hosts": self._known_hosts,
        }
        if self._password is not None:
            connect_kwargs["password"] = self._password
        if self._key_file is not None:
            connect_kwargs["client_keys"] = [self._key_file]

        self._conn = await asyncssh.connect(**connect_kwargs, **self._extra_kwargs)
        self._chan = await self._conn.create_process()
        self._byte_reader = TextReaderAsBytes(self._chan.stdout)
        self._connected = True
        logger.info("SSH session to %s@%s:%d established", self._username, self._host, self._port)

    async def disconnect(self) -> None:
        if self._chan is not None:
            self._chan.close()
            await self._chan.wait_closed()
            self._chan = None
            self._byte_reader = None
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
        self._connected = False
        logger.info("SSH session to %s@%s:%d closed", self._username, self._host, self._port)

    async def write(self, data: bytes) -> None:
        if self._chan is None:
            raise RuntimeError("Not connected")
        write_bytes_to_text_stream(self._chan.stdin, data)
        await self._chan.stdin.drain()

    def get_reader(self) -> ByteReader:
        if self._byte_reader is None:
            raise RuntimeError("Not connected")
        return self._byte_reader
