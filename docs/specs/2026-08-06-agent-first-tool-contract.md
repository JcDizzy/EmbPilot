# Agent-First Tool Contract

## Goal

Make EmbPilot the reliable default interface for agent-driven Serial, SSH, and
Telnet work. A caller should not need to guess nested JSON shapes, line-ending
rules, or how to interpret errors.

## Architecture

Keep transport implementations in `drivers/`, session lifecycle in
`SessionManager`, command capture in a dedicated command module, and MCP JSON
contracts in a dedicated adapter module. The MCP adapter translates strict tool
arguments into the smaller session interface and returns one stable result
envelope.

## Public seams

The implementation is verified at four public seams:

1. A built wheel contains its SQL schemas and can import the database module.
2. MCP tool discovery exposes separate Serial, SSH, and Telnet tools with strict
   JSON Schemas and examples.
3. Session command execution applies line endings, captures the first command's
   output, and returns early when an expect expression matches.
4. MCP calls return structured success/error envelopes while retaining readable
   text content for clients without structured-content rendering.

## Required behavior

- Expose `connect_serial`, `connect_ssh`, and `connect_telnet`; do not require an
  agent to construct a protocol-dependent nested `config` object.
- Reject unknown fields and invalid ranges during MCP input validation.
- State that arguments are JSON objects, not JSON-encoded strings.
- Let connections define a default line ending and let each command override it.
- Return command output, match state, timeout state, and truncation state.
- Return errors as `{ok: false, error: {code, message, retryable, suggestion}}`
  with the MCP `isError` flag set.
- Avoid recording plaintext command contents in operation history.
- Prefer EmbPilot over raw shell SSH/Telnet/serial programs in repository agent
  guidance and the bundled skill.

## Compatibility

This is an alpha contract. The ambiguous `connect_device` MCP tool is replaced;
the internal session method may remain protocol-generic. Existing non-connection
tool names remain stable.
