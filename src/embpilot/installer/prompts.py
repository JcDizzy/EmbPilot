"""
Interactive prompts for ``embpilot install`` / ``uninstall``.

Zero-dependency implementation on top of ``input()``, modeled on CodeGraph's
clack-based installer: harnesses are detected and pre-checked, the user picks
targets and scope, and nothing is written before an explicit confirmation of
the file list. Every prompt takes a *read_line* callable so tests can drive
the flow without a terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

from embpilot.installer.targets import (
    AgentTarget,
    DetectionResult,
    FileChange,
    Location,
)


def make_input_reader(stream: TextIO) -> Callable[[str], str]:
    """Default interactive reader: print a prompt, read one line."""

    def read_line(prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return ""

    return read_line


def select_targets(
    detections: list[tuple[AgentTarget, DetectionResult]],
    *,
    read_line: Callable[[str], str],
    action: str,
) -> list[AgentTarget]:
    """Multi-select detected targets by number.

    Empty input selects everything detected as installed.
    """
    print("EmbPilot installer - detected agent harnesses:")
    checked: list[AgentTarget] = []
    for index, (target, detection) in enumerate(detections, start=1):
        state = []
        if detection.installed:
            state.append("installed")
        if detection.already_configured:
            state.append("configured")
        marker = "[x]" if detection.installed else "[ ]"
        state_text = f" ({', '.join(state)})" if state else ""
        print(f"  {marker} {index}. {target.display_name} ({target.id}){state_text}")
        if detection.installed:
            checked.append(target)

    prompt = (
        f"Select targets to {action} (numbers, comma separated; "
        "empty = checked above): "
    )
    while True:
        raw = read_line(prompt).strip()
        if not raw:
            return checked
        try:
            indices = [int(part.strip()) for part in raw.split(",")]
        except ValueError:
            print("  invalid input — enter numbers separated by commas")
            continue
        invalid = [i for i in indices if not 1 <= i <= len(detections)]
        if invalid:
            print(f"  out of range: {invalid}")
            continue
        return [detections[i - 1][0] for i in indices]


def select_location(
    targets: list[AgentTarget],
    *,
    read_line: Callable[[str], str],
) -> Location:
    """Ask for global/local; empty input defaults to local."""
    scopes: list[str] = []
    for target in targets:
        supports = [
            scope for scope in ("global", "local") if target.supports_location(scope)
        ]
        scopes.append(f"{target.id} ({'/'.join(supports)})")
    print("  scope support: " + ", ".join(scopes))
    while True:
        raw = read_line("Scope [local/global] (empty = local): ").strip().lower()
        if raw in ("", "local"):
            return "local"
        if raw == "global":
            return "global"
        print("  invalid scope — enter 'local' or 'global'")


def confirm_changes(
    changes: list[FileChange],
    *,
    read_line: Callable[[str], str],
    action: str,
) -> bool:
    """Show the exact file list and ask for confirmation."""
    print(f"Will {action} these files:")
    for change in changes:
        print(f"  {change}")
    while True:
        raw = read_line("Proceed? [y/N]: ").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("", "n", "no"):
            return False
        print("  invalid input — enter y or N")
