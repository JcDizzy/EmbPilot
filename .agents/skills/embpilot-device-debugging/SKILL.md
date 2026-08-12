---
name: embpilot-device-debugging
description: Use EmbPilot for embedded-device inspection, command execution, log capture, and session management over Serial/UART, SSH, or Telnet. Trigger whenever a task mentions a COM port, /dev/tty device, SSH/Telnet target, firmware console, boot log, device shell, or remote embedded debugging.
---

# EmbPilot Device Debugging

Route device access through EmbPilot whenever it is available. Do not open a
raw `ssh`/`telnet` process, serial terminal, or custom socket script for a
capability EmbPilot provides.

## Tool catalog

- Connect: `connect_serial`, `connect_ssh`, `connect_telnet` (exactly one).
- Interact: `send_command` (send + capture), `read_output` (observe without
  sending any bytes), `reset_target` (reboot).
- Sessions: `list_sessions`, `search_history_logs`, `delete_session`,
  `export_session`, `disconnect_device`.

Run `embpilot tools` for the full list and `embpilot help <tool>` for the
schema, defaults, and guidance of any tool.

## Workflow

1. Select exactly one connection tool: `connect_serial`, `connect_ssh`, or
   `connect_telnet`.
2. Pass arguments as a JSON object. Never serialize that object into a JSON
   string and never invent wrapper keys such as `config`.
3. Use `send_command` for interaction. Set `expect_regex` when a known prompt
   or completion marker exists; otherwise choose a bounded timeout. When the
   device emits output on its own (boot logs, periodic status), use
   `read_output` instead of sending commands — it never writes to the device
   and returns early on `expect_regex` or after `duration_ms`.
4. Inspect structured results first. On `{ "ok": false }`, use `error.code`,
   `retryable`, and `suggestion` to decide whether to retry or ask the user.
   Connection failures distinguish timeout / authentication / refused.
5. Use EmbPilot resources and history tools for logs instead of duplicating
   capture in a shell process. `search_history_logs` accepts an optional
   `session_id` (from `list_sessions`) to search closed sessions after
   disconnection.
6. Call `disconnect_device` when the task finishes or before abandoning a
   session.

## CLI-only agents (no MCP client, e.g. pi)

`embpilot` is a stdio MCP server by default; agents without MCP support call
the CLI instead:

- `embpilot batch` — scripted JSONL mode: one `{"tool": ..., "args": {...}}`
  request per stdin line, one result envelope per stdout line, no banner.
  Use it for connect -> commands -> disconnect in a single invocation.
- `embpilot serve` + `--socket` — keeps one session alive across separate
  invocations. Start `embpilot --data-dir X serve` once, then call
  `embpilot --socket X/daemon.json tool send_command --json '...'` (POSIX:
  unix socket; Windows: TCP loopback; the daemon writes its endpoint to
  `<data-dir>/daemon.json`).
- `embpilot run --connect '<json>' cmd1 cmd2` — connect, run commands,
  disconnect in one call.
- `embpilot tool <name> --port COM3 --baudrate 115200` — schema-driven flags
  avoid inline JSON quoting; `--json '{"...": ...}'` remains canonical and
  explicit flags override its keys.
- Exit codes: 0 success / 1 tool failure / 2 usage or argument error.
- `--data-dir` and other path options must appear before the subcommand.

## JSON examples

Serial:

```json
{"port":"COM3","baudrate":115200,"line_ending":"crlf"}
```

SSH:

```json
{"host":"192.168.1.10","username":"root","key_file":"~/.ssh/id_ed25519"}
```

Passive observation:

```json
{"duration_ms":15000,"expect_regex":"login:"}
```

Never include passwords, tokens, or private-key contents in commands. Prefer a
key-file path and verified known hosts. Use
`insecure_skip_host_key_check: true` only when the user explicitly accepts that
risk for a controlled device. Fall back to raw shell tools only if EmbPilot is
unavailable or lacks the required capability, and explain the fallback.
