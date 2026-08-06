"""CLI bootstrap behavior."""

from __future__ import annotations

import subprocess
import sys


def test_help_does_not_import_server_runtime() -> None:
    script = (
        "import runpy, sys; "
        "sys.modules['embpilot.server'] = None; "
        "sys.argv = ['embpilot', '--help']; "
        "runpy.run_module('embpilot.__main__', run_name='__main__')"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Embedded Debugging MCP Server" in completed.stdout
