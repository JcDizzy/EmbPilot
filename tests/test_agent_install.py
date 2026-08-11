from __future__ import annotations

import json
from pathlib import Path

import pytest

from embpilot.agent_install import (
    InstallContext,
    install_targets,
    resolve_target_ids,
    uninstall_targets,
)
from embpilot.agent_install.files import upsert_json_value
from embpilot.agent_install.targets import SECTION_START


def _context(tmp_path: Path) -> InstallContext:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    return InstallContext(
        project_dir=project,
        home_dir=home,
        server_command="C:/Tools/embpilot.exe",
    )


def _seed_detected_targets(context: InstallContext) -> None:
    (context.home_dir / ".claude").mkdir(parents=True)
    (context.home_dir / ".codex").mkdir(parents=True)
    (context.home_dir / ".zcode" / "cli").mkdir(parents=True)
    (context.home_dir / ".zcode" / "cli" / "config.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (context.home_dir / ".config" / "opencode").mkdir(parents=True)


def test_global_install_configures_all_four_targets_and_preserves_siblings(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _seed_detected_targets(context)
    (context.home_dir / ".claude.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}}),
        encoding="utf-8",
    )
    (context.home_dir / ".codex" / "config.toml").write_text(
        '[mcp_servers.other]\ncommand = "other"\n',
        encoding="utf-8",
    )
    (context.home_dir / ".zcode" / "cli" / "config.json").write_text(
        json.dumps({"mcp": {"servers": {"other": {"type": "stdio"}}}}),
        encoding="utf-8",
    )
    opencode = context.home_dir / ".config" / "opencode" / "opencode.jsonc"
    opencode.write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {"other": {"enabled": True}}}),
        encoding="utf-8",
    )

    summary = install_targets(
        context,
        ("claude", "codex", "zcode", "opencode"),
        "global",
    )

    assert summary.changed is True
    claude = json.loads((context.home_dir / ".claude.json").read_text(encoding="utf-8"))
    assert claude["mcpServers"]["other"] == {"command": "other"}
    assert claude["mcpServers"]["embpilot"]["command"] == context.server_command
    codex = (context.home_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in codex
    assert "[mcp_servers.embpilot]" in codex
    zcode = json.loads(
        (context.home_dir / ".zcode" / "cli" / "config.json").read_text(encoding="utf-8")
    )
    assert zcode["mcp"]["servers"]["other"] == {"type": "stdio"}
    assert zcode["mcp"]["servers"]["embpilot"]["type"] == "stdio"
    skill_path = (
        context.home_dir
        / ".zcode"
        / "cli"
        / "skills"
        / "embpilot-device-debugging"
        / "SKILL.md"
    )
    assert zcode["skills"][skill_path.as_posix()] == {"enable": True}
    assert skill_path.exists()
    open_data = json.loads(opencode.read_text(encoding="utf-8"))
    assert open_data["mcp"]["other"] == {"enabled": True}
    assert open_data["mcp"]["embpilot"]["command"] == [context.server_command]
    assert SECTION_START in (context.home_dir / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert SECTION_START in (context.home_dir / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert SECTION_START in (context.home_dir / ".config" / "opencode" / "AGENTS.md").read_text(encoding="utf-8")

    second = install_targets(
        context,
        ("claude", "codex", "zcode", "opencode"),
        "global",
    )
    assert second.changed is False


def test_global_uninstall_removes_only_embpilot_entries(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _seed_detected_targets(context)
    (context.home_dir / ".codex" / "config.toml").write_text(
        '[mcp_servers.other]\ncommand = "other"\n',
        encoding="utf-8",
    )
    install_targets(context, ("claude", "codex", "zcode", "opencode"), "global")
    upsert_json_value(
        context.home_dir / ".claude.json",
        ("mcpServers", "other"),
        {"command": "other"},
    )

    summary = uninstall_targets(
        context,
        ("claude", "codex", "zcode", "opencode"),
        "global",
    )

    assert summary.changed is True
    claude = json.loads((context.home_dir / ".claude.json").read_text(encoding="utf-8"))
    assert claude == {"mcpServers": {"other": {"command": "other"}}}
    codex = (context.home_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in codex
    assert "[mcp_servers.embpilot]" not in codex
    zcode = json.loads(
        (context.home_dir / ".zcode" / "cli" / "config.json").read_text(encoding="utf-8")
    )
    assert "embpilot" not in zcode.get("mcp", {}).get("servers", {})
    assert not (
        context.home_dir
        / ".zcode"
        / "cli"
        / "skills"
        / "embpilot-device-debugging"
        / "SKILL.md"
    ).exists()


def test_local_install_skips_global_only_targets(tmp_path: Path) -> None:
    context = _context(tmp_path)

    summary = install_targets(
        context,
        ("claude", "codex", "zcode", "opencode"),
        "local",
    )

    assert {report.target_id for report in summary.reports} == {"claude", "opencode"}
    assert len(summary.skipped) == 2
    assert (context.project_dir / ".mcp.json").exists()
    assert (context.project_dir / ".claude" / "CLAUDE.md").exists()
    assert (context.project_dir / "opencode.jsonc").exists()
    assert (context.project_dir / "AGENTS.md").exists()


def test_auto_target_detection_uses_location_support(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _seed_detected_targets(context)

    assert resolve_target_ids("auto", context=context, location="global") == (
        "claude",
        "codex",
        "zcode",
        "opencode",
    )
    assert resolve_target_ids("auto", context=context, location="local") == (
        "claude",
        "opencode",
    )


def test_unknown_target_is_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path)

    with pytest.raises(ValueError, match="Unknown target"):
        resolve_target_ids("cursor", context=context, location="global")


def test_target_aliases_are_accepted(tmp_path: Path) -> None:
    context = _context(tmp_path)

    assert resolve_target_ids(
        "claudecode,open-code",
        context=context,
        location="global",
    ) == ("claude", "opencode")


def test_jsonc_comments_are_refused_without_modifying_file(tmp_path: Path) -> None:
    path = tmp_path / "opencode.jsonc"
    original = '{\n  // keep this comment\n  "mcp": {}\n}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="cannot yet be edited safely"):
        upsert_json_value(path, ("mcp", "embpilot"), {"enabled": True})

    assert path.read_text(encoding="utf-8") == original


def test_jsonc_trailing_comma_cleanup_does_not_change_strings(tmp_path: Path) -> None:
    path = tmp_path / "opencode.jsonc"
    path.write_text('{"value": ",}", "mcp": {},}\n', encoding="utf-8")

    upsert_json_value(path, ("mcp", "embpilot"), {"enabled": True})

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["value"] == ",}"
