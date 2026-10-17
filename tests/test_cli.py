import json
from pathlib import Path

import pytest

from naruto_daily.cli import main


class FakeClient:
    def __init__(self, devices, size=(1280, 720)):
        self._devices = devices
        self._size = size
        self.inputs = []

    def devices(self): return self._devices
    def screen_size(self): return self._size
    def tap(self, x, y): self.inputs.append(("tap", x, y))
    def swipe(self, *args): self.inputs.append(("swipe", *args))
    def back(self): self.inputs.append(("back",))


def config_path(tmp_path, serial=None, actions=None):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "adb_path": "adb.exe",
        "serial": serial,
        "screen": {"width": 1280, "height": 720},
        "tasks": {
            "login": [],
            "organization_pray": [],
            "sign_in": actions or [],
            "stamina": [],
            "recruit": [],
            "abundance": [],
        },
    }), encoding="utf-8")
    return path


def factory_for(client):
    return lambda adb_path, serial=None: client


def test_check_rejects_zero_devices(tmp_path, capsys):
    result = main(["check", "--config", str(config_path(tmp_path))], client_factory=factory_for(FakeClient([])))

    assert result == 2
    assert "没有发现在线的 adb 设备" in capsys.readouterr().err.lower()


def test_check_rejects_multiple_devices_without_serial(tmp_path, capsys):
    result = main(["check", "--config", str(config_path(tmp_path))], client_factory=factory_for(FakeClient(["a", "b"])))

    assert result == 2
    assert "多个在线 adb 设备" in capsys.readouterr().err.lower()


def test_check_rejects_resolution_mismatch(tmp_path, capsys):
    client = FakeClient(["a"], size=(1920, 1080))

    result = main(["check", "--config", str(config_path(tmp_path))], client_factory=factory_for(client))

    assert result == 2
    assert "分辨率不一致" in capsys.readouterr().err.lower()


def test_check_accepts_one_matching_device(tmp_path, capsys):
    result = main(["check", "--config", str(config_path(tmp_path))], client_factory=factory_for(FakeClient(["a"])))

    assert result == 0
    assert "1280x720" in capsys.readouterr().out


def test_dry_run_never_sends_input_or_sleeps(tmp_path):
    client = FakeClient(["a"])
    sleeps = []
    path = config_path(tmp_path, actions=[{"type": "tap", "x": 10, "y": 20}, {"type": "wait", "seconds": 2}])

    result = main(["run", "sign_in", "--config", str(path), "--dry-run"], client_factory=factory_for(client), sleep=sleeps.append)

    assert result == 0
    assert client.inputs == []
    assert sleeps == []


def test_squad_assault_is_an_independently_runnable_command(tmp_path):
    client = FakeClient(['a'])
    path = config_path(tmp_path)
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['tasks']['squad_assault'] = [{'type': 'tap', 'x': 10, 'y': 20}]
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert main(['run', 'squad_assault', '--config', str(path)],
                client_factory=factory_for(client), sleep=lambda _: None) == 0
    assert client.inputs == [('tap', 10, 20)]


def test_run_all_counts_down_before_input(tmp_path):
    client = FakeClient(["a"])
    events = []
    client.tap = lambda x, y: events.append(("tap", x, y))
    path = config_path(tmp_path, actions=[{"type": "tap", "x": 10, "y": 20}])

    result = main(["run", "all", "--config", str(path)], client_factory=factory_for(client), sleep=lambda seconds: events.append(("sleep", seconds)))

    assert result == 0
    assert events[:5] == [("sleep", 1)] * 5
    assert events[5] == ("tap", 10, 20)


def test_ctrl_c_stops_without_an_empty_error(tmp_path, capsys):
    client = FakeClient(["a"])

    def interrupt(_seconds):
        raise KeyboardInterrupt

    result = main(
        ["run", "all", "--config", str(config_path(tmp_path))],
        client_factory=factory_for(client),
        sleep=interrupt,
    )

    output = capsys.readouterr()
    assert result == 130
    assert "已停止" in output.out
    assert output.err == ""
    assert client.inputs == []


def test_login_is_an_independently_runnable_command(tmp_path):
    client = FakeClient(["a"], size=(1920, 1080))
    payload = json.loads(config_path(tmp_path).read_text(encoding="utf-8"))
    payload["screen"] = {"width": 1920, "height": 1080}
    payload["tasks"]["login"] = [{"type": "tap", "x": 158, "y": 924}]
    path = tmp_path / "login.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = main(["run", "login", "--config", str(path)], client_factory=factory_for(client), sleep=lambda _: None)

    assert result == 0
    assert client.inputs == [("tap", 158, 924)]


@pytest.mark.parametrize('dry_run', [False, True])
def test_local_qr_secret_selects_device_and_skips_saved_account(tmp_path, monkeypatch, dry_run):
    from naruto_daily.login_service import local
    path = tmp_path / 'qr.json'
    original = Path(__file__).parents[1] / 'config.example.json'
    path.write_bytes(original.read_bytes())
    monkeypatch.delenv('LOGIN_ACCOUNT_NAME')
    selected, runs = [], []

    def factory(adb_path, serial=None):
        selected.append(serial)
        client = FakeClient(['emulator-5554', 'emulator-5556'], size=(1920, 1080))
        client.serial = serial
        client.tap = lambda *args: pytest.fail('CLI 不应直接点击或执行原账号登录')
        return client

    def run(config, adb, serial, **kwargs):
        assert not dry_run
        runs.append((config, serial))
        return 3

    monkeypatch.setattr(local, 'run_local_qr_secret', run)
    argv = ['run', 'secret_realm_farm', '--qr-login', '--config', str(path), '--serial', 'emulator-5556']
    assert main(argv + (['--dry-run'] if dry_run else []), client_factory=factory) == 0
    assert selected == [None, 'emulator-5556']
    assert runs == ([] if dry_run else [(path, 'emulator-5556')])


def test_qr_login_rejects_other_task_routes_before_device_access(capsys):
    def factory(*args, **kwargs):
        pytest.fail('错误线路不应连接设备')
    assert main(['run', 'all', '--qr-login', '--config', 'unused.json'], client_factory=factory) == 2
    assert '仅用于 run secret_realm_farm' in capsys.readouterr().err
