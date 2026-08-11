"""CLI bootstrap and one-shot tool behavior."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, data_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "embpilot"]
    if data_dir is not None:
        argv += ["--data-dir", str(data_dir)]
    argv += list(args)
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


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


def test_help_lists_cli_subcommands() -> None:
    completed = _run_cli("--help")

    assert completed.returncode == 0, completed.stderr
    assert "{tools,tool,shell}" in completed.stdout or "tools" in completed.stdout
    assert "tool" in completed.stdout
    assert "shell" in completed.stdout


def test_tools_lists_every_advertised_tool() -> None:
    completed = _run_cli("tools")

    assert completed.returncode == 0, completed.stderr
    for name in (
        "connect_serial",
        "connect_ssh",
        "connect_telnet",
        "send_command",
        "list_sessions",
        "export_session",
    ):
        assert name in completed.stdout


def test_one_shot_list_sessions_success(tmp_path: Path) -> None:
    completed = _run_cli("tool", "list_sessions", data_dir=tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "Found 0 session(s)." in completed.stdout


def test_one_shot_json_output_prints_structured_result(tmp_path: Path) -> None:
    completed = _run_cli("tool", "list_sessions", "--json-output", data_dir=tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert '"ok": true' in completed.stdout
    assert '"sessions"' in completed.stdout


def test_one_shot_invalid_json_exits_2(tmp_path: Path) -> None:
    completed = _run_cli("tool", "list_sessions", "--json", "{bad", data_dir=tmp_path)

    assert completed.returncode == 2
    assert "invalid JSON" in completed.stderr


def test_one_shot_unknown_tool_exits_2() -> None:
    completed = _run_cli("tool", "bogus_tool")

    assert completed.returncode == 2
    assert "unknown tool 'bogus_tool'" in completed.stderr


def test_one_shot_schema_violation_exits_2(tmp_path: Path) -> None:
    completed = _run_cli(
        "tool",
        "connect_serial",
        "--json",
        '{"port": ""}',
        data_dir=tmp_path,
    )

    assert completed.returncode == 2
    assert "INVALID_ARGUMENT" in completed.stdout or "invalid" in completed.stdout.lower()
def test_shell_accepts_utf8_bom_piped_stdin(tmp_path: Path) -> None:
    """PowerShell pipes native stdin with a UTF-8 BOM; the shell must cope."""
    completed = subprocess.run(
        [sys.executable, "-m", "embpilot", "--data-dir", str(tmp_path), "shell"],
        input=b"\xef\xbb\xbflist_sessions {}\nexit\n",
        check=False,
        capture_output=True,
        cwd=REPO_ROOT,
    )

    out = completed.stdout.decode("utf-8", errors="replace")
    assert completed.returncode == 0, completed.stderr
    assert "unknown tool" not in out
    assert "Found 0 session(s)." in out
