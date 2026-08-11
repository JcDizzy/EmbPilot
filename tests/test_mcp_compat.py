"""Compatibility helpers must resolve mcp 1.x field names."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from embpilot.mcp_compat import result_structured, tool_input_schema
from embpilot.mcp_contracts import build_tool_definitions


def test_tool_input_schema_resolves_installed_field_name() -> None:
    tools = {t.name: t for t in build_tool_definitions()}
    schema = tool_input_schema(tools["connect_serial"])

    assert schema["type"] == "object"
    assert schema["properties"]["port"]["type"] == "string"
    assert schema["required"] == ["port"]


def test_result_structured_resolves_installed_field_name() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="ok")],
        structuredContent={"ok": True, "data": {"session_id": "abc"}},
        isError=False,
    )

    assert result_structured(result) == {"ok": True, "data": {"session_id": "abc"}}