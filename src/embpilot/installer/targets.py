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

import json
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
    atomic_write,
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

#: opencode wants command as an argv array plus an explicit enabled flag.
OPENCODE_MCP_ENTRY: dict = {
    "type": "local",
    "command": ["embpilot", "--data-dir", "./.embpilot-data"],
    "enabled": True,
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
        changes.append(
            upsert_mcp_entry_json(_claude_mcp_json(loc), "mcpServers", MCP_SERVER_ENTRY)
        )
        changes.append(upsert_instructions(_claude_instructions(loc)))
        return changes

    def uninstall(self, loc: Location) -> list[FileChange]:
        changes: list[FileChange] = []
        changes.append(
            remove_mcp_entry_json(_claude_mcp_json(loc), "mcpServers")
        )
        changes.append(remove_instructions(_claude_instructions(loc)))
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


def upsert_instructions(path: Path) -> FileChange:
    """Upsert the marker-fenced EmbPilot instructions block into *path*."""
    return FileChange(
        path,
        replace_or_append_marked_section(
            path, INSTRUCTIONS_BLOCK, SECTION_START, SECTION_END
        ),
    )


def remove_instructions(path: Path) -> FileChange:
    """Remove the marker-fenced EmbPilot instructions block from *path*."""
    return FileChange(
        path,
        remove_marked_section(path, SECTION_START, SECTION_END),
    )


def install_skill_to(dest: Path) -> FileChange:
    """Copy the packaged skill template into a harness skill directory."""
    if dest.exists():
        return FileChange(dest, "unchanged")
    if not SKILL_SOURCE_DIR.exists():
        return FileChange(dest, "not-found", note="skill template missing from package")
    shutil.copytree(SKILL_SOURCE_DIR, dest)
    return FileChange(dest, "created")


def remove_skill_from(dest: Path) -> FileChange:
    """Remove a previously installed skill directory."""
    if not dest.exists():
        return FileChange(dest, "not-found")
    shutil.rmtree(dest)
    return FileChange(dest, "removed")


def upsert_mcp_entry_json(
    config_path: Path,
    servers_key: str,
    entry: dict,
) -> FileChange:
    """Merge *entry* under ``<root>[servers_key][embpilot]`` in a JSON file."""
    config = read_json(config_path) or {}
    servers = config.setdefault(servers_key, {})
    if json_deep_equal(servers.get(MCP_SERVER_NAME), entry):
        return FileChange(config_path, "unchanged")
    servers[MCP_SERVER_NAME] = entry
    write_json(config_path, config)
    existed = config_path.exists()
    return FileChange(config_path, "updated" if existed else "created")


def remove_mcp_entry_json(
    config_path: Path,
    servers_key: str,
) -> FileChange:
    """Remove only the embpilot entry from a JSON MCP config."""
    config = read_json(config_path) or {}
    servers = config.get(servers_key) or {}
    if MCP_SERVER_NAME not in servers:
        return FileChange(config_path, "not-found")
    del servers[MCP_SERVER_NAME]
    if servers:
        config[servers_key] = servers
    else:
        config.pop(servers_key, None)
    if config:
        write_json(config_path, config)
    else:
        config_path.unlink(missing_ok=True)
    return FileChange(config_path, "removed")


def upsert_zcode_mcp(config_path: Path) -> FileChange:
    """Merge the entry under the nested ``mcp.servers.embpilot`` path."""
    config = read_json(config_path) or {}
    mcp = config.setdefault("mcp", {})
    servers = mcp.setdefault("servers", {})
    if json_deep_equal(servers.get(MCP_SERVER_NAME), MCP_SERVER_ENTRY):
        return FileChange(config_path, "unchanged")
    servers[MCP_SERVER_NAME] = MCP_SERVER_ENTRY
    write_json(config_path, config)
    return FileChange(config_path, "updated" if config_path.exists() else "created")


def remove_zcode_mcp(config_path: Path) -> FileChange:
    """Remove only the ``mcp.servers.embpilot`` entry."""
    config = read_json(config_path) or {}
    mcp = config.get("mcp") or {}
    servers = mcp.get("servers") or {}
    if MCP_SERVER_NAME not in servers:
        return FileChange(config_path, "not-found")
    del servers[MCP_SERVER_NAME]
    if servers:
        mcp["servers"] = servers
    else:
        mcp.pop("servers", None)
    if mcp:
        config["mcp"] = mcp
    else:
        config.pop("mcp", None)
    if config:
        write_json(config_path, config)
    else:
        config_path.unlink(missing_ok=True)
    return FileChange(config_path, "removed")


def _toml_section_end(lines: list[str], start_idx: int) -> int:
    """Index just past the table started at *start_idx*: everything up to the
    next ``[header]`` line belongs to the current table (TOML semantics)."""
    idx = start_idx + 1
    while idx < len(lines):
        if lines[idx].strip().startswith("["):
            break
        idx += 1
    return idx


def upsert_toml_table(
    config_path: Path,
    table_key: str,
    entry: dict,
) -> FileChange:
    """Upsert a dotted TOML table (e.g. ``mcp_servers.embpilot``).

    Values are serialized conservatively (strings and string lists); the
    table replaces an existing same-key section or is appended at the end.
    """
    section = f"[{table_key}]"
    body_lines: list[str] = []
    for key, value in entry.items():
        if isinstance(value, list):
            rendered = ", ".join(json.dumps(item) for item in value)
            body_lines.append(f"  {key} = [{rendered}]")
        else:
            body_lines.append(f"  {key} = {json.dumps(value)}")
    block = "\n".join([section, *body_lines])

    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
    else:
        content = ""
    lines = content.splitlines()
    start_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == section),
        None,
    )
    if start_idx is not None:
        end_idx = _toml_section_end(lines, start_idx)
        new_lines = lines[:start_idx] + block.splitlines() + lines[end_idx:]
        joined = "\n".join(new_lines).strip() + "\n"
        atomic_write(config_path, joined)
        return FileChange(config_path, "updated")
    if content.strip():
        atomic_write(config_path, content.rstrip() + "\n\n" + block + "\n")
    else:
        atomic_write(config_path, block + "\n")
    return FileChange(config_path, "created")


