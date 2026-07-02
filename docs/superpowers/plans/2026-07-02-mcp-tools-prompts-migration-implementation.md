# MCP Tools & Prompts Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the MCP server surface described in spec §7 (Stage 3): connect the four core tools (`connect_device`, `send_command`, `reset_target`, `disconnect_device`) and the two prompts (`analyze_crash_log`, `hardware_sanity_check`) to the runtime `SessionManager`, so that `mcp_app.py` exposes a fully functional MCP server rather than only resources. Deliver in small, test-driven increments that keep the package green at every commit.

**Architecture:** This stage builds on the already-merged runtime rearchitecture (Tasks 1–7 of `2026-06-27-runtime-rearchitecture-implementation.md`). The runtime layer (`runtime/session.py`) is the single owner of device state; the MCP layer (`mcp_app.py`) is a thin, schema-centralized adapter that translates MCP requests into `SessionManager` calls and translates exceptions back into `TextContent` error messages. The single remaining runtime gap is `reset_target` (spec §7.7); `connect_device` / `send_command` / `disconnect_device` already exist on `SessionManager`. To keep the MCP handlers unit-testable without a real device, the dispatch logic lives in module-level pure functions (`dispatch_tool`, `render_prompt`) and the `@app.*` decorators only delegate to them.

**Tech Stack:** Python 3.11+, `asyncio`, `mcp` (`Server`, `mcp.types`), `pytest`, `pytest-asyncio`

---

## Scope and explicit non-goals

In scope (spec §7 core contract):

- `reset_target` runtime method (reboot only — spec §7.7)
- MCP tools: `connect_device`, `send_command`, `reset_target`, `disconnect_device`
- MCP prompts: `analyze_crash_log`, `hardware_sanity_check` (with the §7.6 adjustment — see Task 3)

Out of scope (future work; database layer already provides primitives, so no runtime change is blocked by these):

- `list_sessions` / `delete_session` / `export_session` tools — `MainDatabase` already exposes `list_sessions`, `delete_session`; wiring them is a separate stage.
- `search_history_logs` tool — `SessionDatabase.search_logs` already exists; wiring is a separate stage.
- Subscription/notification emission for resources (current `subscribe_resource` only logs).

**Do not** in this stage:

- Reintroduce `device://sysinfo` — keep `device://session_info`.
- Recouple MCP protocol registration with runtime state management. `SessionManager` stays unaware of MCP.
- Reintroduce `dtr` / `rts` reset methods in the public tool schema until the runtime truly supports them (spec §7.7).

---

## File map

- Modify: `src/embpilot/runtime/session.py`
- Modify: `src/embpilot/mcp_app.py`
- Modify: `tests/runtime/test_session.py`
- Modify: `tests/integration/test_mcp_app.py`
- Modify: `docs/mcp_embedded_debug_spec.md`
- Modify: `README.md`
- Modify: `PROGRESS.md`
- Modify: `change.log`

---

### Task 1: Add `reset_target` (reboot) to `SessionManager`

The runtime currently lacks `reset_target`. Per spec §7.7, implement only `reboot` honestly; reject other methods. Mirror the write + `insert_operation` pattern already used by `send_command`, and serialize on `_command_lock` so a reset never interleaves with an in-flight command write.

**Files:**
- Modify: `src/embpilot/runtime/session.py`
- Modify: `tests/runtime/test_session.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/runtime/test_session.py`, reusing whatever fake-device fixture/pattern that file already uses for its existing `send_command` test (construct the manager the same way the existing cases do — typically `EmbPilotConfig(data_dir=tmp_path)`, `await manager.start()`, then monkeypatch `embpilot.runtime.session.build_device` with a fake that exposes `connect`/`get_reader`/`write`/`disconnect`):

```python
@pytest.mark.asyncio
async def test_reset_target_reboot_writes_reboot_command(manager_with_fake_device):
    manager, fake = manager_with_fake_device

    message = await manager.reset_target()

    assert message == "Reset command sent (reboot)."
    assert fake.writes == [b"reboot\n"]


@pytest.mark.asyncio
async def test_reset_target_rejects_unsupported_method(manager_with_fake_device):
    manager, _ = manager_with_fake_device

    with pytest.raises(ValueError):
        await manager.reset_target(method="dtr")


@pytest.mark.asyncio
async def test_reset_target_requires_active_connection(started_manager):
    with pytest.raises(RuntimeError, match="No active device connection"):
        await started_manager.reset_target()
```

