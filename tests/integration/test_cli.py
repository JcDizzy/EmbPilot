from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def test_project_rejects_unsupported_mcp_major_version() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    mcp_requirement = next(
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.startswith("mcp")
    )

    assert "<2" in mcp_requirement


def test_project_and_runtime_versions_match() -> None:
    from embpilot import __version__

    repo_root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__


def test_build_parser_includes_data_dir_flag() -> None:
    from embpilot.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["--data-dir", "tmp-data"])

    assert "--data-dir" in parser._option_string_actions
    assert args.data_dir == "tmp-data"


def test_build_parser_includes_safety_limit_flags() -> None:
    from embpilot.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "--command-timeout-max-ms",
            "30000",
            "--search-limit-max",
            "500",
            "--export-limit-max",
            "5000",
            "--audit-export-limit-max",
            "1000",
            "--tool-rate-limit-per-minute",
            "60",
        ]
    )

    assert args.command_timeout_max_ms == 30000
    assert args.search_limit_max == 500
    assert args.export_limit_max == 5000
    assert args.audit_export_limit_max == 1000
    assert args.tool_rate_limit_per_minute == 60


def test_main_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    from embpilot import __version__
    from embpilot.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"embpilot {__version__}"


def test_main_install_and_uninstall_agent_integration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from embpilot.agent_install.targets import SECTION_START
    from embpilot.cli import main

    main(
        [
            "install",
            "--target",
            "claude,opencode",
            "--location",
            "local",
            "--project-dir",
            str(tmp_path),
            "--server-command",
            "embpilot",
        ]
    )

    assert SECTION_START in (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert SECTION_START in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["embpilot"]["command"] == "embpilot"
    assert "Claude Code: created" in capsys.readouterr().out

    main(
        [
            "uninstall",
            "--target",
            "claude,opencode",
            "--location",
            "local",
            "--project-dir",
            str(tmp_path),
        ]
    )

    assert not (tmp_path / "AGENTS.md").exists()
    assert json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8")) == {}
    assert "OpenCode: removed" in capsys.readouterr().out


def test_interactive_install_prompts_for_targets_and_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from embpilot.agent_install import cli as install_cli
    from embpilot.agent_install.targets import InstallContext
    from embpilot.cli import main

    context = InstallContext(
        project_dir=tmp_path,
        home_dir=tmp_path / "home",
        server_command="embpilot",
    )
    (context.home_dir / ".claude").mkdir(parents=True)
    monkeypatch.setattr(
        install_cli.InstallContext,
        "current",
        classmethod(lambda cls, project_dir, server_command: context),
    )
    answers = iter(["1", "local"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    main(["install", "--project-dir", str(tmp_path), "--server-command", "embpilot"])

    assert (tmp_path / ".mcp.json").exists()
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()


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


def test_installed_package_includes_runtime_resource_files(tmp_path: Path) -> None:
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

    locate_result = subprocess.run(
        [
            str(venv_python),
            "-c",
            (
                "from importlib.util import find_spec; "
                "from pathlib import Path; "
                "import sys; "
                "package_dir = Path(find_spec('embpilot').submodule_search_locations[0]); "
                "expected = (package_dir / 'core' / 'schema_main.sql', "
                "package_dir / 'core' / 'schema_session.sql', "
                "package_dir / 'agent_install' / 'assets' / "
                "'embpilot-device-debugging' / 'SKILL.md'); "
                "missing = [str(path) for path in expected if not path.is_file()]; "
                "sys.exit('\\n'.join(missing) if missing else 0)"
            ),
        ],
        capture_output=True,
        check=False,
        cwd=repo_root,
        text=True,
    )

    assert locate_result.returncode == 0, (
        "Missing packaged runtime resource files:\n"
        f"{locate_result.stderr or locate_result.stdout}"
    )


def test_clean_install_can_import_mcp_app(tmp_path: Path) -> None:
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

    install_result = subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--use-pep517",
            str(repo_root),
        ],
        capture_output=True,
        check=False,
        cwd=repo_root,
        text=True,
        timeout=120,
    )
    assert install_result.returncode == 0, install_result.stderr

    import_result = subprocess.run(
        [
            str(venv_python),
            "-c",
            "from embpilot.mcp_app import create_mcp_app; "
            "from embpilot.agent_install import install_targets; print('ok')",
        ],
        capture_output=True,
        check=False,
        cwd=repo_root,
        text=True,
    )

    assert import_result.returncode == 0, import_result.stderr
    assert import_result.stdout.strip() == "ok"


def test_main_doctor_reports_version_and_core_deps(capsys: pytest.CaptureFixture[str]) -> None:
    from embpilot import __version__
    from embpilot.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["doctor"])
    # doctor exits 0 (all required checks pass) or 1 (some failed); both mean it ran
    assert exc_info.value.code in (0, 1)

    out = capsys.readouterr().out
    assert f"embpilot {__version__}" in out
    assert "python" in out
    assert "core dependencies" in out
    assert "mcp" in out
    assert "optional RAG dependencies" in out
