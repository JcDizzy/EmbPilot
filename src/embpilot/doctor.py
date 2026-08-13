"""Environment diagnostics for embpilot (`embpilot doctor`).

Checks the runtime environment and reports what is installed / working, so a
user (or a support session) can see at a glance why something does not run.
Exit code 0 when all required checks pass, 1 otherwise.
"""

from __future__ import annotations

import importlib
import platform
import sqlite3
import sys
from dataclasses import dataclass

from embpilot import __version__

# (display_name, import_path, version_attr)
CORE_DEPS = [
    ("mcp", "mcp", "__version__"),
    ("pyserial-asyncio", "serial_asyncio", "__version__"),
    ("aiosqlite", "aiosqlite", "__version__"),
    ("telnetlib3", "telnetlib3", "__version__"),
    ("asyncssh", "asyncssh", "__version__"),
]
RAG_DEPS = [
    ("fastembed", "fastembed", "__version__"),
    ("lancedb", "lancedb", "__version__"),
]
DRIVERS = [
    "embpilot.drivers.serial_dev",
    "embpilot.drivers.telnet_dev",
    "embpilot.drivers.ssh_dev",
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _check_import(name: str, import_path: str, version_attr: str | None) -> CheckResult:
    try:
        mod = importlib.import_module(import_path)
        version = getattr(mod, version_attr, None) if version_attr else None
        return CheckResult(name, True, f"v{version}" if version else "ok")
    except Exception as exc:  # noqa: BLE001 - report any import failure
        return CheckResult(name, False, f"not importable: {exc}")


def _check_data_dir() -> CheckResult:
    from embpilot.config import EmbPilotConfig

    try:
        config = EmbPilotConfig()
        config.ensure_data_dirs()
        probe = config.data_dir / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult("data dir", True, str(config.data_dir))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("data dir", False, str(exc))


def _check_serial_ports() -> CheckResult:
    try:
        from serial.tools import list_ports

        ports = [p.device for p in list_ports.comports()]
        return CheckResult(
            "serial ports",
            True,
            ", ".join(ports) if ports else "(none detected)",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("serial ports", False, str(exc))


def _print(result: CheckResult, optional: bool = False) -> None:
    if result.ok:
        tag = "OK"
    elif optional:
        tag = "--"
    else:
        tag = "FAIL"
    print(f"  [{tag}] {result.name}: {result.detail}")


def run_doctor() -> int:
    """Run all checks, print a report, return 0 if required checks pass."""
    print(f"embpilot {__version__}")
    print(
        f"python {sys.version.split()[0]} on "
        f"{platform.system()} {platform.machine()}"
    )

    required: list[CheckResult] = []

    print("\n== core dependencies ==")
    for name, path, vattr in CORE_DEPS:
        result = _check_import(name, path, vattr)
        _print(result)
        required.append(result)

    print("\n== optional RAG dependencies (install with embpilot[rag]) ==")
    for name, path, vattr in RAG_DEPS:
        _print(_check_import(name, path, vattr), optional=True)

    print("\n== drivers ==")
    for driver in DRIVERS:
        result = _check_import(driver, driver, None)
        _print(result)
        required.append(result)

    print("\n== storage ==")
    sqlite_result = CheckResult("sqlite3", True, f"v{sqlite3.sqlite_version}")
    _print(sqlite_result)
    data_result = _check_data_dir()
    _print(data_result)
    required.append(data_result)
    _print(_check_serial_ports(), optional=True)

    failed = [result for result in required if not result.ok]
    print()
    if failed:
        print(f"FAIL: {len(failed)} required check(s) failed:")
        for result in failed:
            print(f"  - {result.name}: {result.detail}")
        return 1
    print("OK: all required checks passed")
    return 0
