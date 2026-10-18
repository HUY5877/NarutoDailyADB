import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from cloudflare import Cloudflare
import httpx
import pytest

from naruto_daily.common import cloudflare_kv as kv, environment
from naruto_daily.login_service import __main__ as entrypoint
from naruto_daily.login_service.config import load_config
from naruto_daily.multimodal.gateway import initialize_gateway
from oss_fakes import OSS_ENV


BOOTSTRAP = dict(zip(kv.BOOTSTRAP_NAMES, ('test-cf-token', 'a' * 32, 'b' * 32)))


@pytest.fixture(autouse=True)
def fresh_start(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    for name in environment.CLOUD_ENV_NAMES:
        os.environ.pop(name, None)
    (tmp_path / '.env').write_text('\n'.join(f'{key}={value}' for key, value in BOOTSTRAP.items()),
                                   encoding='utf-8')


def install_sdk(monkeypatch, handler):
    def client(**kwargs):
        assert kwargs['base_url'] == kv.API_BASE_URL
        assert kwargs['timeout'] == 15.0
        assert kwargs['max_retries'] == 2
        return Cloudflare(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(kv, 'Cloudflare', client)


def response(result, **kwargs):
    return httpx.Response(200, json={'success': True, 'errors': [], 'messages': [], 'result': result, **kwargs})


def test_sdk_reads_all_pages_including_empty_page_and_batches_values(monkeypatch, caplog):
    values = {f'CONFIG_{index}': f'value-{index}' for index in range(205)}
    values['CONFIG_0'] = '原样保留 $value #1\n第二行'
    calls = []

    def handler(request):
        assert request.headers['Authorization'] == 'Bearer test-cf-token'
        assert '/accounts/' + BOOTSTRAP['CLOUDFLARE_ACCOUNT_ID'] + '/' in request.url.path
        calls.append(request)
        if request.method == 'GET':
            assert 'prefix' not in request.url.params
            cursor = request.url.params.get('cursor')
            if cursor is None:
                return response([{'name': name} for name in list(values)[:101]], result_info={'cursor': 'next'})
            if cursor == 'next':
                return response([], result_info={'cursor': 'last'})
            assert cursor == 'last'
            return response([{'name': name} for name in list(values)[101:]], result_info={'cursor': ''})
        body = json.loads(request.content)
        assert body['type'] == 'text' and body['withMetadata'] is False
        return response({'values': {key: values[key] for key in body['keys']}})

    install_sdk(monkeypatch, handler)
    with caplog.at_level(logging.INFO):
        environment.initialize_environment()
    assert {key: os.environ[key] for key in values} == values
    assert [len(json.loads(r.content)['keys']) for r in calls if r.method == 'POST'] == [100, 100, 5]
    assert '205' in caplog.text and 'test-cf-token' not in caplog.text
    assert all(value not in caplog.text for value in values.values())
    environment.initialize_environment()
    environment.read_environment()
    assert len(calls) == 6


def test_publish_is_all_or_nothing_when_a_later_batch_fails(monkeypatch):
    keys = [f'CONFIG_{index}' for index in range(101)]
    def handler(request):
        if request.method == 'GET':
            return response([{'name': key} for key in keys], result_info={'cursor': ''})
        batch = json.loads(request.content)['keys']
        if len(batch) == 1:
            return httpx.Response(403, json={'errors': [{'message': 'private-response test-cf-token'}]})
        return response({'values': dict.fromkeys(batch, 'private-value')})
    install_sdk(monkeypatch, handler)
    with pytest.raises(environment.EnvironmentConfigError, match='HTTP 403') as error:
        environment.initialize_environment()
    assert 'private-response' not in str(error.value) and 'test-cf-token' not in str(error.value)
    assert not environment._initialized
    assert all(key not in os.environ for key in keys)


@pytest.mark.parametrize('value', [None, 12, {'value': 'text'}, 'text\x00invalid'])
def test_missing_non_text_or_invalid_values_fail_before_injection(monkeypatch, value):
    def handler(request):
        if request.method == 'GET':
            return response([{'name': 'NEW_SETTING'}], result_info={'cursor': ''})
        return response({'values': {'NEW_SETTING': value}})
    install_sdk(monkeypatch, handler)
    with pytest.raises(environment.EnvironmentConfigError):
        environment.initialize_environment()
    assert 'NEW_SETTING' not in os.environ


@pytest.mark.parametrize('name', ['bad-name', 'contains space', 'APP_ENV', environment.INHERITED_CONFIG_MARKER, *kv.BOOTSTRAP_NAMES])
def test_invalid_or_reserved_key_names_are_rejected(monkeypatch, name):
    install_sdk(monkeypatch, lambda request: response([{'name': name}], result_info={'cursor': ''}))
    with pytest.raises(environment.EnvironmentConfigError, match='变量名'):
        environment.initialize_environment()


@pytest.mark.parametrize('returned_name', ['', 'prod_REDIS_URL', 'test:REDIS_URL'])
def test_invalid_variable_is_rejected_before_values(monkeypatch, returned_name):
    def handler(request):
        assert request.method == 'GET'
        return httpx.Response(200, json={'success': True, 'result': [{'name': returned_name}],
                                        'result_info': {'cursor': ''}, 'errors': [], 'messages': []})
    install_sdk(monkeypatch, handler)
    with pytest.raises(environment.EnvironmentConfigError, match='变量名'):
        environment.initialize_environment()


def test_repeated_cursor_fails_instead_of_looping_forever(monkeypatch):
    install_sdk(monkeypatch, lambda request: response([], result_info={'cursor': 'same'}))
    with pytest.raises(environment.EnvironmentConfigError, match='游标重复'):
        environment.initialize_environment()


@pytest.mark.parametrize('name', ['CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_KV_NAMESPACE_ID'])
def test_missing_bootstrap_setting_fails_without_an_api_call(monkeypatch, name):
    monkeypatch.setattr(kv, 'Cloudflare', lambda **kwargs: pytest.fail('No API call with missing configuration'))
    settings = {key: value for key, value in BOOTSTRAP.items() if key != name}
    with pytest.raises(environment.EnvironmentConfigError, match=name):
        kv.fetch_environment(settings)


def test_cloud_overrides_local_and_process_settings_and_clears_deleted_keys(monkeypatch, tmp_path):
    with (tmp_path / '.env').open('a', encoding='utf-8') as stream:
        stream.write('\nDEEPSEEK_API_KEY=old-file-secret\nOSS_STS_TOKEN=old-token\nWORKER_CONCURRENCY=3\n')
    os.environ.update(YUWAN_API_KEY='old-process-secret', OSS_STS_TOKEN='old-process-token')
    remote = {**OSS_ENV, 'DEEPSEEK_API_KEY': 'cloud-key', 'DEEPSEEK_MODEL': 'cloud-model',
              'REDIS_URL': 'redis://cloud.invalid/2', 'WORKER_CONCURRENCY': '4'}
    calls = []
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: calls.append(1) or dict(remote))
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: environment.initialize_environment(), range(8)))
    assert calls == [1]
    assert 'YUWAN_API_KEY' not in environment.read_environment()
    assert 'OSS_STS_TOKEN' not in os.environ
    assert load_config().concurrency == 4
    assert load_config().redis_url == remote['REDIS_URL']
    assert initialize_gateway().channels[0].api_key == 'cloud-key'
    assert initialize_gateway().channels[0].name == 'deepseek'