Notes for the implementer:
- `manager_with_fake_device` is a fixture that connects a fake device and returns `(manager, fake)`. If no such fixture exists yet, extract one from the body of the existing `send_command` test rather than duplicating setup.
- `started_manager` is a manager with `start()` awaited but no device connected (so `self._device is None`).
- `dtr` is deliberately used as the rejected method to encode the §7.7 contract in the test.

- [ ] **Step 2: Verify the tests fail**

```
py -3.11 -m pytest tests/runtime/test_session.py -q
```

Expect three failures tied to the missing `reset_target` attribute (`AttributeError`) before the body assertions are even reached.

- [ ] **Step 3: Implement `reset_target`**

Add the method to `SessionManager` in `src/embpilot/runtime/session.py`, placed immediately after `send_command` so the two write paths sit together:

```python
    async def reset_target(self, method: str = "reboot") -> str:
        if method != "reboot":
            raise ValueError(
                f"Unsupported reset method: {method!r} (only 'reboot' is supported)"
            )
        async with self._command_lock:
            if self._device is None:
                raise RuntimeError("No active device connection")
            await self._device.write(b"reboot\n")
            if self._session_info is not None:
                await self._main_db.insert_operation(
                    actor="AI",
                    action_type="call_tool",
                    detail={"tool": "reset_target", "method": method},
                    session_id=self._session_info.session_id,
                )
            return "Reset command sent (reboot)."
```

Rationale (record in the commit/PROGRESS): method validation happens outside the lock so an invalid argument never blocks other commands; the `reboot` write and the operation log happen inside the lock to stay consistent with `send_command`. We send `reboot\n` as a line because the device reader is line-framed.

- [ ] **Step 4: Verify the tests pass**

```
py -3.11 -m pytest tests/runtime/test_session.py -q
```

All cases in the file, including the three new ones, must pass.

- [ ] **Step 5: Commit**

```
git add src/embpilot/runtime/session.py tests/runtime/test_session.py
git commit -m "feat: add reset_target reboot method to SessionManager"
```

---

### Task 2: Wire the four core MCP tools in `mcp_app.py`

Expose `connect_device`, `send_command`, `reset_target`, `disconnect_device` as MCP tools. Centralize the tool schema in `build_tool_catalog()` (spec §14) and put the dispatch logic in a module-level `dispatch_tool()` so it is unit-testable without standing up a real MCP transport or a real device — error paths are exercised against a started-but-disconnected `SessionManager`.

**Files:**
- Modify: `src/embpilot/mcp_app.py`
- Modify: `tests/integration/test_mcp_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_mcp_app.py`. Construct the manager exactly as the existing resource tests in that file already do (the file has the canonical `EmbPilotConfig` + `SessionManager` + `start`/`shutdown` pattern — reuse the same fixture rather than inventing a new one):

```python
import pytest

from mcp.types import TextContent

from embpilot.config import EmbPilotConfig
from embpilot.mcp_app import build_tool_catalog, dispatch_tool
from embpilot.runtime.session import SessionManager


def test_build_tool_catalog_lists_core_tools():
    names = {tool.name for tool in build_tool_catalog()}
    assert {
        "connect_device",
        "send_command",
        "reset_target",
        "disconnect_device",
    } <= names


def test_reset_target_schema_excludes_dtr_and_rts():
    tool = next(t for t in build_tool_catalog() if t.name == "reset_target")
    method_schema = tool.inputSchema["properties"]["method"]
    assert method_schema.get("enum") == ["reboot"]


@pytest.mark.asyncio
async def test_dispatch_tool_unknown_returns_error_text(tmp_path):
    manager = SessionManager(EmbPilotConfig(data_dir=str(tmp_path)))
    await manager.start()
    try:
        result = await dispatch_tool(manager, "no_such_tool", {})
    finally:
        await manager.shutdown()

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "unknown tool" in result[0].text.lower()


@pytest.mark.asyncio
async def test_dispatch_tool_send_command_without_connection_returns_error(tmp_path):
    manager = SessionManager(EmbPilotConfig(data_dir=str(tmp_path)))
    await manager.start()
    try:
        result = await dispatch_tool(manager, "send_command", {"command": "ls"})
    finally:
        await manager.shutdown()

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "error" in result[0].text.lower()
```

