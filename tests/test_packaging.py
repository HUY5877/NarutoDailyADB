import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile


SHARED_TEMPLATES = {
    "qq_android": "qq_android_button.png",
    "start_game": "start_game_button.png",
    "switch_account": "qq_account_switch_account.png",
}
SERVICE_TEMPLATES = {
    "add_account", "game_back", "other_login", "qr_heading", "scan_login",
    "web_qr_heading", "web_qr_instruction", "web_qr_expired",
}


def copy_build_project(tmp_path):
    project_root = Path(__file__).parents[1]
    build_root = tmp_path / "构建副本"
    build_root.mkdir()
    for filename in ("pyproject.toml", "setup.py"):
        shutil.copy2(project_root / filename, build_root / filename)
    shutil.copytree(
        project_root / "naruto_daily", build_root / "naruto_daily",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env"),
    )
    shutil.copytree(project_root / "assets", build_root / "assets")
    return build_root


def run_python(code, cwd, *arguments):
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, *map(str, arguments)],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_editable_build_accepts_shared_assets_package(tmp_path):
    run_python(
        "import setuptools.build_meta as backend; backend.get_requires_for_build_editable()",
        copy_build_project(tmp_path),
    )


def test_sdist_rebuilds_wheel_with_shared_templates_usable_outside_checkout(tmp_path):
    build_root = copy_build_project(tmp_path)
    run_python(
        "import setuptools.build_meta as backend; backend.build_sdist('dist')",
        build_root,
    )
    sdist = next((build_root / "dist").glob("*.tar.gz"))
    extracted_source = tmp_path / "源码发行包"
    extracted_source.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted_source, filter="data")
    source_root = next(extracted_source.iterdir())
    assert (source_root / "setup.py").is_file()
    assert (source_root / "assets" / "__init__.py").is_file()
    for filename in SHARED_TEMPLATES.values():
        assert (source_root / "assets" / filename).read_bytes() == (
            build_root / "assets" / filename
        ).read_bytes()

    run_python(
        "import setuptools.build_meta as backend; backend.build_wheel('dist')",
        source_root,
    )
    wheel = next((source_root / "dist").glob("*.whl"))
    installed_root = tmp_path / "安装副本"
    with ZipFile(wheel) as archive:
        expected_pngs = {
            *(f"naruto_daily/assets/{filename}" for filename in SHARED_TEMPLATES.values()),
            *(f"naruto_daily/login_service/templates/{name}.png" for name in SERVICE_TEMPLATES),
        }
        actual_pngs = {name for name in archive.namelist() if name.endswith(".png")}
        assert actual_pngs == expected_pngs
        for filename in SHARED_TEMPLATES.values():
            assert archive.read(f"naruto_daily/assets/{filename}") == (
                build_root / "assets" / filename
            ).read_bytes()
        archive.extractall(installed_root)

    unrelated_cwd = tmp_path / "其他工作目录"
    unrelated_cwd.mkdir()
    run_python(
        """
import sys
from pathlib import Path

import cv2
import numpy as np

installed_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(installed_root))
from naruto_daily.login_service import qr_navigation

assert Path(qr_navigation.__file__).resolve().is_relative_to(installed_root)
names = ('qq_android', 'start_game', 'switch_account', 'add_account',
         'game_back', 'other_login', 'qr_heading', 'scan_login',
         'web_qr_heading', 'web_qr_instruction', 'web_qr_expired')
for name in names:
    template = qr_navigation._template(name)
    assert template is not None and template.ndim == 2 and template.size > 0, name

def frame_with(*placements):
    frame = np.full((1080, 1920, 3), 255, np.uint8)
    for name, x, y in placements:
        template = qr_navigation._template(name)
        h, w = template.shape
        frame[y:y+h, x:x+w] = cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)
    return frame

frame = frame_with(('qq_android', 1400, 770))
frame[910:942, 142:176] = (100, 235, 45)
assert qr_navigation.choose_action(frame, '').name == 'qq_android'
assert qr_navigation.choose_action(
    frame_with(('switch_account', 900, 905)), ''
).name == 'switch_account'
assert qr_navigation.choose_action(
    frame_with(('start_game', 700, 820), ('game_back', 1680, 30)), ''
).name == 'game_back'
""",
        unrelated_cwd,
        installed_root,
    )
