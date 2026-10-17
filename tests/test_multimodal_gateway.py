import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import logging
from urllib.error import HTTPError
from urllib.request import Request

import httpx2 as httpx
import pytest

from naruto_daily.common import environment
from naruto_daily.common.execution import execution_scope
from naruto_daily.multimodal import gateway, transport
from naruto_daily.multimodal.channels import CHANNELS
from naruto_daily.multimodal.client import MultimodalClient


def settings(**overrides):
    return gateway.load_gateway_settings({
        'YUWAN_API_KEY': 'yuwan-private', 'YUWAN_MODEL': 'yuwan-vision',
        'DEEPSEEK_API_KEY': 'deepseek-private', 'DEEPSEEK_MODEL': 'deepseek-vision',
        'REDIS_URL': 'redis://account:redis-private@test.invalid/0',
        'MULTIMODAL_ROUTING_COUNTER_KEY': 'naruto:multimodal:route:sequence',
        **overrides,
    })


@pytest.mark.parametrize('key', [None, '', ' \t '])
def test_only_nonempty_keys_enable_channels(key):
    assert [channel.name for channel in settings(YUWAN_API_KEY=key).channels] == ['deepseek']
    assert [channel.name for channel in settings(DEEPSEEK_API_KEY=key).channels] == ['yuwan']


def test_keys_are_not_probed_or_format_validated_and_models_are_independent(configured_environment):
    config = settings(YUWAN_API_KEY='not a verified key')
    assert config.channels[0].api_key == 'not a verified key'
    assert [channel.model for channel in config.channels] == ['yuwan-vision', 'deepseek-vision']
    assert configured_environment.value == 0
    assert all(secret not in repr(config) for secret in ('not a verified key', 'deepseek-private', 'redis-private'))


@pytest.mark.parametrize('overrides,variable', [
    ({'YUWAN_API_KEY': '', 'DEEPSEEK_API_KEY': ''}, 'API_KEY'),
    ({'YUWAN_MODEL': ''}, 'YUWAN_MODEL'),
    ({'DEEPSEEK_MODEL': ''}, 'DEEPSEEK_MODEL'),
    ({'REDIS_URL': ''}, 'REDIS_URL'),
    ({'REDIS_URL': 'https://account:redis-private@wrong.test'}, 'REDIS_URL'),
    ({'MULTIMODAL_ROUTING_COUNTER_KEY': ''}, 'MULTIMODAL_ROUTING_COUNTER_KEY'),
])
def test_invalid_startup_configuration_fails_without_exposing_values(overrides, variable):
    with pytest.raises(environment.EnvironmentConfigError, match=variable) as caught:
        settings(**overrides)
    assert 'redis-private' not in str(caught.value)


def test_missing_counter_never_uses_a_hardcoded_default():
    with pytest.raises(environment.EnvironmentConfigError, match='MULTIMODAL_ROUTING_COUNTER_KEY'):
        gateway.load_gateway_settings({'YUWAN_API_KEY': 'key', 'YUWAN_MODEL': 'model',
                                       'REDIS_URL': 'redis://unused.invalid/0'})


def test_disabled_channel_does_not_require_model():
    config = settings(DEEPSEEK_API_KEY='', DEEPSEEK_MODEL='')
    assert len(config.channels) == 1


def test_incr_then_modulo_is_shared_between_instances_and_async_calls(configured_environment):
    first, second = gateway.MultimodalGateway(settings()), gateway.MultimodalGateway(settings())
    assert first.select().name == 'deepseek'  # INCR 从 0 到 1，然后 1 % 2。
    assert second.select().name == 'yuwan'
    assert asyncio.run(first.select_async()).name == 'deepseek'
    assert asyncio.run(second.select_async()).name == 'yuwan'
    assert configured_environment.keys == [first.settings.routing_counter_key] * 4


