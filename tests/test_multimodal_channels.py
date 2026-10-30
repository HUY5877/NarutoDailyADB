import asyncio
import io
import json
import logging
from urllib.error import HTTPError

import httpx2 as httpx
import pytest

from naruto_daily.common.environment import EnvironmentConfigError
from naruto_daily.login_service.log_events import forward_task_logs, local_log_file
from naruto_daily.multimodal import gateway, transport
from naruto_daily.multimodal.client import MultimodalClient


def settings(**overrides):
    return gateway.load_gateway_settings({
        'YUWAN_API_KEY': '["yuwan-first", "yuwan-second"]', 'YUWAN_MODEL': 'yuwan-vision',
        'DEEPSEEK_API_KEY': 'deepseek-single', 'DEEPSEEK_MODEL': 'deepseek-vision',
        'OPENAI_API_KEY': '["openai-first", "openai-second"]', 'OPENAI_MODEL': 'gpt-4.1',
        'REDIS_URL': 'redis://unused.invalid/0',
        'MULTIMODAL_ROUTING_COUNTER_KEY': 'test:unchanged:counter',
        **overrides,
    })


def test_json_keys_flatten_in_channel_then_key_order_and_share_the_model():
    config = settings()
    assert [(c.name, c.api_key, c.model) for c in config.channels] == [
        ('yuwan', 'yuwan-first', 'yuwan-vision'),
        ('yuwan', 'yuwan-second', 'yuwan-vision'),
        ('deepseek', 'deepseek-single', 'deepseek-vision'),
        ('openai', 'openai-first', 'gpt-4.1'),
        ('openai', 'openai-second', 'gpt-4.1'),
    ]
    assert config.channels[-1].base_url == 'https://api.openai.com/v1'
    assert 'openai-first' not in repr(config) and 'yuwan-second' not in repr(config)


def test_multiple_keys_share_incr_modulo_counter_across_sync_and_async(configured_environment):
    first, second = gateway.MultimodalGateway(settings()), gateway.MultimodalGateway(settings())
    selected = [first.select().api_key, asyncio.run(second.select_async()).api_key,
                first.select().api_key, second.select().api_key, first.select().api_key]
    assert selected == ['yuwan-second', 'deepseek-single', 'openai-first', 'openai-second', 'yuwan-first']
    assert configured_environment.value == 5
    assert configured_environment.keys == ['test:unchanged:counter'] * 5


@pytest.mark.parametrize('prefix', ['DEEPSEEK', 'OPENAI'])
def test_explicitly_disabled_channel_retains_keys_without_requiring_a_model(prefix):
    config = settings(**{prefix + '_ENABLED': 'false', prefix + '_MODEL': ''})
    assert prefix.lower() not in [c.name for c in config.channels]


@pytest.mark.parametrize('prefix', ['DEEPSEEK', 'OPENAI'])
@pytest.mark.parametrize('value', ['1', 'yes', 'FALSE', '', 'null'])
def test_enabled_flag_rejects_values_other_than_true_and_false(prefix, value):
    with pytest.raises(EnvironmentConfigError, match=prefix + '_ENABLED'):
        settings(**{prefix + '_ENABLED': value})


@pytest.mark.parametrize('enabled', ['true', 'false', '', 'obsolete-value'])
def test_yuwan_keys_enable_the_local_channel_without_consulting_a_flag(enabled):
    config = settings(YUWAN_ENABLED=enabled)
    assert [(c.api_key, c.model) for c in config.channels if c.name == 'yuwan'] == [
        ('yuwan-first', 'yuwan-vision'), ('yuwan-second', 'yuwan-vision')]


@pytest.mark.parametrize('value', [
    '["private-key",', '{"private-key": "secret"}', '"private-key"', '[null]',
    '[true]', '[123]', '[[]]', '[""]', '["   "]', '["private-key\\n"]',
    '["private-key\\u007f"]', '["private-key\\u0085"]', 'private-key\ninvalid',
    json.dumps(['x' * 513]), json.dumps(['private-key'] * 21), 'private-key' * 50,
])
def test_invalid_key_lists_fail_without_echoing_secrets(value):
    with pytest.raises(EnvironmentConfigError, match='OPENAI_API_KEY') as caught:
        settings(OPENAI_API_KEY=value)
    assert 'private-key' not in str(caught.value)


def test_empty_key_list_disables_channel_and_boundary_limits_are_accepted():
    config = settings(YUWAN_API_KEY='[]', OPENAI_API_KEY=json.dumps(['x' * 512] * 20),
                      OPENAI_MODEL='m' * 200, OPENAI_ENABLED='true')
    assert len(config.channels) == 21
    assert config.channels[0].name == 'deepseek'
    assert all(c.api_key == 'x' * 512 for c in config.channels[1:])


@pytest.mark.parametrize('model', ['m' * 201, 'invalid\nmodel', 'invalid\x7fmodel'])
def test_model_is_bounded_and_has_no_control_characters(model):
    with pytest.raises(EnvironmentConfigError, match='OPENAI_MODEL'):
        settings(OPENAI_MODEL=model)


