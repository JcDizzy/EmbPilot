"""
Abstract base class for all device drivers (Serial, Telnet, SSH).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class BaseDevice(ABC):
    """Interface that every hardware driver must implement.

    Each driver wraps an async connection oriented around a pair of
    ``StreamReader`` / ``StreamWriter``, which is consumed by
    ``embpilot.core.engine.LogProducer``.
    """

    def __init__(self) -> None:
        self._connected = False

    # ── Lifecycle ────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection to the physical device.

        Must set ``_connected = True`` on success and raise on failure.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection and release all resources.

        Must set ``_connected = False`` when done.
        """

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── I/O ──────────────────────────────────────────────────────────

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write raw *data* to the device."""

    @abstractmethod
    def get_reader(self) -> asyncio.StreamReader:
        """Return the stream reader consumed by ``LogProducer``.

        Raises ``RuntimeError`` if called before ``connect()``.
        """

    # ── Context manager support ──────────────────────────────────────

    async def __aenter__(self) -> "BaseDevice":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: object = None,
        exc_val: object = None,
        exc_tb: object = None,
    ) -> None:
        await self.disconnect()
