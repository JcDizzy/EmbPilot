# Progress

## 2026-08-12 - interactive installer cancellations (implemented)

### Done
- Every interactive prompt accepts q/quit/cancel/esc/exit; Ctrl+C and EOF
  (Ctrl+D / closed pipe) cancel the flow cleanly with "cancelled - nothing
  written" and zero files written. EOF no longer falls through to defaults
  (real bug: closed stdin used to mean "select all detected").
- Cancellation unified via a `Cancelled` exception caught together with
  KeyboardInterrupt/EOFError in the interactive orchestrator; non-
  interactive (--yes) Ctrl+C exits 130.
- All installer output switched to ASCII punctuation (em dashes rendered as
  garbage in GBK Windows consoles).
- Full suite: 138 passed, 1 skipped.

## 2026-08-12 - interactive installer mode (implemented)

### Done
- `embpilot install` / `uninstall` with no --target now run a CodeGraph-style
  interactive flow (`installer/prompts.py`, zero-dependency stdlib input):
  detected harnesses listed pre-checked, target multi-select by number,
  scope prompt with per-target support hints, exact file-list preview, and
  confirmation BEFORE any write — cancelling writes nothing.
- `--yes` flag added for non-interactive scripting; --target/--location/
  --check/--print-config unchanged.
- Fixed a real ordering bug found by tests: the first interactive draft
  executed install() before confirmation; the flow now previews via
  describe_paths() (no writes) and executes only after approval.
- Interactive flow verified end-to-end on a fake home: pick claude+codex,
  local scope, confirm, files written, codex correctly skipped as global-only.
- Full suite: 133 passed, 1 skipped.

## 2026-08-12 — installer targets: zcode/opencode/codex added

### Done
- Researched ZCode (Z.ai) config layout: MCP at ~/.zcode/cli/config.json
  (mcp.servers) or .agents/mcp.json (mcpServers — same shape as Claude),
  skills at ~/.zcode/skills/, AGENTS.md supported.
- New targets: zcode (global mcp.servers + skill + AGENTS.md; local
  .agents/mcp.json), opencode (opencode.json mcp.<name> wrapper, XDG
  honored, + AGENTS.md), codex (~/.codex/config.toml [mcp_servers.embpilot]
  TOML + AGENTS.md, global only). Registry order: claude, zcode, opencode,
  codex, pi, agents.
- Shared helpers extracted: install_skill_to/remove_skill_from,
  upsert/remove_mcp_entry_json, upsert/remove_toml_table,
  upsert/remove_zcode_mcp (nested mcp.servers shape).
- Fixed TOML section boundary semantics (table ends at the next header),
  so uninstall leaves no body residue; body lines indented.
- Full suite: 129 passed, 1 skipped.

### Current status
- Installer covers claude/zcode/opencode/codex/pi/agents.

## 2026-08-12 — installer: embpilot install/uninstall (implemented)

### Done
- Analyzed CodeGraph's installer architecture from source (cloned to
  /tmp/codegraph-src): marker-fenced instructions blocks
  (`<!-- CODEGRAPH_START/END -->`) upserted into CLAUDE.md / AGENTS.md,
  multi-target registry (detect/install/uninstall/printConfig), conditional
  wording so global installs don't mislead unindexed projects.
- `embpilot install` / `embpilot uninstall` with targets:
  - claude: MCP entry (.mcp.json local / ~/.claude.json global) + CLAUDE.md
  - pi: AGENTS.md + skill copied to ~/.pi/agent/skills/ (PI_HOME override
    for tests); pi has no MCP client, so instructions emphasize the CLI
  - agents: project AGENTS.md (Cursor/Codex/Gemini/opencode), local only
  Flags: --target auto/all/none/comma, --location global|local, --check
  (exit 0/1), --print-config <target> (writes nothing). Idempotent upserts,
  uninstall removes only EmbPilot-owned pieces.
- Skill template ships in the package (installer/skill_template/SKILL.md,
  package-data) so pi installs work from a wheel.
- End-to-end verified: real-machine --check (claude configured via existing
  .mcp.json), tmp-dir pi global install (AGENTS.md + skill) and uninstall.
- Full suite: 120 passed, 1 skipped.

### Current status
- Installer done. Optionally run `embpilot install --target pi --location
  global` on this machine to wire pi (writes ~/.pi/agent/AGENTS.md + copies
  the skill).

## 2026-08-12 — session_info resource + skill sync (implemented)

### Done
- `device://session_info` resource (session_id, ring depth, stored log rows
  as JSON); resources extracted into module-level
  `build_resources`/`render_resource` (same pattern as prompts/tools).
