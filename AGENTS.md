# Repository Guidelines

## Project Structure & Module Organization

EmbPilot is a Python 3.11+ MCP server using a `src/` layout. Code lives in `src/embpilot/`: `server.py` registers MCP interfaces, `config.py` owns configuration, `core/` contains log processing, SQLite, and RAG search, and `drivers/` implements Serial, Telnet, and SSH transports. Tests mirror these areas under `tests/`; design notes and plans belong in `docs/`. Do not commit generated databases, caches, environments, or build output.

## Build, Test, and Development Commands

- `python -m venv .venv` creates an isolated environment.
- `python -m pip install -e ".[dev]"` installs EmbPilot and test dependencies. Run this before tests because the package uses a `src/` layout.
- `python -m pytest -q` runs the complete test suite.
- `python -m pytest tests/test_engine.py -q` runs a focused test module.
- `embpilot --help` verifies the installed CLI entry point.

## Coding Style & Naming Conventions

Follow `.editorconfig`: UTF-8 without BOM, LF endings, four-space indentation, a final newline, and no trailing whitespace. Use type hints for public interfaces and async I/O for device and database operations. Use `snake_case` for modules/functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Keep transport behavior in `drivers/` and shared orchestration in `core/`. No formatter or linter is configured; match nearby code.

## Testing Guidelines

Tests use `pytest` and `pytest-asyncio`; async tests require `@pytest.mark.asyncio`. Name files `test_<area>.py` and functions `test_<behavior>`. Mock devices and network connections; tests must not require live targets. Add regression tests for fixes and run the full suite. No coverage threshold is enforced, but cover success, failure, and lifecycle paths.

## Commit & Pull Request Guidelines

History uses Conventional Commit prefixes such as `chore:` and `docs:`; write concise imperative subjects (`fix: flush pending log batch`). Before substantial changes, create a commit-based rollback point. Pull requests should explain scope, link issues or designs, list verification commands, and call out configuration or schema changes. Include logs or screenshots only when useful.

## Documentation & Agent Workflow

Keep `README.md` and relevant files in `docs/` synchronized with behavior. Record progress and pitfalls in `PROGRESS.md`, and summarize user-visible or structural changes in `change.log`. If blocked, review those files and existing design plans before changing direction.
