"""
Configuration management for EmbPilot.
Loads config from CLI args / environment variables and computes XDG-compliant paths.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _default_data_dir() -> Path:
    """Return the platform-appropriate XDG data directory for EmbPilot.

    Order of precedence:
      1. $EMBPILOT_DATA_DIR environment variable
      2. XDG platform default
    """
    env = os.environ.get("EMBPILOT_DATA_DIR")
    if env:
        return Path(env)

    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
        return Path(base) / "embpilot"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "embpilot"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        return base / "embpilot"


@dataclass
class EmbPilotConfig:
    """Central configuration for EmbPilot — dual-track database paths.

    All paths are resolved at construction time; directories are created lazily
    by the components that use them.
    """

    # ── Data paths ──────────────────────────────────────────────────
    data_dir: Path = field(default_factory=_default_data_dir)
    main_db_path: Optional[Path] = None    # default: <data_dir>/embpilot_main.db
    session_data_dir: Optional[Path] = None  # default: <data_dir>/sessions/
    lancedb_path: Optional[Path] = None    # default: <data_dir>/lancedb

    # ── Retention (auto-cleanup) ────────────────────────────────────
    retention_days: int = 30       # delete session files older than N days
    retention_max_gb: int = 5      # cap total session directory size (GB)

    # ── Framing ─────────────────────────────────────────────────────
    framing_timeout_ms: int = 50

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Connection defaults ─────────────────────────────────────────
    serial_baudrate_default: int = 115200
    serial_timeout_default: float = 5.0
    network_timeout_default: float = 10.0

    def __post_init__(self) -> None:
        if self.main_db_path is None:
            self.main_db_path = self.data_dir / "embpilot_main.db"
        if self.session_data_dir is None:
            self.session_data_dir = self.data_dir / "sessions"
        if self.lancedb_path is None:
            self.lancedb_path = self.data_dir / "lancedb"

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "EmbPilotConfig":
        """Build a config from parsed CLI arguments."""
        kwargs: dict = {}

        if args.data_dir is not None:
            kwargs["data_dir"] = Path(args.data_dir)
        if args.main_db_path is not None:
            kwargs["main_db_path"] = Path(args.main_db_path)
        if args.session_data_dir is not None:
            kwargs["session_data_dir"] = Path(args.session_data_dir)
        if args.lancedb_path is not None:
            kwargs["lancedb_path"] = Path(args.lancedb_path)
        if args.framing_timeout_ms is not None:
            kwargs["framing_timeout_ms"] = args.framing_timeout_ms
        if args.log_level is not None:
            kwargs["log_level"] = args.log_level
        if args.retention_days is not None:
            kwargs["retention_days"] = args.retention_days
        if args.retention_max_gb is not None:
            kwargs["retention_max_gb"] = args.retention_max_gb

        return cls(**kwargs)

    def ensure_data_dirs(self) -> None:
        """Create data directories if they do not exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.session_data_dir:
            self.session_data_dir.mkdir(parents=True, exist_ok=True)
        if self.lancedb_path:
            self.lancedb_path.mkdir(parents=True, exist_ok=True)
