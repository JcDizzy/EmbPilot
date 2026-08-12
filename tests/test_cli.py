"""CLI bootstrap and one-shot tool behavior."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(
    *args: str,
    data_dir: Path | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "embpilot"]
    if data_dir is not None:
        argv += ["--data-dir", str(data_dir)]
    argv += list(args)
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
        input=input,
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
        encoding="utf-8",
        errors="replace",
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


def test_tools_output_includes_example_lines() -> None:
    completed = _run_cli("tools")

    assert completed.returncode == 0, completed.stderr
    assert "example: {" in completed.stdout
    assert 'example: {"port": "COM3"' in completed.stdout


def test_help_subcommand_shows_schema_and_guidance() -> None:
    completed = _run_cli("help", "connect_serial")

    assert completed.returncode == 0, completed.stderr
    assert "When to use:" in completed.stdout
    assert "Arguments (JSON object):" in completed.stdout
    assert "port" in completed.stdout
    assert "required" in completed.stdout
    assert "Examples:" in completed.stdout


def test_help_unknown_tool_exits_2() -> None:
    completed = _run_cli("help", "bogus_tool")

    assert completed.returncode == 2
    assert "unknown tool 'bogus_tool'" in completed.stderr


def test_schema_flags_reach_the_dispatch_layer(tmp_path: Path) -> None:
    completed = _run_cli(
        "tool",
        "connect_serial",
        "--port",
        "COM9",
        "--baudrate",
        "9600",
        "--json-output",
        data_dir=tmp_path,
    )

    # COM9 does not exist: the point is the flags parsed and reached dispatch.
    assert completed.returncode == 1
    assert '"ok": false' in completed.stdout
    assert "CONNECTION_FAILED" in completed.stdout


def test_schema_flags_reject_bad_enum_values() -> None:
    completed = _run_cli("tool", "connect_serial", "--port", "COM3", "--parity", "Z")

    assert completed.returncode == 2
    assert "invalid choice" in completed.stderr


def test_run_connects_runs_and_disconnects(tmp_path: Path) -> None:
    completed = _run_cli(
        "run",
        "--connect",
        '{"port": "COM9"}',
        "help",
        "version",
        data_dir=tmp_path,
    )

    # fail-fast: the failed connect (COM9 missing) stops the sequence, so
    # only the connect line is emitted and the exit code is 1.
    assert completed.returncode == 1
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "CONNECTION_FAILED" in lines[0]


def test_run_rejects_invalid_connect_json() -> None:
    completed = _run_cli("run", "--connect", "{bad", "help")

    assert completed.returncode == 2
    assert "invalid JSON for --connect" in completed.stderr


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
def test_batch_runs_jsonl_requests_and_prints_one_envelope_per_line(
    tmp_path: Path,
) -> None:
    completed = _run_cli(
        "batch",
        input='{"tool": "list_sessions", "args": {}}\n'
        '{"tool": "list_sessions", "args": {}}\n',
        data_dir=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    assert all('"ok": true' in line for line in lines)


def test_batch_invalid_json_exits_2(tmp_path: Path) -> None:
    completed = _run_cli(
        "batch",
        input='{"tool": "list_sessions"\n',
        data_dir=tmp_path,
    )

    assert completed.returncode == 2
    assert "invalid JSON" in completed.stderr


def test_batch_unknown_tool_exits_2(tmp_path: Path) -> None:
    completed = _run_cli(
        "batch",
        input='{"tool": "bogus"}\n',
        data_dir=tmp_path,
    )

    assert completed.returncode == 2
    assert "unknown tool 'bogus'" in completed.stderr


def test_batch_connect_failure_sets_exit_code_1(tmp_path: Path) -> None:
    completed = _run_cli(
        "batch",
        input='{"tool": "connect_serial", "args": {"port": "COM9"}}\n',
        data_dir=tmp_path,
    )

    assert completed.returncode == 1
    assert '"ok": false' in completed.stdout
    assert "CONNECTION_FAILED" in completed.stdout


def test_batch_fail_fast_stops_at_first_failure(tmp_path: Path) -> None:
    completed = _run_cli(
        "batch",
        "--fail-fast",
        input='{"tool": "send_command", "args": {"command": "x"}}\n'
        '{"tool": "list_sessions", "args": {}}\n',
        data_dir=tmp_path,
    )

    assert completed.returncode == 1
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert '"ok": false' in lines[0]
    assert "NO_ACTIVE_DEVICE" in lines[0]


def _start_serve(data_dir: Path) -> subprocess.Popen:
    argv = [sys.executable, "-m", "embpilot", "--data-dir", str(data_dir), "serve"]
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
    )


def _wait_for_endpoint_file(data_dir: Path, timeout_s: float = 15.0) -> Path:
    path = data_dir / "daemon.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return path
        time.sleep(0.2)
    raise TimeoutError(f"daemon endpoint file never appeared: {path}")


def _stop_serve(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def test_socket_client_round_trip_with_daemon(tmp_path: Path) -> None:
    process = _start_serve(tmp_path)
    try:
        endpoint_file = _wait_for_endpoint_file(tmp_path)
        completed = _run_cli(
            "--socket",
            str(endpoint_file),
            "tool",
            "list_sessions",
            "--json-output",
        )
        assert completed.returncode == 0, completed.stderr
        assert '"ok": true' in completed.stdout
        assert '"sessions"' in completed.stdout
    finally:
        _stop_serve(process)


def test_socket_one_shot_human_readable_and_error_exit(tmp_path: Path) -> None:
    process = _start_serve(tmp_path)
    try:
        endpoint_file = _wait_for_endpoint_file(tmp_path)
        failed = _run_cli(
            "--socket",
            str(endpoint_file),
            "tool",
            "send_command",
            "--json",
            '{"command": "x"}',
        )
        assert failed.returncode == 1
        assert "NO_ACTIVE_DEVICE" in failed.stdout
        assert "suggestion" in failed.stdout
    finally:
        _stop_serve(process)


def test_socket_batch_forwards_requests_to_daemon(tmp_path: Path) -> None:
    process = _start_serve(tmp_path)
    try:
        endpoint_file = _wait_for_endpoint_file(tmp_path)
        completed = _run_cli(
            "--socket",
            str(endpoint_file),
            "batch",
            input='{"tool": "list_sessions", "args": {}}\n'
            '{"tool": "send_command", "args": {"command": "x"}}\n',
        )
        assert completed.returncode == 1  # send_command fails without a connection
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        assert len(lines) == 2
        assert '"ok": true' in lines[0]
        assert '"ok": false' in lines[1]
        assert "NO_ACTIVE_DEVICE" in lines[1]
    finally:
        _stop_serve(process)


def test_socket_unknown_endpoint_file_exits_2(tmp_path: Path) -> None:
    completed = _run_cli(
        "--socket",
        str(tmp_path / "missing.json"),
        "tool",
        "list_sessions",
    )

    assert completed.returncode == 2
    assert "cannot read daemon endpoint file" in completed.stderr


def test_socket_dead_daemon_fails_cleanly_without_traceback(tmp_path: Path) -> None:
    """A reachable endpoint file but a dead daemon must not traceback."""
    endpoint_file = tmp_path / "daemon.json"
    endpoint_file.write_text(
        '{"endpoint": "tcp:127.0.0.1:59999"}', encoding="utf-8"
    )
    completed = _run_cli(
        "--socket",
        str(endpoint_file),
        "tool",
        "list_sessions",
    )

    assert completed.returncode == 1
    assert "cannot reach daemon" in completed.stderr
    assert "Traceback" not in completed.stderr


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


def test_shell_help_tool_shows_tool_help(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "embpilot", "--data-dir", str(tmp_path), "shell"],
        input="help read_output\nexit\n".encode("utf-8"),
        check=False,
        capture_output=True,
        cwd=REPO_ROOT,
    )

    out = completed.stdout.decode("utf-8", errors="replace")
    assert completed.returncode == 0, completed.stderr
    assert "read_output" in out
    assert "Arguments (JSON object):" in out


def test_tool_help_flag_shows_tool_help(tmp_path: Path) -> None:
    completed = _run_cli("tool", "send_command", "--help")

    assert completed.returncode == 0, completed.stderr
    assert "Arguments (JSON object):" in completed.stdout
    assert "expect_regex" in completed.stdout


def _read_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if pid_file.exists():
            return int(pid_file.read_text(encoding="utf-8").strip())
        time.sleep(0.2)
    raise TimeoutError(f"pid file never appeared: {pid_file}")


def _kill_pid(pid: int) -> None:
    try:
        import signal

        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def test_serve_daemon_detaches_and_becomes_ready(tmp_path: Path) -> None:
    """serve --daemon spawns a detached process, writes pid + endpoint files,
    and the parent exits 0 once ready; the daemon answers --socket calls."""
    completed = _run_cli("serve", "--daemon", data_dir=tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "daemon started" in completed.stdout
    endpoint_file = tmp_path / "daemon.json"
    assert endpoint_file.exists()
    pid = _read_pid(tmp_path / "daemon.pid")

    try:
        # The detached daemon is reachable via --socket.
        call = _run_cli(
            "--socket", str(endpoint_file), "tool", "list_sessions", "--json-output"
        )
        assert call.returncode == 0, call.stderr
        assert '"ok": true' in call.stdout
    finally:
        _kill_pid(pid)
        time.sleep(1)


def test_serve_daemon_idempotent_when_already_running(tmp_path: Path) -> None:
    """A second --daemon must NOT kill the running daemon (regression: the
    Windows pid probe used os.kill, which is TerminateProcess there)."""
    first = _run_cli("serve", "--daemon", data_dir=tmp_path)
    assert first.returncode == 0, first.stderr
    pid = _read_pid(tmp_path / "daemon.pid")
    try:
        second = _run_cli("serve", "--daemon", data_dir=tmp_path)
        assert second.returncode == 0, second.stderr
        assert "already running" in second.stdout

        # The first daemon must still be alive and reachable.
        from embpilot.cli import _pid_alive

        assert _pid_alive(pid), "idempotent re-run killed the running daemon"
        call = _run_cli(
            "--socket",
            str(tmp_path / "daemon.json"),
            "tool",
            "list_sessions",
            "--json-output",
        )
        assert call.returncode == 0, call.stderr
        assert '"ok": true' in call.stdout
    finally:
        _kill_pid(pid)
        time.sleep(1)


def test_serve_daemon_writes_log_file(tmp_path: Path) -> None:
    completed = _run_cli("serve", "--daemon", data_dir=tmp_path)
    assert completed.returncode == 0, completed.stderr
    pid = _read_pid(tmp_path / "daemon.pid")
    try:
        assert (tmp_path / "serve.log").exists()
    finally:
        _kill_pid(pid)
        time.sleep(1)


def test_pid_alive_is_probe_only_on_windows() -> None:
    """The Windows pid probe must never kill the target process."""
    import subprocess

    from embpilot.cli import _pid_alive

    if sys.platform != "win32":
        return  # POSIX os.kill(pid, 0) is already a pure probe

    code = "import os, time; print(os.getpid(), flush=True); time.sleep(20)"
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        child = int(proc.stdout.readline().strip())
        assert _pid_alive(child) is True
        # The probe must not have terminated the child.
        assert proc.poll() is None, "pid probe killed the target process"
    finally:
        _kill_pid(proc.pid)
        proc.wait(timeout=10)
