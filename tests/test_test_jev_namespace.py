"""Cloud profile selection owns resource isolation; no resource name is rewritten."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from naruto_daily.common import cloudflare_kv, environment
from naruto_daily.common.namespace import validate_oss_prefix, validate_redis_key
from naruto_daily.login_service.config import ServiceConfig, load_config
from naruto_daily.login_service.models import HttpError, parse_queue_task
from naruto_daily.login_service.queue import RedisTaskQueue
from naruto_daily.login_service.refresh import RefreshRequests
from naruto_daily.login_service.run_logs import RedisRunReporter
from naruto_daily.login_service.runtime import LoginRuntime
from naruto_daily.login_service.storage import AliyunObjectStorage
from naruto_daily.multimodal import gateway
from oss_fakes import FakeBucket, OSS_ENV


TEST_RESOURCES = {
    'REDIS_QUEUE_KEY': 'naruto:login-test-jev:tasks',
    'OSS_KEY_PREFIX': 'login-test-jev',
    'MULTIMODAL_ROUTING_COUNTER_KEY': 'naruto:multimodal-test-jev:route:sequence',
}


@pytest.mark.parametrize('raw', ['naruto:login:tasks', 'custom:test:tasks', 'tasks', '{login}:tasks'])
def test_queue_name_is_taken_from_configuration_unchanged(raw):
    assert validate_redis_key(raw) == raw
    assert load_config(OSS_ENV | {'REDIS_QUEUE_KEY': raw}).redis_queue_key == raw
    assert replace(ServiceConfig(redis_queue_key=raw)).redis_queue_key == raw


@pytest.mark.parametrize('raw', ['login', 'custom/qr', 'login-test-jev'])
def test_oss_name_is_taken_from_configuration_unchanged(raw):
    assert validate_oss_prefix(raw) == raw
    assert load_config(OSS_ENV | {'OSS_KEY_PREFIX': raw}).key_prefix == raw


@pytest.mark.parametrize('name', ['REDIS_QUEUE_KEY', 'OSS_KEY_PREFIX', 'LDPLAYER_STATE_DIR'])
@pytest.mark.parametrize('value', [None, '', ' '])
def test_resource_names_are_required_without_production_defaults(name, value):
    values = dict(OSS_ENV)
    values.pop(name)
    if value is not None:
        values[name] = value
    with pytest.raises(environment.EnvironmentConfigError, match=name):
        load_config(values)


def test_state_directory_is_not_rewritten(tmp_path):
    config = load_config(OSS_ENV | {'LDPLAYER_STATE_DIR': str(tmp_path)})
    assert config.ldplayer_state_dir == tmp_path
    assert replace(config).ldplayer_state_dir == tmp_path
    assert ServiceConfig(ldplayer_state_dir=tmp_path).ldplayer_state_dir == tmp_path


@pytest.mark.parametrize('profile', ['prod', 'test'])
def test_only_selected_cloud_namespace_is_listed_and_loaded(monkeypatch, tmp_path, profile):
    from test_cloudflare_kv import BOOTSTRAP, install_sdk
    import httpx

    monkeypatch.setattr(environment, '_initialized', False)
    namespace = ('c' if profile == 'test' else 'd') * 32
    bootstrap = dict(BOOTSTRAP, APP_ENV=profile,
                     CLOUDFLARE_KV_NAMESPACE_ID_PROD='d' * 32,
                     CLOUDFLARE_KV_NAMESPACE_ID_TEST='c' * 32)
    monkeypatch.setattr(os, 'environ', bootstrap)
    # Old resources in the local file cannot fill absent selected-profile keys.
    (tmp_path / '.env').write_text('REDIS_QUEUE_KEY=stale-production\nDATABASE_URL=stale-db\n', encoding='utf-8')
    selected = OSS_ENV | (TEST_RESOURCES if profile == 'test' else {})
    selected |= {'REDIS_URL': 'redis://shared.invalid/1', 'DATABASE_URL': 'postgresql://db.invalid/' + profile}
    calls = []

    def handler(request):
        calls.append(request)
        assert f'/namespaces/{namespace}/' in request.url.path
        if request.method == 'GET':
            assert 'prefix' not in request.url.params
            result = [{'name': name} for name in selected]
        else:
            names = json.loads(request.content)['keys']
            result = {'values': {name: selected[name] for name in names}}
        return httpx.Response(200, json={'success': True, 'errors': [], 'messages': [],
                                        'result': result, 'result_info': {'cursor': ''}})

    install_sdk(monkeypatch, handler)
    environment.initialize_environment()
    config = load_config()
    assert config.redis_queue_key == selected['REDIS_QUEUE_KEY']
    assert config.key_prefix == selected['OSS_KEY_PREFIX']
    assert config.database_url == selected['DATABASE_URL']
    assert os.environ['APP_ENV'] == profile
    assert len(calls) == 2


@pytest.mark.parametrize('legacy_namespace', ['', 'b' * 32])
def test_test_namespace_is_required_without_fallback(monkeypatch, legacy_namespace):
    from test_cloudflare_kv import BOOTSTRAP
    settings = dict(BOOTSTRAP, APP_ENV='test', CLOUDFLARE_KV_NAMESPACE_ID=legacy_namespace,
                    CLOUDFLARE_KV_NAMESPACE_ID_PROD='d' * 32)
    monkeypatch.setattr(cloudflare_kv, 'Cloudflare', lambda **kwargs: pytest.fail('Never fall back to production'))
    with pytest.raises(environment.EnvironmentConfigError, match='CLOUDFLARE_KV_NAMESPACE_ID_TEST'):
        cloudflare_kv.fetch_environment(settings)


@pytest.mark.parametrize('production_setting', ['CLOUDFLARE_KV_NAMESPACE_ID_PROD', 'CLOUDFLARE_KV_NAMESPACE_ID'])
def test_test_namespace_cannot_equal_known_production_namespace(monkeypatch, production_setting):
    from test_cloudflare_kv import BOOTSTRAP
    settings = dict(BOOTSTRAP, APP_ENV='test', CLOUDFLARE_KV_NAMESPACE_ID_TEST='C' * 32)
    settings[production_setting] = 'c' * 32
    monkeypatch.setattr(cloudflare_kv, 'Cloudflare', lambda **kwargs: pytest.fail('Shared namespace must fail before network'))
    with pytest.raises(environment.EnvironmentConfigError, match='不同的 Cloudflare KV namespace'):
        cloudflare_kv.fetch_environment(settings)


@pytest.mark.parametrize('name', ['REDIS_QUEUE_KEY', 'OSS_KEY_PREFIX', 'LDPLAYER_STATE_DIR'])
def test_missing_selected_resource_cannot_reuse_process_or_file_value(monkeypatch, tmp_path, name):
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.setenv(name, 'stale-production')
    (tmp_path / '.env').write_text('REDIS_QUEUE_KEY=stale-file\nOSS_KEY_PREFIX=old-prefix\n'
                                  'LDPLAYER_STATE_DIR=old-state\nDATABASE_URL=old-db\n', encoding='utf-8')
    remote = {key: value for key, value in OSS_ENV.items() if key != name}
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: remote)
    environment.initialize_environment()
    with pytest.raises(environment.EnvironmentConfigError, match=name):
        load_config()
    assert name not in environment.read_environment()
    assert 'DATABASE_URL' not in environment.read_environment()


@pytest.mark.parametrize('profile', ['', 'staging', 'TEST', 'prod:test'])
def test_invalid_profile_fails_before_network_or_publication(monkeypatch, profile):
    monkeypatch.setattr(environment, '_initialized', False)
    monkeypatch.setenv('APP_ENV', profile)
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda _: pytest.fail('No network for invalid APP_ENV'))
    with pytest.raises(environment.EnvironmentConfigError, match='APP_ENV'):
        environment.initialize_environment()
    assert not environment._initialized


def test_profile_process_value_overrides_dotenv_and_missing_defaults_to_prod(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(cloudflare_kv, 'fetch_environment', lambda settings: captured.append(settings['APP_ENV']) or {})
    (tmp_path / '.env').write_text('APP_ENV=test\n', encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'prod')
    monkeypatch.setattr(environment, '_initialized', False)
    environment.initialize_environment()
    assert captured == ['prod']
    monkeypatch.delenv('APP_ENV')
    monkeypatch.setattr(environment, '_initialized', False)
    environment.initialize_environment()
    assert captured == ['prod', 'test']
    monkeypatch.delenv('APP_ENV')
    (tmp_path / '.env').write_text('', encoding='utf-8')
    monkeypatch.setattr(environment, '_initialized', False)
    environment.initialize_environment()
    assert captured == ['prod', 'test', 'prod']


def _body(config, **changes):
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    return {
        'version': 1, 'id': task_id,
        'createdAt': now.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'expiresAt': (now + timedelta(seconds=60)).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'objectKey': f'{config.key_prefix}/{task_id}/qr.png',
    } | changes


async def _enqueue(client, prefix, body, **extra):
    session = {key: body[key] for key in ('id', 'createdAt', 'expiresAt')}
    session.update(status='queued', simulation=True, tokenHash='ab' * 32, **extra)
    await client.set(f'{prefix}:sessions:{body["id"]}', json.dumps(session), px=60_000)
    await client.lpush(prefix, json.dumps(body))


def test_configured_test_queue_refresh_logs_cleanup_leave_production_untouched():
    async def scenario():
        fakeredis = pytest.importorskip('fakeredis.aioredis')
        pytest.importorskip('lupa')
        client = fakeredis.FakeRedis(decode_responses=True)
        config = load_config(OSS_ENV | TEST_RESOURCES | {'REDIS_URL': 'redis://shared.invalid/0'})
        queue = RedisTaskQueue(config, client=client)
        reporter = RedisRunReporter(config, queue)
        prefix, production = config.redis_queue_key, 'naruto:login:tasks'
        body = _body(config, loginTimeoutSeconds=600)
        task_id, user_id = body['id'], str(uuid4())
        try:
            await _enqueue(client, production, body | {'objectKey': f'login/{task_id}/qr.png'}, userId=user_id)
            await client.set(f'{production}:users:{user_id}:active', task_id, ex=600)
            await client.set(f'{production}:refresh:{task_id}', 'production refresh', ex=600)
            await client.set(f'{production}:runs:{task_id}', 'production run', ex=600)
            await client.xadd(f'{production}:logs:{task_id}', {'json': 'production log'})
            before = {key: await client.dump(key) for key in await client.keys(f'{production}*')}
            await _enqueue(client, prefix, body, userId=user_id)
            await client.set(f'{prefix}:users:{user_id}:active', task_id, ex=600)
            record = await queue.pop()
            assert record is not None and record.id == task_id
            request_id = str(uuid4())
            await client.set(f'{prefix}:refresh:{task_id}', json.dumps({'taskId': task_id, 'requestId': request_id}))
            read_keys = []
            reader = RefreshRequests('unused', prefix, task_id, client=SimpleNamespace(
                get=lambda key: read_keys.append(key) or json.dumps({'taskId': task_id, 'requestId': request_id})))
            assert reader.poll() == request_id
            assert read_keys == [f'{prefix}:refresh:{task_id}']
            assert await queue.pending_refresh(record, request_id)
            reporter.start()
            reporter.emit(record, status='waiting_scan')
            await reporter.flush(task_id)
            assert await client.xlen(f'{prefix}:logs:{task_id}') == 1
            record.daily_error_code = 'worker_stopped'
            assert await queue.finish_failed(record)
            assert await client.get(f'{prefix}:users:{user_id}:active') is None
            assert await client.get(f'{prefix}:refresh:{task_id}') is None
            assert before == {key: await client.dump(key) for key in await client.keys(f'{production}*')}
        finally:
            await reporter.close()
            await client.aclose()
    asyncio.run(scenario())


def test_configured_oss_prefix_leaves_production_objects_untouched():
    async def scenario():
        config = load_config(OSS_ENV | TEST_RESOURCES)
        body = _body(config)
        assert parse_queue_task(body, config.key_prefix).id == body['id']
        production_key = f'login/{body["id"]}/qr.png'
        with pytest.raises(HttpError):
            parse_queue_task(body | {'objectKey': production_key}, config.key_prefix)
        bucket = FakeBucket()
        bucket.objects[production_key] = (b'production qr', {})
        storage = AliyunObjectStorage(config.key_prefix, config, bucket=bucket)
        await storage.put(body['id'], b'test qr', body['expiresAt'])
        assert bucket.objects[body['objectKey']][0] == b'test qr'
        await storage.delete(body['id'])
        assert bucket.objects[production_key][0] == b'production qr'
    asyncio.run(scenario())


def test_child_receives_configured_queue_and_state_directory(monkeypatch, tmp_path):
    config = load_config(OSS_ENV | TEST_RESOURCES | {'LDPLAYER_STATE_DIR': str(tmp_path)})
    config = replace(config, ldplayer_console_path=Path('unused.exe'))
    record = parse_queue_task(_body(config, loginTimeoutSeconds=600), config.key_prefix)
    runtime = LoginRuntime(config, storage=object())
    messages = []
    async def child(record, command, message, **kwargs):
        messages.append(message)
        return b'png'
    monkeypatch.setattr(runtime, '_run_child', child)
    asyncio.run(runtime._run_ldplayer(record))
    assert messages[0]['refreshQueueKey'] == TEST_RESOURCES['REDIS_QUEUE_KEY']
    assert Path(messages[0]['stateDir']) == tmp_path


def test_model_counter_uses_selected_configuration(configured_environment):
    settings = gateway.load_gateway_settings(TEST_RESOURCES | {
        'YUWAN_API_KEY': 'fake-key', 'YUWAN_MODEL': 'fake-model', 'REDIS_URL': 'redis://unused.invalid/0'})
    gateway.MultimodalGateway(settings).select()
    assert configured_environment.keys == [TEST_RESOURCES['MULTIMODAL_ROUTING_COUNTER_KEY']]
