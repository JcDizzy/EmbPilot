from __future__ import annotations

import asyncio
import pytest
from pathlib import Path

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
    assert types.SubscribeRequest in app.request_handlers
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


def test_reset_target_schema_excludes_dtr_and_rts() -> None:
    from embpilot.mcp_app import build_tool_catalog

    tool = next(t for t in build_tool_catalog() if t.name == "reset_target")
    method_schema = tool.inputSchema["properties"]["method"]

    assert method_schema.get("enum") == ["reboot"]


def test_dispatch_tool_unknown_returns_error_text(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(manager, "no_such_tool", {})
        finally:
            await manager.shutdown()

        assert len(result) == 1
        assert isinstance(result[0], types.TextContent)
        assert "unknown tool" in result[0].text.lower()

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

        assert len(result) == 1
        assert isinstance(result[0], types.TextContent)
        assert "error" in result[0].text.lower()

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

        assert len(result) == 1
        assert isinstance(result[0], types.TextContent)
        assert result[0].text.strip().startswith("[")

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

        assert len(result) == 1
        assert "error" in result[0].text.lower()

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

        assert len(result) == 1
        assert "error" in result[0].text.lower()

    asyncio.run(scenario())
