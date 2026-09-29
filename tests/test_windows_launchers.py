"""Run the real CMD files with a harmless Python entry point, without ADB."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows CMD entry points')
@pytest.mark.parametrize('filename,task', [
    ('启动日常.cmd', 'all'),
    ('启动秘境.cmd', 'secret_realm_farm'),
])
@pytest.mark.parametrize('code_page', [936, 65001])
def test_launcher_from_chinese_path_forwards_arguments(tmp_path, filename, task, code_page):
    root = Path(__file__).parents[1]
    project = tmp_path / '启动测试 & 中文目录'
    project.mkdir()
    shutil.copy2(root / filename, project / filename)
    scripts = project / '.venv' / 'Scripts'
    scripts.mkdir(parents=True)
    # The GUI interpreter avoids opening a test window; CMD still launches
    # the exact executable path and arguments used by the real launcher.
    shutil.copy2(Path(sys.executable).with_name('pythonw.exe'), scripts / 'python.exe')
    # Use the environment running the tests, including .venv-login-service.
    shutil.copy2(Path(sys.prefix) / 'pyvenv.cfg', project / '.venv' / 'pyvenv.cfg')
    (project / 'config.local.json').write_text('{}')
    package = project / 'naruto_daily'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'launcher.py').write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path('launch-result.json').write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n",
        encoding='utf-8',
    )
    wrapper = tmp_path / 'run-launcher.cmd'
    wrapper_text = (
        '@echo off\r\n'
        f'chcp {code_page} >nul\r\n'
        f'call "{project / filename}" --dry-run\r\n'
        'exit /b %errorlevel%\r\n'
    )
    wrapper.write_bytes(wrapper_text.encode('gbk' if code_page == 936 else 'utf-8'))
    completed = subprocess.run(
        [os.environ['COMSPEC'], '/d', '/c', str(wrapper)],
        cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == b''
    result_file = project / 'launch-result.json'
    deadline = time.monotonic() + 5
    while not result_file.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert result_file.exists(), completed.stdout
    result = json.loads(result_file.read_text())
    assert Path(result['cwd']) == project
    assert result['argv'] == ['run', task, '--config', 'config.local.json', '--dry-run']
