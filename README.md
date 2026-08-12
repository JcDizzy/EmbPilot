# EmbPilot

> **Emb**edded device debugging via the **Pilot** (MCP) protocol.

EmbPilot is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
server that lets LLM agents connect to, control, and debug physical embedded
devices over Serial, Telnet, or SSH.

## Features

- **Connect** to devices via Serial UART, Telnet, or SSH
- **Send commands** and capture output with regular-expression interception (Expect)
- **Browse recent live logs** through MCP resources
- **Persist sessions** in SQLite (WAL mode) for RAG-backed historical search
- **Local vector search** (fastembed + LanceDB) over Datasheets, Error Code manuals, and KB articles
- **Analyse crash logs** and run hardware sanity checks with guided prompts

## Quick Start

```bash
pip install embpilot
embpilot --help
```

Install local RAG support only when needed:

```bash
pip install "embpilot[rag]"
```

EmbPilot is a stdio MCP server, so starting `embpilot` directly waits for an
MCP client rather than opening an interactive terminal.

## Command Line Interface

`embpilot` starts the stdio MCP server by default, and also exposes every MCP
tool through a thin CLI that shares the exact same dispatch layer:

```bash
embpilot --help                    # server by default; subcommands for tools
embpilot tools                      # list available tools
embpilot tool connect_serial --json '{"port":"COM3","baudrate":115200}'
embpilot tool send_command --json '{"command":"help","line_ending":"crlf"}'
embpilot tool list_sessions
embpilot shell                      # interactive REPL with a persistent session
embpilot batch                      # scripted JSONL mode: one request per stdin line
embpilot serve                      # persistent daemon sharing one session across calls
embpilot --socket daemon.json tool list_sessions   # talk to a running daemon
```

## Agent Harness Installation

Wire EmbPilot into the agents you use — MCP config where the harness supports
it, plus a marker-fenced instructions block (and the pi skill) where it does
not. Modeled on CodeGraph's installer; the block lists both the MCP tools and
their CLI equivalents, so an agent whose MCP server fails to start falls back
to the CLI automatically:

```bash
embpilot install                      # auto-detect installed harnesses
embpilot install --target pi          # pi: AGENTS.md instructions + user skill
embpilot install --target claude      # Claude Code: .mcp.json + CLAUDE.md
embpilot install --target agents      # project AGENTS.md (Cursor/Codex/Gemini/...)
embpilot install --target all --location global --check
embpilot install --print-config claude   # show the manual snippet, write nothing
embpilot uninstall --target pi        # remove only what install wrote
```

Targets: `claude` (MCP + CLAUDE.md), `zcode` (MCP + skill +
AGENTS.md), `opencode` (MCP + AGENTS.md), `codex` (TOML MCP +
AGENTS.md, global), `pi` (CLI-only: AGENTS.md + skill copied into
`~/.pi/agent/skills/`), `agents` (project AGENTS.md).
`--location local` writes project files, `--location global` writes user-scope
files; `--check` reports state without writing and exits 0 when everything
selected is configured.

`batch` reads one request object per stdin line
(`{"tool": "connect_serial", "args": {"port": "COM3"}}`) and prints one
result envelope per stdout line, without any banner; add `--fail-fast` to stop
at the first failing call. `serve` keeps a single session manager alive so
`connect` survives across separate invocations: start it once, then pass
`--socket <daemon.json>` (or a `unix:PATH` / `tcp:HOST:PORT` endpoint) to
`tool` / `tools` / `batch`. POSIX uses a unix socket; Windows falls back to a
TCP loopback bound to 127.0.0.1 because the standard library has no named-pipe
server API.

`read_output` observes device output without sending any bytes; it returns
early when `expect_regex` matches or after `duration_ms`, which is how boot
logs and periodic device output are captured passively.

Tool arguments also accept schema-driven flags instead of inline JSON:

```bash
embpilot tool connect_serial --port COM3 --baudrate 115200 --line-ending crlf
embpilot help connect_serial            # schema, examples, guidance for one tool
```

`run` connects, executes several commands, and disconnects in one call:

```bash
embpilot run --connect '{"port":"COM3"}' help version uname -a
```

`search_history_logs` accepts an optional `session_id` so closed sessions can
be searched after disconnection (`list_sessions` shows the ids).

One-shot tool calls exit `0` on success, `1` on tool failure, and `2` on usage
or argument errors. Pass `--json-output` to print the structured
`ok`/`data`/`error` envelope instead of readable text. In the shell, connect
once and then keep issuing tool calls against the same active session.
`monitor` streams new device log lines (prefixed `[log]`) while commands stay
usable (results prefixed `[cmd]`); type `stop` to leave monitor mode. Data
path options (`--data-dir`, etc.) must appear before the subcommand:

```bash
embpilot --data-dir ./.embpilot-data tool list_sessions
```

## MCP Client Configuration

Configure the client to start the installed executable; do not copy a
developer-specific absolute path:

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

Prefer `connect_serial`, `connect_ssh`, or `connect_telnet`. Arguments are JSON
objects, not JSON strings:

```json
{"port":"COM3","baudrate":115200,"line_ending":"crlf"}
```

```json
{"host":"192.168.1.10","username":"root","key_file":"~/.ssh/id_ed25519"}
```

`send_command` accepts `line_ending`, `expect_regex`, `timeout_ms`, and
`max_output_chars`. Results contain structured `ok`, `data`, or `error` fields
alongside readable text. SSH host-key verification is enabled by default.

## Project Status

**Alpha** — active development.

## Architecture

```
src/embpilot/
├── __main__.py        # CLI entry point
├── cli.py             # CLI subcommands: tools, one-shot tool calls
├── cli_shell.py       # Interactive REPL reusing the MCP dispatch layer
├── cli_format.py      # Terminal result formatting
├── config.py          # Configuration (XDG paths, framing timeout)
├── server.py          # MCP Tools / Resources / Prompts registration
├── core/
│   ├── engine.py      # Frame assembly, ring buffer, Expect engine
│   ├── commands.py    # Command line endings, expect, and output capture
│   ├── database.py    # SQLite WAL layer
│   ├── rag.py         # fastembed + LanceDB vector search
│   └── schema_*.sql   # Main/session DDL
└── drivers/
    ├── base.py        # Abstract device interface
    ├── serial_dev.py  # pyserial-asyncio
    ├── telnet_dev.py  # telnetlib3
    └── ssh_dev.py     # asyncssh
```

Agent routing guidance is bundled in
`.agents/skills/embpilot-device-debugging/`.

## License

MIT
