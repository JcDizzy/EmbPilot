"""Installer orchestration across agent harness adapters."""

from __future__ import annotations

from dataclasses import dataclass

from embpilot.agent_install.targets import (
    ALL_TARGETS,
    TARGETS_BY_ID,
    InstallContext,
    Location,
    TargetReport,
)


@dataclass(frozen=True)
class InstallSummary:
    reports: tuple[TargetReport, ...]
    skipped: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return any(
            file.action in {"created", "updated", "removed"}
            for report in self.reports
            for file in report.files
        )


def resolve_target_ids(
    selection: str,
    *,
    context: InstallContext,
    location: Location,
) -> tuple[str, ...]:
    normalized = selection.strip().lower()
    if normalized == "all":
        return tuple(target.id for target in ALL_TARGETS)
    if normalized == "auto":
        return tuple(
            target.id
            for target in ALL_TARGETS
            if target.supports_location(location) and target.detect(context, location)
        )
    if normalized == "none" or not normalized:
        return ()
    aliases = {"claudecode": "claude", "claude-code": "claude", "open-code": "opencode"}
    ids = tuple(
        dict.fromkeys(
            aliases.get(part.strip(), part.strip())
            for part in normalized.split(",")
            if part.strip()
        )
    )
    unknown = [target_id for target_id in ids if target_id not in TARGETS_BY_ID]
    if unknown:
        raise ValueError(
            f"Unknown target(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(TARGETS_BY_ID)}"
        )
    return ids


def install_targets(
    context: InstallContext,
    target_ids: tuple[str, ...],
    location: Location,
) -> InstallSummary:
    return _run(context, target_ids, location, uninstall=False)


def uninstall_targets(
    context: InstallContext,
    target_ids: tuple[str, ...],
    location: Location,
) -> InstallSummary:
    return _run(context, target_ids, location, uninstall=True)


def _run(
    context: InstallContext,
    target_ids: tuple[str, ...],
    location: Location,
    *,
    uninstall: bool,
) -> InstallSummary:
    reports: list[TargetReport] = []
    skipped: list[str] = []
    for target_id in target_ids:
        target = TARGETS_BY_ID[target_id]
        if not target.supports_location(location):
            skipped.append(
                f"{target.display_name}: {location} configuration is not supported"
            )
            continue
        operation = target.uninstall if uninstall else target.install
        reports.append(operation(context, location))
    return InstallSummary(tuple(reports), tuple(skipped))