- `SessionDatabase.count_logs()` added.
- SKILL.md synced: new tools (read_output, batch, serve, run, flags, help),
  structured guidance, and a CLI-only agent section (pi-style callers).
- Decision recorded: monitor `--until/--timeout` (M5) skipped — `read_output`
  already provides wait-until-marker semantics at every entry point.
- Full suite: 105 passed, 1 skipped (unix-socket test, POSIX only).

### Current status
- All planned changes done. Remaining: run the full suite in WSL/Linux to
  exercise the unix-socket path (skipped on Windows). Commands:
  `python -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/python -m pytest -q`

## 2026-08-12 — M4: session_id search, schema flags, run (implemented)

### Done
- `search_history_logs` accepts optional `session_id`: closed sessions open
  read-only (`SessionDatabase.open(readonly=True)`: no schema init, no WAL
  checkpoint on close) and search without touching the active connection;
  unknown id -> INVALID_ARGUMENT, missing file -> NOT_FOUND.
- Schema-driven flags for `embpilot tool` (`cli_flags.py`): kebab-case
  flags generated from each tool's JSON Schema; enum/type/boolean handling;
  explicit flags override `--json`. Fixed an argparse collision where the
  `--command` flag dest clobbered the subcommand dest (`schema_` prefix).
- `embpilot run`: connect -> commands -> disconnect as one batch; identical
  exit-code/envelope contracts; `--interface`, `--timeout-ms`, `--line-ending`.
- Fixed a bug: flags were attached to the top-level parser instead of the
  `tool` subparser (`_tool_subparser` helper).
- Full suite: 102 passed, 1 skipped.

### Current status
- M1-M4 done. Remaining (optional): M5 monitor `--until/--timeout` and
  `device://session_info` resource.

## 2026-08-12 — M3: prompts, guidance, and CLI help (implemented)

### Done
- Prompt catalog 2 -> 7: connect_and_explore, capture_boot_log,
  diagnose_connection, design_expect, session_handoff; each renders an
  actionable tool sequence (reset_target -> read_output, etc.). Extracted
  into module-level `build_prompts()` / `render_prompt()` (mirrors
  `build_tool_definitions`) so the catalog is unit-testable.
- Tool descriptions carry structured guidance (When to use / Avoid when /
  Typical flow / Pitfalls) via one `_tool()` helper; every tool covered.
- CONNECTION_FAILED suggestions classified by failure kind (timeout / auth /
  refused); send_command timed_out without expect_regex appends a hint to
  the human-readable text (structured envelope unchanged).
- `embpilot help <tool>` (schema, defaults, enums, examples, guidance) and
  example lines in `embpilot tools` output.
- Full suite: 92 passed, 1 skipped.

### Current status
- M1-M3 done. Next: M4 (search_history_logs session_id, schema-driven
  flags, `run`), then optional M5 (monitor --until/--timeout, session_info).

## 2026-08-12 — M2: serve daemon + --socket forwarding (implemented)

### Done
- Windows feasibility check: `asyncio.start_unix_server` is unavailable on
  win32 (AttributeError) and the stdlib has no named-pipe server API; TCP
  loopback works. Decision: unix socket on POSIX, TCP 127.0.0.1 fallback on
  Windows, unified `unix:` / `tcp:` endpoint syntax.
- `rpc.py`: JSONL wire protocol (request `{id, tool, args}`; response
  `{id, ok, data|error, text}`), `RpcServer` with per-connection id spaces
  and an asyncio lock serializing dispatch across clients, `RpcClient` with
  id-correlated responses, `resolve_endpoint` accepting daemon.json paths.
- `embpilot serve`: daemon writes its real endpoint to
  `<data-dir>/daemon.json` (resolves port 0); `--socket` on
  `tool`/`tools`/`batch` forwards requests (same exit-code/output contracts).
- `batch_loop` gained a pluggable `dispatcher` so batch forwarding reuses the
  same parsing/exit-code machinery.
- End-to-end verified on Windows: daemon up, daemon.json discovery, tool
  call, batch forwarding with a failing line (exit 1, structured error).
- Full suite: 83 passed, 1 skipped (unix-socket test, POSIX only).

### Current status
- M1 + M2 done. Next: M3 (prompts expansion, description/suggestion
  hardening, CLI help), then M4 (session_id search, schema-driven flags, run).

## 2026-08-12 — M1: batch mode + read_output tool (implemented)

### Done
- Extracted the line-driven dispatch core into `cli_loop.py` (A1): shell,
  batch, and the future serve share read/parse/dispatch/render machinery.
