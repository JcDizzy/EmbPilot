"""
Installer orchestrator: ``embpilot install`` / ``uninstall`` / ``--check`` /
``--print-config`` across the agent targets (claude, zcode, opencode, codex,
pi, agents, dsh).

Modeled on CodeGraph's installer: marker-fenced instructions blocks are
upserted into each harness's CLAUDE.md / AGENTS.md, MCP entries are merged
into the harness config, and the pi/dsh targets additionally install the
device-debugging skill. Uninstall removes only what install wrote.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from embpilot.installer.targets import (
    ALL_TARGETS,
    AgentTarget,
    DetectionResult,
    FileChange,
    Location,
    detect_all,
    get_target,
    resolve_target_flag,
)


def _run_interactive(
    *,
    mutator: Callable[[AgentTarget, Location], list[FileChange]],
    action: str,
    read_line: Callable[[str], str] | None = None,
) -> list[str]:
    """Shared interactive flow: detect -> pick targets/scope -> preview ->
    confirm -> mutate. Cancellations (q / Ctrl+C / EOF) write nothing."""
    from embpilot.installer.prompts import (
        Cancelled,
        confirm_changes,
        make_input_reader,
        select_location,
        select_targets,
    )

    if read_line is None:
        import sys

        read_line = make_input_reader(sys.stdin)
    loc: Location = "local"
    try:
        targets = select_targets(detect_all(loc), read_line=read_line, action=action)
        if not targets:
            return ["no targets selected - nothing to do"]
        loc = select_location(targets, read_line=read_line)

        # Preview the file list WITHOUT writing anything; confirm first.
        preview: list[FileChange] = []
        for target in targets:
            if not target.supports_location(loc):
                preview.append(
                    FileChange(
                        Path("."),
                        "kept",
                        note=f"{target.id} unsupported at {loc}, skipped",
                    )
                )
                continue
            for path_str in target.describe_paths(loc):
                preview.append(FileChange(Path(path_str), "pending"))
        if not confirm_changes(preview, read_line=read_line, action=action):
            return ["cancelled - nothing written"]
    except (KeyboardInterrupt, EOFError, Cancelled):
        return ["cancelled - nothing written"]

    changes: list[FileChange] = []
    skipped: list[str] = []
    for target in targets:
        if not target.supports_location(loc):
            skipped.append(target.id)
            continue
        changes.extend(mutator(target, loc))
    verb = "removed EmbPilot from" if action == "uninstall" else "installed into"
    lines = [f"{verb} {len(targets)} target(s) at {loc} scope:"]
    for target_id in skipped:
        lines.append(f"  - {target_id}: unsupported at {loc}, skipped")
    lines.extend(f"  - {change}" for change in changes)
    return lines


def run_interactive_install(
    *,
    read_line: Callable[[str], str] | None = None,
) -> list[str]:
    """Detect harnesses, let the user pick targets/scope, then install."""
    return _run_interactive(
        mutator=lambda target, loc: target.install(loc),
        action="install",
        read_line=read_line,
    )


def run_interactive_uninstall(
    *,
    read_line: Callable[[str], str] | None = None,
) -> list[str]:
    """Detect harnesses, let the user pick targets/scope, then uninstall."""
    return _run_interactive(
        mutator=lambda target, loc: target.uninstall(loc),
        action="uninstall",
        read_line=read_line,
    )




def run_install(
    *,
    target: str = "auto",
    location: str = "local",
) -> list[str]:
    """Install EmbPilot into the selected harnesses.

    Returns one log line per file touched (idempotent re-runs report
    ``unchanged``).
    """
    loc = _location(location)
    targets = resolve_target_flag(target, loc)
    if not targets:
        return [
            "no target harnesses selected or detected "
            "(try --target all, or --target claude,pi,agents)"
        ]
    lines = [f"installing into {len(targets)} target(s) at {loc} scope:"]
    for item in targets:
        if not item.supports_location(loc):
            lines.append(f"  - {item.display_name}: unsupported at {loc} scope, skipped")
            continue
        for change in item.install(loc):
            lines.append(f"  - {change}")
    return lines


def run_uninstall(*, target: str = "auto", location: str = "local") -> list[str]:
    """Remove EmbPilot wiring from the selected harnesses."""
    loc = _location(location)
    targets = resolve_target_flag(target, loc)
    if not targets:
        return ["no target harnesses selected or detected"]
    lines = [f"removing EmbPilot from {len(targets)} target(s) at {loc} scope:"]
    for item in targets:
        if not item.supports_location(loc):
            lines.append(f"  - {item.id}: unsupported at {loc}, skipped")
            continue
        for change in item.uninstall(loc):
            lines.append(f"  - {change}")
    return lines


def run_check(*, target: str = "auto", location: str = "local") -> tuple[list[str], int]:
    """Report installation state per target. Exit 0 when all selected are
    configured, 1 otherwise."""
    loc = _location(location)
    targets = resolve_target_flag(target, loc)
    lines: list[str] = []
    all_configured = True
    for item in targets:
        detection = item.detect(loc)
        state = "configured" if detection.already_configured else "not configured"
        if not detection.already_configured:
            all_configured = False
        lines.append(
            f"{item.id}: {state}"
            + (f" ({detection.config_path})" if detection.config_path else "")
        )
    if not targets:
        lines.append("no target harnesses selected or detected")
        all_configured = False
    return lines, 0 if all_configured else 1


def run_print_config(target_id: str) -> str:
    """Print the manual config snippet for one target; touches nothing."""
    target = get_target(target_id)
    if target is None:
        raise ValueError(
            f"unknown target '{target_id}' (choose from "
            + ", ".join(t.id for t in ALL_TARGETS)
            + ")"
        )
    return target.print_config()


def describe_targets() -> str:
    """Human-readable target list for help output."""
    return ", ".join(f"{t.id} ({t.display_name})" for t in ALL_TARGETS)


def _location(value: str) -> Location:
    if value not in ("global", "local"):
        raise ValueError("--location must be 'global' or 'local'")
    return value  # type: ignore[return-value]
