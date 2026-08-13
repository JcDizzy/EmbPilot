"""
Daemon lifecycle for ``embpilot serve --daemon``: detached subprocess
management, pid probing, endpoint validation and reachability checks.

Kept separate from cli.py so the daemon topic (Windows redirector stubs,
side-effect-free pid probes, readiness probing) is testable in isolation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from embpilot.config import EmbPilotConfig

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

def _validate_socket_endpoint(endpoint: str) -> str:
    """Reject non-loopback TCP endpoints: the daemon is a local-only service."""
    if not endpoint.startswith("tcp:"):
        return endpoint
    host = endpoint[len("tcp:") :].rsplit(":", 1)[0]
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing non-loopback daemon endpoint '{endpoint}': "
            "bind 127.0.0.1 (or use unix:PATH on POSIX)"
        )
    return endpoint




def _daemon_argv_and_env(config: EmbPilotConfig, endpoint: str | None):
    """Build the detached child argv + env for ``serve --daemon``.

    Uses the same interpreter as the current process (sys.executable), so the
    child always runs this exact environment and codebase. Anaconda venv
    python.exe is a redirector stub (CreateProcess returns the stub pid while
    the real interpreter runs under another pid); the stub chain survives the
    parent's exit, which is all that matters here.
    """
    argv = [sys.executable, "-m", "embpilot"]
    if args_data_dir_flag(config):
        argv += ["--data-dir", str(config.data_dir)]
    argv += ["serve"]
    if endpoint:
        argv += ["--socket", endpoint]
    return argv, os.environ.copy()




def _run_serve_daemon(config: EmbPilotConfig, endpoint: str | None) -> int:
    """Implement ``serve --daemon``: detach a serve subprocess and wait for
    it to become ready (endpoint file written). Returns the exit code.
    """
    import subprocess
    import time

    config.ensure_data_dirs()
    pid_file = config.data_dir / "daemon.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid and _pid_alive(old_pid):
            print(f"daemon already running (pid {old_pid}); endpoint: "
                  f"{config.data_dir / 'daemon.json'}")
            return 0
        pid_file.unlink(missing_ok=True)

    log_path = config.data_dir / "serve.log"
    log_file = open(log_path, "ab", buffering=0)
    argv, env = _daemon_argv_and_env(config, endpoint)

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": log_file,
        "close_fds": True,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        log_file.close()
        print(f"error: failed to start daemon: {exc}", file=sys.stderr)
        return 1

    # The detached serve process writes daemon.pid itself; wait until the
    # endpoint file exists AND the endpoint actually accepts connections
    # (on Anaconda venvs the Popen pid is a redirector stub whose lifetime
    # differs from the real interpreter, so file presence + connectability
    # is the reliable readiness signal).
    endpoint_file = config.data_dir / "daemon.json"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if endpoint_file.exists() and pid_file.exists():
            try:
                import json as _json

                info = _json.loads(endpoint_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = None
            if info and info.get("endpoint") and _endpoint_reachable(info["endpoint"]):
                log_file.close()
                # proc.pid can be a redirector stub on Anaconda venvs; the
                # authoritative pid is what the serve process wrote itself.
                try:
                    real_pid = int(pid_file.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    real_pid = proc.pid
                print(f"daemon started (pid {real_pid})")
                print(f"endpoint: {info['endpoint']}")
                print(f"endpoint file: {endpoint_file}")
                print(f"log file: {log_path}")
                return 0
        if proc.poll() is not None:
            log_file.close()
            print(
                f"error: daemon exited during startup (code {proc.returncode}); "
                f"see {log_path}",
                file=sys.stderr,
            )
            return 1
        time.sleep(0.2)
    log_file.close()
    print("error: daemon did not become ready within 10s; see " + str(log_path),
          file=sys.stderr)
    return 1




def _endpoint_reachable(endpoint: str) -> bool:
    """Probe whether a daemon endpoint accepts connections (no protocol I/O)."""
    import socket

    try:
        if endpoint.startswith("unix:"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(endpoint[len("unix:") :])
            sock.close()
            return True
        host, _, port = endpoint[len("tcp:") :].rpartition(":")
        with socket.create_connection((host, int(port)), timeout=1.0):
            return True
    except OSError:
        return False
    return False




def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check.

    On Windows, ``os.kill`` is NEVER a probe: CPython implements it with
    ``TerminateProcess`` for every signal (sig=0 included), so probing with
    it would kill the daemon. Use OpenProcess + GetExitCodeProcess instead.
    """
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but is not owned by us.
        return True
    except OSError:
        return False




def _win_pid_alive(pid: int) -> bool:
    """Side-effect-free liveness probe for Windows."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)




def args_data_dir_flag(config: EmbPilotConfig) -> bool:
    """Whether the data dir differs from the platform default (so the
    detached child inherits it explicitly)."""
    from embpilot.config import _default_data_dir

    return config.data_dir != _default_data_dir()

