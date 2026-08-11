---
name: embpilot-device-debugging
description: Use EmbPilot for embedded-device inspection, command execution, log capture, and session management over Serial/UART, SSH, or Telnet. Trigger whenever a task mentions a COM port, /dev/tty device, SSH/Telnet target, firmware console, boot log, device shell, or remote embedded debugging.
---

# EmbPilot Device Debugging

Route supported device access through EmbPilot instead of opening raw `ssh`,
`telnet`, or serial clients. Keep connection arguments machine-readable and use
EmbPilot's session history, safety checks, and structured results.

## Workflow

1. Select `connect_serial`, `connect_ssh`, or `connect_telnet`.
2. Pass one JSON object whose fields are the connection settings. Do not encode
   JSON as a string and do not wrap settings in a `config` property.
3. Use `send_command` for device interaction. Set `line_ending` when the target
   requires `lf`, `crlf`, or `cr`; the default is `as-is`. Use `expect_regex`
   and `timeout_ms` for deterministic command completion. Never send an empty
   command with `as-is` or `none`; to send only a blank line, use `lf`, `crlf`,
   or `cr` so EmbPilot writes at least one byte.
4. Inspect `structuredContent` when present. Connection successes include it,
   and runtime failures use `{"ok":false,"error":{...}}`; other successful
   tools may return text. Malformed inputs are MCP invalid-parameter errors and
   should be corrected before retrying.
5. Read `device://live_log` for the active session's current log snapshot.
   It is not a subscription; repeat the read only when the task has a clear
   stop condition. Use history tools for previous sessions and exports.
6. Call `disconnect_device` when finished, including after timeout or error.

To reboot a device, use `send_command` with the command supported by that
device, for example `reboot`, an explicit line ending such as `lf`, and
`confirm_dangerous_command: true` after the user authorizes the operation.

## Connection JSON

Serial:

```json
{"port":"COM3","baudrate":115200}
```

SSH:

```json
{"host":"192.168.1.10","username":"root","key_file":"~/.ssh/id_ed25519"}
```

Telnet:

```json
{"host":"192.168.1.10","port":23}
```

## Safety

- Preserve SSH host-key verification. Pass `known_hosts: null` only when the
  user explicitly accepts that risk.
- Supply `confirm_dangerous_command: true` only after the user authorizes a
  command EmbPilot classifies as dangerous.
- Supply `confirm: true` for session or document deletion only when that
  destructive action is clearly requested.
- Never echo passwords, tokens, or private-key contents into chat or logs.
