"""Installer tests: marker sections, target wiring, idempotence, uninstall.

All tests run against tmp_path via monkeypatched cwd / PI_HOME; the real
home directory is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from embpilot.installer.instructions import (
    INSTRUCTIONS_BLOCK,
    SECTION_END,
    SECTION_START,
)
from embpilot.installer.shared import (
    remove_marked_section,
    replace_or_append_marked_section,
)
from embpilot.installer.targets import (
    AgentsTarget,
    ClaudeCodeTarget,
    PiTarget,
    get_target,
    resolve_target_flag,
)


# ── marker section helpers ───────────────────────────────────────────────────


def test_marker_section_upsert_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    first = replace_or_append_marked_section(
        path, INSTRUCTIONS_BLOCK, SECTION_START, SECTION_END
    )
    second = replace_or_append_marked_section(
        path, INSTRUCTIONS_BLOCK, SECTION_START, SECTION_END
    )
    assert first == "created"
    assert second == "unchanged"
    assert path.read_text(encoding="utf-8").count(SECTION_START) == 1


def test_marker_section_replaces_stale_content(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    stale = (
        f"{SECTION_START}\n## EmbPilot\nold stale guidance\n{SECTION_END}\n"
    )
    path.write_text(stale, encoding="utf-8")
    action = replace_or_append_marked_section(
        path, INSTRUCTIONS_BLOCK, SECTION_START, SECTION_END
    )
    content = path.read_text(encoding="utf-8")
    assert action == "updated"
    assert "old stale guidance" not in content
    assert INSTRUCTIONS_BLOCK in content


def test_marker_section_preserves_sibling_content(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("# Project\n\nKeep tests green.\n", encoding="utf-8")
    replace_or_append_marked_section(
        path, INSTRUCTIONS_BLOCK, SECTION_START, SECTION_END
    )
    remove_marked_section(path, SECTION_START, SECTION_END)
    content = path.read_text(encoding="utf-8")
    assert "# Project" in content
    assert "Keep tests green." in content
    assert SECTION_START not in content


def test_remove_marked_section_deletes_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(INSTRUCTIONS_BLOCK + "\n", encoding="utf-8")
    assert remove_marked_section(path, SECTION_START, SECTION_END) == "removed"
    assert not path.exists()


# ── claude target ────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_claude_local_install_writes_mcp_and_instructions(project: Path) -> None:
    target = ClaudeCodeTarget()
    changes = target.install("local")

    mcp = project / ".mcp.json"
    claude_md = project / ".claude" / "CLAUDE.md"
    assert mcp.exists()
    assert claude_md.exists()
    config = json.loads(mcp.read_text(encoding="utf-8"))
    assert config["mcpServers"]["embpilot"]["command"] == "embpilot"
    assert SECTION_START in claude_md.read_text(encoding="utf-8")
    assert any(change.action != "not-found" for change in changes)


def test_claude_local_install_preserves_sibling_mcp_servers(project: Path) -> None:
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}),
        encoding="utf-8",
    )
    ClaudeCodeTarget().install("local")
    config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "other" in config["mcpServers"]
    assert "embpilot" in config["mcpServers"]


def test_claude_local_uninstall_removes_only_embpilot(project: Path) -> None:
    (project / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"embpilot": {"command": "embpilot"}, "other": {"command": "x"}}}
        ),
        encoding="utf-8",
    )
    (project / ".claude" / "CLAUDE.md").parent.mkdir(parents=True)
    (project / ".claude" / "CLAUDE.md").write_text(
        "before\n" + INSTRUCTIONS_BLOCK + "\nafter\n", encoding="utf-8"
    )
    ClaudeCodeTarget().uninstall("local")

    config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "embpilot" not in config["mcpServers"]
    assert "other" in config["mcpServers"]
    content = (project / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert SECTION_START not in content
    assert "before" in content and "after" in content


def test_claude_detect_reports_already_configured(project: Path) -> None:
    assert ClaudeCodeTarget().detect("local").already_configured is False
    ClaudeCodeTarget().install("local")
    assert ClaudeCodeTarget().detect("local").already_configured is True


def test_claude_print_config_touches_nothing(project: Path) -> None:
    text = ClaudeCodeTarget().print_config()
    assert '"command": "embpilot"' in text
    assert list(project.iterdir()) == []


# ── pi target ────────────────────────────────────────────────────────────────


def test_pi_local_install_writes_project_agents_md(project: Path, monkeypatch) -> None:
    pi_home = project / "fake_pi_home"
    monkeypatch.setenv("PI_HOME", str(pi_home))
    target = PiTarget()
    changes = target.install("local")

    agents = project / "AGENTS.md"
    assert agents.exists()
    assert SECTION_START in agents.read_text(encoding="utf-8")
    # Local scope must NOT copy the skill into the user pi dir.
    assert not (pi_home / "skills" / "embpilot-device-debugging").exists()
    assert any(change.path == agents for change in changes)


def test_pi_global_install_copies_skill(project: Path, monkeypatch) -> None:
    pi_home = project / "fake_pi_home"
    monkeypatch.setenv("PI_HOME", str(pi_home))
    PiTarget().install("global")

    skill = pi_home / "skills" / "embpilot-device-debugging" / "SKILL.md"
    assert skill.exists()
    assert "EmbPilot" in skill.read_text(encoding="utf-8")
    assert SECTION_START in (pi_home / "AGENTS.md").read_text(encoding="utf-8")


def test_pi_global_uninstall_removes_skill_and_block(
    project: Path, monkeypatch
) -> None:
    pi_home = project / "fake_pi_home"
    monkeypatch.setenv("PI_HOME", str(pi_home))
    PiTarget().install("global")
    PiTarget().uninstall("global")

    assert not (pi_home / "skills" / "embpilot-device-debugging").exists()
    assert not (pi_home / "AGENTS.md").exists()


# ── agents target + registry ─────────────────────────────────────────────────


def test_agents_target_local_only(project: Path) -> None:
    target = AgentsTarget()
    assert target.supports_location("local") is True
    assert target.supports_location("global") is False
    target.install("local")
    content = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert SECTION_START in content
    target.uninstall("local")
    assert not (project / "AGENTS.md").exists()


def test_resolve_target_flag_all_and_unknown(project: Path) -> None:
    assert [t.id for t in resolve_target_flag("all", "local")] == [
        "claude",
        "zcode",
        "opencode",
        "codex",
        "pi",
        "agents",
    ]
    assert resolve_target_flag("none", "local") == []
    with pytest.raises(ValueError):
        resolve_target_flag("bogus", "local")


def test_registry_has_expected_targets() -> None:
    assert get_target("claude") is not None
    assert get_target("pi") is not None
    assert get_target("agents") is not None
    assert get_target("zcode") is not None
    assert get_target("opencode") is not None
    assert get_target("codex") is not None
    assert get_target("bogus") is None


# ── codex target ─────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_codex_global_install_writes_toml_and_agents(fake_home: Path) -> None:
    from embpilot.installer.targets import CodexTarget

    CodexTarget().install("global")
    toml = fake_home / ".codex" / "config.toml"
    agents = fake_home / ".codex" / "AGENTS.md"
    assert toml.exists()
    content = toml.read_text(encoding="utf-8")
    assert "[mcp_servers.embpilot]" in content
    assert 'command = "embpilot"' in content
    assert SECTION_START in agents.read_text(encoding="utf-8")


def test_codex_global_install_preserves_sibling_toml(fake_home: Path) -> None:
    from embpilot.installer.targets import CodexTarget

    toml = fake_home / ".codex" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(
        '[mcp_servers.other]\ncommand = "x"\n\n[model]\nname = "gpt"\n',
        encoding="utf-8",
    )
    CodexTarget().install("global")
    content = toml.read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in content
    assert "[mcp_servers.embpilot]" in content
    assert '[model]' in content

    CodexTarget().uninstall("global")
    content = toml.read_text(encoding="utf-8")
    assert "[mcp_servers.embpilot]" not in content
    assert "[mcp_servers.other]" in content


def test_codex_is_global_only() -> None:
    from embpilot.installer.targets import CodexTarget

    assert CodexTarget().supports_location("global") is True
    assert CodexTarget().supports_location("local") is False


def test_codex_uninstall_leaves_no_body_residue(fake_home: Path) -> None:
    """Uninstall must remove the whole table, not just the header line."""
    from embpilot.installer.targets import CodexTarget

    target = CodexTarget()
    target.install("global")
    toml = fake_home / ".codex" / "config.toml"
    assert toml.exists()
    target.uninstall("global")
    assert not toml.exists()


# ── opencode target ──────────────────────────────────────────────────────────


def test_opencode_local_install_uses_mcp_wrapper(project: Path) -> None:
    from embpilot.installer.targets import OpenCodeTarget

    OpenCodeTarget().install("local")
    config = json.loads((project / "opencode.json").read_text(encoding="utf-8"))
    entry = config["mcp"]["embpilot"]
    assert entry["type"] == "local"
    assert entry["command"] == ["embpilot", "--data-dir", "./.embpilot-data"]
    assert entry["enabled"] is True
    assert SECTION_START in (project / "AGENTS.md").read_text(encoding="utf-8")

    OpenCodeTarget().uninstall("local")
    assert not (project / "opencode.json").exists()


def test_opencode_global_uses_xdg_config(project: Path, monkeypatch) -> None:
    from embpilot.installer.targets import OpenCodeTarget

    xdg = project / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    OpenCodeTarget().install("global")
    assert (xdg / "opencode" / "opencode.json").exists()
    assert (xdg / "opencode" / "AGENTS.md").exists()


# ── zcode target ─────────────────────────────────────────────────────────────


def test_zcode_local_install_writes_agents_mcp_json(project: Path) -> None:
    from embpilot.installer.targets import ZCodeTarget

    ZCodeTarget().install("local")
    path = project / ".agents" / "mcp.json"
    assert path.exists()
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["embpilot"]["command"] == "embpilot"
    assert SECTION_START in (project / "AGENTS.md").read_text(encoding="utf-8")

    ZCodeTarget().uninstall("local")
    assert not path.exists()


def test_zcode_global_install_nested_mcp_and_skill(fake_home: Path) -> None:
    from embpilot.installer.targets import ZCodeTarget

    ZCodeTarget().install("global")
    config_path = fake_home / ".zcode" / "cli" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcp"]["servers"]["embpilot"]["command"] == "embpilot"
    skill = fake_home / ".zcode" / "skills" / "embpilot-device-debugging" / "SKILL.md"
    assert skill.exists()
    assert SECTION_START in (fake_home / ".zcode" / "AGENTS.md").read_text(encoding="utf-8")

    ZCodeTarget().uninstall("global")
    assert not config_path.exists()
    assert not skill.exists()


def test_resolve_target_flag_includes_new_targets(project: Path) -> None:
    assert [t.id for t in resolve_target_flag("all", "local")] == [
        "claude",
        "zcode",
        "opencode",
        "codex",
        "pi",
        "agents",
    ]


# ── interactive flow ─────────────────────────────────────────────────────────


def _scripted_reader(answers: list[str]):
    iterator = iter(answers)

    def read_line(prompt: str) -> str:
        print(prompt, end="")  # mirror the interactive reader for capsys
        try:
            return next(iterator)
        except StopIteration:
            return ""

    return read_line


def test_interactive_install_selects_and_confirms(project: Path, capsys) -> None:
    """Empty selection = detected targets; confirm writes the files."""
    from embpilot.installer.install import run_interactive_install

    # project has no harness files, so nothing is detected: pick agents by number.
    lines = run_interactive_install(
        read_line=_scripted_reader(["6", "", "y"]),
    )
    assert "installed into 1 target(s) at local scope" in "\n".join(lines)
    assert (project / "AGENTS.md").exists()
    assert "Proceed?" in capsys.readouterr().out


def test_interactive_install_cancel_writes_nothing(project: Path, capsys) -> None:
    from embpilot.installer.install import run_interactive_install

    lines = run_interactive_install(read_line=_scripted_reader(["6", "", "n"]))
    assert "cancelled" in "\n".join(lines)
    assert not (project / "AGENTS.md").exists()


def test_interactive_uninstall_round_trip(project: Path, capsys) -> None:
    from embpilot.installer.install import (
        run_interactive_install,
        run_interactive_uninstall,
    )

    run_interactive_install(read_line=_scripted_reader(["6", "", "y"]))
    assert (project / "AGENTS.md").exists()

    lines = run_interactive_uninstall(read_line=_scripted_reader(["6", "", "y"]))
    assert "removed EmbPilot from" in "\n".join(lines)
    assert not (project / "AGENTS.md").exists()


def test_interactive_global_only_target_skipped_at_local(project: Path, capsys) -> None:
    """Selecting codex (global-only) at local scope reports a skip."""
    from embpilot.installer.install import run_interactive_install

    lines = run_interactive_install(read_line=_scripted_reader(["4", "", "y"]))
    joined = "\n".join(lines)
    assert "unsupported at local, skipped" in joined
    assert not (project / ".codex").exists()