- `embpilot batch`: JSONL in / JSONL out, one envelope per line, no banner,
  `--fail-fast`, exit codes 0/1/2, comments/blank lines/`exit` handled.
- `read_output` tool: passive observation without writing to the device;
  `expect_regex` early return, `duration_ms` window, `max_chars` truncation;
  registered in the shared contract layer (MCP + CLI identical).
- Fixed a batch args falsy-value bug (`[] or {}` silently swallowed non-object
  args).
- Full suite green (70 tests, incl. 10 new batch/read_output cases).

### Current status
- M1 done. Next: M2 (`serve` daemon + `--socket` forwarding; start with the
  Windows named-pipe feasibility check).

## 2026-08-12 — CLI agent-access design (not implemented)

### Done
- Wrote `docs/specs/2026-08-12-cli-agent-access.md`: a reviewed plan to make the
  CLI convenient for agents without MCP support (e.g. pi) and for humans.
  Gap analysis with measured evidence (FIFO keep-alive instability on Windows,
  no read-only capture, one-shot session loss, historical search bound to the
  active connection, shell quoting fragility, mixed banner output).
- Prioritized change list: P0 `batch` (JSONL in/out) + `read_output` tool;
  P1 `serve` daemon with `--socket` client forwarding; P2 `session_id` search,
  schema-driven flags, `run`; P3 monitor `--until/--timeout`.
- Follow-up review (MCP completeness + prompting): RAG engine has no tool-layer
  exposure (`RagEngine` implemented, zero tools advertise it); only 2 thin
  prompts; tool descriptions are one-liners; error suggestions are generic.
  Added section 10: RAG tools (`search_kb`/`list_kb_sources`/`ingest_doc`, P1),
  5 new prompts (P2), description/suggestion hardening (P2), CLI help (P2),
  optional `device://session_info` (P3). Explicitly out: resources/subscribe
  push and dtr/rts reset (kept honestly unadvertised).
- Explicitly out of scope: cross-process session resurrection, TCP/auth, GUI,
  changes to the existing exit-code and envelope contracts.

### Current status
- Design only; no code changes. Rollback point for future implementation: create
  a commit before starting M1 (P0 batch + read_output).

## 2026-08-11 — CLI mode

### Done
- Added a thin CLI over the existing MCP dispatch layer (`dispatch_tool` +
  `build_tool_definitions`), so all advertised tools work outside MCP.
- `embpilot tools` lists the tool catalog; `embpilot tool <name> --json '<args>'`
  runs one tool in a fresh session; `embpilot shell` keeps one `SessionManager`
  alive for a persistent connection.
- Exit codes: 0 success, 1 tool failure, 2 usage/argument error; `--json-output`
  prints the structured `ok`/`data`/`error` envelope.
- REPL reads piped stdin with explicit UTF-8 (BOM-aware) decoding so PowerShell
  pipes on Chinese Windows no longer corrupt the first line.
- `embpilot shell` gained a background `monitor` command that streams new device
  log lines (`[log]` prefix) while tool calls stay usable (`[cmd]` prefix);
  `stop` exits monitor mode.
- Tests: CLI bootstrap, tool catalog, one-shot success/failure/usage paths, and
  REPL behavior with a fake session manager.
- Hardened the shared contract/CLI layer against mcp SDK 2.x field renames
  (`input_schema`, `structured_content`) via `mcp_compat.py`, so the CLI keeps
  working even if mcp 2.x is installed.
- Pinned `mcp>=1.0.0,<2`: the MCP server runtime still uses mcp 1.x
  decorators, so the global uv tool was reinstalled on mcp 1.29.0.

### Current status
- Phase 1 (one-shot) and Phase 2 (shell) implemented; full suite green.

### Known issues / pitfalls
- Data-path flags (`--data-dir` etc.) must appear before the subcommand, e.g.
  `embpilot --data-dir X tool list_sessions`.
- One-shot mode cannot resume a connection across invocations; use `shell` for
  a persistent session.
- MCP server mode is mcp 1.x-only until it is migrated to the mcp 2.x server
  API; the CLI tolerates both 1.x and 2.x.

## 2026-08-06 — Agent-first MCP contract

### Done
- Fixed wheel packaging so both SQLite schema files ship in installed builds.
- Split RAG dependencies into the optional `embpilot[rag]` extra while keeping
  the development environment capable of running the full suite.
- Replaced ambiguous nested `connect_device` arguments with strict, documented
  `connect_serial`, `connect_ssh`, and `connect_telnet` JSON contracts.
