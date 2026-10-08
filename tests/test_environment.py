import json
from pathlib import Path

import pytest

from naruto_daily.common import environment
from naruto_daily.config import ConfigError, load_settings


ENV_NAMES = ("MULTIMODAL_API_KEY", "MULTIMODAL_BASE_URL", "MULTIMODAL_MODEL", "LOGIN_ACCOUNT_NAME")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, configured_environment):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def configured_file(tmp_path):
    payload = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))
    action = next(a for a in payload["tasks"]["login"] if a["type"] == "authorize_account")
    action["account_name"] = "${LOGIN_ACCOUNT_NAME}"
    action["multimodal"].update(base_url="${MULTIMODAL_BASE_URL}", model="${MULTIMODAL_MODEL}")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_env(tmp_path, nickname="另一个测试昵称 #1", *, model='test-vision', base_url='https://example.test/v1'):
    (tmp_path / ".env").write_text(
        f'# 测试配置\nMULTIMODAL_BASE_URL="{base_url}"\n'
        f'MULTIMODAL_MODEL={model}\nMULTIMODAL_API_KEY=test-secret\n'
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
    assert result["multimodal"]["base_url"] == "https://example.test/v1"
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
    write_env(root, '根目录账号', model='root-model', base_url='https://root.test/v1')
    write_env(service, '服务目录账号', model='service-model', base_url='https://service.test/v1')
    write_env(elsewhere, '工作目录账号', model='cwd-model', base_url='https://cwd.test/v1')
    monkeypatch.setenv('MULTIMODAL_MODEL', 'process-model')
    monkeypatch.setenv('MULTIMODAL_BASE_URL', 'https://process.test/v1')
    monkeypatch.setenv('MULTIMODAL_API_KEY', 'process-key')
    monkeypatch.chdir(elsewhere)

    result = authorization(load_settings(configured_file(service)))

    assert result['account_name'] == '根目录账号'
    assert result['multimodal']['base_url'] == 'https://root.test/v1'
    assert result['multimodal']['model'] == 'root-model'
    assert environment.read_environment()['MULTIMODAL_API_KEY'] == 'test-secret'


def test_reloading_external_config_reads_edited_root_model_values(tmp_path, monkeypatch):
    root, service = tmp_path / 'project', tmp_path / 'service'
    root.mkdir()
    service.mkdir()
    monkeypatch.setattr(environment, 'PROJECT_ROOT', root)
    write_env(service, '服务账号')
    path = configured_file(service)

    write_env(root, model='first-model', base_url='https://first.test/v1')
    first = authorization(load_settings(path))
    write_env(root, model='second-model', base_url='https://second.test/v1')
    second = authorization(load_settings(path))

    assert first['multimodal']['model'] == 'first-model'
    assert first['multimodal']['base_url'] == 'https://first.test/v1'
    assert second['multimodal']['model'] == 'second-model'
    assert second['multimodal']['base_url'] == 'https://second.test/v1'
    assert first['account_name'] == second['account_name'] == '另一个测试昵称 #1'


def test_reload_reads_edited_dotenv_without_stale_values(tmp_path):
    path = configured_file(tmp_path)
    write_env(tmp_path, "第一个账号")
    assert authorization(load_settings(path))["account_name"] == "第一个账号"
    write_env(tmp_path, "第二个账号")
    assert authorization(load_settings(path))["account_name"] == "第二个账号"


def test_old_json_placeholders_resolve_from_channel_config_with_empty_legacy_values(tmp_path):
    path = configured_file(tmp_path)
    (tmp_path / '.env').write_text(
        'DEEPSEEK_API_KEY=official-private\n'
        'DEEPSEEK_MODEL=official-vision\n'
        'MULTIMODAL_MODEL=\nMULTIMODAL_BASE_URL=\nLOGIN_ACCOUNT_NAME=测试账号\n', encoding='utf-8')
    result = authorization(load_settings(path))
    assert result['multimodal']['model'] == 'official-vision'
    assert result['multimodal']['base_url'] == 'https://api.deepseek.com'
    assert 'official-private' not in repr(result)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_empty_nickname_fails_clearly(tmp_path, monkeypatch, value):
    path = configured_file(tmp_path)
    if value is not None:
        monkeypatch.setenv("LOGIN_ACCOUNT_NAME", value)
    with pytest.raises(ConfigError, match="LOGIN_ACCOUNT_NAME"):
        load_settings(path)
