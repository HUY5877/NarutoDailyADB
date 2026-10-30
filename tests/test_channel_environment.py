import json
import os

import pytest

from naruto_daily.common import cloudflare_kv, environment
from naruto_daily.multimodal.gateway import initialize_gateway, load_gateway_settings


@pytest.fixture
def remote_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    for name in environment.CLOUD_ENV_NAMES:
        os.environ.pop(name, None)
    remote = {'DEEPSEEK_API_KEY': 'cloud-deepseek', 'DEEPSEEK_MODEL': 'cloud-model',
              'REDIS_URL': 'redis://unused.invalid/0', 'MULTIMODAL_ROUTING_COUNTER_KEY': 'test:counter'}
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda settings: dict(remote))
    return remote


def test_child_inherits_json_keys_models_and_enabled_flags_without_refetch(monkeypatch, remote_environment):
    remote_environment.update(OPENAI_API_KEY='["openai-one", "openai-two"]', OPENAI_MODEL='gpt-4.1',
                              OPENAI_ENABLED='true', DEEPSEEK_ENABLED='false', JEV_API_KEY='jev-single')
    environment.initialize_environment()
    child = environment.model_environment_for_child()
    assert child['OPENAI_API_KEY'] == '["openai-one", "openai-two"]'
    assert child['OPENAI_MODEL'] == 'gpt-4.1' and child['OPENAI_ENABLED'] == 'true'
    assert child['DEEPSEEK_ENABLED'] == 'false' and child['JEV_API_KEY'] == 'jev-single'
    monkeypatch.setattr(os, 'environ', child)
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: pytest.fail('Child must use startup snapshot'))
    channels = initialize_gateway().channels
    assert [(c.name, c.api_key) for c in channels] == [('openai', 'openai-one'), ('openai', 'openai-two')]


@pytest.mark.parametrize('enabled', ['true', 'false', '', 'obsolete-value'])
def test_only_local_yuwan_keys_and_model_are_used_regardless_of_enabled_flags(
        monkeypatch, tmp_path, remote_environment, enabled):
    remote_environment.update(YUWAN_API_KEY='cloud-yuwan', YUWAN_MODEL='cloud-model',
                              YUWAN_ENABLED=enabled)
    monkeypatch.setenv('YUWAN_API_KEY', 'stale-process-yuwan')
    monkeypatch.setenv('YUWAN_MODEL', 'stale-process-model')
    monkeypatch.setenv('YUWAN_ENABLED', 'false')
    (tmp_path / '.env').write_text(
        'YUWAN_API_KEY=\'["local-one", "local-two"]\'\n'
        f'YUWAN_ENABLED={enabled}\nYUWAN_MODEL=local-model\n',
        encoding='utf-8')
    environment.initialize_environment()
    child = environment.model_environment_for_child()
    assert json.loads(child['YUWAN_API_KEY']) == ['local-one', 'local-two']
    assert 'YUWAN_ENABLED' not in child and 'YUWAN_ENABLED' not in environment.read_environment()
    monkeypatch.setattr(os, 'environ', child)
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: pytest.fail('Child must use local snapshot'))
    assert [(c.api_key, c.model) for c in initialize_gateway().channels if c.name == 'yuwan'] == [
        ('local-one', 'local-model'), ('local-two', 'local-model')]


@pytest.mark.parametrize('local_key', [None, '', '   ', '[]'])
def test_missing_local_yuwan_key_discards_cloud_and_stale_process_config(
        monkeypatch, tmp_path, remote_environment, local_key):
    remote_environment.update(YUWAN_API_KEY='cloud-yuwan', YUWAN_MODEL='cloud-model', YUWAN_ENABLED='true')
    monkeypatch.setenv('YUWAN_API_KEY', 'stale-process-yuwan')
    monkeypatch.setenv('YUWAN_MODEL', 'stale-process-model')
    monkeypatch.setenv('YUWAN_ENABLED', 'true')
    local_config = 'YUWAN_ENABLED=true\nYUWAN_MODEL=unused-local-model\n'
    if local_key is not None:
        local_config += f"YUWAN_API_KEY='{local_key}'\n"
    (tmp_path / '.env').write_text(local_config, encoding='utf-8')
    environment.initialize_environment()
    child = environment.model_environment_for_child()
    for values in (environment.read_environment(), child):
        assert all(name not in values for name in ('YUWAN_API_KEY', 'YUWAN_MODEL', 'YUWAN_ENABLED'))
    monkeypatch.setattr(os, 'environ', child)
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: pytest.fail('Child must not refetch'))
    assert [(c.name, c.api_key) for c in initialize_gateway().channels] == [('deepseek', 'cloud-deepseek')]


def test_invalid_cloud_yuwan_values_are_ignored_entirely(remote_environment):
    remote_environment.update(YUWAN_API_KEY='["unfinished"', YUWAN_MODEL='invalid\nmodel',
                              YUWAN_ENABLED='obsolete-value')
    environment.initialize_environment()
    child = environment.model_environment_for_child()
    assert all(name not in child for name in ('YUWAN_API_KEY', 'YUWAN_MODEL', 'YUWAN_ENABLED'))
    assert [(c.name, c.api_key) for c in load_gateway_settings(child).channels] == [('deepseek', 'cloud-deepseek')]


@pytest.mark.parametrize('enabled', ['true', 'false', 'obsolete-value'])
def test_local_yuwan_key_requires_local_model_even_with_a_legacy_disabled_flag(
        tmp_path, remote_environment, enabled):
    remote_environment.update(YUWAN_API_KEY='cloud-key', YUWAN_MODEL='cloud-model', YUWAN_ENABLED='false')
    (tmp_path / '.env').write_text(f"YUWAN_API_KEY=local-key\nYUWAN_ENABLED='{enabled}'\n", encoding='utf-8')
    with pytest.raises(environment.EnvironmentConfigError, match='YUWAN_MODEL'):
        environment.initialize_environment()
    assert not environment._initialized


def test_invalid_local_yuwan_key_is_not_hidden_by_a_legacy_disabled_flag(tmp_path, remote_environment):
    (tmp_path / '.env').write_text("YUWAN_API_KEY='[\"unfinished\"'\nYUWAN_ENABLED=false\n", encoding='utf-8')
    with pytest.raises(environment.EnvironmentConfigError, match='YUWAN_API_KEY'):
        environment.initialize_environment()
    assert not environment._initialized


def test_stale_openai_process_and_dotenv_values_do_not_replace_cloud(monkeypatch, tmp_path, remote_environment):
    for name, value in {'OPENAI_API_KEY': 'stale-private', 'OPENAI_MODEL': 'stale-model',
                        'OPENAI_ENABLED': 'true'}.items():
        monkeypatch.setenv(name, value)
    (tmp_path / '.env').write_text('OPENAI_API_KEY=stale-file\nOPENAI_MODEL=stale-model\nOPENAI_ENABLED=true',
                                 encoding='utf-8')
    environment.initialize_environment()
    values = environment.read_environment()
    assert all(name not in values for name in ('OPENAI_API_KEY', 'OPENAI_MODEL', 'OPENAI_ENABLED'))
