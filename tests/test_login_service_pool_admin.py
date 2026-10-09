import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto_daily.login_service import pool_admin


TASK_ID = "11111111-1111-4111-8111-111111111111"


def config(tmp_path, **changes):
    value = {
        "emulator_driver": "ldplayer",
        "ldplayer_console_path": tmp_path / "ldconsole.exe",
        "ldplayer_template": "",
        "ldplayer_state_dir": tmp_path / "state",
        "ldplayer_command_timeout_seconds": 20,
        "ldplayer_pool_indices": (0, 2),
    }
    value.update(changes)
    return SimpleNamespace(**value)


def install_fakes(monkeypatch, configured, statuses=()):
    observed = {"loads": [], "managers": [], "closed": []}

    def initialize_environment(path):
        observed["loads"].append(path)

    class Manager:
        def __init__(self, **kwargs):
            observed["managers"].append(kwargs)

        def pool_status(self):
            return list(statuses)

        def close(self, task_id):
            observed["closed"].append(task_id)

    monkeypatch.setattr(pool_admin.environment, "initialize_environment", initialize_environment)
    monkeypatch.setattr("naruto_daily.login_service.config.load_config", lambda: configured)
    monkeypatch.setattr("naruto_daily.login_service.ldplayer.LDPlayerManager", Manager)
    return observed


def test_list_loads_selected_env_and_prints_only_pool_status(monkeypatch, tmp_path, capsys):
    statuses = [
        {"name": "existing-zero", "index": 0, "taskId": TASK_ID, "state": "qr_ready"},
        {"name": "installed-template", "index": 2, "taskId": None, "state": "available"},
    ]
    observed = install_fakes(monkeypatch, config(tmp_path), statuses)
    env_file = tmp_path / "service.env"

    assert pool_admin.main(["--env-file", str(env_file), "list"]) == 0

    assert observed["loads"] == [env_file]
    assert observed["managers"] == [{
        "console_path": tmp_path / "ldconsole.exe",
        "template": "",
        "state_dir": tmp_path / "state",
        "command_timeout": 20,
        "pool_indices": (0, 2),
    }]
    assert json.loads(capsys.readouterr().out) == statuses


def test_default_pool_admin_loads_root_env_from_another_working_directory(monkeypatch, tmp_path):
    observed = install_fakes(monkeypatch, config(tmp_path))
    elsewhere = tmp_path / '其他启动目录'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert pool_admin.main(['list']) == 0
    assert observed['loads'] == [tmp_path / '.env']


def test_release_requires_uuid_and_calls_manager_close(monkeypatch, tmp_path, capsys):
    observed = install_fakes(monkeypatch, config(tmp_path))

    assert pool_admin.main(["release", TASK_ID]) == 0

    assert observed["closed"] == [TASK_ID]
    assert capsys.readouterr().out.strip() == json.dumps(
        {"released": TASK_ID}, separators=(",", ":")
    )


@pytest.mark.parametrize("task_id", ["not-a-uuid", "11111111-1111-1111-8111-111111111111"])
def test_release_rejects_non_v4_uuid_before_loading_configuration(task_id):
    with pytest.raises(SystemExit) as error:
        pool_admin.main(["release", task_id])
    assert error.value.code == 2


@pytest.mark.parametrize("changes", [
    {"emulator_driver": "mock"},
    {"ldplayer_pool_indices": ()},
])
def test_commands_refuse_non_pool_configuration_without_leaking_values(
    monkeypatch, tmp_path, capsys, changes,
):
    observed = install_fakes(monkeypatch, config(tmp_path, **changes))

    assert pool_admin.main(["list"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "LDPlayer pool mode is not configured.\n"
    assert observed["managers"] == []
