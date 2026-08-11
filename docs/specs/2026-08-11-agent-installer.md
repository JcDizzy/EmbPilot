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
- `agent_install/installer.py` resolves selections and runs adapters through a
  small common interface.
- `agent_install/cli.py` owns prompts and human-readable reporting.

All writes are UTF-8 without BOM and LF-normalized. Configuration edits are
idempotent and uninstall removes only the EmbPilot key/table/marker block.

## Target Matrix

| Harness | Global MCP config | Global instructions | Local support |
| --- | --- | --- | --- |
| Claude Code | `~/.claude.json` | `~/.claude/CLAUDE.md` | `.mcp.json` + `.claude/CLAUDE.md` |
| Codex | `~/.codex/config.toml` | `~/.codex/AGENTS.md` | No |
| ZCode | `~/.zcode/cli/config.json` | Registered `embpilot-device-debugging` skill | No |
| OpenCode | `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/AGENTS.md` | `opencode.jsonc` + `AGENTS.md` |

OpenCode honors `XDG_CONFIG_HOME`. Existing `opencode.json` is used when no
`opencode.jsonc` exists. Comment-bearing JSONC is currently rejected without
writing because the standard library cannot preserve comments safely.

## Interaction

With no flags, installation displays all supported harnesses, marks detected
ones, accepts a multi-selection, and then asks for global or local scope.
`--yes` chooses `--target auto --location global`. Uninstall defaults to all
targets under `--yes`, making cleanup independent of current detection state.

ZCode installs and enables a dedicated skill through its confirmed `skills`
configuration map, avoiding reliance on an unverified global instruction file.
Codex and ZCode are skipped with an explicit note when local scope is selected.
The server command written to configs is the absolute running `embpilot`
launcher when available, avoiding GUI harness PATH differences.
