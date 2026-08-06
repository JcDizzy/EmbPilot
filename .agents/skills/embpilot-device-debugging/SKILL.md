---
name: embpilot-device-debugging
description: Use EmbPilot for embedded-device inspection, command execution, log capture, and session management over Serial/UART, SSH, or Telnet. Trigger whenever a task mentions a COM port, /dev/tty device, SSH/Telnet target, firmware console, boot log, device shell, or remote embedded debugging.
---

# EmbPilot Device Debugging

Route device access through EmbPilot whenever its MCP tools are available. Do
not open a raw `ssh`/`telnet` process, serial terminal, or custom socket script
for a capability EmbPilot provides.

## Workflow

1. Select exactly one connection tool: `connect_serial`, `connect_ssh`, or
   `connect_telnet`.
2. Pass arguments as a JSON object. Never serialize that object into a JSON
   string and never invent wrapper keys such as `config`.
3. Use `send_command` for interaction. Set `expect_regex` when a known prompt or
   completion marker exists; otherwise choose a bounded timeout.
4. Inspect structured results first. On `{ "ok": false }`, use `error.code`,
   `retryable`, and `suggestion` to decide whether to retry or ask the user.
5. Use EmbPilot resources and history tools for logs instead of duplicating
   capture in a shell process.
6. Call `disconnect_device` when the task finishes or before abandoning a
   session.

## JSON examples

Serial:

```json
{"port":"COM3","baudrate":115200,"line_ending":"crlf"}
```

SSH:

```json
{"host":"192.168.1.10","username":"root","key_file":"~/.ssh/id_ed25519"}
```

Never include passwords, tokens, or private-key contents in commands. Prefer a
key-file path and verified known hosts. Use
`insecure_skip_host_key_check: true` only when the user explicitly accepts that
risk for a controlled device. Fall back to raw shell tools only if EmbPilot is
unavailable or lacks the required capability, and explain the fallback.
