# Progress

## 2026-07-02 — DbSink flush latency fix

### Done
- Fixed active-session search/export staleness found during real-device (COM32) testing:
  - `DbSink` now flushes on a configurable interval (default 1.0s), so low-rate
    sessions persist without waiting for `batch_size=200` (previously a ~2 line/sec
    device didn't write to SQLite for ~100s).
  - `SessionManager` holds the `DbSink` reference and flushes it before
    `search_session_logs`/`export_session` on the active session, so queries return
    the latest device output immediately.
  - Verified with `tests/runtime/test_pipeline.py` (periodic flush + close-cancels)
    and `tests/runtime/test_session.py` (active-session flush before search).

## 2026-07-02 — device://analytics resource

### Done
- Exposed `device://analytics` to complete the MCP surface (now 8 tools / 3 resources / 2 prompts): added `SessionManager.get_analytics()` (active session only, empty otherwise) and wired it through `build_resource_catalog()` + `read_resource` in `mcp_app.py`.
- Verified with the project's pytest command (72 passed, excluding `tests/test_rag.py`).

## 2026-07-02 — session-query tools

### Done
- Completed the session-query tools stage in the `feat/runtime-rearchitecture`
  worktree — exposes the four tools previously listed as "planned":
  - core primitives: added `MainDatabase.get_session_db_path(session_id)` and
    `SessionDatabase.fetch_logs(limit, offset)` to `src/embpilot/core/database.py`
  - runtime layer: added a `_open_session_db(session_id)` asynccontextmanager on
    `SessionManager` (reuses the active connection when the id matches, otherwise
    opens the historical db transiently) plus `list_sessions`, `delete_session`
    (refuses the active session), `search_session_logs`, and `export_session`
  - mcp layer: extended `build_tool_catalog()` and `dispatch_tool()` in
    `mcp_app.py` with `list_sessions`, `delete_session`, `search_history_logs`,
    and `export_session`; JSON-shaped results are pretty-printed
  - tests: added coverage in `tests/test_database.py`,
    `tests/runtime/test_session.py` (including the active-session shortcut
    path), and `tests/integration/test_mcp_app.py`
  - ran independent spec-compliance and code-quality reviews; addressed the one
    medium finding by adding the active-session-shortcut test
- Verified with the project's pytest command; all tests pass except
  `tests/test_rag.py` (excluded in the slim venv — needs lancedb/fastembed).

### Known issues / pitfalls
- `search_history_logs` accepts a `time_window_seconds` filter, but it is only
  meaningful for the active session; historical sessions will usually return
  empty for a "last N seconds" window. Documented in the spec.

### Next good step
- The MCP surface is now complete (8 tools / 3 resources / 2 prompts). Next:
  pursue packaging/release (0.1.0), or push the `feat/runtime-rearchitecture`
  branch and open a PR.

## 2026-07-02

### Done
- Completed the MCP tools & prompts migration stage (spec §7, Stage 3) in the
  `feat/runtime-rearchitecture` worktree:
  - added `SessionManager.reset_target(method="reboot")` to
    `src/embpilot/runtime/session.py` — reboot only; non-reboot methods raise
    ValueError, no active connection raises RuntimeError, and the write +
    `insert_operation` path mirrors `send_command` (spec §7.7)
  - wired the four core MCP tools in `src/embpilot/mcp_app.py` via
    `build_tool_catalog()` + module-level `dispatch_tool()` +
    `@app.list_tools`/`@app.call_tool`; tool failures surface as TextContent
    error messages instead of crashing the transport
  - migrated the two prompts via `build_prompt_catalog()` + module-level
    `render_prompt()` + `_prompt_result()` + `@app.list_prompts`/`@app.get_prompt`;
    `analyze_crash_log` still routes to `device://live_log`, and
    `hardware_sanity_check` no longer hardcodes a generic command bundle (spec §7.6)
  - extended `tests/runtime/test_session.py` and `tests/integration/test_mcp_app.py`
    with reset_target, tool catalog/schema, dispatch_tool error paths, and prompt
    catalog/render coverage; evolved the handler-registration test to assert all
    resource/tool/prompt handlers
  - ran independent spec-compliance and code-quality reviews; addressed review
    feedback by tightening `arguments` type hints to `dict[str, Any]` and
    documenting the reset_target line-terminator design choice
  - aligned `docs/mcp_embedded_debug_spec.md` (reset_target reboot-only,
    hardware_sanity without hardcoded commands), corrected the `change.log`
    tool/resource inventory to the implemented 4 tools / 2 resources / 2 prompts,
    and added a reset feature line to `README.md`
- Verified the stage with the project's pytest command; all tests pass except
  `tests/test_rag.py`, which is excluded in the slim verification venv because it
  requires `lancedb`/`fastembed`.

### Known issues / pitfalls
- `tests/test_rag.py` cannot run in the slim verification venv (no
  `lancedb`/`fastembed`); it is unrelated to this stage and was excluded from
  local verification. A full venv with the heavy deps is needed to exercise it.
- This machine has no Python 3.11; verification used the isolated `.venv`
  (Python 3.12). Commands in the plan/spec text still prescribe `py -3.11` to
  match `requires-python = ">=3.11"`.

### Next good step
- Expose the remaining session-query tools (`list_sessions`, `delete_session`,
  `export_session`, `search_history_logs`) — the database primitives in
  `core/database.py` already back them, so this is pure MCP-layer wiring.

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
- Completed Task 5 in the runtime rearchitecture worktree:
  - added `src/embpilot/runtime/session.py` as the foundational session layer
  - added `tests/runtime/test_session.py` covering explicit `device_name`
    precedence and expect-window command output capture
  - verified the new tests with Python 3.11 using
    `py -3.11 -m pytest tests/runtime/test_session.py -v`
  - fixed two follow-up quality issues in Task 5:
    - `disconnect_device()` / `shutdown()` now cancel the producer task so
      disconnect does not hang if the reader never emits EOF
    - `send_command()` now cleans up the opened expect window if
      `device.write()` fails
    - `disconnect_device()` / `shutdown()` now also swallow a stale producer
      failure that already happened in the background, so explicit cleanup
      still completes instead of re-raising an old reader exception
    - `send_command()` now runs single-flight under a session lock so
      overlapping command windows cannot consume each other's output
  - added focused regressions for the non-EOF disconnect path and
    write-failure window cleanup
  - added a focused regression for a background producer crash before
    explicit disconnect
  - added a focused regression for overlapping `send_command()` calls to
    prove outputs stay isolated per command
- Completed Task 6 in the runtime rearchitecture worktree:
  - added `src/embpilot/mcp_app.py` to own MCP app assembly and stdio startup
  - added `tests/integration/test_mcp_app.py` covering resource catalog
    exposure for `device://session_info`, non-null app/manager creation, and
    the narrowed resource-only handler registration
  - tightened the resource-catalog expectation to the exact Task 6 URI set:
    `device://live_log` and `device://session_info`, explicitly excluding
    `device://sysinfo`
  - added compatibility-entrypoint tests proving `cli.main(...)` calls
    `run_stdio_mcp_server(...)` and `server.serve(...)` forwards to the new
    runner
  - replaced `device://sysinfo` with `device://session_info` in the new MCP
    resource catalog while keeping `device://live_log`
  - turned `src/embpilot/server.py` into a thin compatibility wrapper over the
    new MCP app runner
  - updated `src/embpilot/cli.py` to launch through `run_stdio_mcp_server()`
  - kept Task 6 scoped to the MCP app split and basic resource registration,
    without migrating the full tool or prompt catalogs
  - verified the focused Task 6 test first fails and then passes with
    `py -3.11 -m pytest tests/integration/test_mcp_app.py -v`
- Completed Task 7 in the runtime rearchitecture worktree:
  - added packaging verification in `tests/integration/test_cli.py` that
    installs the project into a clean Python 3.11 virtual environment and
    checks the installed `embpilot/core/` package data for
    `schema_main.sql` and `schema_session.sql`
  - updated `pyproject.toml` to explicitly package `core/*.sql`
  - updated `README.md` to reflect the new `cli.py` / `mcp_app.py` /
    `runtime/` architecture and the `device://live_log` +
    `device://session_info` resource direction
  - updated `docs/mcp_embedded_debug_spec.md` to replace the fake generic
    `device://sysinfo` concept with honest session metadata and to describe
    the dispatcher-based runtime split
  - queued broader verification for the Task 6/7 boundary after the focused
    packaging regression flips green

### Current status
- The repository had no `.git` directory before initialization.
- The workspace already contained generated artifacts such as `.venv`, `.pytest_cache`, `.codegraph`, `__pycache__`, and `src/embpilot.egg-info`.
- Task 5 now lives in the isolated worktree
  `E:\jc\杂项\EmbPilot\.worktrees\feat-runtime-rearchitecture`.

### Known issues / pitfalls
- Running `pytest -q` directly in the repository currently fails during test collection with `ModuleNotFoundError: No module named 'embpilot'`.
- The immediate cause is that the `src/` layout is not automatically on Python's import path in the current shell session.
- Likely follow-up options:
  - run tests with an editable install such as `pip install -e .[dev]`
  - or add explicit pytest path configuration if the project wants zero-setup local test runs
- On this machine, plain `pytest` resolves to Python 3.9 and fails against
  current runtime code which relies on Python 3.11 features such as
  `dataclass(slots=True)`. Use `py -3.11 -m pytest ...` for runtime tasks.

### Next good step
- Continue broader runtime verification from this worktree baseline, especially
  the Task 6/7 integration path covering installed packaging and updated
  runtime-facing documentation.
