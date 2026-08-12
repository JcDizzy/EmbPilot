"""
Shared filesystem helpers for the installer targets: idempotent marker
section upsert/removal and JSON config editing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

Action = Literal[
    "created", "updated", "unchanged", "removed", "not-found", "kept"
]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    tmp.replace(path)


def replace_or_append_marked_section(
    file_path: Path,
    block: str,
    start_marker: str,
    end_marker: str,
) -> Action:
    """Upsert *block* (with its markers) into *file_path*.

    Replaces any existing section between the markers (self-healing stale
    content) or appends at the end. Returns what changed on disk.
    """
    if file_path.exists():
        content = file_path.read_text(encoding="utf-8")
    else:
        content = ""

    start_idx = content.find(start_marker)
    end_idx = content.find(end_marker)
    if start_idx != -1 and end_idx > start_idx:
        if content[start_idx : end_idx + len(end_marker)] == block:
            return "unchanged"
        before = content[:start_idx].rstrip()
        after = content[end_idx + len(end_marker) :].lstrip()
        joined = before + ("\n\n" if before and after else "\n") + block
        if after:
            joined += "\n" + after
        atomic_write(file_path, joined.rstrip() + "\n")
        return "updated"
    if content:
        atomic_write(file_path, content.rstrip() + "\n\n" + block + "\n")
        return "updated"
    atomic_write(file_path, block + "\n")
    return "created"


def remove_marked_section(
    file_path: Path,
    start_marker: str,
    end_marker: str,
) -> Action:
    """Strip the marker section. Deletes the file if it becomes empty."""
    if not file_path.exists():
        return "kept"
    content = file_path.read_text(encoding="utf-8")
    start_idx = content.find(start_marker)
    end_idx = content.find(end_marker)
    if start_idx == -1 or end_idx <= start_idx:
        return "not-found"
    before = content[:start_idx].rstrip()
    after = content[end_idx + len(end_marker) :].lstrip()
    joined = before + ("\n\n" if before and after else "") + after
    if joined.strip() == "":
        file_path.unlink()
        return "removed"
    atomic_write(file_path, joined.rstrip() + "\n")
    return "removed"


def read_json(path: Path) -> dict[str, Any] | None:
    """Return parsed JSON, or None when the file is missing or invalid."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def json_deep_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
