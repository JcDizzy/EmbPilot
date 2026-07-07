"""
Abstract base class for all device drivers (Serial, Telnet, SSH).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable


@runtime_checkable
class ByteReader(Protocol):
    """Minimal byte-stream reader contract consumed by the runtime pipeline."""

    async def read(self, n: int = -1) -> bytes:
        ...


class TextReaderAsBytes:
    """Adapt text-mode network readers to EmbPilot's byte-stream contract."""

    def __init__(self, reader: object, encoding: str = "utf-8") -> None:
        self._reader = reader
        self._encoding = encoding

    async def read(self, n: int = -1) -> bytes:
        data = await self._reader.read(n)
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode(self._encoding)
        if data is None:
            return b""
        raise TypeError(f"Reader returned unsupported type: {type(data).__name__}")


def write_bytes_to_text_stream(writer: object, data: bytes, encoding: str = "utf-8") -> None:
    """Write bytes through text-first stream writers."""

    try:
        writer.write(data.decode(encoding, errors="replace"))
    except TypeError:
        writer.write(data)


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
    def get_reader(self) -> ByteReader:
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
