# EmbPilot Runtime Rearchitecture Design

Date: 2026-06-27
Status: Approved in conversation, pending written-spec review
Scope: Runtime rearchitecture for long-term maintainability and standard PyPI packaging

## 1. Goal

EmbPilot already has a clear product direction: expose embedded device debugging
through MCP over Serial, Telnet, and SSH. The current implementation proves the
core idea, but too much runtime logic is concentrated in a single server module.

This design restructures the package around stable boundaries so that future
features such as device profiles, richer subscriptions, RAG triggers, and
session analytics can be added without turning the MCP entrypoint into a
monolith again.

The design also treats standard Python packaging as a first-class requirement.
The result must remain a normal `src/`-layout PyPI package that installs and
runs through standard Python tooling.

## 2. Problems in the Current Shape

The current codebase has four structural issues:

1. MCP registration, runtime state, and device-session behavior are tightly
   coupled inside `src/embpilot/server.py`.
2. The current log pipeline is built around a single queue with multiple
   consumer concepts, which is not a good long-term fit for true fan-out
   behavior.
3. `send_command(..., expect_regex=...)` is described as a first-class feature,
   but the current implementation does not provide a real expect-driven wait
   path.
4. `device://sysinfo` pretends to be device-agnostic, but its hardcoded command
   sequence is not reliable across MCU, RTOS, and embedded Linux targets.

## 3. Design Goals

This rearchitecture is guided by the following goals:

- Keep a standard `src/` package structure suitable for PyPI publishing.
- Separate MCP protocol wiring from runtime business logic.
- Replace implicit multi-consumer queue behavior with explicit dispatcher-based
  fan-out.
- Make `send_command` support true expect-style waiting.
- Replace fake generic sysinfo probing with stable, honest session metadata.
- Keep drivers focused only on transport concerns.
- Make shutdown, flushing, and session closure deterministic.
- Support gradual migration without breaking the public CLI entrypoint.

## 4. Non-Goals

The following are explicitly out of scope for this pass:

- Adding new transport types beyond Serial, Telnet, and SSH
- Shipping device-specific command profiles in this phase
- Expanding RAG capabilities beyond preserving compatibility with the new
  runtime shape
- Implementing DTR/RTS reset behavior unless it already falls out naturally
  from the refactor

## 5. Target Package Structure

EmbPilot will keep the standard `src/` layout and move runtime responsibilities
into dedicated modules:

```text
EmbPilot/
├─ pyproject.toml
├─ README.md
├─ CHANGE_LOG.md
├─ PROGRESS.md
├─ docs/
├─ tests/
└─ src/
   └─ embpilot/
      ├─ __init__.py
      ├─ __main__.py
      ├─ cli.py
      ├─ mcp_app.py
      ├─ config.py
      ├─ runtime/
      │  ├─ __init__.py
      │  ├─ models.py
      │  ├─ session.py
      │  ├─ pipeline.py
      │  ├─ expect.py
      │  └─ resources.py
      ├─ core/
      │  ├─ __init__.py
      │  ├─ database.py
      │  ├─ rag.py
      │  ├─ schema_main.sql
      │  └─ schema_session.sql
      └─ drivers/
         ├─ __init__.py
         ├─ base.py
         ├─ serial_dev.py
         ├─ telnet_dev.py
         └─ ssh_dev.py
```

### Module responsibilities

- `cli.py`
  - parse CLI arguments
  - build config
  - start the MCP application
- `mcp_app.py`
  - register MCP tools, resources, and prompts
  - translate MCP requests to runtime method calls
- `runtime/session.py`
  - session lifecycle
  - device connection orchestration
  - state transitions
- `runtime/pipeline.py`
  - log producer
  - dispatcher
  - DB sink coordination
  - live subscriber fan-out
- `runtime/expect.py`
  - expect waiters
  - command-window output collection
  - timeout and regex matching
- `runtime/resources.py`
  - resource read models
  - live log subscription state
  - session metadata resource generation
- `core/`
  - databases
  - RAG
  - data persistence helpers
- `drivers/`
  - transport-specific connect, disconnect, write, and read access

## 6. Runtime Architecture

### 6.1 Session state machine

Each active connection is managed as a formal session with these states:

