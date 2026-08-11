"""CLI surface for interactive and scripted agent harness installation."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from embpilot.agent_install.installer import (
    InstallSummary,
    install_targets,
    resolve_target_ids,
    uninstall_targets,
)
from embpilot.agent_install.targets import ALL_TARGETS, InstallContext, Location


def add_agent_subcommands(subparsers: argparse._SubParsersAction) -> None:
    install = subparsers.add_parser(
        "install",
        help="Install EmbPilot into supported agent harnesses",
    )
    uninstall = subparsers.add_parser(
        "uninstall",
        help="Remove EmbPilot from supported agent harnesses",
    )
    for parser in (install, uninstall):
        parser.add_argument(
            "--target",
            help="Comma-separated targets, or auto/all/none",
        )
        parser.add_argument(
            "--location",
            choices=("global", "local"),
            help="Configure all projects or only --project-dir",
        )
        parser.add_argument(
            "--project-dir",
            type=Path,
            default=Path.cwd(),
            help="Project directory for local configuration (default: current directory)",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Use defaults without prompting",
        )
        parser.add_argument(
            "--server-command",
            help=argparse.SUPPRESS,
        )


def run_agent_command(args: argparse.Namespace) -> InstallSummary:
    server_command = args.server_command or _resolve_server_command()
    context = InstallContext.current(args.project_dir.resolve(), server_command)
    selection = args.target
    location = args.location

    if args.yes:
        selection = selection or ("all" if args.command == "uninstall" else "auto")
        location = location or "global"
    else:
        if selection is None:
            selection = _prompt_targets(context, uninstall=args.command == "uninstall")
        if location is None:
            selected_for_prompt = _selection_without_auto(selection)
            location = _prompt_location(selected_for_prompt)

    location = location or "global"
    target_ids = resolve_target_ids(
        selection or "none",
        context=context,
        location=location,
    )
    operation = uninstall_targets if args.command == "uninstall" else install_targets
    summary = operation(context, target_ids, location)
    _print_summary(summary)
    return summary


def _resolve_server_command() -> str:
    launcher = Path(sys.argv[0])
    if launcher.exists() and launcher.stem.lower() == "embpilot":
        return str(launcher.resolve())
    return shutil.which("embpilot") or "embpilot"


def _prompt_targets(context: InstallContext, *, uninstall: bool) -> str:
    detected = [
        target.id
        for target in ALL_TARGETS
        if target.detect(context, "global")
    ]
    print("EmbPilot agent setup")
    print("Select agent harnesses:")
    for index, target in enumerate(ALL_TARGETS, start=1):
        marker = "detected" if target.id in detected else "not detected"
        print(f"  {index}. {target.display_name} [{marker}]")
    default_ids = [target.id for target in ALL_TARGETS] if uninstall else detected
    if not default_ids:
        default_ids = ["claude"]
    default = ",".join(default_ids)
    answer = input(f"Targets (numbers/ids, comma separated) [{default}]: ").strip()
    if not answer:
        return default
    number_map = {str(index): target.id for index, target in enumerate(ALL_TARGETS, start=1)}
    values = [number_map.get(part.strip(), part.strip()) for part in answer.split(",")]
    return ",".join(values)


def _selection_without_auto(selection: str) -> tuple[str, ...]:
    normalized = selection.strip().lower()
    if normalized == "all":
        return tuple(target.id for target in ALL_TARGETS)
    if normalized in {"auto", "none", ""}:
        return ()
    return tuple(part.strip() for part in normalized.split(",") if part.strip())


def _prompt_location(selected: tuple[str, ...]) -> Location:
    if selected and all(
        not next(target for target in ALL_TARGETS if target.id == target_id).supports_location("local")
        for target_id in selected
    ):
        print("Selected harnesses support global configuration only.")
        return "global"
    answer = input("Install for all projects or this project? [global/local] (global): ").strip().lower()
    if answer in {"", "g", "global"}:
        return "global"
    if answer in {"l", "local"}:
        return "local"
    raise ValueError("Location must be 'global' or 'local'")


def _print_summary(summary: InstallSummary) -> None:
    for report in summary.reports:
        for file in report.files:
            print(f"{report.display_name}: {file.action} {file.path}")
        for note in report.notes:
            print(f"{report.display_name}: {note}")
    for note in summary.skipped:
        print(f"Skipped: {note}")
    if not summary.reports and not summary.skipped:
        print("No agent targets selected; nothing to do.")
    elif summary.changed:
        print("Restart configured agent harnesses to apply the changes.")
