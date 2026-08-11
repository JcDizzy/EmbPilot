"""Agent harness installation for EmbPilot."""

from embpilot.agent_install.installer import (
    InstallSummary,
    install_targets,
    resolve_target_ids,
    uninstall_targets,
)
from embpilot.agent_install.targets import ALL_TARGETS, InstallContext

__all__ = [
    "ALL_TARGETS",
    "InstallContext",
    "InstallSummary",
    "install_targets",
    "resolve_target_ids",
    "uninstall_targets",
]
