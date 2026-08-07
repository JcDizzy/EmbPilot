# Agent-First Release Contract

## Goal

Prepare EmbPilot 0.1.1 for agents and package users without changing the
runtime transport architecture.

## Requirements

- Replace the ambiguous public `connect_device` MCP tool with
  `connect_serial`, `connect_ssh`, and `connect_telnet`.
- Each connection tool accepts a flat JSON object with protocol-specific
  fields and concrete examples. Encoded JSON strings and a nested `config`
  wrapper are not part of the public contract.
- Keep the internal `SessionManager.connect_device(interface_type, config)`
  seam so drivers and lifecycle code remain centralized.
- Return readable text plus structured connection-success and runtime-error
  envelopes. Keep malformed arguments and unknown tools as MCP protocol errors.
- Add a repository skill that directs agents to use EmbPilot first for Serial,
  SSH, and Telnet tasks and documents the JSON contract and safety rules.
- Synchronize README, contributor guidance, progress, and change log.
- Preserve existing security defaults, especially SSH host-key verification
  and explicit confirmation for dangerous actions.
- Constrain the package to the supported MCP SDK 1.x API and use current SPDX
  package metadata. Version 0.1.1 is required because PyPI already contains the
  immutable 0.1.0 release.

## Verification

Run the complete test suite, validate the repository skill, build both wheel
and source distribution, and run package metadata checks before publication.
