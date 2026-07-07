from __future__ import annotations

import asyncio
import pytest
from pathlib import Path

from mcp.shared.exceptions import McpError
from mcp import types

from embpilot.config import EmbPilotConfig
from embpilot.runtime.session import SessionManager


def build_config(tmp_path: Path) -> EmbPilotConfig:
    data_dir = tmp_path / "data"
    return EmbPilotConfig(
        data_dir=data_dir,
        main_db_path=data_dir / "embpilot_main.db",
        session_data_dir=data_dir / "sessions",
        lancedb_path=data_dir / "lancedb",
    )


def test_build_resource_catalog_exposes_session_info_resource() -> None:
    from embpilot.mcp_app import build_resource_catalog

    resources = build_resource_catalog()

    resource_uris = {str(resource.uri) for resource in resources}

    assert resource_uris == {
        "device://live_log",
        "device://session_info",
        "device://analytics",
    }
    assert "device://sysinfo" not in resource_uris


def test_create_mcp_app_returns_app_and_session_manager(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    config = build_config(tmp_path)

    app, manager = create_mcp_app(config)

    assert app is not None
    assert isinstance(manager, SessionManager)


def test_create_mcp_app_registers_all_handlers(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    config = build_config(tmp_path)

    app, _ = create_mcp_app(config)

    assert types.ListResourcesRequest in app.request_handlers
    assert types.ReadResourceRequest in app.request_handlers
    assert types.ListToolsRequest in app.request_handlers
    assert types.CallToolRequest in app.request_handlers
    assert types.ListPromptsRequest in app.request_handlers
    assert types.GetPromptRequest in app.request_handlers


def test_cli_main_calls_run_stdio_mcp_server(monkeypatch, tmp_path: Path) -> None:
    from embpilot import cli

    captured: dict[str, EmbPilotConfig] = {}

    def fake_run_stdio_mcp_server(config: EmbPilotConfig) -> None:
        captured["config"] = config

    monkeypatch.setattr("embpilot.mcp_app.run_stdio_mcp_server", fake_run_stdio_mcp_server)

    cli.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "--main-db-path",
            str(tmp_path / "data" / "main.db"),
            "--session-data-dir",
            str(tmp_path / "data" / "sessions"),
            "--lancedb-path",
            str(tmp_path / "data" / "lancedb"),
        ]
    )

    assert "config" in captured
    assert captured["config"].data_dir == tmp_path / "data"


def test_server_serve_forwards_to_new_runner(monkeypatch, tmp_path: Path) -> None:
    from embpilot.config import EmbPilotConfig
    from embpilot import server

    captured: dict[str, EmbPilotConfig] = {}

    def fake_run_stdio_mcp_server(config: EmbPilotConfig) -> None:
        captured["config"] = config

    monkeypatch.setattr("embpilot.mcp_app.run_stdio_mcp_server", fake_run_stdio_mcp_server)

    config = build_config(tmp_path)
    server.serve(config)

    assert captured["config"] is config


def test_build_tool_catalog_lists_core_tools() -> None:
    from embpilot.mcp_app import build_tool_catalog

    names = {tool.name for tool in build_tool_catalog()}

    assert {
        "connect_device",
        "send_command",
        "reset_target",
        "disconnect_device",
    } <= names


def test_send_command_schema_exposes_line_ending_strategy() -> None:
    from embpilot.mcp_app import build_tool_catalog

    tool = next(t for t in build_tool_catalog() if t.name == "send_command")
    line_ending_schema = tool.inputSchema["properties"]["line_ending"]

    assert line_ending_schema["default"] == "as-is"
    assert line_ending_schema["enum"] == ["as-is", "none", "lf", "crlf", "cr"]
    assert tool.inputSchema["properties"]["confirm_dangerous_command"]["default"] is False


def test_tool_schemas_reject_extra_properties() -> None:
    from embpilot.mcp_app import build_tool_catalog

    tools = build_tool_catalog()

    assert all(tool.inputSchema.get("additionalProperties") is False for tool in tools)


def test_reset_target_schema_excludes_dtr_and_rts() -> None:
    from embpilot.mcp_app import build_tool_catalog

    tool = next(t for t in build_tool_catalog() if t.name == "reset_target")
    method_schema = tool.inputSchema["properties"]["method"]

    assert method_schema.get("enum") == ["reboot"]


def test_delete_session_schema_requires_confirmation() -> None:
    from embpilot.mcp_app import build_tool_catalog

    tool = next(t for t in build_tool_catalog() if t.name == "delete_session")

    assert "confirm" in tool.inputSchema["required"]
    assert tool.inputSchema["properties"]["confirm"]["default"] is False


def test_call_tool_handler_unknown_tool_raises_protocol_error(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    async def scenario() -> None:
        app, manager = create_mcp_app(build_config(tmp_path))
        await manager.start()
        try:
            request = types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="no_such_tool",
                    arguments={},
                )
            )
            with pytest.raises(McpError) as exc_info:
                await app.request_handlers[types.CallToolRequest](request)
        finally:
            await manager.shutdown()

        assert exc_info.value.error.code == types.INVALID_PARAMS
        assert "Unknown tool" in exc_info.value.error.message

    asyncio.run(scenario())


