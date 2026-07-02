# Session-Query Tools Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the remaining four MCP tools — `list_sessions`, `delete_session`, `search_history_logs`, `export_session` — so that an LLM agent can browse, search, export, and clean up recorded sessions, not just drive the active connection. These were explicitly deferred as "future work" by the prior stage (`2026-07-02-mcp-tools-prompts-migration-implementation.md`). The database layer already owns most of the behavior; this stage is primarily correct wiring plus two small, honest primitives the layer is missing.

**Architecture:** This stage touches three layers, each as a single task:

1. **core** (`core/database.py`) — add the two missing primitives: `MainDatabase.get_session_db_path(session_id)` (single-row lookup so a historical session's db file can be opened) and `SessionDatabase.fetch_logs(limit, offset)` (ordered bulk read for export; `search_logs` cannot express "all rows").
2. **runtime** (`runtime/session.py`) — add a single `_open_session_db(session_id)` async context manager on `SessionManager` that reuses the active connection when the id matches and otherwise opens the historical db file transiently, then four thin public methods (`list_sessions`, `delete_session`, `search_session_logs`, `export_session`) built on it. `delete_session` refuses to delete the active session (callers must `disconnect_device` first) so runtime state never references a deleted file.
3. **mcp** (`mcp_app.py`) — extend `build_tool_catalog()` and `dispatch_tool()` with the four tools. JSON-shaped results (`list_sessions`, `search_history_logs`) are returned as pretty-printed JSON `TextContent`; `export_session` returns the serialized text or JSON.

The `_open_session_db` helper is the one piece of non-trivial logic; it is shared by both `search_session_logs` and `export_session`, which is why this stage does all four tools together rather than shipping `list_sessions`/`delete_session` alone — a search/export that only works on the active session would be a confusing half-feature.

**Tech Stack:** Python 3.11+, `asyncio`, `aiosqlite`, `contextlib.asynccontextmanager`, `mcp`, `pytest`, `pytest-asyncio`

---

## Scope and explicit non-goals

In scope:

- `MainDatabase.get_session_db_path` and `SessionDatabase.fetch_logs` core primitives.
- `SessionManager.list_sessions` / `delete_session` / `search_session_logs` / `export_session`.
- MCP tools `list_sessions`, `delete_session`, `search_history_logs`, `export_session`.

Behavioral decisions (recorded so reviewers can challenge them):

- **`delete_session` guards the active session.** If `session_id` matches the active connection, raise `RuntimeError("Cannot delete the active session; disconnect_device first")` rather than silently unlinking the file the runtime is still writing to.
- **`search_history_logs` and `export_session` operate on any session** (active or historical) via `_open_session_db`. A missing session id raises `KeyError` from the helper, which `dispatch_tool` converts to an error `TextContent`.
- **`export_session` supports `format` = `"text"` (default) or `"json"`** and caps output with `limit` (default 2000) to keep MCP responses bounded. `text` renders `[timestamp] source> text` lines; `json` returns the row list.

Out of scope (still future work):

- `device://analytics` resource (separate resource stage).
- Streaming/paginated export beyond the simple `limit`/`offset` knobs.
- Restoring or replaying an exported session.

**Do not** in this stage:

- Change the active-session lifecycle (`connect_device` / `send_command` / `reset_target` / `disconnect_device`).
- Reintroduce `device://sysinfo`.
- Couple the new core primitives to MCP — they stay plain database methods.

---

## File map

- Modify: `src/embpilot/core/database.py`
- Modify: `src/embpilot/runtime/session.py`
- Modify: `src/embpilot/mcp_app.py`
- Modify: `tests/test_database.py`
- Modify: `tests/runtime/test_session.py`
- Modify: `tests/integration/test_mcp_app.py`
- Modify: `docs/mcp_embedded_debug_spec.md`
- Modify: `change.log`
- Modify: `PROGRESS.md`

---

### Task 1: Add the two missing core primitives

`SessionDatabase` can already `search_logs`, but `export_session` needs an ordered full read; `search_logs(keyword="")` is a misuse (its `LIKE` clause and defaults are tuned for keyword search). And nothing in `MainDatabase` returns a single session's `db_path` without fetching the whole list. Add both.

**Files:**
- Modify: `src/embpilot/core/database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_database.py`, matching that file's `@pytest.mark.asyncio` + `tempfile.TemporaryDirectory` style:

```python
@pytest.mark.asyncio
async def test_main_database_get_session_db_path():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        main = MainDatabase(d / "embpilot_main.db")
        await main.open()

        await main.register_session(
            "sess-a", "COM3", "serial", str(d / "sessions" / "a.db")
        )

        assert await main.get_session_db_path("sess-a") == str(d / "sessions" / "a.db")
        assert await main.get_session_db_path("does-not-exist") is None

        await main.close()


@pytest.mark.asyncio
async def test_session_database_fetch_logs_is_ordered():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        session = SessionDatabase(d / "session_test.db")
        await session.open()

        lines = [
            LogLine(datetime.now(timezone.utc), f"line-{i}")
            for i in range(5)
        ]
        await session.bulk_insert_logs(lines, source="serial")

        fetched = await session.fetch_logs()
        assert [r["text"] for r in fetched] == [f"line-{i}" for i in range(5)]
        assert all(set(r) == {"timestamp", "source", "text"} for r in fetched)

        paged = await session.fetch_logs(limit=2, offset=1)
        assert [r["text"] for r in paged] == ["line-1", "line-2"]

        await session.close()
```

- [ ] **Step 2: Verify the tests fail**

```
py -3.11 -m pytest tests/test_database.py -q
```

Expect `AttributeError`/`ImportError` for `get_session_db_path` and `fetch_logs`.

- [ ] **Step 3: Implement the primitives**

In `src/embpilot/core/database.py`, add `get_session_db_path` to `MainDatabase` (alongside `list_sessions`/`delete_session`):

```python
    async def get_session_db_path(self, session_id: str) -> Optional[str]:
        """Return the db_path for a session, or None if not found."""
        if self._conn is None:
            return None
        cursor = await self._conn.execute(
            "SELECT db_path FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row["db_path"] if row else None
```

Add `fetch_logs` to `SessionDatabase` (alongside `search_logs`):

```python
    async def fetch_logs(
        self, limit: int = 5000, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return device logs in insertion order (for export)."""
        if self._conn is None:
            return []
        cursor = await self._conn.execute(
            "SELECT timestamp, source, text FROM device_logs "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Verify the tests pass**

```
py -3.11 -m pytest tests/test_database.py -q
```

All cases, including the two new ones, must pass.

- [ ] **Step 5: Commit**

```
git add src/embpilot/core/database.py tests/test_database.py
git commit -m "feat: add session db path lookup and log fetch primitives"
```

---

### Task 2: Add session-query methods to `SessionManager`

Add the `_open_session_db` helper and the four public methods. The helper reuses the active connection when possible (zero extra IO) and otherwise opens the historical db file transiently and closes it in a `finally`.

**Files:**
- Modify: `src/embpilot/runtime/session.py`
- Modify: `tests/runtime/test_session.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/runtime/test_session.py`, matching that file's `asyncio.run(scenario())` + `_FakeDevice`/`monkeypatch` style. These tests need real `SessionDatabase` files on disk so the helper's historical-open path is exercised; register a second session through the main db directly to simulate a historical one:

```python
def test_list_sessions_returns_recorded_sessions(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _FakeDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            await manager.connect_device("serial", {"port": "COM9"})

            sessions = await manager.list_sessions()

            assert len(sessions) == 1
            assert sessions[0]["interface"] == "serial"
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_delete_session_refuses_active_session(tmp_path, monkeypatch):
    async def scenario() -> None:
        fake = _FakeDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            session_id = await manager.connect_device("serial", {"port": "COM9"})

            with pytest.raises(RuntimeError, match="active session"):
                await manager.delete_session(session_id)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_search_and_export_work_on_historical_session(tmp_path, monkeypatch):
    from embpilot.core.database import SessionDatabase
    from embpilot.core.engine import LogLine

    async def scenario() -> None:
        fake = _FakeDevice()
        monkeypatch.setattr(
            "embpilot.runtime.session.build_device",
            lambda interface_type, config: fake,
        )
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            # record a historical session directly, then disconnect so the
            # active-session shortcut in _open_session_db does NOT fire
            hist_path = tmp_path / "sessions" / "hist.db"
            hist_db = SessionDatabase(hist_path)
            await hist_db.open()
            await hist_db.bulk_insert_logs(
                [
                    LogLine(datetime.now(timezone.utc), "boot ok"),
                    LogLine(datetime.now(timezone.utc), "ERROR: boom"),
                ],
                source="serial",
            )
            await hist_db.close()
            await manager._main_db.register_session(
                "hist-1", "board-x", "serial", str(hist_path)
            )

            results = await manager.search_session_logs("hist-1", "boom")
            assert len(results) == 1
            assert "boom" in results[0]["text"]

            exported = await manager.export_session("hist-1", fmt="text")
            assert "boot ok" in exported
            assert "boom" in exported

            as_json = await manager.export_session("hist-1", fmt="json")
            assert '"boom"' in as_json
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_search_session_logs_raises_for_unknown_session(tmp_path):
    async def scenario() -> None:
        config = EmbPilotConfig(
            data_dir=tmp_path,
            main_db_path=tmp_path / "embpilot_main.db",
            session_data_dir=tmp_path / "sessions",
            lancedb_path=tmp_path / "lancedb",
        )
        manager = SessionManager(config)
        await manager.start()
        try:
            with pytest.raises(KeyError):
                await manager.search_session_logs("nope", "anything")
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
```

(`datetime` and `timezone` are already imported at the top of `test_session.py` via the runtime? — check; if not, add `from datetime import datetime, timezone`.)

- [ ] **Step 2: Verify the tests fail**

```
py -3.11 -m pytest tests/runtime/test_session.py -q
```

Expect `AttributeError` for the four missing methods and for `_open_session_db`.

- [ ] **Step 3: Implement the manager methods**

In `src/embpilot/runtime/session.py`:

1. Extend the imports:

```python
import json
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Callable
```

2. Add the helper and four methods to `SessionManager`, placed after `active_ring` (or after `get_session_info`/`active_ring`, keeping the read-side methods together). The helper yields the active db when the id matches, otherwise opens the historical file:

```python
    @asynccontextmanager
    async def _open_session_db(self, session_id: str) -> AsyncIterator[SessionDatabase]:
        if (
            self._session_info is not None
            and self._session_info.session_id == session_id
            and self._session_db is not None
        ):
            yield self._session_db
            return
        db_path = await self._main_db.get_session_db_path(session_id)
        if db_path is None:
            raise KeyError(f"Session not found: {session_id}")
        db = SessionDatabase(Path(db_path))
        await db.open()
        try:
            yield db
        finally:
            await db.close()

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._main_db.list_sessions()

    async def delete_session(self, session_id: str) -> None:
        if self._session_info is not None and self._session_info.session_id == session_id:
            raise RuntimeError(
                "Cannot delete the active session; disconnect_device first"
            )
        await self._main_db.delete_session(session_id)

    async def search_session_logs(
        self,
        session_id: str,
        keyword: str,
        time_window_seconds: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._open_session_db(session_id) as db:
            return await db.search_logs(
                keyword, time_window_seconds, limit, offset
            )

    async def export_session(
        self,
        session_id: str,
        fmt: str = "text",
        limit: int = 2000,
        offset: int = 0,
    ) -> str:
        async with self._open_session_db(session_id) as db:
            rows = await db.fetch_logs(limit=limit, offset=offset)
        if fmt == "json":
            return json.dumps(rows, ensure_ascii=False, indent=2)
        if fmt == "text":
            return "\n".join(
                f"[{r['timestamp']}] {r['source']}> {r['text']}" for r in rows
            )
        raise ValueError(
            f"Unsupported export format: {fmt!r} (use 'text' or 'json')"
        )
```

- [ ] **Step 4: Verify the tests pass**

```
py -3.11 -m pytest tests/runtime/test_session.py -q
```

All cases, including the four new ones, must pass.

- [ ] **Step 5: Commit**

```
git add src/embpilot/runtime/session.py tests/runtime/test_session.py
git commit -m "feat: add session query and export methods to SessionManager"
```

---

### Task 3: Expose the four tools in `mcp_app.py`

Extend the catalog and dispatcher. JSON results are pretty-printed so an agent can read them; `export_session` returns the serialized payload directly.

**Files:**
- Modify: `src/embpilot/mcp_app.py`
- Modify: `tests/integration/test_mcp_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_mcp_app.py`, matching that file's style (sync for catalog, `asyncio.run(scenario())` for `dispatch_tool`):

```python
def test_build_tool_catalog_lists_session_query_tools() -> None:
    from embpilot.mcp_app import build_tool_catalog

    names = {tool.name for tool in build_tool_catalog()}

    assert {
        "list_sessions",
        "delete_session",
        "search_history_logs",
        "export_session",
    } <= names


def test_dispatch_tool_list_sessions_returns_json(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(manager, "list_sessions", {})
        finally:
            await manager.shutdown()

        assert len(result) == 1
        assert isinstance(result[0], types.TextContent)
        assert result[0].text.strip().startswith("[")

    asyncio.run(scenario())


def test_dispatch_tool_delete_unknown_session_returns_error(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(
                manager, "delete_session", {"session_id": "nope"}
            )
        finally:
            await manager.shutdown()

        assert len(result) == 1
        assert "error" in result[0].text.lower()

    asyncio.run(scenario())


def test_dispatch_tool_export_session_rejects_bad_format(tmp_path: Path) -> None:
    from embpilot.mcp_app import dispatch_tool

    async def scenario() -> None:
        manager = SessionManager(build_config(tmp_path))
        await manager.start()
        try:
            result = await dispatch_tool(
                manager,
                "export_session",
                {"session_id": "hist-1", "format": "xml"},
            )
        finally:
            await manager.shutdown()

        assert len(result) == 1
        assert "error" in result[0].text.lower()

    asyncio.run(scenario())
```

- [ ] **Step 2: Verify the tests fail**

```
py -3.11 -m pytest tests/integration/test_mcp_app.py -q
```

Expect failures: the catalog does not list the four tools; `dispatch_tool` returns "unknown tool" for them.

- [ ] **Step 3: Implement the MCP layer**

In `src/embpilot/mcp_app.py`:

1. Append the four tools to the list returned by `build_tool_catalog()`:

```python
        Tool(
            name="list_sessions",
            description="List all recorded device sessions, newest first.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="delete_session",
            description=(
                "Delete a recorded session's database file and remove its index "
                "entry. The active session cannot be deleted — disconnect first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="search_history_logs",
            description=(
                "Search a session's device logs by keyword, optionally within a "
                "recent time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "keyword": {"type": "string"},
                    "time_window_seconds": {
                        "type": "integer",
                        "description": "Optional: restrict to the last N seconds.",
                    },
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["session_id", "keyword"],
            },
        ),
        Tool(
            name="export_session",
            description=(
                "Export a session's device logs as text or JSON. Output is capped "
                "by limit (default 2000)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["text", "json"], "default": "text"},
                    "limit": {"type": "integer", "default": 2000},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["session_id"],
            },
        ),
```

2. Extend `dispatch_tool()` with four new branches before the `return [... unknown tool ...]` fallback. JSON-shaped results are dumped with `ensure_ascii=False, indent=2`:

```python
        if name == "list_sessions":
            sessions = await manager.list_sessions()
            payload = json.dumps(sessions, ensure_ascii=False, indent=2)
            return [TextContent(type="text", text=payload)]
        if name == "delete_session":
            await manager.delete_session(session_id=arguments["session_id"])
            return [
                TextContent(
                    type="text", text=f"Deleted session {arguments['session_id']!r}."
                )
            ]
        if name == "search_history_logs":
            logs = await manager.search_session_logs(
                session_id=arguments["session_id"],
                keyword=arguments["keyword"],
                time_window_seconds=arguments.get("time_window_seconds"),
                limit=arguments.get("limit", 50),
                offset=arguments.get("offset", 0),
            )
            payload = json.dumps(logs, ensure_ascii=False, indent=2)
            return [TextContent(type="text", text=payload)]
        if name == "export_session":
            text = await manager.export_session(
                session_id=arguments["session_id"],
                fmt=arguments.get("format", "text"),
                limit=arguments.get("limit", 2000),
                offset=arguments.get("offset", 0),
            )
            return [TextContent(type="text", text=text)]
```

(`json` is already imported at the top of `mcp_app.py` — confirm and add `import json` if missing.)

- [ ] **Step 4: Verify the tests pass**

```
py -3.11 -m pytest tests/integration/test_mcp_app.py -q
```

All tool/prompt/resource tests must pass.

- [ ] **Step 5: Commit**

```
git add src/embpilot/mcp_app.py tests/integration/test_mcp_app.py
git commit -m "feat: expose session query tools in mcp_app"
```

---

### Task 4: Align documentation and run full verification

Update the inventory that previously listed these four tools as "planned", then re-run the whole suite plus the packaging regression.

**Files:**
- Modify: `docs/mcp_embedded_debug_spec.md`
- Modify: `change.log`
- Modify: `PROGRESS.md`

- [ ] **Step 1: Update `docs/mcp_embedded_debug_spec.md`**

Add the four tools to the §1 Tools list (after `reset_target`), with honest one-liners: `list_sessions()`, `delete_session(session_id)` (note: active session protected), `search_history_logs(session_id, keyword, ...)`, `export_session(session_id, format, limit, ...)`. Keep `device://analytics` noted as still unimplemented.

- [ ] **Step 2: Update `change.log`**

- In the `0.1.0 (unreleased)` → Added section, change the MCP inventory from "4 tools" to "8 tools" and add the four new tool names to the Tools line; remove (or move) the "Planned MCP surface not yet exposed" sentence for `list_sessions`/`delete_session`/`export_session`/`search_history_logs` since they are now exposed (keep `device://analytics` as still-planned).
- Append a new `[2026-07-02]` block describing: the two core primitives, the four `SessionManager` methods + `_open_session_db`, the four MCP tools, and the active-session delete guard.

- [ ] **Step 3: Update `PROGRESS.md`**

Add a `## 2026-07-02` entry (above the existing one) for "Session-query tools stage": the core primitives, manager methods/helper, four MCP tools, and that `device://analytics` remains the only still-deferred MCP surface item.

- [ ] **Step 4: Run the full suite**

```
py -3.11 -m pytest tests -q --ignore=tests/test_rag.py
```

Every test must pass. (`tests/test_rag.py` is excluded only because it needs `lancedb`/`fastembed`, which are absent from the slim verification venv — see PROGRESS.md known issues.)

- [ ] **Step 5: Packaging regression**

```
py -3.11 -m pytest tests/integration/test_cli.py -q
```

Confirm `test_installed_package_includes_sql_schema_files` and `test_installed_console_script_prints_version` still pass (the package must still install cleanly after the core/runtime/mcp changes).

- [ ] **Step 6: Commit**

```
git add docs/mcp_embedded_debug_spec.md change.log PROGRESS.md
git commit -m "docs: align session query tools documentation"
```

---

## Spec coverage self-check

- `list_sessions` — `test_list_sessions_returns_recorded_sessions` (manager) + `test_dispatch_tool_list_sessions_returns_json` (mcp) + `test_build_tool_catalog_lists_session_query_tools`.
- `delete_session` — `test_delete_session_refuses_active_session` (active guard) + `test_dispatch_tool_delete_unknown_session_returns_error` (mcp error path).
- `search_history_logs` — `test_search_and_export_work_on_historical_session` (historical open + search) + `test_search_session_logs_raises_for_unknown_session` (KeyError) + catalog test.
- `export_session` — `test_search_and_export_work_on_historical_session` (text + json) + `test_dispatch_tool_export_session_rejects_bad_format` (ValueError → error TextContent) + catalog test.
- Core primitives — `test_main_database_get_session_db_path` + `test_session_database_fetch_logs_is_ordered`.

## Consistency self-check

- The four new `SessionManager` methods all delegate to existing/added database primitives; none open MCP types or reimplement SQL.
- `_open_session_db` is the only place that opens a historical `SessionDatabase`; both `search_session_logs` and `export_session` go through it, so the active-session shortcut and the not-found error are handled in exactly one place.
- `delete_session` guard mirrors the style of the existing `RuntimeError("No active device connection")` checks.
- `dispatch_tool` keeps the single `except Exception → TextContent` policy; the new branches raise `KeyError`/`ValueError`/`RuntimeError` which that handler already converts for the client.
- Test style matches each file: `@pytest.mark.asyncio` in `tests/test_database.py`, `asyncio.run(scenario())` in `tests/runtime/test_session.py` and `tests/integration/test_mcp_app.py`.
