import json
from pathlib import Path

import pytest

from naruto_daily.common import environment
from naruto_daily.config import ConfigError, load_settings


ENV_NAMES = (*environment.CHANNEL_ENV_NAMES, "LOGIN_ACCOUNT_NAME")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, configured_environment):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def configured_file(tmp_path, *, model_variable='YUWAN_MODEL'):
    payload = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))
    action = next(a for a in payload["tasks"]["login"] if a["type"] == "authorize_account")
    action["account_name"] = "${LOGIN_ACCOUNT_NAME}"
    action["multimodal"].update(model='${' + model_variable + '}')
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_env(tmp_path, nickname="另一个测试昵称 #1", *, model='test-vision'):
    (tmp_path / ".env").write_text(
        f'# 测试配置\nYUWAN_MODEL={model}\nYUWAN_API_KEY=test-secret\n'
        f'LOGIN_ACCOUNT_NAME="{nickname}"\n', encoding="utf-8-sig")


def authorization(settings):
    return next(a for a in settings.tasks["login"] if a["type"] == "authorize_account")


def test_config_reads_root_env_not_working_directory(tmp_path, monkeypatch):
    path = configured_file(tmp_path)
    write_env(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / ".env").write_text('LOGIN_ACCOUNT_NAME=错误账号', encoding="utf-8")
    monkeypatch.chdir(other)
    result = authorization(load_settings(path))
    assert result["account_name"] == "另一个测试昵称 #1"
    assert result["multimodal"]["model"] == "test-vision"
    assert "test-secret" not in repr(result)


def test_process_environment_still_overrides_account_dotenv(tmp_path, monkeypatch):
    path = configured_file(tmp_path)
    write_env(tmp_path)
    monkeypatch.setenv("LOGIN_ACCOUNT_NAME", "进程环境中的昵称")
    assert authorization(load_settings(path))["account_name"] == "进程环境中的昵称"


def test_model_config_uses_project_root_despite_external_config_process_and_cwd(tmp_path, monkeypatch):
    root, service, elsewhere = (tmp_path / name for name in ('project', 'service', 'elsewhere'))
    for directory in (root, service, elsewhere):
        directory.mkdir()
    monkeypatch.setattr(environment, 'PROJECT_ROOT', root)
    write_env(root, '根目录账号', model='root-model')
    write_env(service, '服务目录账号', model='service-model')
    write_env(elsewhere, '工作目录账号', model='cwd-model')
    monkeypatch.setenv('YUWAN_MODEL', 'process-model')
    monkeypatch.setenv('YUWAN_API_KEY', 'process-key')
    monkeypatch.chdir(elsewhere)

    result = authorization(load_settings(configured_file(service)))

    assert result['account_name'] == '根目录账号'
    assert result['multimodal']['model'] == 'root-model'
    assert environment.read_environment()['YUWAN_API_KEY'] == 'test-secret'


def test_reloading_external_config_reads_edited_root_model_values(tmp_path, monkeypatch):
    root, service = tmp_path / 'project', tmp_path / 'service'
    root.mkdir()
    service.mkdir()
    monkeypatch.setattr(environment, 'PROJECT_ROOT', root)
    write_env(service, '服务账号')
    path = configured_file(service)

    write_env(root, model='first-model')
    first = authorization(load_settings(path))
    write_env(root, model='second-model')
    second = authorization(load_settings(path))

    assert first['multimodal']['model'] == 'first-model'
    assert second['multimodal']['model'] == 'second-model'
    assert first['account_name'] == second['account_name'] == '另一个测试昵称 #1'


def test_reload_reads_edited_dotenv_without_stale_values(tmp_path):
    path = configured_file(tmp_path)
    write_env(tmp_path, "第一个账号")
    assert authorization(load_settings(path))["account_name"] == "第一个账号"
    write_env(tmp_path, "第二个账号")
    assert authorization(load_settings(path))["account_name"] == "第二个账号"


def test_official_channel_model_placeholder_resolves_without_yuwan_config(tmp_path):
    path = configured_file(tmp_path, model_variable='DEEPSEEK_MODEL')
    (tmp_path / '.env').write_text(
        'DEEPSEEK_API_KEY=official-private\n'
        'DEEPSEEK_MODEL=official-vision\n'
        'LOGIN_ACCOUNT_NAME=测试账号\n', encoding='utf-8')
    result = authorization(load_settings(path))
    assert result['multimodal']['model'] == 'official-vision'
    assert 'base_url' not in result['multimodal']
    assert 'official-private' not in repr(result)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_empty_nickname_fails_clearly(tmp_path, monkeypatch, value):
    path = configured_file(tmp_path)
    if value is not None:
        monkeypatch.setenv("LOGIN_ACCOUNT_NAME", value)
    with pytest.raises(ConfigError, match="LOGIN_ACCOUNT_NAME"):
        load_settings(path)
