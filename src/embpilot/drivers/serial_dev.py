"""
Serial (UART) device driver — wraps pyserial-asyncio.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import serial_asyncio

from embpilot.drivers.base import BaseDevice

logger = logging.getLogger(__name__)


class SerialDevice(BaseDevice):
    """Connect to a device over a serial (RS-232 / UART) port.

    Parameters
    ----------
    port:
        Serial port name, e.g. ``"COM3"`` (Windows) or ``"/dev/ttyUSB0"`` (Linux).
    baudrate:
        Communication speed in bits per second (default 115200).
    bytesize:
        Number of data bits (default 8).
    parity:
        Parity checking (``"N"``, ``"E"``, ``"O"``, ``"M"``, ``"S"``).
    stopbits:
        Number of stop bits (default 1).
    timeout:
        Read timeout in seconds (default 5.0).
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 5.0,
        **kwargs,
    ) -> None:
        super().__init__()
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._timeout = timeout
        self._extra_kwargs = kwargs

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        logger.info("Opening serial port %s @ %d baud", self._port, self._baudrate)
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port,
            baudrate=self._baudrate,
            bytesize=self._bytesize,
            parity=self._parity,
            stopbits=self._stopbits,
            timeout=self._timeout,
            **self._extra_kwargs,
        )
        self._connected = True
        logger.info("Serial port %s opened", self._port)

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            if hasattr(self._writer, "wait_closed"):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None
        self._connected = False
        logger.info("Serial port %s closed", self._port)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise RuntimeError("Not connected")
        self._writer.write(data)
        await self._writer.drain()

    def get_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            raise RuntimeError("Not connected")
        return self._reader
