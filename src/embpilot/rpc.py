"""
Line-protocol RPC layer for the ``serve`` daemon and ``--socket`` clients.

Transport
---------
POSIX uses a unix socket (``unix:/path/to.sock``).  Windows has no unix
sockets and the standard library offers no named-pipe server API, so Windows
uses a TCP loopback socket bound to 127.0.0.1 (``tcp:127.0.0.1:PORT``).  Both
forms share the same wire protocol, so a daemon started on either platform is
used identically from the CLI.

Wire protocol (JSONL, one object per line)
------------------------------------------
request:  {"id": 1, "tool": "send_command", "args": {...}}
response: {"id": 1, "ok": true, "data": {...}, "text": "..."}
          {"id": 1, "ok": false, "error": {...}, "text": "..."}

``id`` correlates a response with its request inside one connection;
``text`` carries the human-readable content for terminal rendering.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

from embpilot.mcp_compat import result_structured
from embpilot.mcp_contracts import SessionOperations, dispatch_tool

_ENDPOINT_FILE_NAME = "daemon.json"


def parse_endpoint(endpoint: str) -> tuple[str, str]:
    """Split an endpoint string into (kind, address).

    Supported forms: ``unix:/path/to.sock`` and ``tcp:host:port``.
    """
    if endpoint.startswith("unix:"):
        return "unix", endpoint[len("unix:") :]
    if endpoint.startswith("tcp:"):
        address = endpoint[len("tcp:") :]
        _split_tcp_address(address)  # validate host:port up front
        return "tcp", address
    raise ValueError(
        "invalid endpoint (expected 'unix:PATH' or 'tcp:HOST:PORT'): "
        + endpoint
    )


def default_endpoint(data_dir: Path) -> str:
    """Choose the platform-appropriate default endpoint."""
    if sys.platform == "win32":
        return "tcp:127.0.0.1:0"
    return f"unix:{data_dir / 'embpilot.sock'}"


def _split_tcp_address(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"invalid tcp endpoint address: {address}")
    return host, int(port)


class RpcServer:
    """Accepts JSONL tool-call requests over one transport endpoint.

    One ``SessionManager`` is shared by every connection.  Tool dispatch is
    serialized with an asyncio lock so concurrent clients cannot interleave
    long-running device operations; responses always go back to the
    connection that sent the request.
    """

    def __init__(self, manager: SessionOperations, *, endpoint: str) -> None:
        self._manager = manager
        self._kind, self._address = parse_endpoint(endpoint)
        self._server: Optional[asyncio.AbstractServer] = None
        self._lock = asyncio.Lock()
        self._listening: Optional[str] = None

    @property
    def listening_endpoint(self) -> str:
        """The real endpoint (resolves port 0 to the bound port)."""
        if self._listening is None:
            raise RuntimeError("server has not started yet")
        return self._listening

    async def start(self) -> None:
        handler = self._handle_connection
        if self._kind == "unix":
            self._server = await asyncio.start_unix_server(handler, self._address)
            self._listening = f"unix:{self._address}"
        else:
            host, port = _split_tcp_address(self._address)
            self._server = await asyncio.start_server(handler, host, port)
            bound = self._server.sockets[0].getsockname()
            self._listening = f"tcp:{bound[0]}:{bound[1]}"

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("call start() first")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._kind == "unix":
            try:
                Path(self._address).unlink()
            except FileNotFoundError:
                pass

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                response = await self._dispatch_line(line)
                await self._write(writer, response)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch_line(self, line: str) -> dict:
        from embpilot.cli_loop import RequestParseError, parse_request_line

        try:
            request, name, arguments = parse_request_line(line)
        except RequestParseError as exc:
            return self._parse_error(None, str(exc))
        req_id = request.get("id")

        async with self._lock:
            result = await dispatch_tool(self._manager, name, arguments)
        payload = result_structured(result) or {}
        text = "\n".join(item.text for item in result.content if item.text)
        return {"id": req_id, **payload, "text": text}

    @staticmethod
    def _parse_error(req_id: Any, message: str) -> dict:
        return {
            "id": req_id,
            "ok": False,
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": message,
                "retryable": False,
                "suggestion": "Send a JSONL request object matching the tool schema.",
            },
            "text": f"error (INVALID_ARGUMENT): {message}",
        }

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()


class RpcClient:
    """One persistent connection to an ``RpcServer``."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._kind, self._address = parse_endpoint(endpoint)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_id = 1

    async def connect(self) -> None:
        if self._kind == "unix":
            self._reader, self._writer = await asyncio.open_unix_connection(
                self._address
            )
        else:
            host, port = _split_tcp_address(self._address)
            self._reader, self._writer = await asyncio.open_connection(host, port)

    async def call(self, tool: str, arguments: dict) -> dict:
        """Send one request and wait for the matching response."""
        if self._writer is None:
            raise RuntimeError("call connect() first")
        req_id = self._next_id
        self._next_id += 1
        request = json.dumps(
            {"id": req_id, "tool": tool, "args": arguments},
            ensure_ascii=False,
        )
        self._writer.write((request + "\n").encode("utf-8"))
        await self._writer.drain()

        while True:
            raw = await self._reader.readline()
            if not raw:
                raise ConnectionError("daemon closed the connection")
            response = json.loads(raw.decode("utf-8", errors="replace"))
            if response.get("id") == req_id:
                return response

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None


def resolve_endpoint(value: str) -> str:
    """Turn a ``--socket`` argument into an endpoint string.

    Accepts ``unix:PATH`` / ``tcp:HOST:PORT`` directly, or the path of a
    ``daemon.json`` endpoint file written by ``embpilot serve``.
    """
    if value.startswith("unix:") or value.startswith("tcp:"):
        return value
    path = Path(value)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"cannot read daemon endpoint file {path}: {exc}"
        ) from exc
    endpoint = info.get("endpoint")
    if not endpoint:
        raise ValueError(f"daemon endpoint file {path} has no 'endpoint' field")
    return endpoint


def write_endpoint_file(data_dir: Path, endpoint: str) -> Path:
    """Persist the daemon endpoint so ``--socket <file>`` can discover it."""
    path = data_dir / _ENDPOINT_FILE_NAME
    path.write_text(
        json.dumps({"endpoint": endpoint}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
