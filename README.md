# EmbPilot

> **Emb**edded device debugging via the **Pilot** (MCP) protocol.

EmbPilot is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
server that lets LLM agents connect to, control, and debug physical embedded
devices over Serial, Telnet, or SSH.

## Features

- **Connect** to devices via Serial UART, Telnet, or SSH
- **Send commands** and capture output with regular-expression interception (Expect)
- **Reset targets** by issuing a reboot to the connected device
- **Guard risky operations** with dangerous-command confirmation, delete
  confirmation, redacted audit history, rate limiting, and bounded exports
- **Expose active-session resources** through MCP, including `device://live_log`
  and `device://session_info`
- **Persist sessions** in SQLite (WAL mode) with FTS5-backed historical search
- **Search and export** recorded sessions (by session id, keyword, or full export)
- **Local vector search tools** (optional `embpilot[rag]`) over Datasheets,
  Error Code manuals, and KB articles
- **Analyse crash logs** and run hardware sanity checks with guided prompts

## Quick Start

```bash
pip install embpilot        # core: Serial/Telnet/SSH MCP server
pip install embpilot[rag]   # + optional local RAG (fastembed + LanceDB)
embpilot install            # detect and configure supported agent harnesses
embpilot --help
```

`embpilot install` follows the explicit installer model used by agent tooling
such as CodeGraph. It detects supported harnesses, lets you select targets and
global/local scope, then idempotently installs MCP config and a marker-fenced
EmbPilot routing block where the harness supports one. Existing instructions
and other MCP servers are preserved. Run `embpilot uninstall` to remove only
content managed by EmbPilot.

Run `embpilot doctor` for
environment diagnostics (Python, core/RAG deps, drivers, storage, serial ports).
The plain `embpilot` command starts the MCP stdio server and waits for an MCP
client; it is not an interactive shell.

To configure another project without changing directories:

```bash
embpilot install --project-dir path/to/project
```

Non-interactive examples:

```bash
embpilot install --target claude,codex,zcode,opencode --location global
embpilot install --target claude,opencode --location local --project-dir .
embpilot install --yes  # auto-detect targets, global scope
```

Supported installation surfaces:

| Harness | Global | Local project |
| --- | --- | --- |
| Claude Code | `~/.claude.json`, `~/.claude/CLAUDE.md` | `.mcp.json`, `.claude/CLAUDE.md` |
| Codex | `~/.codex/config.toml`, `~/.codex/AGENTS.md` | Not supported |
| ZCode | `~/.zcode/cli/config.json` + registered EmbPilot skill | Not supported |
| OpenCode | `~/.config/opencode/opencode.jsonc`, `AGENTS.md` | `opencode.jsonc`, `AGENTS.md` |

OpenCode follows `XDG_CONFIG_HOME` when set. ZCode installs its confirmed MCP
entry plus an enabled `embpilot-device-debugging` skill; no unverified global
instruction path is written.

## MCP Client Configuration

Configure your agent to start EmbPilot as an MCP server:

```json
{
  "mcpServers": {
    "embpilot": {
      "command": "embpilot",
      "args": ["--data-dir", "./.embpilot-data"]
    }
  }
}
```

Agents should use EmbPilot before raw `ssh`, `telnet`, or serial clients. Pick
the protocol-specific tool and pass an actual JSON object, not an encoded JSON
string or a nested `config` object:

```json
{"port":"COM3","baudrate":115200}
{"host":"192.168.1.10","username":"root","key_file":"~/.ssh/id_ed25519"}
{"host":"192.168.1.10","port":23}
```

These map to `connect_serial`, `connect_ssh`, and `connect_telnet`. Then use
`send_command`; set `expect_regex`, `timeout_ms`, or `line_ending` when the
target requires them. Connection successes and runtime failures include
structured JSON for agents as well as readable text. Invalid arguments remain
MCP invalid-parameter errors so clients can repair the call.

An empty command with `line_ending="as-is"` or `"none"` is rejected before it
reaches a transport. To send only Enter/a blank line, use `line_ending="lf"`,
`"crlf"`, or `"cr"` as required by the target.

Current resource direction:

- `device://live_log` exposes the active session's recent log snapshot and is
  currently a snapshot resource. EmbPilot does not advertise subscriptions
  until it sends MCP `notifications/resources/updated` events.
- `device://session_info` exposes honest session metadata for the current
  connection instead of pretending EmbPilot can issue one generic sysinfo probe
  across all targets.

Safety defaults:

- SSH uses AsyncSSH host-key defaults unless `known_hosts: null` is explicitly
  passed in the connection config.
- Destructive actions such as `reset_target`, dangerous commands, session
  deletion, and RAG document deletion require explicit confirmation flags.
- Operation audit export redacts sensitive config keys and inline command
  secrets such as passwords, tokens, Authorization headers, and AT Wi-Fi
  passwords.
- Safety limits can be tuned from the CLI with flags such as
  `--command-timeout-max-ms`, `--export-limit-max`, and
  `--tool-rate-limit-per-minute`; MCP tool schemas are generated from those
  configured limits.

## Project Status

**Alpha** — active development.

## Architecture

```
src/embpilot/
├── __main__.py        # Python module entry point
├── cli.py             # CLI argument parsing and startup wiring
├── mcp_app.py         # MCP app assembly and stdio server runner
├── config.py          # Configuration (XDG paths, framing timeout, retention)
├── server.py          # Compatibility wrapper over the new MCP runner
├── runtime/
│   ├── __init__.py
│   ├── models.py      # SessionInfo plus canonical log/ring exports
│   ├── pipeline.py    # Dispatcher-based log fan-out and framing
│   ├── expect.py      # Command windows and expect matching
│   ├── resources.py   # device://live_log and device://session_info payloads
│   └── session.py     # Session lifecycle and active connection state
├── core/
│   ├── engine.py      # Canonical LogLine/RingBuffer types
│   ├── database.py    # SQLite WAL layer and schema loading
│   ├── rag.py         # fastembed + LanceDB vector search
│   ├── schema_main.sql
│   └── schema_session.sql
└── drivers/
    ├── base.py        # Abstract device interface
    ├── serial_dev.py  # pyserial-asyncio
    ├── telnet_dev.py  # telnetlib3
    └── ssh_dev.py     # asyncssh
```

- `mcp_app.py` owns MCP protocol registration and delegates runtime behavior to
  the `runtime/` package.
- `runtime/pipeline.py` now uses explicit dispatcher fan-out instead of the old
  implicit multi-consumer queue description.
- Driver implementations expose a byte-oriented runtime contract. Text-mode
  transports such as Telnet and SSH adapt their library streams internally, so
  the log pipeline always receives bytes before frame assembly. `send_command`
  accepts an explicit `line_ending` strategy (`as-is`, `none`, `lf`, `crlf`,
  `cr`) for targets which require a specific terminator.
- Session logs store inferred `level` and `tag` metadata and are indexed through
  SQLite FTS5 for historical search. `search_history_logs` defaults to
  `mode="fts"` and also supports `mode="substring"` for literal partial-token
  searches such as register names, paths, and abbreviated error fragments.

## License

MIT
