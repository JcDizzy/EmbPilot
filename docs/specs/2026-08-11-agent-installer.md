# Multi-Harness Agent Installer

## Goal

Make a PyPI-installed EmbPilot discoverable and callable from supported agent
harnesses without requiring users to hand-edit MCP or instruction files.

## Interface

The installed package exposes one interactive entry point and one inverse:

```text
embpilot install
embpilot uninstall
```

Scripted callers can use `--target`, `--location`, and `--yes`. Target selection
accepts `auto`, `all`, `none`, or a comma-separated list of `claude`, `codex`,
`zcode`, and `opencode`.

## Module Design

- `agent_install/files.py` owns marker, JSON/JSONC, and TOML edits.
- `agent_install/targets.py` contains one adapter per harness and is the only
  module which knows harness-specific paths and config shapes.
- `agent_install/assets/embpilot-device-debugging/SKILL.md` is the packaged
  source of truth for the detailed agent workflow.
- `agent_install/installer.py` resolves selections and runs adapters through a
  small common interface.
- `agent_install/cli.py` owns prompts and human-readable reporting.

All writes are UTF-8 without BOM and LF-normalized. Configuration edits are
idempotent. Uninstall removes the EmbPilot key/table/marker block and deletes
the skill only when its content still exactly matches the packaged template.
User-modified skills are preserved, and only an empty EmbPilot skill directory
is cleaned up.

## Target Matrix

| Harness | Global MCP config | Routing hook | Detailed skill | Local support |
| --- | --- | --- | --- | --- |
| Claude Code | `~/.claude.json` | `~/.claude/CLAUDE.md` | `~/.claude/skills/embpilot-device-debugging/SKILL.md` | `.mcp.json` + `.claude/CLAUDE.md` + `.claude/skills/...` |
| Codex | `~/.codex/config.toml` | `~/.codex/AGENTS.md` | `~/.codex/skills/embpilot-device-debugging/SKILL.md` | No |
| ZCode | `~/.zcode/cli/config.json` | Config-registered skill | `~/.zcode/cli/skills/embpilot-device-debugging/SKILL.md` | No |
| OpenCode | `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/AGENTS.md` | `~/.config/opencode/skills/embpilot-device-debugging/SKILL.md` | `opencode.jsonc` + `AGENTS.md` + `.opencode/skills/...` |

OpenCode honors `XDG_CONFIG_HOME`. Existing `opencode.json` is used when no
`opencode.jsonc` exists. Comment-bearing JSONC is currently rejected without
writing because the standard library cannot preserve comments safely.

## Interaction

With no flags, installation displays all supported harnesses, marks detected
ones, accepts a multi-selection, and then asks for global or local scope.
`--yes` chooses `--target auto --location global`. Uninstall defaults to all
targets under `--yes`, making cleanup independent of current detection state.

Claude Code, Codex, and OpenCode use a dual-layer route: a short marker-fenced
instruction is always visible, while the detailed skill is discovered from the
harness-native skill directory. ZCode installs and enables the same skill
through its confirmed `skills` configuration map, avoiding reliance on an
unverified global instruction file. Codex and ZCode are skipped with an
explicit note when local scope is selected. The server command written to
configs is the absolute running `embpilot` launcher when available, avoiding
GUI harness PATH differences.