def test_sync_client_openai_payload_is_adapted_without_changing_the_next_retry(monkeypatch):
    router = gateway.MultimodalGateway(settings(YUWAN_API_KEY='', OPENAI_API_KEY='openai-first'))
    monkeypatch.setattr(gateway, '_gateway', router)
    sent = []

    def send(request, timeout):
        sent.append(request)
        if len(sent) == 1:
            raise HTTPError(request.full_url, 400, 'rejected', {}, None)
        return io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}')

    client = MultimodalClient({'timeout_seconds': 30}, open_request=send, sleep=lambda _: None)
    assert client.complete_json(b'image', 'prompt') == {}
    openai_body, deepseek_body = [json.loads(r.data) for r in sent]
    assert sent[0].full_url == 'https://api.openai.com/v1/chat/completions'
    assert sent[0].get_header('Authorization') == 'Bearer openai-first'
    assert openai_body['model'] == 'gpt-4.1'
    assert openai_body['max_completion_tokens'] == 4096
    assert 'max_tokens' not in openai_body and 'reasoning_effort' not in openai_body
    assert deepseek_body['max_tokens'] == 4096 and deepseek_body['reasoning_effort'] == 'low'
    assert 'max_completion_tokens' not in deepseek_body
    assert openai_body['messages'] == deepseek_body['messages']


def test_async_openai_adaptation_preserves_tools_and_original_retry_payload(monkeypatch):
    router = gateway.MultimodalGateway(settings(DEEPSEEK_API_KEY='', YUWAN_API_KEY='yuwan-first',
                                                OPENAI_API_KEY='openai-first'))
    sent = []
    original = {'model': 'bootstrap', 'max_tokens': 4096, 'max_completion_tokens': 8192,
                'reasoning_effort': 'low', 'thinking': {'type': 'enabled'},
                'tool_choice': 'required', 'parallel_tool_calls': False,
                'messages': [{'role': 'user', 'content': 'goal'}],
                'tools': [{'type': 'function', 'function': {'name': 'finish'}}]}

    async def send(request):
        sent.append(request)
        return httpx.Response(400 if len(sent) == 1 else 200, json={'ok': True})

    async def no_sleep(_):
        pass

    async def run():
        async with transport.ModelAsyncClient(request_timeout=30, route_request=router.route_async_request,
                                             transport=httpx.MockTransport(send)) as client:
            response = await client.post('https://initial.invalid', json=original)
            assert response.json() == {'ok': True}

    monkeypatch.setattr(transport.asyncio, 'sleep', no_sleep)
    asyncio.run(run())
    openai_body, yuwan_body = [json.loads(r.content) for r in sent]
    assert str(sent[0].url) == 'https://api.openai.com/v1/chat/completions'
    assert openai_body['max_completion_tokens'] == 8192
    assert 'max_tokens' not in openai_body and 'thinking' not in openai_body
    assert 'reasoning_effort' not in openai_body
    assert openai_body['tool_choice'] == 'required' and openai_body['parallel_tool_calls'] is False
    assert openai_body['tools'] == original['tools'] and openai_body['messages'] == original['messages']
    assert yuwan_body == {**original, 'model': 'yuwan-vision'}


def test_openai_navigation_sdk_sends_supported_payload_and_preserves_tool_constraints(monkeypatch):
    from agent_fakes import tool_call
    from naruto_daily.multimodal.agent import Joystick, run_agent

    monkeypatch.setattr(gateway, '_gateway', gateway.MultimodalGateway(settings(
        YUWAN_API_KEY='', DEEPSEEK_API_KEY='', OPENAI_API_KEY='openai-first')))
    sent = []

    async def send(self, request, **kwargs):
        sent.append(request)
        return httpx.Response(200, request=request, json={
            'id': 'offline', 'object': 'chat.completion', 'created': 1, 'model': 'gpt-4.1',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [
                    tool_call('finish', {'status': 'reached', 'observation': 'arrived'})],
            }}],
        })

    class Device:
        def screenshot(self):
            return b'image'

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    result = run_agent(Device(), MultimodalClient({'timeout_seconds': 30}), 'goal',
                       joystick=Joystick(200, 800, 80))
    assert result.reached and result.steps == 0
    assert len(sent) == 1 and str(sent[0].url) == 'https://api.openai.com/v1/chat/completions'
    body = json.loads(sent[0].content)
    assert body['model'] == 'gpt-4.1' and body['tool_choice'] == 'required'
    assert body['max_completion_tokens'] == 4096 and body['parallel_tool_calls'] is False
    assert 'reasoning_effort' not in body and 'thinking' not in body and 'max_tokens' not in body
    assert any(tool['function']['name'] == 'finish' for tool in body['tools'])


@pytest.mark.parametrize('variable', ['YUWAN_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY'])
def test_every_json_key_is_redacted_from_protocol_terminal_and_local_file(tmp_path, monkeypatch, variable):
    keys = ['private-first-key', 'private-second-key']
    monkeypatch.setenv(variable, json.dumps(keys))
    events, terminal = [], io.StringIO()
    with local_log_file(tmp_path), forward_task_logs(events.append, diagnostic_stream=terminal):
        logging.getLogger('naruto_daily').info('upstream echoed %s and %s', *keys)
    assert events[0]['message'] == 'upstream echoed [REDACTED] and [REDACTED]'
    output = terminal.getvalue() + (tmp_path / 'logs' / 'worker.log').read_text(encoding='utf-8')
    assert '[REDACTED]' in output
    assert all(key not in output for key in keys)
