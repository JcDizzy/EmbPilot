# EmbPilot

> **Emb**edded device debugging via the **Pilot** (MCP) protocol.

EmbPilot is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
server that lets LLM agents connect to, control, and debug physical embedded
devices over Serial, Telnet, or SSH.

## Features

- **Connect** to devices via Serial UART, Telnet, or SSH
- **Send commands** and capture output with regular-expression interception (Expect)
- **Stream live logs** through MCP resource subscriptions
- **Persist sessions** in SQLite (WAL mode) for RAG-backed historical search
- **Local vector search** (fastembed + LanceDB) over Datasheets, Error Code manuals, and KB articles
- **Analyse crash logs** and run hardware sanity checks with guided prompts

## Quick Start

```bash
pip install embpilot
embpilot
```

See `embpilot --help` for available options.

## Project Status

**Alpha** — active development.

## Architecture

```
src/embpilot/
├── __main__.py        # CLI entry point
├── config.py          # Configuration (XDG paths, framing timeout)
├── server.py          # MCP Tools / Resources / Prompts registration
├── core/
│   ├── engine.py      # Frame assembly, ring buffer, Expect engine
│   ├── database.py    # SQLite WAL layer
│   ├── rag.py         # fastembed + LanceDB vector search
│   └── schema.sql     # DDL
└── drivers/
    ├── base.py        # Abstract device interface
    ├── serial_dev.py  # pyserial-asyncio
    ├── telnet_dev.py  # telnetlib3
    └── ssh_dev.py     # asyncssh
```

## License

MIT