def test_routing_urls_stay_in_backend_while_business_logs_reach_client():
    from naruto_daily.login_service.log_events import forward_task_logs

    router = gateway.MultimodalGateway(settings())
    events, terminal = [], io.StringIO()
    with forward_task_logs(events.append, diagnostic_stream=terminal):
        router.route_request(Request('https://initial.test', data=b'{"model":"initial"}'))
        asyncio.run(router.route_async_request(httpx.Request('POST', 'https://initial.test', json={'model': 'initial'})))
        router.route_request(Request('https://initial.test', data=b'{"model":"initial"}'))
        logging.getLogger('naruto_daily').info('日常任务执行中')

    assert [event['message'] for event in events] == ['日常任务执行中']
    output = terminal.getvalue()
    assert output.count('[多模态路由]') == 3
    assert output.count('url=' + CHANNELS[1].base_url + '/chat/completions') == 2
    assert output.count('url=' + CHANNELS[0].base_url + '/chat/completions') == 1
    assert output.count('model=deepseek-vision') == 2
    assert output.count('model=yuwan-vision') == 1
    assert all(secret not in output for secret in ('yuwan-private', 'deepseek-private', 'redis-private'))


def test_single_channel_still_uses_shared_counter(configured_environment):
    router = gateway.MultimodalGateway(settings(YUWAN_API_KEY=''))
    assert [router.select().name for _ in range(3)] == ['deepseek'] * 3
    assert configured_environment.value == 3


def test_distinct_redis_clients_and_event_loops_share_one_counter(monkeypatch):
    fakeredis = pytest.importorskip('fakeredis')
    server = fakeredis.FakeServer()
    monkeypatch.setattr(gateway, '_redis_client', lambda _: fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(gateway, '_async_redis_client', lambda _: fakeredis.aioredis.FakeRedis(server=server))
    first, second = gateway.MultimodalGateway(settings()), gateway.MultimodalGateway(settings())
    assert first.select().name == 'deepseek'
    assert asyncio.run(second.select_async()).name == 'yuwan'
    assert asyncio.run(first.select_async()).name == 'deepseek'
    assert second.select().name == 'yuwan'
    with fakeredis.FakeRedis(server=server) as client:
        assert client.get(first.settings.routing_counter_key) == b'4'


def test_concurrent_clients_do_not_read_then_write_counter(configured_environment):
    routers = [gateway.MultimodalGateway(settings()) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        selected = list(executor.map(lambda number: routers[number % 4].select().name, range(100)))
    assert selected.count('yuwan') == selected.count('deepseek') == 50
    assert configured_environment.value == 100
    assert set(configured_environment.keys) == {routers[0].settings.routing_counter_key}


def test_startup_snapshot_uses_injected_cloud_values_and_ignores_later_changes(tmp_path, monkeypatch, configured_environment):
    path = tmp_path / '.env'
    path.write_text('DEEPSEEK_API_KEY=official-root\n'
                    'DEEPSEEK_MODEL=official-model\nREDIS_URL=redis://test.invalid/0\n', encoding='utf-8')
    monkeypatch.delenv('YUWAN_API_KEY')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'cloud-key')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'cloud-model')
    router = gateway.initialize_gateway()
    assert [(channel.name, channel.model, channel.api_key) for channel in router.channels] == [
        ('deepseek', 'cloud-model', 'cloud-key')]
    path.write_text('', encoding='utf-8')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'later-model')
    assert gateway.initialize_gateway() is router
    assert router.channels[0].model == 'cloud-model'
    assert configured_environment.value == 0


