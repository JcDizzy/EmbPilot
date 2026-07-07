"""
Telnet device driver — wraps telnetlib3.
"""

from __future__ import annotations

import logging
from typing import Optional

import telnetlib3

from embpilot.drivers.base import (
    BaseDevice,
    ByteReader,
    TextReaderAsBytes,
    write_bytes_to_text_stream,
)

logger = logging.getLogger(__name__)


class TelnetDevice(BaseDevice):
    """Connect to a device over the Telnet protocol.

    Parameters
    ----------
    host:
        Target hostname or IP address.
    port:
        TCP port (default 23).
    timeout:
        Connection timeout in seconds (default 10.0).
    """

    def __init__(
        self,
        host: str,
        port: int = 23,
        timeout: float = 10.0,
        **kwargs,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._timeout = timeout
        self._extra_kwargs = kwargs

        self._reader: Optional[object] = None
        self._writer: Optional[object] = None
        self._byte_reader: Optional[ByteReader] = None

    async def connect(self) -> None:
        logger.info("Connecting via Telnet to %s:%d", self._host, self._port)
        self._reader, self._writer = await telnetlib3.open_connection(
            host=self._host,
            port=self._port,
            connect_timeout=self._timeout,
            **self._extra_kwargs,
        )
        self._byte_reader = TextReaderAsBytes(self._reader)
        self._connected = True
        logger.info("Telnet connection to %s:%d established", self._host, self._port)

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None
        self._byte_reader = None
        self._connected = False
        logger.info("Telnet connection to %s:%d closed", self._host, self._port)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise RuntimeError("Not connected")
        write_bytes_to_text_stream(self._writer, data)
        await self._writer.drain()

    def get_reader(self) -> ByteReader:
        if self._byte_reader is None:
            raise RuntimeError("Not connected")
        return self._byte_reader