def test_call_tool_handler_invalid_arguments_raises_protocol_error(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    async def scenario() -> None:
        app, manager = create_mcp_app(build_config(tmp_path))
        await manager.start()
        try:
            request = types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="send_command",
                    arguments={"command": "status", "unexpected": True},
                )
            )
            with pytest.raises(McpError) as exc_info:
                await app.request_handlers[types.CallToolRequest](request)
        finally:
            await manager.shutdown()

        assert exc_info.value.error.code == types.INVALID_PARAMS
        assert "Invalid arguments" in exc_info.value.error.message

    asyncio.run(scenario())


def test_call_tool_handler_rate_limit_returns_tool_error(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    async def scenario() -> None:
        config = build_config(tmp_path)
        config.tool_rate_limit_per_minute = 1
        app, manager = create_mcp_app(config)
        await manager.start()
        try:
            request = types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="list_sessions",
                    arguments={},
                )
            )
            first = await app.request_handlers[types.CallToolRequest](request)
            second = await app.request_handlers[types.CallToolRequest](request)
        finally:
            await manager.shutdown()

        assert first.root.isError is False
        assert second.root.isError is True
        assert "rate limit" in second.root.content[0].text.lower()

    asyncio.run(scenario())


def test_read_resource_unknown_uri_raises_resource_not_found(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    async def scenario() -> None:
        app, manager = create_mcp_app(build_config(tmp_path))
        await manager.start()
        try:
            request = types.ReadResourceRequest(
                params=types.ReadResourceRequestParams(uri="device://missing")
            )
            with pytest.raises(McpError) as exc_info:
                await app.request_handlers[types.ReadResourceRequest](request)
        finally:
            await manager.shutdown()

        assert exc_info.value.error.code == types.INVALID_PARAMS
        assert exc_info.value.error.message == "Resource not found"
        assert exc_info.value.error.data == {"uri": "device://missing"}

    asyncio.run(scenario())


def test_dispatch_tool_unknown_returns_error_text(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(manager, "no_such_tool", {})
        finally:
            await manager.shutdown()

        assert result.isError is True
        assert len(result.content) == 1
        assert isinstance(result.content[0], types.TextContent)
        assert "unknown tool" in result.content[0].text.lower()

    asyncio.run(scenario())


def test_dispatch_tool_send_command_without_connection_returns_error(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(manager, "send_command", {"command": "ls"})
        finally:
            await manager.shutdown()

        assert result.isError is True
        assert len(result.content) == 1
        assert isinstance(result.content[0], types.TextContent)
        assert "error" in result.content[0].text.lower()

    asyncio.run(scenario())


def test_build_prompt_catalog_lists_prompts() -> None:
    from embpilot.mcp_app import build_prompt_catalog

    names = {prompt.name for prompt in build_prompt_catalog()}

    assert {"analyze_crash_log", "hardware_sanity_check"} <= names


def test_render_analyze_crash_log_points_at_live_log() -> None:
    from embpilot.mcp_app import render_prompt

    text = render_prompt("analyze_crash_log", {})

    assert "device://live_log" in text


def test_render_hardware_sanity_omits_hardcoded_commands() -> None:
    from embpilot.mcp_app import render_prompt

    text = render_prompt("hardware_sanity_check", {})
    lowered = text.lower()

    # spec §7.6: no hardcoded generic command bundle
    for forbidden in ("dmesg", "uname", "help", "version"):
        assert forbidden not in lowered
    # it should instead route through send_command and session context
    assert "send_command" in lowered
    assert "device://session_info" in lowered


def test_render_hardware_sanity_incorporates_focus_argument() -> None:
    from embpilot.mcp_app import render_prompt

    text = render_prompt("hardware_sanity_check", {"focus": "power rails"})

    assert "power rails" in text


def test_render_unknown_prompt_raises() -> None:
    from embpilot.mcp_app import render_prompt

    with pytest.raises(ValueError):
        render_prompt("nonexistent", {})


def test_build_tool_catalog_lists_session_query_tools() -> None:
    from embpilot.mcp_app import build_tool_catalog

    names = {tool.name for tool in build_tool_catalog()}

    assert {
        "list_sessions",
        "delete_session",
        "search_history_logs",
        "export_session",
        "export_operation_history",
    } <= names


def test_dispatch_tool_list_sessions_returns_json(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(manager, "list_sessions", {})
        finally:
            await manager.shutdown()

        assert result.isError is False
        assert len(result.content) == 1
        assert isinstance(result.content[0], types.TextContent)
        assert result.content[0].text.strip().startswith("[")

    asyncio.run(scenario())


def test_dispatch_tool_delete_session_without_id_returns_error(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(manager, "delete_session", {})
        finally:
            await manager.shutdown()

        assert result.isError is True
        assert len(result.content) == 1
        assert "error" in result.content[0].text.lower()

    asyncio.run(scenario())


def test_dispatch_tool_export_unknown_session_returns_error(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(
                manager, "export_session", {"session_id": "missing-1"}
            )
        finally:
            await manager.shutdown()

        assert result.isError is True
        assert len(result.content) == 1
        assert "error" in result.content[0].text.lower()

    asyncio.run(scenario())
