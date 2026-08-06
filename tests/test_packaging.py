"""Installed-wheel smoke tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def test_built_wheel_contains_database_schemas(tmp_path: Path) -> None:
    """A normal wheel install must include the SQL files imported at runtime."""
    project_root = Path(__file__).parents[1]
    build_root = tmp_path / "project"
    shutil.copytree(project_root / "src", build_root / "src")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(project_root / name, build_root / name)

    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--disable-pip-version-check",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=build_root,
        check=True,
        capture_output=True,
        text=True,
    )

    wheel = next(wheel_dir.glob("embpilot-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "embpilot/core/schema_main.sql" in names
    assert "embpilot/core/schema_session.sql" in names


def test_rag_dependencies_are_optional() -> None:
    project_root = Path(__file__).parents[1]
    with (project_root / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]

    core_dependencies = "\n".join(project["dependencies"]).lower()
    rag_dependencies = "\n".join(project["optional-dependencies"]["rag"]).lower()
    dev_dependencies = "\n".join(project["optional-dependencies"]["dev"]).lower()

    for package in ("fastembed", "lancedb", "pandas", "pyarrow"):
        assert package not in core_dependencies
        assert package in rag_dependencies
        assert package in dev_dependencies