def remove_toml_table(config_path: Path, table_key: str) -> FileChange:
    """Remove one dotted TOML table section if present."""
    if not config_path.exists():
        return FileChange(config_path, "kept")
    section = f"[{table_key}]"
    lines = config_path.read_text(encoding="utf-8").splitlines()
    start_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == section),
        None,
    )
    if start_idx is None:
        return FileChange(config_path, "not-found")
    end_idx = _toml_section_end(lines, start_idx)
    remaining = lines[:start_idx] + lines[end_idx:]
    joined = "\n".join(remaining).strip()
    if joined:
        atomic_write(config_path, joined + "\n")
    else:
        config_path.unlink(missing_ok=True)
    return FileChange(config_path, "removed")


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
        changes = [upsert_instructions(_pi_instructions(loc))]
        if loc == "global":
            changes.append(install_skill_to(_pi_skill_dest()))
        return changes

    def uninstall(self, loc: Location) -> list[FileChange]:
        changes = [remove_instructions(_pi_instructions(loc))]
        if loc == "global":
            changes.append(remove_skill_from(_pi_skill_dest()))
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
        return [upsert_instructions(path)]

    def uninstall(self, loc: Location) -> list[FileChange]:
        path = project_dir() / "AGENTS.md"
        return [remove_instructions(path)]

    def print_config(self) -> str:
        return "Append the block below to the project AGENTS.md:\n\n" + INSTRUCTIONS_BLOCK

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(project_dir() / "AGENTS.md")]


# ── OpenAI Codex CLI ────────────────────────────────────────────────────────


