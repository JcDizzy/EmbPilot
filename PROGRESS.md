# Progress

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