Note: the existing resource tests in `test_mcp_app.py` already import and exercise `create_mcp_app` and `build_resource_catalog`, so the `SessionManager(EmbPilotConfig(...))` construction is already established there — match it. If those tests already use a shared `tmp_path`-based fixture, reuse it instead of constructing inline.

- [ ] **Step 2: Verify the tests fail**

```
py -3.11 -m pytest tests/integration/test_mcp_app.py -q
```

Expect import errors (`cannot import name 'build_tool_catalog' / 'dispatch_tool'`) since neither exists yet.

- [ ] **Step 3: Implement the tool layer**

In `src/embpilot/mcp_app.py`:

1. Extend the `mcp.types` import:

```python
from mcp.types import Resource, TextContent, Tool
```

2. Add `build_tool_catalog()` next to `build_resource_catalog()`:

```python
def build_tool_catalog() -> list[Tool]:
    return [
        Tool(
            name="connect_device",
            description=(
                "Connect to an embedded device over Serial, Telnet, or SSH. "
                "Replaces any active connection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "interface_type": {
                        "type": "string",
                        "enum": ["serial", "telnet", "ssh"],
                    },
                    "config": {
                        "type": "object",
                        "description": "Interface-specific connection parameters.",
                    },
                },
                "required": ["interface_type", "config"],
            },
        ),
        Tool(
            name="send_command",
            description=(
                "Send a command line to the active device and return captured output "
                "until the expect window closes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "expect_regex": {
                        "type": "string",
                        "description": "Optional regex marking the end of the response.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "default": 5000,
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="reset_target",
            description=(
                "Reset the active device. Only the 'reboot' method is currently supported."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["reboot"],
                        "default": "reboot",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="disconnect_device",
            description="Disconnect the active device and close the session.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]
```

3. Add the module-level dispatcher (the single source of tool behavior; exceptions become `TextContent` errors rather than propagating into the MCP transport):

```python
async def dispatch_tool(
    manager: SessionManager, name: str, arguments: dict
) -> list[TextContent]:
    try:
        if name == "connect_device":
            session_id = await manager.connect_device(
                interface_type=arguments["interface_type"],
                config=arguments.get("config") or {},
            )
            return [TextContent(type="text", text=f"Connected. session_id={session_id}")]
        if name == "send_command":
            output = await manager.send_command(
                command=arguments["command"],
                expect_regex=arguments.get("expect_regex"),
                timeout_ms=arguments.get("timeout_ms", 5000),
            )
            return [TextContent(type="text", text=output)]
        if name == "reset_target":
            message = await manager.reset_target(
                method=arguments.get("method", "reboot")
            )
            return [TextContent(type="text", text=message)]
        if name == "disconnect_device":
            await manager.disconnect_device()
            return [TextContent(type="text", text="Disconnected.")]
        return [
            TextContent(type="text", text=f"Error: unknown tool {name!r}")
        ]
    except Exception as exc:  # noqa: BLE001 — surface tool failures to the client
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {exc}")]
```

4. Register the handlers inside `create_mcp_app`, after the existing `subscribe_resource` handler and before `return app, manager`:

```python
    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return build_tool_catalog()

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await dispatch_tool(manager, name, arguments)
```

- [ ] **Step 4: Verify the tests pass**

```
py -3.11 -m pytest tests/integration/test_mcp_app.py -q
```

All tool tests and the pre-existing resource tests must pass.

- [ ] **Step 5: Commit**

```
git add src/embpilot/mcp_app.py tests/integration/test_mcp_app.py
git commit -m "feat: wire MCP tools to SessionManager in mcp_app"
```

---

### Task 3: Migrate the two prompts into `mcp_app.py`

Expose `analyze_crash_log` and `hardware_sanity_check`. Per spec §7.6, the crash prompt continues to point at `device://live_log`, while the hardware-sanity prompt must stop hardcoding a generic command bundle (e.g. `help` / `version` / `dmesg`) and instead rely on session context plus user- or agent-supplied commands. As with tools, the rendering lives in a module-level `render_prompt()` so it is unit-testable directly.

**Files:**
- Modify: `src/embpilot/mcp_app.py`
- Modify: `tests/integration/test_mcp_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_mcp_app.py`:

