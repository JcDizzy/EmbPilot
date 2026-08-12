"""
Marker-fenced agent-instructions block for ``embpilot install``.

Modeled on CodeGraph's installer: a short block wrapped in HTML-comment
markers is upserted into each harness's instructions file (CLAUDE.md /
AGENTS.md). The block lists both surfaces — the MCP tools when the harness
has an MCP client, and the equivalent CLI commands that always work — so an
agent whose MCP server fails to start naturally falls back to the CLI with
identical output.

Keep the block SHORT: the main agent reads it every turn.
"""

from __future__ import annotations

SECTION_START = "<!-- EMBPILOT_START -->"
SECTION_END = "<!-- EMBPILOT_END -->"

INSTRUCTIONS_BLOCK = f"""{SECTION_START}
## EmbPilot

For embedded-device debugging (Serial/UART, SSH, Telnet) in repositories where
EmbPilot is installed (`embpilot` on PATH), route device access through
EmbPilot instead of raw ssh/telnet/serial clients:

- **MCP tools** (when available): connect_serial / connect_ssh / connect_telnet,
  send_command, read_output, reset_target, search_history_logs, list_sessions,
  delete_session, export_session, disconnect_device.
- **Shell** (always works — fall back here if the MCP server is unavailable or
  its tools fail to start): `embpilot tool <name> --json '<args>'` returns the
  same structured `ok`/`data`/`error` envelope; `embpilot batch` runs a scripted
  sequence (JSONL in, JSONL out); `embpilot run --connect '<json>' cmd1 cmd2`
  connects, runs, and disconnects in one call; `embpilot help <tool>` shows the
  schema and guidance; `embpilot serve` + `--socket` keeps one session alive
  across invocations.

If EmbPilot is not installed or lacks the required capability, fall back to raw
tools and explain why.
{SECTION_END}"""

#: Marker used inside the block when the CLI fallback must be emphasized for
#: harnesses without any MCP client (pi). Kept as a separate constant so the
#: block can be tailored per target without duplicating the whole text.
CLI_ONLY_NOTE = (
    "This harness has no MCP client: use the Shell commands only; "
    "`embpilot batch` and `embpilot serve` replace the MCP session flow."
)
