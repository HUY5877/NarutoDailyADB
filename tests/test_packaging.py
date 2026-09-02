import shutil
import subprocess
import sys
from pathlib import Path


def test_editable_build_ignores_asset_directory_as_python_package(tmp_path):
    project_root = Path(__file__).parents[1]
    build_root = tmp_path / "构建副本"
    build_root.mkdir()
    shutil.copy2(project_root / "pyproject.toml", build_root / "pyproject.toml")
    shutil.copytree(project_root / "naruto_daily", build_root / "naruto_daily")
    shutil.copytree(project_root / "assets", build_root / "assets")

    command = [
        sys.executable,
        "-c",
        "import setuptools.build_meta as backend; backend.get_requires_for_build_editable()",
    ]
    result = subprocess.run(
        command,
        cwd=build_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
