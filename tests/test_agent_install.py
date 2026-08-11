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
from embpilot.agent_install.targets import EMBPILOT_SKILL, SECTION_START, SKILL_NAME


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


def _global_skill_paths(context: InstallContext) -> dict[str, Path]:
    return {
        "claude": context.home_dir / ".claude" / "skills" / SKILL_NAME / "SKILL.md",
        "codex": context.home_dir / ".codex" / "skills" / SKILL_NAME / "SKILL.md",
        "zcode": context.home_dir / ".zcode" / "cli" / "skills" / SKILL_NAME / "SKILL.md",
        "opencode": (
            context.home_dir
            / ".config"
            / "opencode"
            / "skills"
            / SKILL_NAME
            / "SKILL.md"
        ),
    }


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
    skill_paths = _global_skill_paths(context)
    skill_path = skill_paths["zcode"]
    assert zcode["skills"][skill_path.as_posix()] == {"enable": True}
    for installed_skill in skill_paths.values():
        assert installed_skill.read_text(encoding="utf-8") == EMBPILOT_SKILL
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
    for skill_path in _global_skill_paths(context).values():
        assert not skill_path.exists()
        assert not skill_path.parent.exists()
    assert not (context.home_dir / ".codex" / "AGENTS.md").exists()


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
    assert (
        context.project_dir / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    ).exists()
    assert (context.project_dir / "opencode.jsonc").exists()
    assert (context.project_dir / "AGENTS.md").exists()
    assert (
        context.project_dir / ".opencode" / "skills" / SKILL_NAME / "SKILL.md"
    ).exists()

    uninstall_targets(context, ("claude", "opencode"), "local")

    assert not (context.project_dir / ".claude" / "CLAUDE.md").exists()
    assert not (
        context.project_dir / ".claude" / "skills" / SKILL_NAME
    ).exists()
    assert not (context.project_dir / "AGENTS.md").exists()
    assert not (
        context.project_dir / ".opencode" / "skills" / SKILL_NAME
    ).exists()


def test_packaged_skill_matches_repository_skill() -> None:
    repo_skill = (
        Path(__file__).resolve().parents[1]
        / ".agents"
        / "skills"
        / SKILL_NAME
        / "SKILL.md"
    )

    assert repo_skill.read_text(encoding="utf-8") == EMBPILOT_SKILL


@pytest.mark.parametrize("target_id", ("claude", "codex", "zcode", "opencode"))
def test_uninstall_preserves_user_modified_skill(
    tmp_path: Path,
    target_id: str,
) -> None:
    context = _context(tmp_path)
    _seed_detected_targets(context)
    install_targets(context, (target_id,), "global")
    skill_path = _global_skill_paths(context)[target_id]
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nUser customization.\n",
        encoding="utf-8",
    )

    summary = uninstall_targets(context, (target_id,), "global")

    assert skill_path.exists()
    assert "User customization." in skill_path.read_text(encoding="utf-8")
    skill_result = next(
        file for file in summary.reports[0].files if file.path == skill_path
    )
    assert skill_result.action == "unchanged"
    if target_id == "zcode":
        config = json.loads(
            (context.home_dir / ".zcode" / "cli" / "config.json").read_text(
                encoding="utf-8"
            )
        )
        assert skill_path.as_posix() not in config.get("skills", {})


def test_uninstall_keeps_nonempty_skill_directory(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _seed_detected_targets(context)
    install_targets(context, ("codex",), "global")
    skill_path = _global_skill_paths(context)["codex"]
    sibling = skill_path.parent / "notes.md"
    sibling.write_text("keep\n", encoding="utf-8")

    uninstall_targets(context, ("codex",), "global")

    assert not skill_path.exists()
    assert sibling.read_text(encoding="utf-8") == "keep\n"


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
