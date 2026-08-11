"""Agent harness adapters for EmbPilot installation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from embpilot.agent_install.files import (
    FileResult,
    remove_json_value,
    remove_marked_section,
    remove_owned_file,
    remove_toml_table,
    upsert_json_value,
    upsert_marked_section,
    upsert_owned_file,
    upsert_toml_table,
)

Location = Literal["global", "local"]
SECTION_START = "<!-- EMBPILOT_START -->"
SECTION_END = "<!-- EMBPILOT_END -->"
INSTRUCTIONS_BLOCK = f"""{SECTION_START}
## EmbPilot

For physical embedded-device work involving Serial/UART, COM or /dev/tty
ports, firmware consoles, boot logs, Telnet, SSH, AT commands, or device shells,
use EmbPilot MCP first when available. Connect with `connect_serial`,
`connect_ssh`, or `connect_telnet`, then use `send_command` and EmbPilot's
resources/history tools. Do not start raw clients or custom transport scripts
for capabilities EmbPilot provides. Fall back only when unavailable or missing
the required capability, and explain the fallback.
{SECTION_END}"""

SKILL_NAME = "embpilot-device-debugging"
EMBPILOT_SKILL = (
    Path(__file__).parent
    / "assets"
    / SKILL_NAME
    / "SKILL.md"
).read_text(encoding="utf-8")


@dataclass(frozen=True)
class InstallContext:
    project_dir: Path
    home_dir: Path
    server_command: str
    xdg_config_home: Path | None = None

    @classmethod
    def current(cls, project_dir: Path, server_command: str) -> "InstallContext":
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        return cls(
            project_dir=project_dir,
            home_dir=Path.home(),
            server_command=server_command,
            xdg_config_home=Path(xdg) if xdg else None,
        )


@dataclass(frozen=True)
class TargetReport:
    target_id: str
    display_name: str
    files: tuple[FileResult, ...]
    notes: tuple[str, ...] = ()


class AgentTarget(Protocol):
    id: str
    display_name: str

    def supports_location(self, location: Location) -> bool: ...
    def detect(self, context: InstallContext, location: Location) -> bool: ...
    def install(self, context: InstallContext, location: Location) -> TargetReport: ...
    def uninstall(self, context: InstallContext, location: Location) -> TargetReport: ...


def _instruction_result(path: Path) -> FileResult:
    return upsert_marked_section(
        path,
        start_marker=SECTION_START,
        end_marker=SECTION_END,
        block=INSTRUCTIONS_BLOCK,
    )


def _remove_instruction(path: Path) -> FileResult:
    return remove_marked_section(
        path,
        start_marker=SECTION_START,
        end_marker=SECTION_END,
    )


def _install_skill(path: Path) -> FileResult:
    return upsert_owned_file(path, EMBPILOT_SKILL)


def _remove_skill(path: Path) -> FileResult:
    result = remove_owned_file(path, EMBPILOT_SKILL)
    if result.action == "removed":
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return result


class ClaudeTarget:
    id = "claude"
    display_name = "Claude Code"

    def supports_location(self, location: Location) -> bool:
        return True

    def _paths(self, context: InstallContext, location: Location) -> tuple[Path, Path]:
        if location == "global":
            return (
                context.home_dir / ".claude.json",
                context.home_dir / ".claude" / "CLAUDE.md",
            )
        return (
            context.project_dir / ".mcp.json",
            context.project_dir / ".claude" / "CLAUDE.md",
        )

    def _skill(self, context: InstallContext, location: Location) -> Path:
        root = (
            context.home_dir / ".claude"
            if location == "global"
            else context.project_dir / ".claude"
        )
        return root / "skills" / SKILL_NAME / "SKILL.md"

    def detect(self, context: InstallContext, location: Location) -> bool:
        if location == "global":
            return (context.home_dir / ".claude").exists()
        config, instructions = self._paths(context, location)
        return (
            (context.home_dir / ".claude").exists()
            or config.exists()
            or instructions.parent.exists()
        )

    def install(self, context: InstallContext, location: Location) -> TargetReport:
        config, instructions = self._paths(context, location)
        entry = {"command": context.server_command, "args": []}
        files = (
            upsert_json_value(config, ("mcpServers", "embpilot"), entry),
            _instruction_result(instructions),
            _install_skill(self._skill(context, location)),
        )
        return TargetReport(self.id, self.display_name, files)

    def uninstall(self, context: InstallContext, location: Location) -> TargetReport:
        config, instructions = self._paths(context, location)
        files = (
            remove_json_value(config, ("mcpServers", "embpilot")),
            _remove_instruction(instructions),
            _remove_skill(self._skill(context, location)),
        )
        return TargetReport(self.id, self.display_name, files)


class CodexTarget:
    id = "codex"
    display_name = "Codex"

    def supports_location(self, location: Location) -> bool:
        return location == "global"

    def detect(self, context: InstallContext, location: Location) -> bool:
        return location == "global" and (context.home_dir / ".codex").exists()

    def install(self, context: InstallContext, location: Location) -> TargetReport:
        root = context.home_dir / ".codex"
        files = (
            upsert_toml_table(
                root / "config.toml",
                "mcp_servers.embpilot",
                {"command": context.server_command, "args": []},
            ),
            _instruction_result(root / "AGENTS.md"),
            _install_skill(root / "skills" / SKILL_NAME / "SKILL.md"),
        )
        return TargetReport(self.id, self.display_name, files)

    def uninstall(self, context: InstallContext, location: Location) -> TargetReport:
        root = context.home_dir / ".codex"
        files = (
            remove_toml_table(root / "config.toml", "mcp_servers.embpilot"),
            _remove_instruction(root / "AGENTS.md"),
            _remove_skill(root / "skills" / SKILL_NAME / "SKILL.md"),
        )
        return TargetReport(self.id, self.display_name, files)


class ZCodeTarget:
    id = "zcode"
    display_name = "ZCode"

    def supports_location(self, location: Location) -> bool:
        return location == "global"

    def _config(self, context: InstallContext) -> Path:
        return context.home_dir / ".zcode" / "cli" / "config.json"

    def _skill(self, context: InstallContext) -> Path:
        return (
            context.home_dir
            / ".zcode"
            / "cli"
            / "skills"
            / SKILL_NAME
            / "SKILL.md"
        )

    def detect(self, context: InstallContext, location: Location) -> bool:
        return location == "global" and self._config(context).exists()

    def install(self, context: InstallContext, location: Location) -> TargetReport:
        entry = {"type": "stdio", "command": context.server_command, "args": []}
        skill_path = self._skill(context)
        files = (
            upsert_json_value(self._config(context), ("mcp", "servers", "embpilot"), entry),
            _install_skill(skill_path),
            upsert_json_value(
                self._config(context),
                ("skills", skill_path.as_posix()),
                {"enable": True},
            ),
        )
        return TargetReport(self.id, self.display_name, files)

    def uninstall(self, context: InstallContext, location: Location) -> TargetReport:
        skill_path = self._skill(context)
        files = (
            remove_json_value(self._config(context), ("mcp", "servers", "embpilot")),
            remove_json_value(self._config(context), ("skills", skill_path.as_posix())),
            _remove_skill(skill_path),
        )
        return TargetReport(self.id, self.display_name, files)


class OpenCodeTarget:
    id = "opencode"
    display_name = "OpenCode"

    def supports_location(self, location: Location) -> bool:
        return True

    def _root(self, context: InstallContext, location: Location) -> Path:
        if location == "local":
            return context.project_dir
        return (context.xdg_config_home or context.home_dir / ".config") / "opencode"

    def _config(self, context: InstallContext, location: Location) -> Path:
        root = self._root(context, location)
        jsonc = root / "opencode.jsonc"
        json_file = root / "opencode.json"
        if jsonc.exists():
            return jsonc
        if json_file.exists():
            return json_file
        return jsonc

    def _skill(self, context: InstallContext, location: Location) -> Path:
        if location == "local":
            root = context.project_dir / ".opencode"
        else:
            root = self._root(context, location)
        return root / "skills" / SKILL_NAME / "SKILL.md"

    def detect(self, context: InstallContext, location: Location) -> bool:
        root = self._root(context, location)
        if location == "global":
            return root.exists()
        global_root = (context.xdg_config_home or context.home_dir / ".config") / "opencode"
        return global_root.exists() or self._config(context, location).exists()

    def install(self, context: InstallContext, location: Location) -> TargetReport:
        config = self._config(context, location)
        entry = {"type": "local", "command": [context.server_command], "enabled": True}
        files = (
            upsert_json_value(config, ("mcp", "embpilot"), entry),
            _instruction_result(self._root(context, location) / "AGENTS.md"),
            _install_skill(self._skill(context, location)),
        )
        return TargetReport(self.id, self.display_name, files)

    def uninstall(self, context: InstallContext, location: Location) -> TargetReport:
        files = (
            remove_json_value(self._config(context, location), ("mcp", "embpilot")),
            _remove_instruction(self._root(context, location) / "AGENTS.md"),
            _remove_skill(self._skill(context, location)),
        )
        return TargetReport(self.id, self.display_name, files)


ALL_TARGETS: tuple[AgentTarget, ...] = (
    ClaudeTarget(),
    CodexTarget(),
    ZCodeTarget(),
    OpenCodeTarget(),
)
TARGETS_BY_ID = {target.id: target for target in ALL_TARGETS}