def test_sync_retry_switches_url_key_and_model_and_recaptures_image(monkeypatch, configured_environment):
    router = gateway.MultimodalGateway(settings())
    monkeypatch.setattr(gateway, '_gateway', router)
    sent, waits, now = [], [], [0]

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    def send(request, timeout):
        body = json.loads(request.data)
        image = base64.b64decode(body['messages'][0]['content'][1]['image_url']['url'].split(',')[1])
        sent.append((request.full_url, request.get_header('Authorization'), body['model'], image))
        if len(sent) == 1:
            raise HTTPError(request.full_url, 400, 'private-error', {}, None)
        return io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"ok\\":true}"}}]}')

    client = MultimodalClient({'timeout_seconds': 30}, open_request=send)
    # SDK 的连接预览不能消费轮询计数。
    assert client.agent_connection()['protocol'] == 'openai'
    assert client.agent_connection()['base_url'] == CHANNELS[0].base_url
    assert configured_environment.value == 0
    with execution_scope(100, clock=lambda: now[0], sleep=sleep):
        assert client.complete_json(b'initial', 'prompt', refresh_image=lambda: b'fresh') == {'ok': True}
    assert sent == [(CHANNELS[1].base_url + '/chat/completions', 'Bearer deepseek-private', 'deepseek-vision', b'initial'),
                    (CHANNELS[0].base_url + '/chat/completions', 'Bearer yuwan-private', 'yuwan-vision', b'fresh')]
    assert waits == [10] and configured_environment.value == 2


def test_async_sdk_routes_every_send_and_retry_preserving_tools_history_and_extensions(monkeypatch, configured_environment):
    router = gateway.MultimodalGateway(settings())
    sent, waits = [], []
    history = [{'role': 'assistant', 'tool_calls': [{'id': 'already-executed'}]},
               {'role': 'tool', 'tool_call_id': 'already-executed', 'content': 'finished'}]

    async def sleep(seconds):
        waits.append(seconds)

    async def send(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(400)
        return httpx.Response(200, json={'ok': True})

    async def refresh(request):
        body = json.loads(request.content)
        body['frame'] = 'fresh'
        return httpx.Request(request.method, request.url, headers=request.headers, json=body, extensions=request.extensions)

    async def run():
        async with transport.ModelAsyncClient(request_timeout=30, transport=httpx.MockTransport(send),
                refresh_request=refresh, route_request=router.route_async_request) as client:
            for _ in range(2):
                result = await client.post(CHANNELS[0].base_url + '/chat/completions',
                    json={'model': 'bootstrap', 'history': history, 'frame': 'initial',
                          'tool_choice': 'required', 'reasoning_effort': 'low'},
                    headers={'Authorization': 'Bearer bootstrap-private'})
                assert result.json() == {'ok': True}

    monkeypatch.setattr(transport.asyncio, 'sleep', sleep)
    asyncio.run(run())
    assert [str(request.url) for request in sent] == [CHANNELS[index].base_url + '/chat/completions' for index in (1, 0, 1)]
    assert [request.headers['authorization'] for request in sent] == ['Bearer deepseek-private', 'Bearer yuwan-private', 'Bearer deepseek-private']
    assert [json.loads(request.content)['model'] for request in sent] == ['deepseek-vision', 'yuwan-vision', 'deepseek-vision']
    assert [json.loads(request.content)['frame'] for request in sent] == ['initial', 'fresh', 'initial']
    assert all(json.loads(request.content)['history'] == history for request in sent)
    assert [json.loads(request.content)['reasoning_effort'] for request in sent] == ['none', 'low', 'none']
    assert 'thinking' not in json.loads(sent[1].content)  # 官方适配不能影响下次鱼玩重试。
    assert all(request.headers['host'] == request.url.host for request in sent)
    assert all(int(request.headers['content-length']) == len(request.content) for request in sent)
    assert all('timeout' in request.extensions for request in sent)
    assert waits == [10] and configured_environment.value == 3


def test_official_channel_without_v1_uses_openai_navigation_sdk(monkeypatch, configured_environment):
    from naruto_daily.multimodal.agent import Joystick, run_agent
    from agent_fakes import tool_call
    monkeypatch.setattr(gateway, '_gateway', gateway.MultimodalGateway(settings(YUWAN_API_KEY='')))
    sent = []

    async def send(self, request, **kwargs):
        sent.append(request)
        return httpx.Response(200, request=request, json={
            'id': 'offline', 'object': 'chat.completion', 'created': 1, 'model': 'deepseek-vision',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [
                    tool_call('finish', {'status': 'reached', 'observation': 'arrived'})],
            }}],
        })

    class Device:
        def screenshot(self):
            return b'image'

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    result = run_agent(Device(), MultimodalClient({'timeout_seconds': 30}), 'goal', joystick=Joystick(200, 800, 80))
    assert result.reached and result.steps == 0
    assert len(sent) == 1 and configured_environment.value == 1
    assert str(sent[0].url) == CHANNELS[1].base_url + '/chat/completions'
    assert sent[0].headers['authorization'] == 'Bearer deepseek-private'
    assert json.loads(sent[0].content)['model'] == 'deepseek-vision'
    assert json.loads(sent[0].content)['tool_choice'] == 'required'
    assert json.loads(sent[0].content)['reasoning_effort'] == 'none'
    assert json.loads(sent[0].content)['thinking'] == {'type': 'disabled'}


