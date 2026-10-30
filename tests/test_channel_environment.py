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


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_local_yuwan_json_keys_and_explicit_enabled_override_cloud(tmp_path, remote_environment, enabled):
    remote_environment.update(YUWAN_API_KEY='cloud-yuwan', YUWAN_MODEL='cloud-model',
                              YUWAN_ENABLED='false' if enabled == 'true' else 'true')
    (tmp_path / '.env').write_text(
        'YUWAN_API_KEY=\'["local-one", "local-two"]\'\n'
        f'YUWAN_ENABLED={enabled}\n' + ('YUWAN_MODEL=local-model\n' if enabled == 'true' else ''),
        encoding='utf-8')
    environment.initialize_environment()
    child = environment.model_environment_for_child()
    assert json.loads(child['YUWAN_API_KEY']) == ['local-one', 'local-two']
    assert child['YUWAN_ENABLED'] == enabled
    assert [(c.api_key, c.model) for c in load_gateway_settings(child).channels if c.name == 'yuwan'] == (
        [('local-one', 'local-model'), ('local-two', 'local-model')] if enabled == 'true' else [])


def test_local_empty_json_list_preserves_cloud_yuwan_config(tmp_path, remote_environment):
    remote_environment.update(YUWAN_API_KEY='cloud-yuwan', YUWAN_MODEL='cloud-model')
    (tmp_path / '.env').write_text("YUWAN_API_KEY='[]'\nYUWAN_MODEL=unused-local-model", encoding='utf-8')
    environment.initialize_environment()
    assert os.environ['YUWAN_API_KEY'] == 'cloud-yuwan'
    assert os.environ['YUWAN_MODEL'] == 'cloud-model'


@pytest.mark.parametrize('local_key', [None, '', '[]', '["unfinished"'])
def test_explicit_local_yuwan_false_skips_invalid_keys_and_disables_cloud_in_child(
        monkeypatch, tmp_path, remote_environment, local_key):
    remote_environment.update(YUWAN_API_KEY='cloud-yuwan', YUWAN_MODEL='cloud-model', YUWAN_ENABLED='true')
    local_config = 'YUWAN_ENABLED=false\n'
    if local_key is not None:
        local_config += f"YUWAN_API_KEY='{local_key}'\n"
    (tmp_path / '.env').write_text(local_config, encoding='utf-8')
    environment.initialize_environment()
    child = environment.model_environment_for_child()
    assert child['YUWAN_ENABLED'] == 'false'
    monkeypatch.setattr(os, 'environ', child)
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: pytest.fail('Child must inherit local disable'))
    assert [(c.name, c.api_key) for c in initialize_gateway().channels] == [('deepseek', 'cloud-deepseek')]


@pytest.mark.parametrize('enabled', ['', 'no', 'FALSE'])
def test_invalid_local_yuwan_enabled_is_rejected_even_without_a_local_key(
        tmp_path, remote_environment, enabled):
    (tmp_path / '.env').write_text(f"YUWAN_ENABLED='{enabled}'\n", encoding='utf-8')
    with pytest.raises(environment.EnvironmentConfigError, match='YUWAN_ENABLED'):
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
