"""Compatibility helpers for mcp SDK 1.x vs 2.x field renames.

mcp 2.0 renamed pydantic fields to snake_case (``input_schema``,
``structured_content``, ``is_error``) but kept the old names as aliases, so
construction with the 1.x keyword names still works while attribute reads
must use the new names. These helpers resolve the field whichever way the
installed SDK exposes it.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, Tool


def tool_input_schema(tool: Tool) -> dict[str, Any]:
    """Return a Tool's input schema under mcp 1.x or 2.x."""
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema


def result_structured(result: CallToolResult) -> dict[str, Any] | None:
    """Return a CallToolResult's structured payload under mcp 1.x or 2.x."""
    payload = getattr(result, "structured_content", None)
    if payload is None:
        payload = getattr(result, "structuredContent", None)
    return payload