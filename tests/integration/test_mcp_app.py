from __future__ import annotations

import asyncio
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

    assert resource_uris == {"device://live_log", "device://session_info"}
    assert "device://sysinfo" not in resource_uris


def test_create_mcp_app_returns_app_and_session_manager(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    config = build_config(tmp_path)

    app, manager = create_mcp_app(config)

    assert app is not None
    assert isinstance(manager, SessionManager)


def test_create_mcp_app_registers_resource_and_tool_handlers(tmp_path: Path) -> None:
    from embpilot.mcp_app import create_mcp_app

    config = build_config(tmp_path)

    app, _ = create_mcp_app(config)

    assert types.ListResourcesRequest in app.request_handlers
    assert types.ReadResourceRequest in app.request_handlers
    assert types.SubscribeRequest in app.request_handlers
    assert types.ListToolsRequest in app.request_handlers
    assert types.CallToolRequest in app.request_handlers
    assert types.ListPromptsRequest not in app.request_handlers
    assert types.GetPromptRequest not in app.request_handlers


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
