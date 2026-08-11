"""Owned, idempotent edits for agent instruction and MCP config files."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

FileAction = Literal["created", "updated", "unchanged", "removed", "not-found"]


@dataclass(frozen=True)
class FileResult:
    path: Path
    action: FileAction


def write_utf8_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(normalized)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def upsert_owned_file(path: Path, content: str) -> FileResult:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    normalized = content if content.endswith("\n") else content + "\n"
    if existing == normalized:
        return FileResult(path, "unchanged")
    write_utf8_atomic(path, normalized)
    return FileResult(path, "updated" if existing else "created")


def remove_owned_file(path: Path, content: str) -> FileResult:
    if not path.exists():
        return FileResult(path, "not-found")
    expected = content if content.endswith("\n") else content + "\n"
    if path.read_text(encoding="utf-8") != expected:
        return FileResult(path, "unchanged")
    path.unlink()
    return FileResult(path, "removed")


def upsert_marked_section(
    path: Path,
    *,
    start_marker: str,
    end_marker: str,
    block: str,
) -> FileResult:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    start = existing.find(start_marker)
    end = existing.find(end_marker)
    if (start >= 0) != (end >= 0) or (start >= 0 and end < start):
        raise ValueError(f"Incomplete EmbPilot marker block in {path}")
    if start >= 0:
        end += len(end_marker)
        prefix = existing[:start].rstrip("\r\n")
        suffix = existing[end:].lstrip("\r\n")
        updated = (prefix + "\n\n" if prefix else "") + block
        if suffix:
            updated += "\n\n" + suffix
        else:
            updated += "\n"
    else:
        prefix = existing.rstrip("\r\n")
        updated = (prefix + "\n\n" if prefix else "") + block + "\n"
    if updated == existing:
        return FileResult(path, "unchanged")
    write_utf8_atomic(path, updated)
    return FileResult(path, "updated" if existing else "created")


def remove_marked_section(
    path: Path,
    *,
    start_marker: str,
    end_marker: str,
) -> FileResult:
    if not path.exists():
        return FileResult(path, "not-found")
    existing = path.read_text(encoding="utf-8")
    start = existing.find(start_marker)
    end = existing.find(end_marker)
    if start < 0 and end < 0:
        return FileResult(path, "not-found")
    if start < 0 or end < start:
        raise ValueError(f"Incomplete EmbPilot marker block in {path}")
    end += len(end_marker)
    prefix = existing[:start].rstrip("\r\n")
    suffix = existing[end:].lstrip("\r\n")
    updated = prefix + ("\n\n" if prefix and suffix else "") + suffix
    if updated:
        write_utf8_atomic(path, updated)
    else:
        path.unlink()
    return FileResult(path, "removed")


def upsert_json_value(path: Path, keys: tuple[str, ...], value: Any) -> FileResult:
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = _load_json_object(path, existing_text)
    parent = data
    for key in keys[:-1]:
        child = parent.setdefault(key, {})
        if not isinstance(child, dict):
            raise ValueError(f"JSON field {'.'.join(keys[:-1])!r} must be an object: {path}")
        parent = child
    if parent.get(keys[-1]) == value:
        return FileResult(path, "unchanged")
    parent[keys[-1]] = value
    write_utf8_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
    return FileResult(path, "updated" if existing_text else "created")


def remove_json_value(path: Path, keys: tuple[str, ...]) -> FileResult:
    if not path.exists():
        return FileResult(path, "not-found")
    existing_text = path.read_text(encoding="utf-8")
    data = _load_json_object(path, existing_text)
    parents: list[tuple[dict[str, Any], str]] = []
    parent = data
    for key in keys[:-1]:
        child = parent.get(key)
        if not isinstance(child, dict):
            return FileResult(path, "not-found")
        parents.append((parent, key))
        parent = child
    if keys[-1] not in parent:
        return FileResult(path, "not-found")
    del parent[keys[-1]]
    for container, key in reversed(parents):
        child = container.get(key)
        if isinstance(child, dict) and not child:
            del container[key]
        else:
            break
    write_utf8_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
    return FileResult(path, "removed")


def _load_json_object(path: Path, text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    if _has_jsonc_comments(text):
        raise ValueError(
            f"Comments in {path} cannot yet be edited safely; use a comment-free JSON config"
        )
    try:
        parsed = json.loads(_remove_trailing_commas(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON configuration: {path}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"JSON configuration root must be an object: {path}")
    return parsed


def _has_jsonc_comments(text: str) -> bool:
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "/" and index + 1 < len(text) and text[index + 1] in {"/", "*"}:
            return True
        index += 1
    return False


def _remove_trailing_commas(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def upsert_toml_table(path: Path, header: str, values: dict[str, Any]) -> FileResult:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _render_toml_table(header, values)
    updated, found = _replace_toml_table(existing, header, block)
    if updated == existing:
        return FileResult(path, "unchanged")
    write_utf8_atomic(path, updated)
    return FileResult(path, "updated" if existing else "created")


def remove_toml_table(path: Path, header: str) -> FileResult:
    if not path.exists():
        return FileResult(path, "not-found")
    existing = path.read_text(encoding="utf-8")
    updated, found = _replace_toml_table(existing, header, None)
    if not found:
        return FileResult(path, "not-found")
    if updated.strip():
        write_utf8_atomic(path, updated)
    else:
        path.unlink()
    return FileResult(path, "removed")


def _replace_toml_table(
    text: str,
    header: str,
    replacement: str | None,
) -> tuple[str, bool]:
    pattern = re.compile(rf"(?m)^\s*\[{re.escape(header)}\]\s*$")
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise ValueError(f"Duplicate TOML table [{header}]")
    if not matches:
        if replacement is None:
            return text, False
        prefix = text.rstrip("\r\n")
        return (prefix + "\n\n" if prefix else "") + replacement + "\n", False
    start = matches[0].start()
    next_header = re.search(r"(?m)^\s*\[[^\n]+\]\s*$", text[matches[0].end() :])
    end = matches[0].end() + (next_header.start() if next_header else len(text) - matches[0].end())
    prefix = text[:start].rstrip("\r\n")
    suffix = text[end:].lstrip("\r\n")
    parts = [part for part in (prefix, replacement, suffix) if part]
    return "\n\n".join(parts) + ("\n" if parts else ""), True


def _render_toml_table(header: str, values: dict[str, Any]) -> str:
    lines = [f"[{header}]"]
    for key, value in values.items():
        if isinstance(value, str):
            rendered = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            rendered = "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in value) + "]"
        else:
            raise TypeError(f"Unsupported TOML value for {key}")
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines)