- `idle`
- `connecting`
- `active`
- `closing`
- `closed`
- `error`

Rules:

- Only one active device session exists at a time in this phase.
- `connect_device` moves `idle -> connecting -> active`.
- Reconnect first triggers clean closure of the current session.
- Any unrecoverable runtime exception moves the session to `error`, then
  cleanup drives it to `closed`.
- All database closing and flush operations happen inside `closing`, never after
  the session is already considered closed.

### 6.2 Data flow

The new pipeline is explicit fan-out:

```text
Device Driver Reader
  -> LogProducer
  -> SessionDispatcher
     -> RingBufferSink
     -> DbSink
     -> ExpectManager
     -> LiveLogSubscribers
```

Responsibilities:

- `LogProducer`
  - read bytes from the active driver
  - frame them into `LogLine` objects
  - emit structured lines into the dispatcher
- `SessionDispatcher`
  - broadcast each `LogLine` to registered sinks
  - remain thin and avoid business-specific branching
- `RingBufferSink`
  - maintain recent log history for `read_resource(device://live_log)`
- `DbSink`
  - batch and persist log lines
  - flush remaining lines before session closure
- `ExpectManager`
  - manage active command windows and regex waiters
- `LiveLogSubscribers`
  - deliver incremental events to subscribed MCP clients

This structure removes the ambiguity of a single queue pretending to support
independent consumers.

## 7. Public Interface Design

### 7.1 CLI

The console entrypoint remains `embpilot`.

Implementation rules:

- `src/embpilot/__main__.py` becomes a thin module that calls `embpilot.cli:main`
- `pyproject.toml` script entry should point to `embpilot.cli:main`
- CLI behavior remains backward-compatible wherever practical

### 7.2 `connect_device(interface_type, config)`

The MCP tool name stays unchanged.

`config` becomes the stable place for transport arguments and optional logical
identity fields:

- `device_name`
- `port`
- `baudrate`
- `bytesize`
- `parity`
- `stopbits`
- `timeout`
- `host`
- `username`
- `password`
- `key_file`
- `known_hosts`

Behavior:

- `device_name` is optional.
- If present, it becomes the preferred session display name and appears in
  session metadata resources.
- If absent, EmbPilot falls back to serial port name or `host:port`.

### 7.3 `send_command(command, expect_regex, timeout_ms)`

Tool name stays unchanged, but behavior is tightened.

Without `expect_regex`:

- start a command output window
- return collected output when timeout expires

With `expect_regex`:

- start a command output window
- continue collecting output until the regex matches or timeout expires
- return the command window output, not only the matched line

This keeps expect useful for early return while preserving debugging context.

### 7.4 `device://live_log`

This resource remains part of the public interface.

Behavior:

- `read_resource` returns the current ring-buffer snapshot
- `subscribe_resource` provides incremental live-log updates for the active
  session

This resource is session-scoped. If no session is active, the response must say
so clearly.

### 7.5 `device://session_info`

`device://sysinfo` will be replaced by `device://session_info`.

Reason:

- generic automatic sysinfo probing is not reliable across embedded targets
- the runtime can always provide correct session metadata

Returned fields should include:

- session ID
- interface type
- display device name
- sanitized connection summary
- session start time
- active state
- recent log counters or availability summary

### 7.6 Prompts

Prompts remain supported, but they must stop assuming a universal shell-like
command set.

Changes:

- crash-analysis prompts continue to rely on `device://live_log`
- hardware-sanity prompts should rely on session context and user-supplied or
  agent-supplied commands instead of hardcoded generic command bundles

### 7.7 `reset_target`

Near-term behavior must be honest.

Preferred rule:

- keep `reboot` if implemented
- remove `dtr` and `rts` from the public schema until they are truly supported

This avoids publishing interface claims that the runtime cannot fulfill.

## 8. Session Metadata and Resource Model

The session model should become explicit runtime data rather than reconstructed
ad hoc from the MCP layer.

Suggested session model fields:

- `session_id`
- `interface_type`
- `device_name`
- `connection_summary`
- `started_at`
- `state`
- `last_log_at`
- `log_count`

This model will be shared by:

- session management
- `device://session_info`
- diagnostics and future analytics

