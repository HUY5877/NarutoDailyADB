import json
from pathlib import Path

import pytest

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


def write_env(tmp_path, nickname="另一个测试昵称 #1"):
    (tmp_path / ".env").write_text(
        '# 测试配置\nMULTIMODAL_BASE_URL="https://example.test/v1"\n'
        'MULTIMODAL_MODEL=test-vision\nMULTIMODAL_API_KEY=test-secret\n'
        f'LOGIN_ACCOUNT_NAME="{nickname}"\n', encoding="utf-8-sig")


def authorization(settings):
    return next(a for a in settings.tasks["login"] if a["type"] == "authorize_account")


def test_config_reads_env_beside_config_not_working_directory(tmp_path, monkeypatch):
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


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    path = configured_file(tmp_path)
    write_env(tmp_path)
    monkeypatch.setenv("LOGIN_ACCOUNT_NAME", "进程环境中的昵称")
    assert authorization(load_settings(path))["account_name"] == "进程环境中的昵称"


def test_reload_reads_edited_dotenv_without_stale_values(tmp_path):
    path = configured_file(tmp_path)
    write_env(tmp_path, "第一个账号")
    assert authorization(load_settings(path))["account_name"] == "第一个账号"
    write_env(tmp_path, "第二个账号")
    assert authorization(load_settings(path))["account_name"] == "第二个账号"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_empty_nickname_fails_clearly(tmp_path, monkeypatch, value):
    path = configured_file(tmp_path)
    monkeypatch.setenv("MULTIMODAL_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("MULTIMODAL_MODEL", "test-vision")
    if value is not None:
        monkeypatch.setenv("LOGIN_ACCOUNT_NAME", value)
    with pytest.raises(ConfigError, match="LOGIN_ACCOUNT_NAME"):
        load_settings(path)
