from __future__ import annotations

import re
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "password",
    "passwd",
    "token",
    "secret",
    "private_key",
    "client_key",
    "client_keys",
    "key_file",
}

_DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"^\s*rm\s+.*(?:-r|-R|--recursive).*(?:\s/|\s\*)"),
    re.compile(r"^\s*dd\s+.*\bof="),
    re.compile(r"^\s*mkfs(?:\.|\s)"),
    re.compile(r"^\s*format(?:\s|$)", re.IGNORECASE),
    re.compile(r"^\s*(?:shutdown|poweroff|halt|reboot)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^\s*(?:flash_erase|erase_flash|factory_reset)(?:\s|$)", re.IGNORECASE),
]

_COMMAND_REDACTIONS = [
    (
        re.compile(r'(?i)(AT\+CWJAP="[^"]*",)"[^"]*"'),
        r'\1"***REDACTED***"',
    ),
    (
        re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+"),
        r"\1 ***REDACTED***",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|token|secret)\s*([:=])\s*"
            r"(\"[^\"]*\"|'[^']*'|[^\s;]+)"
        ),
        r"\1\2***REDACTED***",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|token|secret)(\s+)"
            r"(\"[^\"]*\"|'[^']*'|[^\s;]+)"
        ),
        r"\1\2***REDACTED***",
    ),
    (
        re.compile(r"(?i)\b(authorization)\s*([:=])\s*[^\r\n;]+"),
        r"\1\2***REDACTED***",
    ),
]


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS:
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def redact_command_text(command: str) -> str:
    redacted = command
    for pattern, replacement in _COMMAND_REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def is_dangerous_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in _DANGEROUS_COMMAND_PATTERNS)


def ensure_path_within(path: Path, root: Path) -> Path:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if resolved_path == resolved_root or resolved_root in resolved_path.parents:
        return resolved_path
    raise ValueError(f"Refusing to operate outside managed session directory: {path}")
