# Repository Guidelines

## Project Structure & Module Organization

EmbPilot is a Python 3.11+ MCP server using a `src/` layout. Application code is
under `src/embpilot/`: `mcp_app.py` defines the MCP contract, `cli.py` owns the
command line, `runtime/` manages sessions and log flow, `core/` contains
storage, search, and safety logic, and `drivers/` implements Serial, Telnet, and
SSH transports. Tests mirror these areas under `tests/`; plans and design notes
belong in `docs/`. Keep generated databases, environments, caches, and build
artifacts out of Git.

## Build, Test, and Development Commands

- `python -m venv .venv` creates a local environment.
- `python -m pip install -e ".[dev]"` installs the package and test tools.
- `python -m pytest -q` runs the full test suite.
- `python -m pytest tests/integration/test_mcp_app.py -q` checks the MCP surface.
- `embpilot doctor` validates dependencies, storage, drivers, and serial ports.
- `embpilot --help` verifies the installed CLI entry point.

## Coding Style & Naming Conventions

Follow `.editorconfig`: UTF-8 without BOM, LF endings, four-space indentation,
a final newline, and no trailing whitespace. Use type hints on public APIs and
async I/O for device and database work. Name modules and functions in
`snake_case`, classes in `PascalCase`, and constants in `UPPER_SNAKE_CASE`.
Keep transport details in `drivers/` and shared orchestration in `runtime/` or
`core/`. No formatter is configured, so match nearby code.

## Testing Guidelines

Tests use `pytest` and `pytest-asyncio`; mark async tests with
`@pytest.mark.asyncio`. Name files `test_<area>.py` and tests
`test_<behavior>`. Mock device and network boundaries so CI never needs real
hardware. Add regression coverage for fixes and exercise success, failure, and
lifecycle paths. Run the full suite before publishing.

## Commit & Pull Request Guidelines

Use concise Conventional Commit subjects, such as
`fix: return structured connection errors`. Create a commit rollback point
before substantial feature or tuning work. Pull requests should explain scope,
link relevant issues or specs, list verification commands, and call out schema,
configuration, or security changes. Add logs or screenshots only when useful.

## Documentation & Agent Workflow

Keep `README.md`, relevant `docs/`, `PROGRESS.md`, and `change.log` synchronized
with behavior. Review those files when blocked to avoid repeating known
pitfalls. For Serial, SSH, or Telnet work, use EmbPilot MCP first. Pass flat JSON
objects to `connect_serial`, `connect_ssh`, or `connect_telnet`; do not invoke
raw clients unless EmbPilot is unavailable and the fallback is explained.