```python
from embpilot.mcp_app import build_prompt_catalog, render_prompt


def test_build_prompt_catalog_lists_prompts():
    names = {prompt.name for prompt in build_prompt_catalog()}
    assert {"analyze_crash_log", "hardware_sanity_check"} <= names


def test_render_analyze_crash_log_points_at_live_log():
    text = render_prompt("analyze_crash_log", {})
    assert "device://live_log" in text


def test_render_hardware_sanity_omits_hardcoded_commands():
    text = render_prompt("hardware_sanity_check", {})
    lowered = text.lower()
    # spec §7.6: no hardcoded generic command bundle
    for forbidden in ("dmesg", "uname", "help", "version"):
        assert forbidden not in lowered
    # it should instead route through send_command and session context
    assert "send_command" in lowered
    assert "device://session_info" in lowered


def test_render_hardware_sanity_incorporates_focus_argument():
    text = render_prompt("hardware_sanity_check", {"focus": "power rails"})
    assert "power rails" in text


def test_render_unknown_prompt_raises():
    with pytest.raises(ValueError):
        render_prompt("nonexistent", {})
```

- [ ] **Step 2: Verify the tests fail**

```
py -3.11 -m pytest tests/integration/test_mcp_app.py -q
```

Expect import errors for `build_prompt_catalog` / `render_prompt`.

- [ ] **Step 3: Implement the prompt layer**

In `src/embpilot/mcp_app.py`:

1. Extend the `mcp.types` import to include the prompt types:

```python
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)
```

2. Add `build_prompt_catalog()` after `build_tool_catalog()`:

```python
def build_prompt_catalog() -> list[Prompt]:
    return [
        Prompt(
            name="analyze_crash_log",
            description=(
                "Analyze a recent crash, panic, hang, or unexpected reboot captured "
                "in the live device log."
            ),
            arguments=[
                PromptArgument(
                    name="context",
                    description="Optional extra context (board, firmware, symptoms).",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="hardware_sanity_check",
            description="Guide a hardware sanity check of the connected device.",
            arguments=[
                PromptArgument(
                    name="focus",
                    description="Optional area to focus on (power, peripherals, boot, ...).",
                    required=False,
                ),
            ],
        ),
    ]
```

3. Add the module-level renderer. The crash prompt depends on `device://live_log` (spec §7.6). The hardware-sanity prompt deliberately references `device://session_info` and `send_command` and asks the agent to obtain the command set from the session/user rather than baking one in:

```python
def render_prompt(name: str, arguments: dict) -> str:
    if name == "analyze_crash_log":
        context = (arguments.get("context") or "").strip()
        body = (
            "You are an embedded debugging assistant. Read the live device log via the "
            "device://live_log resource (or subscribe for updates) and look for crash "
            "signatures, panics, hangs, or unexpected reboots. Form a root-cause "
            "hypothesis and propose the next diagnostic commands to send via send_command."
        )
        if context:
            body += f"\n\nAdditional context:\n{context}"
        return body
    if name == "hardware_sanity_check":
        focus = (arguments.get("focus") or "general health").strip()
        return (
            f"You are an embedded debugging assistant. Perform a hardware sanity check "
            f"focused on: {focus}. Inspect device://session_info to confirm the connection "
            f"is active. Determine the appropriate diagnostic commands for THIS board from "
            f"the session context and the user — do not assume a fixed command set. Use "
            f"send_command to run the agreed commands and read device://live_log to "
            f"interpret the results."
        )
    raise ValueError(f"Unknown prompt: {name!r}")


def _prompt_result(name: str, text: str) -> GetPromptResult:
    return GetPromptResult(
        description=f"EmbPilot prompt: {name}",
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=text)),
        ],
    )
```

4. Register the handlers inside `create_mcp_app`, after the `call_tool` handler:

```python
    @app.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return build_prompt_catalog()

    @app.get_prompt()
    async def get_prompt(name: str, arguments: dict) -> GetPromptResult:
        return _prompt_result(name, render_prompt(name, arguments))
```

- [ ] **Step 4: Verify the tests pass**

```
py -3.11 -m pytest tests/integration/test_mcp_app.py -q
```

All tool + prompt tests and the pre-existing resource tests must pass.

- [ ] **Step 5: Commit**

```
git add src/embpilot/mcp_app.py tests/integration/test_mcp_app.py
git commit -m "feat: migrate crash and hardware sanity prompts to mcp_app"
```

---

### Task 4: Align documentation and run full verification + packaging regression

Bring the docs in line with the now-complete MCP surface, then re-run the whole suite plus the packaging test so this stage lands green and shippable.

