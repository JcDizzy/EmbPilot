# Progress

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
