import asyncio
import json
from http.server import HTTPServer

import httpx2 as httpx
import pytest

from naruto_daily.multimodal import transport
from naruto_daily.multimodal.agent import compat_runner, Joystick, run_agent


URL = 'https://llm-proxy.yuwan-game.com/v1/chat/completions'


@pytest.mark.parametrize('failure', [201, 204, 307, 400, 401, 403, 404, 408, 418, 422, 429, 500, 502, 503, 504, 529, 599, 'network'])
def test_direct_client_retries_same_request_three_times(monkeypatch, failure):
    calls, waits = [], []

    async def wait(seconds):
        waits.append(seconds)

    async def handle(request):
        calls.append((str(request.url), request.headers['Authorization'], request.content))
        if len(calls) < 4:
            if failure == 'network':
                raise httpx.ConnectError('private-key', request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={'ok': True})

    async def run():
        async with transport.ModelAsyncClient(request_timeout=30, transport=httpx.MockTransport(handle)) as client:
            response = await client.post(URL, content=b'{"model":"deepseek-v4.1-flash"}',
                                         headers={'Authorization': 'Bearer private-key'})
            assert response.json() == {'ok': True}

    monkeypatch.setattr(transport.asyncio, 'sleep', wait)
    asyncio.run(run())
    assert len(calls) == 4 and calls == [calls[0]] * 4
    assert calls[0] == (URL, 'Bearer private-key', b'{"model":"deepseek-v4.1-flash"}')
    assert waits == [10, 10, 10]


@pytest.mark.parametrize('status', [201, 204, 307, 400, 401, 403, 404, 418, 429, 529])
def test_direct_client_stops_at_retry_limit_and_does_not_redirect(monkeypatch, status):
    calls, waits = [], []

    async def wait(seconds):
        waits.append(seconds)

    async def handle(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={'Location': 'https://other.invalid'})

    async def run():
        async with transport.ModelAsyncClient(request_timeout=30, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(transport.MultimodalRequestError, match='已重试3次') as caught:
                await client.post(URL, json={})
            assert caught.value.status_code == status

    monkeypatch.setattr(transport.asyncio, 'sleep', wait)
    asyncio.run(run())
    assert calls == [URL] * 4
    assert waits == [10, 10, 10]


def test_direct_client_enforces_request_timeout_and_budget(monkeypatch):
    calls = []

    async def handle(request):
        calls.append(1)
        await asyncio.Event().wait()

    async def run():
        async with transport.ModelAsyncClient(request_timeout=.02, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.post(URL, json={})

    monkeypatch.setattr(transport, 'request_budget', lambda _: .03)
    asyncio.run(run())
    assert calls == [1]


@pytest.mark.parametrize('phase', ['request', 'retry_wait'])
def test_direct_client_cancellation_stops_request_and_future_retries(monkeypatch, phase):
    calls, closed = [], []

    async def run():
        entered = asyncio.Event()

        async def wait(seconds):
            entered.set()
            await asyncio.Event().wait()

        async def handle(request):
            calls.append(1)
            if phase == 'request':
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)
            return httpx.Response(429)

        monkeypatch.setattr(transport.asyncio, 'sleep', wait)
        async with transport.ModelAsyncClient(request_timeout=30, transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(client.post(URL, json={}))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert calls == [1]
    assert closed == ([True] if phase == 'request' else [])


def test_navigation_sdk_uses_configured_gateway_without_http_listener(monkeypatch, caplog):
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail('导航不得创建本地 HTTP 监听端口')

    async def handle(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert str(request.url) == URL
        assert request.headers['Authorization'] == 'Bearer private-key'
        assert payload['model'] == 'deepseek-v4.1-flash'
        return httpx.Response(200, json={
            'id': 'offline', 'object': 'chat.completion', 'created': 1, 'model': payload['model'],
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'finish-1', 'type': 'function', 'function': {
                        'name': 'finish', 'arguments': json.dumps({'status': 'reached', 'observation': '已到达'})},
                }],
            }}],
        })

    class Device:
        def screenshot(self):
            return b'png'

        def swipe(self, *args):
            pytest.fail('已到达，不应移动')

    class Model:
        def agent_connection(self):
            return dict(base_url=URL.removesuffix('/chat/completions'), api_key='private-key',
                        model='deepseek-v4.1-flash', timeout_seconds=30)

    monkeypatch.setattr(HTTPServer, 'server_bind', forbidden)
    monkeypatch.setattr(compat_runner, 'ModelAsyncClient', lambda **kwargs:
                        transport.ModelAsyncClient(**kwargs, transport=httpx.MockTransport(handle)))
    caplog.set_level('INFO')
    result = run_agent(Device(), Model(), '到达目标', joystick=Joystick(200, 800, 80))
    assert result.reached and result.steps == 0 and len(calls) == 1
    assert URL in caplog.text and 'private-key' not in caplog.text