## 9. Shutdown and Reliability Rules

The close path must be deterministic.

Expected order:

1. mark session state as `closing`
2. stop accepting new command windows
3. stop live subscriber emission
4. stop producer loop
5. flush DB sink
6. close the session database
7. mark session as ended in the main database
8. disconnect the device transport
9. mark session state as `closed`

Requirements:

- closing must be idempotent
- partial shutdown must still try to flush persisted data where safe
- unexpected transport errors must not skip database finalization

## 10. Migration Strategy

This rearchitecture should be delivered in stages rather than a big-bang swap.

### Stage 1: introduce new module skeleton

- add `cli.py`, `mcp_app.py`, and `runtime/`
- keep the current public entrypoint working
- allow the old `server.py` to delegate into the new runtime gradually

### Stage 2: move runtime behavior behind the new interfaces

- session lifecycle into `runtime/session.py`
- pipeline behavior into `runtime/pipeline.py`
- expect logic into `runtime/expect.py`
- resource assembly into `runtime/resources.py`

### Stage 3: retire monolithic logic

- reduce legacy `server.py` to a thin compatibility wrapper or remove it if no
  longer needed
- update package entrypoints to the new stable modules

This staged approach lowers the risk of breaking startup behavior during the
refactor.

## 11. Test Strategy

Tests should move from file-oriented coverage to layer-oriented coverage.

Target structure:

- `tests/runtime/`
  - session state transitions
  - dispatcher fan-out
  - expect matching
  - DB flush on close
  - live subscription behavior
- `tests/core/`
  - main/session DB behavior
  - schema use
  - RAG compatibility
- `tests/drivers/`
  - driver contracts
- `tests/integration/`
  - CLI startup
  - MCP tool behavior
  - MCP resource behavior

Additional packaging-related test goal:

- standard local test workflow must work for a fresh checkout
- avoid a setup where plain test collection immediately fails because the
  `src/` package is not importable in the expected development workflow

## 12. Packaging and Distribution Requirements

This project must remain a conventional Python package.

Requirements:

- preserve the `src/` layout
- keep `pyproject.toml` as the package source of truth
- expose console scripts through `project.scripts`
- ensure SQL schema files are packaged as runtime data
- make module entrypoints consistent between `python -m embpilot` and
  installed console scripts

Near-term cleanup expected during implementation:

- move console script target from `embpilot.__main__:main` to
  `embpilot.cli:main`
- verify package data configuration for SQL schema files
- verify editable-install and local-test workflows

## 13. Documentation Impact

The following documents must be updated during implementation:

- `README.md`
  - architecture tree
  - runtime behavior
  - resource names
- `docs/mcp_embedded_debug_spec.md`
  - replace fake generic sysinfo assumptions
  - describe dispatcher-based runtime
  - align tool/resource contracts with implementation
- `PROGRESS.md`
  - record migration stages and pitfalls
- `CHANGE_LOG.md`
  - capture the runtime rearchitecture and interface changes

If the repository continues using `change.log` as the practical changelog file,
that file must stay synchronized as well.

## 14. Risks and Mitigations

Risk: interface drift during staged migration  
Mitigation: keep MCP tool names stable and centralize schema definitions in
`mcp_app.py`.

Risk: regression in log persistence during shutdown  
Mitigation: add close-path tests before removing the old shutdown path.

Risk: packaging regressions after module moves  
Mitigation: verify `python -m embpilot`, console script invocation, and editable
install workflows as part of the refactor.

Risk: accidental reintroduction of monolithic runtime logic  
Mitigation: enforce module responsibility boundaries defined in this spec.

## 15. Success Criteria

This rearchitecture is complete when all of the following are true:

- new runtime-oriented package structure exists
- CLI entry uses `embpilot.cli:main`
- MCP registration is separated from runtime state management
- dispatcher-based fan-out replaces the implicit multi-consumer queue model
- `send_command` supports real expect-driven waiting
- `device://session_info` replaces fake generic sysinfo behavior
- `device://live_log` supports both snapshot reads and live subscription flow
- shutdown flushes logs deterministically
- standard package installation and local development workflows are verified
- documentation is aligned with the shipped behavior