class CodexTarget(AgentTarget):
    """Codex CLI: global only. MCP entry in ~/.codex/config.toml as a dotted
    TOML table plus instructions in ~/.codex/AGENTS.md."""

    id = "codex"
    display_name = "Codex CLI"

    def _config_dir(self) -> Path:
        return home_dir() / ".codex"

    def _toml_path(self) -> Path:
        return self._config_dir() / "config.toml"

    def _instructions_path(self) -> Path:
        return self._config_dir() / "AGENTS.md"

    def supports_location(self, loc: Location) -> bool:
        return loc == "global"

    def detect(self, loc: Location) -> DetectionResult:
        config = self._toml_path()
        content = ""
        if config.exists():
            try:
                content = config.read_text(encoding="utf-8")
            except OSError:
                pass
        already = "[mcp_servers.embpilot]" in content
        return DetectionResult(
            self._config_dir().exists() or config.exists(),
            already,
            str(config),
        )

    def install(self, loc: Location) -> list[FileChange]:
        return [
            upsert_toml_table(self._toml_path(), "mcp_servers.embpilot", MCP_SERVER_ENTRY),
            upsert_instructions(self._instructions_path()),
        ]

    def uninstall(self, loc: Location) -> list[FileChange]:
        return [
            remove_toml_table(self._toml_path(), "mcp_servers.embpilot"),
            remove_instructions(self._instructions_path()),
        ]

    def print_config(self) -> str:
        return (
            "Add to ~/.codex/config.toml (Codex CLI is global-only):\n\n"
            "[mcp_servers.embpilot]\n"
            'command = "embpilot"\n'
            'args = ["--data-dir", "./.embpilot-data"]\n'
            "\nand append the EmbPilot block to ~/.codex/AGENTS.md."
        )

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(self._toml_path()), str(self._instructions_path())]


# ── opencode ─────────────────────────────────────────────────────────────────


def _opencode_config_dir() -> Path:
    override = os.environ.get("XDG_CONFIG_HOME")
    base = Path(override) if override else home_dir() / ".config"
    return base / "opencode"


def _opencode_config_path(loc: Location) -> Path:
    if loc == "global":
        return _opencode_config_dir() / "opencode.json"
    return project_dir() / "opencode.json"


def _opencode_instructions(loc: Location) -> Path:
    if loc == "global":
        return _opencode_config_dir() / "AGENTS.md"
    return project_dir() / "AGENTS.md"


class OpenCodeTarget(AgentTarget):
    """opencode: MCP entry uses the ``mcp.<name>`` wrapper (command as an argv
    array plus an explicit enabled flag); instructions follow AGENTS.md.
    Config is written as opencode.json (a legal fallback name) so plain JSON
    tooling keeps any user comments in an existing .jsonc untouched."""

    id = "opencode"
    display_name = "opencode"

    def detect(self, loc: Location) -> DetectionResult:
        config = _opencode_config_path(loc)
        data = read_json(config) or {}
        already = bool((data.get("mcp") or {}).get(MCP_SERVER_NAME))
        installed = config.exists() or _opencode_config_dir().exists()
        return DetectionResult(installed, already, str(config))

    def install(self, loc: Location) -> list[FileChange]:
        changes = [
            upsert_mcp_entry_json(
                _opencode_config_path(loc), "mcp", OPENCODE_MCP_ENTRY
            )
        ]
        changes.append(upsert_instructions(_opencode_instructions(loc)))
        return changes

    def uninstall(self, loc: Location) -> list[FileChange]:
        changes = [remove_mcp_entry_json(_opencode_config_path(loc), "mcp")]
        changes.append(remove_instructions(_opencode_instructions(loc)))
        return changes

    def print_config(self) -> str:
        import json

        snippet = json.dumps(
            {"mcp": {MCP_SERVER_NAME: OPENCODE_MCP_ENTRY}},
            ensure_ascii=False,
            indent=2,
        )
        return (
            "Add to opencode.json (project) or ~/.config/opencode/opencode.json "
            "(global):\n" + snippet
        )

    def describe_paths(self, loc: Location) -> list[str]:
        return [
            str(_opencode_config_path(loc)),
            str(_opencode_instructions(loc)),
        ]