def test_profile_qq_and_database_credentials_load_without_entering_child_environment(monkeypatch, tmp_path):
    names = ('QQ_BOT_APP_ID', 'QQ_BOT_APP_SECRET', 'QQ_BOT_GROUP_OPENID', 'DATABASE_URL')
    with (tmp_path / '.env').open('a', encoding='utf-8') as stream:
        stream.write('\n' + '\n'.join(f'{name}=stale-local-value' for name in names))
    os.environ.update(dict.fromkeys(names, 'stale-process-value'))
    remote = {**OSS_ENV, 'QQ_BOT_APP_ID': '1900000001', 'QQ_BOT_APP_SECRET': 'cloud-qq-secret',
              'QQ_BOT_GROUP_OPENID': 'A' * 32, 'DATABASE_URL': 'postgresql://user:password@db.invalid/naruto'}
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: dict(remote))
    environment.initialize_environment(tmp_path / '.env')
    config = load_config()
    assert config.qq_bot_app_id == remote['QQ_BOT_APP_ID']
    assert config.database_url == remote['DATABASE_URL']
    assert all(name not in environment.model_environment_for_child() for name in names)
    environment._initialized = False
    remote.clear()
    environment.initialize_environment(tmp_path / '.env')
    assert all(name not in os.environ for name in names)
    assert all(name not in environment._read_local_dotenv(tmp_path / '.env') for name in names)


def test_startup_reports_missing_username_database_configuration(monkeypatch, tmp_path, caplog):
    remote = {**OSS_ENV, 'REDIS_URL': 'redis://unused.invalid/0',
              'QQ_BOT_APP_ID': '1900000001', 'QQ_BOT_APP_SECRET': 'private-qq-secret',
              'QQ_BOT_GROUP_OPENID': 'A' * 32}
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: dict(remote))
    with pytest.raises(SystemExit) as result:
        entrypoint.main(['--env-file', str(tmp_path / '.env')])
    assert result.value.code == 1
    assert 'DATABASE_URL' in caplog.text
    assert 'private-qq-secret' not in caplog.text


