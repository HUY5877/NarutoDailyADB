import json
from pathlib import Path

import pytest

from naruto_daily.common import environment
from naruto_daily.config import ConfigError, load_settings


ENV_NAMES = ("ADB_PATH",)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, configured_environment):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def configured_file(tmp_path, *, model_variable='YUWAN_MODEL'):
    payload = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))
    payload["adb_path"] = "${ADB_PATH}"
    action = next(a for a in payload["tasks"]["recruit"] if a["type"] == "handle_free_recruit")
    action["multimodal"].update(model='${' + model_variable + '}')
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_env(tmp_path, adb_path="测试目录 #1/adb.exe", *, model='test-vision'):
    (tmp_path / ".env").write_text(
        f'# 测试配置\nYUWAN_MODEL={model}\nYUWAN_API_KEY=test-secret\n'
        f'ADB_PATH="{adb_path}"\n', encoding="utf-8-sig")


def recruit_model(settings):
    return next(a["multimodal"] for a in settings.tasks["recruit"] if a["type"] == "handle_free_recruit")


def test_config_reads_root_env_not_working_directory(tmp_path, monkeypatch):
    path = configured_file(tmp_path)
    write_env(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / ".env").write_text('ADB_PATH=错误目录/adb.exe', encoding="utf-8")
    monkeypatch.chdir(other)
    result = load_settings(path)
    assert result.adb_path == Path("测试目录 #1/adb.exe")
    assert recruit_model(result)["model"] == "step-3.7-flash"
    assert "test-secret" not in repr(result)


def test_process_environment_still_overrides_machine_dotenv(tmp_path, monkeypatch):
    path = configured_file(tmp_path)
    write_env(tmp_path)
    monkeypatch.setenv("ADB_PATH", "进程环境目录/adb.exe")
    assert load_settings(path).adb_path == Path("进程环境目录/adb.exe")


def test_model_config_uses_injected_cloud_values_despite_local_files_and_cwd(tmp_path, monkeypatch):
    root, service, elsewhere = (tmp_path / name for name in ('project', 'service', 'elsewhere'))
    for directory in (root, service, elsewhere):
        directory.mkdir()
    monkeypatch.setattr(environment, 'PROJECT_ROOT', root)
    write_env(root, '根目录/adb.exe', model='root-model')
    write_env(service, '服务目录/adb.exe', model='service-model')
    write_env(elsewhere, '工作目录/adb.exe', model='cwd-model')
    monkeypatch.setenv('YUWAN_MODEL', 'process-model')
    monkeypatch.setenv('YUWAN_API_KEY', 'process-key')
    monkeypatch.chdir(elsewhere)

    result = load_settings(configured_file(service))

    assert result.adb_path == Path('根目录/adb.exe')
    assert recruit_model(result)['model'] == 'process-model'
    assert environment.read_environment()['YUWAN_API_KEY'] == 'process-key'


def test_reloading_local_files_keeps_injected_cloud_model(tmp_path, monkeypatch):
    root, service = tmp_path / 'project', tmp_path / 'service'
    root.mkdir()
    service.mkdir()
    monkeypatch.setattr(environment, 'PROJECT_ROOT', root)
    write_env(service, '服务目录/adb.exe')
    path = configured_file(service)

    write_env(root, model='first-model')
    first = load_settings(path)
    write_env(root, model='second-model')
    second = load_settings(path)

    assert recruit_model(first)['model'] == 'step-3.7-flash'
    assert recruit_model(second)['model'] == 'step-3.7-flash'
    assert first.adb_path == second.adb_path == Path('测试目录 #1/adb.exe')


def test_reload_reads_edited_dotenv_without_stale_values(tmp_path):
    path = configured_file(tmp_path)
    write_env(tmp_path, "第一个目录/adb.exe")
    assert load_settings(path).adb_path == Path("第一个目录/adb.exe")
    write_env(tmp_path, "第二个目录/adb.exe")
    assert load_settings(path).adb_path == Path("第二个目录/adb.exe")


def test_official_channel_model_placeholder_resolves_without_yuwan_config(tmp_path, monkeypatch):
    monkeypatch.delenv("YUWAN_API_KEY")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "official-private")
    monkeypatch.setenv("DEEPSEEK_MODEL", "official-vision")
    path = configured_file(tmp_path, model_variable='DEEPSEEK_MODEL')
    (tmp_path / '.env').write_text(
        'DEEPSEEK_API_KEY=official-private\n'
        'DEEPSEEK_MODEL=official-vision\n'
        'ADB_PATH=测试目录/adb.exe\n', encoding='utf-8')
    result = recruit_model(load_settings(path))
    assert result['model'] == 'official-vision'
    assert 'base_url' not in result
    assert 'official-private' not in repr(result)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_empty_machine_variable_fails_clearly(tmp_path, monkeypatch, value):
    path = configured_file(tmp_path)
    if value is not None:
        monkeypatch.setenv("ADB_PATH", value)
    with pytest.raises(ConfigError, match="ADB_PATH"):
        load_settings(path)