# ── ZCode (Z.ai) ─────────────────────────────────────────────────────────────


def _zcode_home() -> Path:
    return home_dir() / ".zcode"


def _zcode_mcp_path(loc: Location) -> Path:
    if loc == "global":
        return _zcode_home() / "cli" / "config.json"
    return project_dir() / ".zcode" / "config.json"


def _zcode_instructions(loc: Location) -> Path:
    if loc == "global":
        return _zcode_home() / "AGENTS.md"
    return project_dir() / "AGENTS.md"


def _zcode_skill_dest() -> Path:
    return _zcode_home() / "skills" / PI_SKILL_DIR_NAME


class ZCodeTarget(AgentTarget):
    """ZCode (Z.ai): MCP under ``mcp.servers`` in ~/.zcode/cli/config.json
    (global) or ./.zcode/config.json (local); skill copied into
    ~/.zcode/skills/; instructions via AGENTS.md. ZCode also scans
    .agents/mcp.json, so the Claude-shaped format is understood too."""

    id = "zcode"
    display_name = "ZCode (Z.ai)"

    def detect(self, loc: Location) -> DetectionResult:
        config = _zcode_mcp_path(loc)
        data = read_json(config) or {}
        already = bool(
            ((data.get("mcp") or {}).get("servers") or {}).get(MCP_SERVER_NAME)
        )
        installed = config.exists() or _zcode_home().exists()
        return DetectionResult(installed, already, str(config))

    def install(self, loc: Location) -> list[FileChange]:
        changes: list[FileChange] = []
        if loc == "global":
            changes.append(upsert_zcode_mcp(_zcode_mcp_path(loc)))
            changes.append(install_skill_to(_zcode_skill_dest()))
        else:
            # Local: write .agents/mcp.json (mcpServers shape), which ZCode
            # scans and which matches the Claude contract exactly.
            changes.append(
                upsert_mcp_entry_json(
                    project_dir() / ".agents" / "mcp.json",
                    "mcpServers",
                    MCP_SERVER_ENTRY,
                )
            )
        changes.append(upsert_instructions(_zcode_instructions(loc)))
        return changes

    def uninstall(self, loc: Location) -> list[FileChange]:
        changes: list[FileChange] = []
        if loc == "global":
            changes.append(remove_zcode_mcp(_zcode_mcp_path(loc)))
            changes.append(remove_skill_from(_zcode_skill_dest()))
        else:
            changes.append(
                remove_mcp_entry_json(
                    project_dir() / ".agents" / "mcp.json", "mcpServers"
                )
            )
        changes.append(remove_instructions(_zcode_instructions(loc)))
        return changes

    def print_config(self) -> str:
        import json

        snippet = json.dumps(
            {"mcp": {"servers": {MCP_SERVER_NAME: MCP_SERVER_ENTRY}}},
            ensure_ascii=False,
            indent=2,
        )
        return (
            "Add to ~/.zcode/cli/config.json (global) or ./.zcode/config.json "
            "(project); ZCode also reads .agents/mcp.json with the Claude "
            "mcpServers shape:\n" + snippet
        )

    def describe_paths(self, loc: Location) -> list[str]:
        paths: list[str] = []
        if loc == "global":
            paths.append(str(_zcode_mcp_path(loc)))
            paths.append(str(_zcode_skill_dest()))
        else:
            paths.append(str(project_dir() / ".agents" / "mcp.json"))
        paths.append(str(_zcode_instructions(loc)))
        return paths


# ── Registry ─────────────────────────────────────────────────────────────────

ALL_TARGETS: list[AgentTarget] = [
    ClaudeCodeTarget(),
    ZCodeTarget(),
    OpenCodeTarget(),
    CodexTarget(),
    PiTarget(),
    AgentsTarget(),
]


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
