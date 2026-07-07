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
embpilot
```

See `embpilot --help` for available options, or run `embpilot doctor` for
environment diagnostics (Python, core/RAG deps, drivers, storage, serial ports).

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
- Safety limits can be tuned from the CLI with flags such as
  `--command-timeout-max-ms`, `--export-limit-max`, and
  `--tool-rate-limit-per-minute`.

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
  SQLite FTS5 for historical search.

## License

MIT
