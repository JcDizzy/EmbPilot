"""
Agent harness targets for ``embpilot install``.

Each target knows where its harness keeps MCP config and instructions files,
how to detect an existing install, and how to write/remove only the pieces
EmbPilot owns (sibling servers and unrelated markdown sections are preserved).

Targets:
- ``claude``  — Claude Code: MCP entry (.mcp.json / ~/.claude.json) +
  instructions (.claude/CLAUDE.md / ~/.claude/CLAUDE.md).
- ``pi``      — pi (no MCP client): instructions (AGENTS.md) + the
  device-debugging skill copied into the pi user skill directory.
- ``agents``  — generic project AGENTS.md for harnesses that read it
  (Cursor, Codex CLI, Gemini CLI, opencode, ...); local only.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Literal

from embpilot.installer.instructions import (
    INSTRUCTIONS_BLOCK,
    SECTION_END,
    SECTION_START,
)
from embpilot.installer.shared import (
    Action,
    json_deep_equal,
    read_json,
    remove_marked_section,
    replace_or_append_marked_section,
    write_json,
)

Location = Literal["global", "local"]

MCP_SERVER_NAME = "embpilot"
#: README-documented MCP entry; keep the executable name, never an absolute
#: developer-specific path.
MCP_SERVER_ENTRY: dict = {
    "command": "embpilot",
    "args": ["--data-dir", "./.embpilot-data"],
}

SKILL_SOURCE_DIR = Path(__file__).resolve().parent / "skill_template"
PI_SKILL_DIR_NAME = "embpilot-device-debugging"


class DetectionResult:
    def __init__(self, installed: bool, already_configured: bool, config_path: str | None = None) -> None:
        self.installed = installed
        self.already_configured = already_configured
        self.config_path = config_path


class FileChange:
    def __init__(self, path: Path, action: Action, note: str | None = None) -> None:
        self.path = path
        self.action = action
        self.note = note

    def __repr__(self) -> str:
        return f"{self.action}: {self.path}" + (f" ({self.note})" if self.note else "")


class AgentTarget:
    """Interface implemented by every install target."""

    id: str = ""
    display_name: str = ""

    def supports_location(self, loc: Location) -> bool:
        return True

    def detect(self, loc: Location) -> DetectionResult:
        raise NotImplementedError

    def install(self, loc: Location) -> list[FileChange]:
        raise NotImplementedError

    def uninstall(self, loc: Location) -> list[FileChange]:
        raise NotImplementedError

    def print_config(self) -> str:
        raise NotImplementedError

    def describe_paths(self, loc: Location) -> list[str]:
        raise NotImplementedError


def home_dir() -> Path:
    return Path(os.path.expanduser("~"))


def project_dir() -> Path:
    return Path.cwd()


# ── Claude Code ──────────────────────────────────────────────────────────────


def _claude_config_dir(loc: Location) -> Path:
    return home_dir() / ".claude" if loc == "global" else project_dir() / ".claude"


def _claude_mcp_json(loc: Location) -> Path:
    return (
        home_dir() / ".claude.json"
        if loc == "global"
        else project_dir() / ".mcp.json"
    )


def _claude_instructions(loc: Location) -> Path:
    return _claude_config_dir(loc) / "CLAUDE.md"


class ClaudeCodeTarget(AgentTarget):
    id = "claude"
    display_name = "Claude Code"

    def detect(self, loc: Location) -> DetectionResult:
        mcp_path = _claude_mcp_json(loc)
        config = read_json(mcp_path) or {}
        already = bool(
            (config.get("mcpServers") or {}).get(MCP_SERVER_NAME)
        )
        installed = (config.get("mcpServers") is not None) or (
            _claude_config_dir(loc).exists()
        )
        return DetectionResult(installed, already, str(mcp_path))

    def install(self, loc: Location) -> list[FileChange]:
        changes: list[FileChange] = []
        changes.append(_write_mcp_entry(loc))
        changes.append(
            FileChange(
                _claude_instructions(loc),
                replace_or_append_marked_section(
                    _claude_instructions(loc),
                    INSTRUCTIONS_BLOCK,
                    SECTION_START,
                    SECTION_END,
                ),
            )
        )
        return changes

    def uninstall(self, loc: Location) -> list[FileChange]:
        changes: list[FileChange] = []
        mcp_path = _claude_mcp_json(loc)
        config = read_json(mcp_path) or {}
        servers = config.get("mcpServers") or {}
        if MCP_SERVER_NAME in servers:
            del servers[MCP_SERVER_NAME]
            if servers:
                config["mcpServers"] = servers
            else:
                config.pop("mcpServers", None)
            if config:
                write_json(mcp_path, config)
            else:
                mcp_path.unlink(missing_ok=True)
            changes.append(FileChange(mcp_path, "removed"))
        else:
            changes.append(FileChange(mcp_path, "not-found"))
        changes.append(
            FileChange(
                _claude_instructions(loc),
                remove_marked_section(
                    _claude_instructions(loc), SECTION_START, SECTION_END
                ),
            )
        )
        return changes

    def print_config(self) -> str:
        return (
            "Add to .mcp.json (project) or ~/.claude.json (global):\n"
            + _mcp_json_snippet()
        )

    def describe_paths(self, loc: Location) -> list[str]:
        return [
            str(_claude_mcp_json(loc)),
            str(_claude_instructions(loc)),
        ]


def _mcp_json_snippet() -> str:
    import json

    return json.dumps(
        {"mcpServers": {MCP_SERVER_NAME: MCP_SERVER_ENTRY}},
        ensure_ascii=False,
        indent=2,
    )


def _write_mcp_entry(loc: Location) -> FileChange:
    mcp_path = _claude_mcp_json(loc)
    config = read_json(mcp_path) or {}
    servers = config.setdefault("mcpServers", {})
    if json_deep_equal(servers.get(MCP_SERVER_NAME), MCP_SERVER_ENTRY):
        return FileChange(mcp_path, "unchanged")
    servers[MCP_SERVER_NAME] = MCP_SERVER_ENTRY
    write_json(mcp_path, config)
    return FileChange(mcp_path, "updated" if config else "created")


# ── pi (no MCP client) ───────────────────────────────────────────────────────


def _pi_agent_dir() -> Path:
    """~/.pi/agent — respects PI_HOME for tests."""
    override = os.environ.get("PI_HOME")
    if override:
        return Path(override)
    return home_dir() / ".pi" / "agent"


def _pi_instructions(loc: Location) -> Path:
    if loc == "global":
        return _pi_agent_dir() / "AGENTS.md"
    return project_dir() / "AGENTS.md"


def _pi_skill_dest() -> Path:
    return _pi_agent_dir() / "skills" / PI_SKILL_DIR_NAME


def _install_skill() -> FileChange:
    dest = _pi_skill_dest()
    if dest.exists():
        return FileChange(dest, "unchanged")
    if not SKILL_SOURCE_DIR.exists():
        return FileChange(dest, "not-found", note="skill template missing from package")
    shutil.copytree(SKILL_SOURCE_DIR, dest)
    return FileChange(dest, "created")


def _remove_skill() -> FileChange:
    dest = _pi_skill_dest()
    if not dest.exists():
        return FileChange(dest, "not-found")
    shutil.rmtree(dest)
    return FileChange(dest, "removed")


class PiTarget(AgentTarget):
    id = "pi"
    display_name = "pi (CLI-only harness)"

    def supports_location(self, loc: Location) -> bool:
        return True

    def detect(self, loc: Location) -> DetectionResult:
        instructions = _pi_instructions(loc)
        content = ""
        if instructions.exists():
            try:
                content = instructions.read_text(encoding="utf-8")
            except OSError:
                pass
        already = SECTION_START in content
        installed = _pi_agent_dir().exists() or instructions.exists()
        return DetectionResult(installed, already, str(instructions))

    def install(self, loc: Location) -> list[FileChange]:
        changes = [
            FileChange(
                _pi_instructions(loc),
                replace_or_append_marked_section(
                    _pi_instructions(loc),
                    INSTRUCTIONS_BLOCK,
                    SECTION_START,
                    SECTION_END,
                ),
            )
        ]
        if loc == "global":
            changes.append(_install_skill())
        return changes

    def uninstall(self, loc: Location) -> list[FileChange]:
        changes = [
            FileChange(
                _pi_instructions(loc),
                remove_marked_section(
                    _pi_instructions(loc), SECTION_START, SECTION_END
                ),
            )
        ]
        if loc == "global":
            changes.append(_remove_skill())
        return changes

    def print_config(self) -> str:
        return (
            "pi has no MCP client; the installer writes the instructions "
            "block to AGENTS.md and (global) copies the skill into "
            f"~/.pi/agent/skills/{PI_SKILL_DIR_NAME}/.\n"
            "Manual equivalent — append the block below to AGENTS.md:\n\n"
            + INSTRUCTIONS_BLOCK
        )

    def describe_paths(self, loc: Location) -> list[str]:
        paths = [str(_pi_instructions(loc))]
        if loc == "global":
            paths.append(str(_pi_skill_dest()))
        return paths


# ── Generic project AGENTS.md ────────────────────────────────────────────────


class AgentsTarget(AgentTarget):
    id = "agents"
    display_name = "Project AGENTS.md (Cursor / Codex / Gemini / opencode ...)"

    def supports_location(self, loc: Location) -> bool:
        return loc == "local"

    def detect(self, loc: Location) -> DetectionResult:
        path = project_dir() / "AGENTS.md"
        content = ""
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                pass
        return DetectionResult(path.exists(), SECTION_START in content, str(path))

    def install(self, loc: Location) -> list[FileChange]:
        path = project_dir() / "AGENTS.md"
        return [
            FileChange(
                path,
                replace_or_append_marked_section(
                    path, INSTRUCTIONS_BLOCK, SECTION_START, SECTION_END
                ),
            )
        ]

    def uninstall(self, loc: Location) -> list[FileChange]:
        path = project_dir() / "AGENTS.md"
        return [
            FileChange(
                path,
                remove_marked_section(path, SECTION_START, SECTION_END),
            )
        ]

    def print_config(self) -> str:
        return "Append the block below to the project AGENTS.md:\n\n" + INSTRUCTIONS_BLOCK

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(project_dir() / "AGENTS.md")]


# ── Registry ─────────────────────────────────────────────────────────────────

ALL_TARGETS: list[AgentTarget] = [ClaudeCodeTarget(), PiTarget(), AgentsTarget()]


def get_target(target_id: str) -> AgentTarget | None:
    return next((t for t in ALL_TARGETS if t.id == target_id), None)


def list_target_ids() -> list[str]:
    return [t.id for t in ALL_TARGETS]


def detect_all(loc: Location) -> list[tuple[AgentTarget, DetectionResult]]:
    return [(t, t.detect(loc)) for t in ALL_TARGETS]


def resolve_target_flag(flag: str, loc: Location) -> list[AgentTarget]:
    """Translate the ``--target`` flag into a concrete target list.

    ``auto`` = detected harnesses; ``all`` = every target; ``none`` = [].
    """
    if flag == "all":
        return list(ALL_TARGETS)
    if flag == "none":
        return []
    if flag == "auto":
        return [
            target
            for target, detection in detect_all(loc)
            if detection.installed
        ]
    targets = [get_target(part.strip()) for part in flag.split(",")]
    missing = [part for part, t in zip(flag.split(","), targets) if t is None]
    if missing:
        raise ValueError(
            f"unknown target(s): {', '.join(missing)} "
            f"(choose from {', '.join(list_target_ids())})"
        )
    return [t for t in targets if t is not None]