@pytest.mark.parametrize('cloud_yuwan', [False, True])
def test_local_yuwan_key_and_model_are_loaded_and_override_cloud(monkeypatch, tmp_path, cloud_yuwan):
    with (tmp_path / '.env').open('a', encoding='utf-8') as stream:
        stream.write('\nYUWAN_API_KEY=local-yuwan-key\nYUWAN_MODEL=local-yuwan-model\n')
    remote = {'DEEPSEEK_API_KEY': 'cloud-deepseek', 'DEEPSEEK_MODEL': 'cloud-deepseek-model',
              'REDIS_URL': 'redis://cloud.invalid/0',
              'MULTIMODAL_ROUTING_COUNTER_KEY': 'naruto:multimodal:route:sequence'}
    if cloud_yuwan:
        remote.update(YUWAN_API_KEY='cloud-yuwan', YUWAN_MODEL='cloud-yuwan-model')
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: dict(remote))
    environment.initialize_environment()
    channels = initialize_gateway().channels
    assert [(channel.name, channel.api_key, channel.model) for channel in channels] == [
        ('yuwan', 'local-yuwan-key', 'local-yuwan-model'),
        ('deepseek', 'cloud-deepseek', 'cloud-deepseek-model'),
    ]
    (tmp_path / '.env').write_text('YUWAN_API_KEY=changed\nYUWAN_MODEL=changed', encoding='utf-8')
    environment.initialize_environment()
    assert environment.read_multimodal_environment()['YUWAN_API_KEY'] == 'local-yuwan-key'


@pytest.mark.parametrize('local_key', ['', '   '])
def test_blank_local_yuwan_key_preserves_cloud_yuwan_model(monkeypatch, tmp_path, local_key):
    with (tmp_path / '.env').open('a', encoding='utf-8') as stream:
        stream.write(f'\nYUWAN_API_KEY="{local_key}"\nYUWAN_MODEL=unused-local-model\n')
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: {
        'YUWAN_API_KEY': 'cloud-yuwan', 'YUWAN_MODEL': 'cloud-model'})
    environment.initialize_environment()
    assert os.environ['YUWAN_API_KEY'] == 'cloud-yuwan'
    assert os.environ['YUWAN_MODEL'] == 'cloud-model'


def test_local_yuwan_requires_its_own_model_before_publishing(monkeypatch, tmp_path):
    with (tmp_path / '.env').open('a', encoding='utf-8') as stream:
        stream.write('\nYUWAN_API_KEY=local-secret\n')
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: {'YUWAN_MODEL': 'cloud-model'})
    with pytest.raises(environment.EnvironmentConfigError, match='YUWAN_MODEL'):
        environment.initialize_environment()
    assert 'YUWAN_API_KEY' not in os.environ and not environment._initialized


def test_child_inherits_models_without_fetching_cloud_or_loading_local_secrets(monkeypatch, tmp_path):
    os.environ.update(YUWAN_API_KEY='inherited-key', YUWAN_MODEL='inherited-model',
                      APP_ENV='test', MULTIMODAL_ROUTING_COUNTER_KEY='private-profile-counter',
                      OSS_ACCESS_KEY_SECRET='private-oss', CLOUDFLARE_API_TOKEN='private-cf')
    child = environment.model_environment_for_child()
    assert 'OSS_ACCESS_KEY_SECRET' not in child and 'CLOUDFLARE_API_TOKEN' not in child
    child['REDIS_URL'] = 'redis://inherited.invalid/1'
    assert child['APP_ENV'] == 'test'
    assert child['MULTIMODAL_ROUTING_COUNTER_KEY'] == 'private-profile-counter'
    (tmp_path / '.env').write_text('APP_ENV=prod\nLOGIN_ACCOUNT_NAME=local-secret\n', encoding='utf-8')
    monkeypatch.setattr(os, 'environ', child)
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: pytest.fail('Child must reuse parent configuration'))
    assert environment.read_environment()['YUWAN_API_KEY'] == 'inherited-key'
    assert 'OSS_ACCESS_KEY_SECRET' not in environment.read_environment()
    assert 'LOGIN_ACCOUNT_NAME' not in environment.read_environment()


def test_entrypoint_fetches_kv_before_redis_validation_and_service_creation(monkeypatch, tmp_path):
    calls = []
    remote = {**OSS_ENV, 'REDIS_URL': 'redis://cloud.invalid/0',
              'YUWAN_API_KEY': 'cloud-key', 'YUWAN_MODEL': 'cloud-model',
              'LDPLAYER_STATE_DIR': str(tmp_path / 'cloud-state')}
    monkeypatch.setattr(kv, 'fetch_environment', lambda settings: calls.append('cloud') or remote)
    async def run(config):
        assert config.redis_url == remote['REDIS_URL'] and config.oss_bucket == OSS_ENV['OSS_BUCKET']
        calls.append('worker')
    monkeypatch.setattr(entrypoint, 'run_worker', run)
    entrypoint.main([])
    assert calls == ['cloud', 'worker']


def test_entrypoint_exits_on_cloud_failure_before_worker_or_gateway(monkeypatch, caplog):
    install_sdk(monkeypatch, lambda request: httpx.Response(401, json={'errors': [{'message': 'private-token'}]}))
    monkeypatch.setattr(entrypoint, 'run_worker', lambda _: pytest.fail('Cannot start without cloud configuration'))
    with pytest.raises(SystemExit) as error:
        entrypoint.main([])
    assert error.value.code == 1 and 'HTTP 401' in caplog.text
    assert 'private-token' not in caplog.text