**Files:**
- Modify: `docs/mcp_embedded_debug_spec.md`
- Modify: `README.md`
- Modify: `PROGRESS.md`
- Modify: `change.log`

- [ ] **Step 1: Update `docs/mcp_embedded_debug_spec.md`**

Reflect the implemented surface:
- Tools section lists exactly `connect_device`, `send_command`, `reset_target`, `disconnect_device`; for `reset_target`, state that only `reboot` is supported and `dtr` / `rts` are intentionally absent until the runtime supports them (spec §7.7).
- Prompts section records the §7.6 adjustment: `analyze_crash_log` relies on `device://live_log`; `hardware_sanity_check` no longer embeds a generic command bundle and instead routes through `send_command` and session/user-supplied commands.
- Note that `list_sessions` / `delete_session` / `export_session` / `search_history_logs` are planned but not yet exposed as tools (cross-reference the database primitives that already back them).

- [ ] **Step 2: Update `README.md`**

If the README's "Tools" / "Prompts" quick-reference lists differ from the implemented set, reconcile them. Do not invent capabilities — only document what the four tools and two prompts actually do.

- [ ] **Step 3: Update `PROGRESS.md` and `change.log`**

- `PROGRESS.md`: mark this stage (MCP tools & prompts migration) as complete under the runtime-rearchitecture progress section; note that `reset_target` is reboot-only by design and that the remaining session-query tools are deferred.
- `change.log`: add entries for `reset_target`, the four MCP tools, and the two migrated prompts.

- [ ] **Step 4: Run the full suite**

```
py -3.11 -m pytest tests -q
```

Every test, including the SQL packaging test from the previous stage (`test_installed_package_includes_sql_schema_files`), must pass.

- [ ] **Step 5: Packaging regression check**

```
py -3.11 -m pytest tests/integration/test_cli.py::test_installed_package_includes_sql_schema_files -q
py -3.11 -m pytest tests/integration/test_cli.py::test_installed_console_script_prints_version -q
```

Both must pass — confirming the package still installs cleanly with `pip install --no-deps --use-pep517` and the `embpilot` console script still works after the MCP layer grew.

- [ ] **Step 6: Commit**

```
git add docs/mcp_embedded_debug_spec.md README.md PROGRESS.md change.log
git commit -m "docs: align tools and prompts documentation"
```

---

## Spec coverage self-check

Before declaring the stage done, confirm each spec §7 contract has a backing test:

- §7.2 `connect_device` — `test_build_tool_catalog_lists_core_tools` lists it; behavior exercised via existing runtime `connect_device` tests.
- §7.3 `send_command` — `test_dispatch_tool_send_command_without_connection_returns_error` (error path) + existing runtime `send_command` tests (happy path).
- §7.4 `device://live_log` — covered in the previous stage; `test_render_analyze_crash_log_points_at_live_log` confirms the prompt still routes there.
- §7.5 `device://session_info` — covered in the previous stage; `test_render_hardware_sanity_omits_hardcoded_commands` confirms the prompt references it.
- §7.6 Prompts — `test_render_hardware_sanity_omits_hardcoded_commands` enforces no hardcoded command bundle.
- §7.7 `reset_target` — `test_reset_target_*` (reboot works; `dtr` rejected; no-connection raises) + `test_reset_target_schema_excludes_dtr_and_rts` (schema honesty).

## Placeholder / dead-code scan

After Task 3, `grep` the repo for stale artifacts that should be gone now that tools and prompts live in `mcp_app.py`:

- No `@app.list_tools` / `@app.call_tool` / `@app.list_prompts` / `@app.get_prompt` registrations outside `mcp_app.py`.
- No `dtr` / `rts` reset references remain anywhere in `src/`.
- No `device://sysinfo` references (must remain `device://session_info`).
- No `TODO` / `FIXME` / `NotImplemented` introduced by this stage in `src/`.

## Type / import consistency self-check

- `mcp_app.py` imports from `mcp.types` only the types it constructs: `Resource`, `TextContent`, `Tool`, `Prompt`, `PromptArgument`, `PromptMessage`, `GetPromptResult`.
- `dispatch_tool` and `render_prompt` are module-level (no `self`, no closure over `manager`) so they are directly importable by the tests.
- The `@app.*` decorators inside `create_mcp_app` only delegate; they contain no branching logic of their own.
