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
embpilot tools                      # list available tools
embpilot tool connect_serial --json '{"port":"COM3","baudrate":115200}'
embpilot tool send_command --json '{"command":"help","line_ending":"crlf"}'
embpilot tool list_sessions
embpilot shell                      # interactive REPL with a persistent session
```

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