- Added structured MCP success/error results with stable recovery fields.
- Added command line-ending policy, cursor-based output capture, early expect
  matching, output limits, and command audit redaction.
- Made SSH host-key verification secure by default and lightweight CLI help
  independent of server runtime imports.
- Standardized Telnet/SSH transports on byte streams and made partial prompts
  observable after the framing timeout even when no newline arrives.
- Added the repository-level `embpilot-device-debugging` agent skill, MCP client
  setup documentation, and wheel/CLI/session/stdio integration tests.

### Current status
- Agent routing, strict tool discovery, structured results, and stdio MCP smoke
  tests are implemented. Live logs are currently read by polling the MCP
  resource; push subscriptions are not advertised.

### Known issues / pitfalls
- DTR/RTS reset remains unimplemented and is intentionally absent from the
  advertised reset schema.
- A clean `pip install embpilot` installs the lightweight core; RAG users and
  contributors need `.[rag]` and `.[dev]`, respectively.

## 2026-08-06

### Done
- Added `AGENTS.md` with repository-specific structure, setup, testing, style,
  commit, pull request, and documentation guidance for contributors and agents.

### Current status
- Contributor guidance now documents the editable-install requirement for the
  `src/` layout and the existing progress/change-log maintenance workflow.

## 2026-06-27

### Done
- Reviewed the project structure, README, design spec, tests, and core code paths.
- Confirmed the current architecture is a Python 3.11+ MCP server for embedded debugging with:
  - CLI entry point in `src/embpilot/__main__.py`
  - configuration in `src/embpilot/config.py`
  - MCP tool/resource/prompt registration in `src/embpilot/server.py`
  - dual-track SQLite storage in `src/embpilot/core/database.py`
  - log framing and batching pipeline in `src/embpilot/core/engine.py`
  - local RAG support in `src/embpilot/core/rag.py`
  - transport drivers under `src/embpilot/drivers/`
- Added repository hygiene files:
  - `.gitignore`
  - `.editorconfig`
- Prepared the project for Git initialization.
- Wrote the approved runtime rearchitecture design spec for long-term
  maintainability and standard PyPI packaging.
- Wrote the implementation plan that decomposes the rearchitecture into
  staged, test-first tasks under `docs/superpowers/plans/`.

### Current status
- The repository had no `.git` directory before initialization.
- The workspace already contained generated artifacts such as `.venv`, `.pytest_cache`, `.codegraph`, `__pycache__`, and `src/embpilot.egg-info`.

### Known issues / pitfalls
- Running `pytest -q` directly in the repository currently fails during test collection with `ModuleNotFoundError: No module named 'embpilot'`.
- The immediate cause is that the `src/` layout is not automatically on Python's import path in the current shell session.
- Likely follow-up options:
  - run tests with an editable install such as `pip install -e .[dev]`
  - or add explicit pytest path configuration if the project wants zero-setup local test runs

### Next good step
- Choose an execution mode for the runtime rearchitecture plan and start Task 1.

## 2026-08-12 - full review round

### Done
- Full code review across all modules (coverage 70% overall; core logic
  85-93%; cli.py/__main__.py 0% is a subprocess-measurement artifact).
- Fixed (commit ff80f34): dead-daemon --socket calls traced back (now exit 1
  with a clear message); reset_target hardcoded \n (now uses session line
  ending); DbConsumer flush tasks had no strong reference (asyncio GC
  pitfall); disconnect_device could hang forever on the Windows proactor
  (wait_for/cancel/feed_data race) - now bounded to 2s with a warning;
  removed unused config fields.
- Docs synced: acceptance checklist in the design spec fully checked, status
  marked implemented; change.log updated.
- Full suite: 141 passed, 1 skipped.
- Remaining: WSL/Linux full run to exercise the unix-socket path; pytest-cov
  installed locally for coverage reporting.

## 2026-08-12 - codex review round (implemented)

### Done
- All codex review findings addressed (except installer keep/remove, which
  was kept and the spec updated to scope it in):
  - behavior bugs: --socket flag merging, NOT_FOUND error codes, batch
    --json-output accepted
  - robustness: UTF-8 stdout on Windows (Chinese system messages), serve
    refuses non-loopback TCP, run fail-fast
  - spec gaps: shell `help <tool>`, `tool <name> --help`, optional
    `run --connect`, Pitfalls on all 11 tools, session_info metadata fields
  - consistency: run_uninstall respects supports_location
  - dedup: interactive flow, one-shot parse/exit-code mapping, JSONL request
    parser (batch + rpc), target instructions helpers
- Full suite: 143 passed, 1 skipped.
