"""Format MCP CallToolResult values for command-line display."""

from __future__ import annotations

import json

from mcp.types import CallToolResult

from embpilot.mcp_compat import result_structured

# Data keys whose list payloads are worth printing in human-readable mode.
_DETAIL_KEYS = ("sessions", "results")


def format_result(result: CallToolResult, *, json_output: bool) -> str:
    """Render one tool result as text (default) or as structured JSON."""
    payload = result_structured(result) or {}

    if json_output:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if payload.get("ok") is not True:
        error = payload.get("error") or {}
        lines = [
            "error ({code}): {message}".format(
                code=error.get("code", "OPERATION_FAILED"),
                message=error.get("message", "(no message)"),
            )
        ]
        if error.get("suggestion"):
            lines.append("suggestion: {suggestion}".format(suggestion=error["suggestion"]))
        return "\n".join(lines)

    lines: list[str] = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text:
            lines.append(text)

    data = payload.get("data")
    if isinstance(data, dict):
        for key in _DETAIL_KEYS:
            value = data.get(key)
            if isinstance(value, list) and value:
                lines.append("")
                lines.append(json.dumps(value, ensure_ascii=False, indent=2))
    return "\n".join(lines)