@pytest.mark.parametrize('asynchronous', [False, True])
def test_redis_failure_never_sends_upstream_or_exposes_credentials(monkeypatch, asynchronous):
    router = gateway.MultimodalGateway(settings())

    def fail(_):
        raise OSError('redis://account:redis-private@test.invalid')

    monkeypatch.setattr(gateway, '_redis_client', fail)
    monkeypatch.setattr(gateway, '_async_redis_client', fail)
    with pytest.raises(gateway.GatewayRoutingError) as caught:
        if asynchronous:
            asyncio.run(router.route_async_request(httpx.Request('POST', 'https://initial.test', json={'model': 'initial'})))
        else:
            router.route_request(Request('https://initial.test', data=b'{"model":"initial"}'))
    assert 'redis-private' not in str(caught.value)


def test_redis_failure_retries_before_sending_upstream(monkeypatch, configured_environment):
    monkeypatch.setattr(gateway, '_gateway', gateway.MultimodalGateway(settings()))
    attempts, sent, waits = [], [], []

    def connect(_):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('redis-private')
        return configured_environment

    def send(request, timeout):
        sent.append(request)
        return io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}')

    monkeypatch.setattr(gateway, '_redis_client', connect)
    client = MultimodalClient({'timeout_seconds': 30}, open_request=send, sleep=waits.append)
    assert client.complete_json(b'image', 'prompt') == {}
    assert len(attempts) == 2 and len(sent) == 1 and waits == [10]
    assert sent[0].full_url == CHANNELS[1].base_url + '/chat/completions'


def test_routing_cannot_send_http_after_task_deadline():
    from naruto_daily.common.execution import DailyDeadlineExceeded
    now = [0]

    def late_route(request):
        now[0] = 3
        return request

    def forbidden_send(*args, **kwargs):
        pytest.fail('Routing consumed the task budget; HTTP must not start')

    with pytest.raises(DailyDeadlineExceeded), execution_scope(2, clock=lambda: now[0]):
        transport.request_with_retry(Request('https://initial.test'), request_timeout=30,
                                     route_request=late_route, open_request=forbidden_send)


def test_async_routing_remains_cancellable(monkeypatch):
    router = gateway.MultimodalGateway(settings())

    async def run():
        entered = asyncio.Event()

        class HangingRedis:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def incr(self, key):
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(gateway, '_async_redis_client', lambda _: HangingRedis())
        async with transport.ModelAsyncClient(request_timeout=30, route_request=router.route_async_request,
                transport=httpx.MockTransport(lambda _: pytest.fail('Cancelled routing must not send HTTP'))) as client:
            task = asyncio.create_task(client.post('https://initial.test', json={'model': 'initial'}))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
