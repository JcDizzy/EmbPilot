from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_build_parser_includes_data_dir_flag() -> None:
    from embpilot.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["--data-dir", "tmp-data"])

    assert "--data-dir" in parser._option_string_actions
    assert args.data_dir == "tmp-data"


def test_main_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    from embpilot import __version__
    from embpilot.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"embpilot {__version__}"


def test_python_module_entrypoint_prints_version() -> None:
    from embpilot import __version__

    repo_root = Path(__file__).resolve().parents[2]
    src_path = str(repo_root / "src")
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_path
        if not existing_pythonpath
        else os.pathsep.join([src_path, existing_pythonpath])
    )

    result = subprocess.run(
        [sys.executable, "-m", "embpilot", "--version"],
        capture_output=True,
        check=False,
        cwd=repo_root,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"embpilot {__version__}"


def test_installed_console_script_prints_version(tmp_path: Path) -> None:
    from embpilot import __version__

    repo_root = Path(__file__).resolve().parents[2]
    venv_dir = tmp_path / "venv"
    if sys.version_info >= (3, 11):
        interpreter_cmd = [sys.executable]
    elif os.name == "nt" and shutil.which("py"):
        interpreter_cmd = ["py", "-3.11"]
    else:
        pytest.skip("A Python 3.11+ interpreter is required to install embpilot")

    create_venv = subprocess.run(
        [*interpreter_cmd, "-m", "venv", str(venv_dir)],
        capture_output=True,
        check=False,
        cwd=repo_root,
        text=True,
    )
    assert create_venv.returncode == 0, create_venv.stderr

    scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    venv_python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
    embpilot_script = scripts_dir / ("embpilot.exe" if os.name == "nt" else "embpilot")

    install_result = subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--disable-pip-version-check",
            "--use-pep517",
            str(repo_root),
        ],
        capture_output=True,
        check=False,
        cwd=repo_root,
        text=True,
    )
    assert install_result.returncode == 0, install_result.stderr
    assert embpilot_script.exists()

    result = subprocess.run(
        [str(embpilot_script), "--version"],
        capture_output=True,
        check=False,
        cwd=repo_root,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"embpilot {__version__}"
